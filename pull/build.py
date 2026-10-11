"""Build this week's props, PPR projections and Top Overs/Unders into output/data/.

Run: python pull/build.py

Lines are self-generated (no paid odds): each player's per-game baseline, rounded to x.5. The projection is
Sleeper's (RotoWire) projected stat line. A lean is the projection against the line. Everything about a player's
history uses only weeks strictly before this one, so a line never sees the game it is for.
"""
import argparse
import json
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import books
import gamemodel
import grade
import news
from matchup import Matchups
import scoring
import sources

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
OUT = ROOT / "output" / "data"
ARCHIVE = OUT / "archive"
WEEKS = OUT / "weeks"

TOP_N = 12
MIN_PICK_SCORE = 0.35
# Top picks are for players with real roles, not backups whose odd projection makes a big gap
PICK_MIN_SNAP_SHARE = 0.5          # average share of the team's offensive snaps over his recent games
PICK_MIN_PROJ_PPR = {"QB": 10, "RB": 6, "WR": 6, "TE": 6}
PICK_PROJ_RANGE = (0.4, 2.5)       # projection vs the line outside this means Sleeper expects a different role  # how far (in typical spreads) the projection must be from the line to be a Top pick
UNAVAILABLE = {"Out", "IR", "PUP", "Suspended", "NA", "Doubtful", "COV", "DNR"}
REGULAR_SEASON_WEEKS = 18


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))


# ---------------------------------------------------------------- loading


def trim_stats(rows):
    out = {}
    for r in rows:
        s = r.get("stats") or {}
        kept = {k: s[k] for k in scoring.STAT_KEYS if s.get(k)}
        if kept:
            out[r["player_id"]] = {"team": r.get("team"), "opp": r.get("opponent"), "stats": kept}
            pos = (r.get("player") or {}).get("position")
            if pos in ("K", "DEF"):
                out[r["player_id"]]["pos"] = pos
    return out


def week_stats(season, week, refresh):
    """Weekly stats, cached on disk. Recent weeks are refetched each run to pick up stat corrections."""
    path = CACHE / f"stats_{season}_{week:02d}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    data = trim_stats(sources.week_stats(season, week))
    dump(path, data)
    return data


FREE_AGENTS = {}   # active players with no NFL team (filled by load_players)
EVERYONE = {}      # every QB, RB, WR, TE, K and DEF Sleeper knows, for past weeks' names (filled by load_players)


def load_players():
    """(skill players, kickers and team defenses) by Sleeper id. A team defense's id is its team code."""
    keep, kdef = {}, {}
    FREE_AGENTS.clear()
    EVERYONE.clear()
    for pid, p in (sources.players() or {}).items():
        if p.get("position") in ("QB", "RB", "WR", "TE", "K", "DEF"):
            EVERYONE[pid] = {"name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                             "pos": p["position"], "espn_id": p.get("espn_id"), "yahoo_id": p.get("yahoo_id"), "team": p.get("team")}
        if p.get("position") in ("QB", "RB", "WR", "TE", "K") and not p.get("team") and p.get("active") and (p.get("search_rank") or 9999) < 1500:
            FREE_AGENTS[pid] = {"name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                                "pos": p["position"], "injury": p.get("injury_status") or None,
                                "espn_id": p.get("espn_id"), "yahoo_id": p.get("yahoo_id")}
        if p.get("position") in ("K", "DEF") and p.get("team") and (p.get("active", True) or p["position"] == "DEF"):
            name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            kdef[pid] = {"name": name, "pos": p["position"], "team": p["team"], "injury": p.get("injury_status"),
                         "injury_part": p.get("injury_body_part"), "depth": p.get("depth_chart_order"), "espn_id": p.get("espn_id"),
                         "yahoo_id": p.get("yahoo_id")}
            continue
        if p.get("position") not in scoring.POSITION_CATEGORIES or not p.get("team"):
            continue
        keep[pid] = {
            "name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "pos": p["position"],
            "team": p["team"],
            "injury": p.get("injury_status"),
            "injury_part": p.get("injury_body_part"),
            "depth": p.get("depth_chart_order"),
            "number": p.get("number"),
            "years": p.get("years_exp"),
            "espn_id": p.get("espn_id"),
            "yahoo_id": p.get("yahoo_id"),
        }
    return keep, kdef


def load_games(season, week):
    games = []
    for e in sources.scoreboard(season, week).get("events", []):
        comp = e["competitions"][0]
        status = comp["status"]["type"]
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        score = lambda side: int(sides[side]["score"]) if status["state"] != "pre" and sides[side].get("score") not in (None, "") else None
        games.append({
            "id": e["id"],
            "kickoff": e["date"],
            "state": status["state"],  # pre, in, post
            "detail": status.get("shortDetail"),
            "home": sources.team_code(sides["home"]["team"]["abbreviation"]),
            "away": sources.team_code(sides["away"]["team"]["abbreviation"]),
            "home_score": score("home"),
            "away_score": score("away"),
            "venue": (comp.get("venue") or {}).get("fullName"),
            "tv": ", ".join(b for g in comp.get("broadcasts", []) for b in g.get("names", [])),
        })
    return sorted(games, key=lambda g: (g["kickoff"], g["id"]))


def projections(season, week):
    out = {}
    for r in sources.week_projections(season, week):
        s = r.get("stats") or {}
        if s.get("pts_ppr") is None:
            continue
        out[r["player_id"]] = {k: round(v, 2) for k, v in s.items() if k in scoring.STAT_KEYS or k in ("pts_ppr",)}
    return out


# ---------------------------------------------------------------- history and matchups


def played(entry):
    s = entry["stats"]
    return (s.get("gp") or 0) > 0 or (s.get("off_snp") or 0) > 0


def build_history(weeks_by_key):
    """player_id -> games in time order: [{season, week, team, opp, stats}], only games the player played."""
    history = defaultdict(list)
    for (season, week), rows in sorted(weeks_by_key.items()):
        for pid, entry in rows.items():
            if played(entry):
                history[pid].append({"season": season, "week": week, "team": entry["team"], "opp": entry["opp"], "stats": entry["stats"]})
    return history


def defense_ranks(this_season_weeks, players):
    """For each defense and position: PPR points allowed per game, ranked 1 (most allowed, softest) to 32."""
    totals = defaultdict(lambda: defaultdict(float))  # (defense, pos) -> week -> points
    for week, rows in this_season_weeks.items():
        for pid, entry in rows.items():
            pos = players.get(pid, {}).get("pos")
            if pos and entry.get("opp") and played(entry):
                totals[(entry["opp"], pos)][week] += scoring.ppr_points(entry["stats"])
    ranks = defaultdict(dict)
    for pos in scoring.POSITION_CATEGORIES:
        per_game = {d: statistics.mean(w.values()) for (d, p), w in totals.items() if p == pos and w}
        for i, (team, pts) in enumerate(sorted(per_game.items(), key=lambda kv: -kv[1]), start=1):
            ranks[team][pos] = {"rank": i, "allowed": round(pts, 1)}
    return ranks


def matchup(ranks, opp, pos, weeks_played):
    """Soft (top 8 most points allowed) or tough (bottom 8), shown as information only until it's validated."""
    r = ranks.get(opp, {}).get(pos)
    if not r or weeks_played < 2:
        return None
    label = "soft" if r["rank"] <= 8 else "tough" if r["rank"] >= 25 else "neutral"
    return {**r, "label": label}


# ---------------------------------------------------------------- props


def player_props(player, proj, games, season, opp, mx):
    """Every prop for one player: our projection = Sleeper's projection x the matchup factor (see matchup.py).
    Only this season's games are used. The line starts as our estimate; apply_book swaps in a real line."""
    this = [g for g in games if g["season"] == season]
    props = []
    for key in scoring.POSITION_CATEGORIES[player["pos"]]:
        projection = round(scoring.stat_value(key, proj), 2) if proj else None
        values = [{"s": g["season"], "w": g["week"], "opp": g["opp"], "v": round(scoring.stat_value(key, g["stats"]), 2)} for g in this]
        season_avg = statistics.mean(x["v"] for x in values) if values else None
        if key == "anytime_td":
            chance = round(scoring.td_probability(projection or 0), 3)
            props.append({"key": key, "line": 0.5, "proj": projection, "adj": None, "lean": "over" if chance >= 0.5 else None,
                          "source": "fixed", "over": sum(1 for x in values if x["v"] > 0.5), "n": len(values), "values": values,
                          "td_chance": chance})
            continue
        base = projection if projection is not None else (season_avg if len(values) >= 2 else None)
        if base is None:
            continue
        factor, n_games = mx.factor(opp, player["pos"], key, season_avg)
        adj = round(base * factor, 2)
        line = scoring.round_to_half(adj)
        prop = {
            "key": key, "line": line, "proj": projection, "adj": adj, "mf": factor, "mf_n": n_games, "lean": None,
            "source": "model" if projection is not None else "average",
            "over": sum(1 for x in values if x["v"] > line), "n": len(values), "values": values,
        }
        if key == "fpts":
            # half PPR versions, for the page's PPR / Half PPR switch
            for x, g in zip(values, this):
                x["h"] = scoring.fantasy_points(g["stats"], player["pos"], half=True)
            half_proj = proj.get("pts_half_ppr") if proj else None
            half_base = half_proj if half_proj is not None else (statistics.mean(x["h"] for x in values) if len(values) >= 2 else None)
            if half_base is not None:
                prop["adj_h"] = round(half_base * factor, 2)
                prop["proj_h"] = half_proj
                prop["line_h"] = scoring.round_to_half(prop["adj_h"])
                prop["over_h"] = sum(1 for x in values if x["h"] > prop["line_h"])
        props.append(prop)
    return props


def apply_book(props, lines, picks_lines=None):
    """Use a real line wherever there is one: DraftKings first, then Sleeper Picks. Ours stays as our_line.

    Leans are then measured against that line, using our number (Sleeper's projection on the same scale as our
    line). DraftKings' pregame lines freeze at kickoff, so this is safe to run on games that have started.
    """
    picks_lines = picks_lines or {}
    for p in props:
        if p["key"] == "anytime_td":
            continue
        p.setdefault("our_line", p["line"])
        dk = lines.get(p["key"])
        slp = picks_lines.get(p["key"])
        if slp:
            p["sleeper"] = slp
        if dk:
            line = dk["close"]
            p.update(line=line, dk_open=dk["open"], line_from="dk")
        elif slp:
            line = slp["line"]
            p.update(line=line, line_from="sleeper")
        else:
            p["line_from"] = "ours"
            continue
        cat = scoring.CATEGORIES[p["key"]]
        diff = p["adj"] - line if p.get("adj") is not None else None
        p["lean"] = None if diff is None else "over" if diff > cat["lean"] else "under" if diff < -cat["lean"] else None
        p["over"] = sum(1 for x in p["values"] if x["v"] > line)
    return props


LOG_STATS = {
    "QB": ["pass_cmp", "pass_att", "pass_yd", "pass_td", "pass_int", "rush_att", "rush_yd", "rush_td"],
    "RB": ["rush_att", "rush_yd", "rush_td", "rec_tgt", "rec", "rec_yd", "rec_td"],
    "WR": ["rec_tgt", "rec", "rec_yd", "rec_td", "rush_att", "rush_yd"],
    "TE": ["rec_tgt", "rec", "rec_yd", "rec_td"],
}


def game_log(pos, games):
    """This season's games, Sleeper game log style: week, opponent, snaps, the position's main stats, points."""
    keys = LOG_STATS[pos]
    return [{"w": g["week"], "opp": g["opp"], "snp": g["stats"].get("off_snp"), "tsnp": g["stats"].get("tm_off_snp"),
             "s": [g["stats"].get(k) or 0 for k in keys], "pts": scoring.ppr_points(g["stats"]),
             "h": scoring.fantasy_points(g["stats"], pos, half=True),
             # the full scoring stat line, so the page can score any league's settings
             "st": {k: v for k, v in g["stats"].items() if v and k in SCORING_STATS}} for g in games]


SCORING_STATS = set(scoring.PPR) | scoring.LEAGUE_STATS | {"pass_cmp", "pass_att", "rush_att", "rec_tgt", "idp_fum_rec"}


def snap_share(games):
    """Average share of the team's offensive snaps over his last 4 games played (None without snap data)."""
    shares = [g["stats"]["off_snp"] / g["stats"]["tm_off_snp"] for g in games[-4:]
              if g["stats"].get("tm_off_snp") and g["stats"].get("off_snp") is not None]
    return round(statistics.mean(shares), 2) if shares else None


def pick_candidates(entry):
    """The player's single best over and best under against DraftKings' lines, scored in typical spreads."""
    best = {}
    if entry.get("injury") in UNAVAILABLE or entry["games_this_season"] < 2:
        return best
    if (entry.get("snap_share") or 0) < PICK_MIN_SNAP_SHARE or (entry.get("proj_ppr") or 0) < PICK_MIN_PROJ_PPR[entry["pos"]]:
        return best
    for p in entry["props"]:
        if p["key"] not in scoring.PICKABLE or p["proj"] is None or p.get("line_from") not in ("dk", "sleeper") or not p["lean"]:
            continue
        if p["line"] < scoring.CATEGORIES[p["key"]]["min_pick"]:
            continue
        cat = scoring.CATEGORIES[p["key"]]
        if p["line"] > 0 and not PICK_PROJ_RANGE[0] <= p["proj"] / p["line"] <= PICK_PROJ_RANGE[1]:
            continue
        score = (p["adj"] - p["line"]) / cat["scale"]
        side = p["lean"]
        if abs(score) < MIN_PICK_SCORE:
            continue
        if side not in best or abs(score) > abs(best[side]["score"]):
            best[side] = {"key": p["key"], "line": p["line"], "line_from": p["line_from"], "our_line": p.get("our_line"), "proj": p["proj"], "adj": p["adj"], "score": round(score, 2), "over": p["over"], "n": p["n"]}
    return best


def build_picks(entries, frozen_picks, started_games):
    picks = {"over": [], "under": []}
    for side in picks:
        # Picks in games that have kicked off stay exactly as they were
        # (only picks against real lines: picks made against our own lines before the switch are dropped)
        kept = [p for p in frozen_picks.get(side, []) if p["game_id"] in started_games and p.get("line_from") in ("dk", "sleeper")]
        fresh = []
        for e in entries:
            if e["game_id"] in started_games:
                continue
            c = pick_candidates(e).get(side)
            if c:
                fresh.append({"player_id": e["id"], "game_id": e["game_id"], "direction": side, **c})
        fresh.sort(key=lambda p: -abs(p["score"]))
        picks[side] = sorted((kept + fresh[: max(0, TOP_N - len(kept))])[:TOP_N], key=lambda p: -abs(p["score"]))
    return picks


# ---------------------------------------------------------------- main


def assemble(season, week, players, games, proj, history, ranks, weeks_played, previous, started, book, picks_lines, mx):
    """Every player's props for one week, plus the Top picks. Entries in games that already started come from
    `previous` unchanged, so nothing about a game moves after kickoff."""
    prev_players = {p["id"]: p for p in previous.get("players", [])}
    game_of_team = {}
    for g in games:
        game_of_team[g["home"]] = (g, g["away"], True)
        game_of_team[g["away"]] = (g, g["home"], False)

    entries = []
    for pid, p in players.items():
        if p["team"] not in game_of_team:
            continue  # bye week
        g, opp, home = game_of_team[p["team"]]
        if g["id"] in started and pid in prev_players:
            entry = dict(prev_players[pid], injury=p["injury"])
            # Fill in display-only fields added after this entry was frozen (they describe past games only)
            this_games = [x for x in history.get(pid, []) if x["season"] == season]
            entry["log"] = game_log(p["pos"], this_games)
            entry.setdefault("snap_share", snap_share(this_games))
            if this_games:
                entry.setdefault("avg_half", round(statistics.mean(scoring.fantasy_points(x["stats"], p["pos"], half=True) for x in this_games), 2))
            if proj.get(pid):
                entry.setdefault("proj_half", proj[pid].get("pts_half_ppr"))
            if any("line_from" not in x for x in entry["props"] if x["key"] != "anytime_td"):
                # Built before DraftKings lines were added: attach the closing lines (fixed at kickoff), leaving
                # the projection exactly as it was frozen
                apply_book(entry["props"], book.get(pid, {}))
            entries.append(entry)
            continue
        late = g["id"] in started  # first seen after kickoff: shown, but never graded
        hist = history.get(pid, [])
        pr = proj.get(pid)
        this_n = sum(1 for x in hist if x["season"] == season)
        if not pr and this_n == 0:
            continue  # nobody expects him to play
        if pr and (pr.get("pts_ppr") or 0) < 0.5 and this_n == 0:
            continue
        props = apply_book(player_props(p, pr, hist, season, opp, mx), book.get(pid, {}), picks_lines.get(pid, {}))
        if not props:
            continue
        entries.append({
            "id": pid, "name": p["name"], "pos": p["pos"], "team": p["team"], "number": p["number"],
            "opp": opp, "home": home, "game_id": g["id"],
            "injury": p["injury"], "injury_part": p["injury_part"], "depth": p["depth"],
            "proj_ppr": pr.get("pts_ppr") if pr else None,
            "proj_half": pr.get("pts_half_ppr") if pr else None,
            "proj": {k: v for k, v in (pr or {}).items() if k != "pts_ppr" and v},
            "games_this_season": this_n,
            "snap_share": snap_share([x for x in hist if x["season"] == season]),
            "avg_ppr": round(statistics.mean(scoring.ppr_points(x["stats"]) for x in hist if x["season"] == season), 2) if this_n else None,
            "avg_half": round(statistics.mean(scoring.fantasy_points(x["stats"], p["pos"], half=True) for x in hist if x["season"] == season), 2) if this_n else None,
            "log": game_log(p["pos"], [x for x in hist if x["season"] == season]),
            "matchup": matchup(ranks, opp, p["pos"], weeks_played),
            "props": props,
            **({"late": True} if late else {}),
        })
    entries.sort(key=lambda e: -(e["proj_ppr"] or 0))
    return entries, build_picks(entries, previous.get("picks", {}), started)


def kdef_entries(season, kdef, games, proj, history, previous, started):
    """Kickers and team defenses: fantasy points only (no props). Same freezing rule as everyone else."""
    prev_players = {p["id"]: p for p in previous.get("players", [])}
    sides = {}
    for g in games:
        sides[g["home"]] = (g, g["away"], True)
        sides[g["away"]] = (g, g["home"], False)
    out = []
    for pid, p in kdef.items():
        if p["team"] not in sides:
            continue
        g, opp, home = sides[p["team"]]
        if g["id"] in started and pid in prev_players:
            out.append(dict(prev_players[pid], injury=p["injury"]))
            continue
        pr = proj.get(pid)
        hist = [x for x in history.get(pid, []) if x["season"] == season]
        if not pr and not hist:
            continue
        pts = [scoring.fantasy_points(x["stats"], p["pos"]) for x in hist]
        espn_proj = round(scoring.fantasy_points(pr, p["pos"]), 2) if pr else None
        out.append({
            "id": pid, "name": p["name"], "pos": p["pos"], "team": p["team"], "opp": opp, "home": home, "game_id": g["id"],
            "injury": p["injury"], "injury_part": p["injury_part"], "depth": p["depth"],
            "proj_ppr": espn_proj, "proj_half": espn_proj, "games_this_season": len(hist),
            # the stat lines behind those numbers, so the page can score a league's own kicker and defense settings
            "proj": {k: v for k, v in (pr or {}).items() if k != "pts_ppr" and v},
            "lg": [{"w": x["week"], "st": {k: v for k, v in x["stats"].items() if v and k in KDEF_STATS}} for x in hist],
            "avg_ppr": round(statistics.mean(pts), 2) if pts else None, "avg_half": round(statistics.mean(pts), 2) if pts else None, "props": [],
            **({"late": True} if g["id"] in started else {}),
        })
    return out


KDEF_STATS = set(scoring.K_SCORING) | set(scoring.DEF_SCORING) | scoring.LEAGUE_STATS | {"fgm_50p", "pts_allow", "yds_allow"}

FUTURE_KEYS = set(scoring.PPR) | set(scoring.K_SCORING) | set(scoring.DEF_SCORING) | scoring.LEAGUE_STATS | {
    "pass_cmp", "pass_att", "rush_att", "rec_tgt", "fgm_50p", "pts_allow", "yds_allow"}


def future_weeks(season, week, entries, everyone, free_agents=None):
    """Sleeper's projections for every remaining week: [{w, opp, st}] per player, plus each team's bye weeks.

    Covers this week's players and anyone else with a real projection (players on bye or hurt this week still
    matter for rest-of-season values and league rosters); `meta` names the ones not in week.json.
    Written to output/data/future.json, which the page loads only when it needs it."""
    in_week = {e["id"] for e in entries}
    ids = set(everyone)
    team_of = {pid: p["team"] for pid, p in everyone.items()}
    out = {pid: [] for pid in ids}
    played_weeks = {}
    for w in range(week + 1, REGULAR_SEASON_WEEKS + 1):
        try:
            rows = sources.week_projections(season, w)
        except Exception as err:
            print(f"Week {w} projections unavailable:", err)
            continue
        for r in rows:
            if r.get("opponent") and r.get("team"):
                played_weeks.setdefault(r["team"], set()).add(w)
            pid = r.get("player_id")
            if pid not in ids or not r.get("opponent"):
                continue
            st = {k: round(v, 2) for k, v in (r.get("stats") or {}).items() if k in FUTURE_KEYS and v}
            row = {"w": w, "opp": r["opponent"], "st": st}
            # Sleeper's own totals, so its projections match the Sleeper app exactly (it scores projected fumbles
            # slightly differently from real games); other scorings score the stats
            if r.get("stats", {}).get("pts_ppr") is not None:
                row["pp"], row["ph"] = r["stats"]["pts_ppr"], r["stats"].get("pts_half_ppr")
            out[pid].append(row)
    weeks = set(range(week + 1, REGULAR_SEASON_WEEKS + 1))
    byes = {team: sorted(weeks - ws) for team, ws in played_weeks.items()}
    # keep players projected for something real; everyone in this week's data stays regardless
    keep = {pid: v for pid, v in out.items() if v and (pid in in_week or sum(x.get("pp") or 0 for x in v) >= 10)}
    # [name, pos, team, injury, espn id, yahoo id]: the ids let synced ESPN and Yahoo rosters match these players
    ids = lambda p: [str(p["espn_id"]) if p.get("espn_id") else None, str(p["yahoo_id"]) if p.get("yahoo_id") else None]
    meta = {pid: [everyone[pid]["name"], everyone[pid]["pos"], everyone[pid]["team"], everyone[pid].get("injury"), *ids(everyone[pid])]
            for pid in keep if pid not in in_week}
    # Free agents (no NFL team, like a released veteran) can still be on fantasy rosters: name them so the page
    # can show them on a synced league's roster, with no projection until they sign
    for pid, p in (free_agents or {}).items():
        if pid not in in_week and pid not in meta:
            meta[pid] = [p["name"], p["pos"], None, p.get("injury"), *ids(p)]
    return {"season": season, "from_week": week + 1, "players": keep, "byes": byes, "meta": meta}


def league_points_per_team(season, week):
    """Average points per team in this season's finished games before this week (last season's in week 1)."""
    seasons = [(season, w) for w in range(1, week)] or [(season - 1, w) for w in range(1, REGULAR_SEASON_WEEKS + 1)]
    pts = []
    for s_, w in seasons:
        for e in sources.scoreboard(s_, w).get("events", []):
            if e["status"]["type"].get("completed"):
                pts += [int(c["score"]) for c in e["competitions"][0]["competitors"] if c.get("score") not in (None, "")]
    return sum(pts) / len(pts) if pts else 22.0


def archive_of(season, week, entries, picks, now, games):
    return {
        "season": season, "week": week, "saved_at": now,
        "picks": picks,
        "games": [{k: g.get(k) for k in ("id", "home", "away", "kickoff", "odds", "ours", "ours_late")} for g in games],
        "players": [{
            "id": e["id"], "name": e["name"], "pos": e["pos"], "team": e["team"], "opp": e["opp"], "game_id": e["game_id"],
            "proj_ppr": e["proj_ppr"], "matchup": (e.get("matchup") or {}).get("label"), "late": e.get("late", False),
            "props": [{k: p.get(k) for k in ("key", "line", "proj", "adj", "lean", "source", "line_from", "mf")} for p in e["props"]],
        } for e in entries],
    }


def past_week(season, week, stats):
    """One finished week for the page's week picker: final scores, DraftKings' game lines and closing player lines,
    and every player's stat line (scored on the page, so any league's settings work)."""
    games = load_games(season, week)
    with ThreadPoolExecutor(8) as pool:
        for g, odds in zip(games, pool.map(lambda g: books.game_odds(g["id"]), games)):
            g["odds"] = odds
    by_team = {}
    for g in games:
        by_team[g["home"]] = (g["id"], True)
        by_team[g["away"]] = (g["id"], False)
    keep = set(FUTURE_KEYS) | {k for ks in LOG_STATS.values() for k in ks} | {"off_snp", "tm_off_snp"}
    rows = {}
    for pid, e in stats.items():
        who = EVERYONE.get(pid)
        if not who or e.get("team") not in by_team or not (played(e) or who["pos"] == "DEF"):
            continue
        gid, home = by_team[e["team"]]
        rows[pid] = {"id": pid, "name": who["name"], "pos": who["pos"], "team": e["team"], "opp": e.get("opp"), "home": home,
                     "game_id": gid, "st": {k: v for k, v in e["stats"].items() if v and k in keep}}
        if who.get("espn_id"):
            rows[pid]["espn_id"] = str(who["espn_id"])
    finished = [g["id"] for g in games if g["state"] == "post"]
    try:
        lines = books.week_lines(season, week, finished, {pid: {"name": r["name"], "team": r["team"], "espn_id": r.get("espn_id")} for pid, r in rows.items()})
    except Exception as err:
        print(f"DraftKings lines for week {week} unavailable:", err)
        lines = {}
    for pid, props in lines.items():
        if pid in rows:
            rows[pid]["lines"] = {k: v["close"] for k, v in props.items()}
    return {"season": season, "week": week, "games": games, "players": list(rows.values())}


# ---------------------------------------------------------------- teams: schedule and depth charts

ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
# offense in the order people read it; ESPN's keys for the 3WR 1TE formation
OFFENSE_ORDER = ["qb", "rb", "wr1", "wr2", "wr3", "te", "lt", "lg", "c", "rg", "rt"]
SLOT_NAMES = {"qb": "QB", "rb": "RB", "wr1": "WR", "wr2": "WR", "wr3": "WR", "te": "TE", "lt": "LT", "lg": "LG", "c": "C", "rg": "RG",
              "rt": "RT", "pk": "K", "p": "P", "h": "Holder", "pr": "PR", "kr": "KR", "ls": "LS"}


def season_schedule(season):
    """Every regular season game (weeks 1 to 18) with kickoff, TV, score and DraftKings' line once it's posted."""
    games = []
    def week_games(w):
        out = []
        for e in sources.scoreboard(season, w).get("events", []):
            comp = e["competitions"][0]
            st = comp["status"]["type"]
            sides = {c["homeAway"]: c for c in comp["competitors"]}
            score = lambda side: int(sides[side]["score"]) if st["state"] != "pre" and sides[side].get("score") not in (None, "") else None
            odds = (comp.get("odds") or [{}])[0]
            out.append({"w": w, "id": e["id"], "kickoff": e["date"], "state": st["state"], "detail": st.get("shortDetail"),
                        "home": sources.team_code(sides["home"]["team"]["abbreviation"]), "away": sources.team_code(sides["away"]["team"]["abbreviation"]),
                        "home_score": score("home"), "away_score": score("away"),
                        "tv": ", ".join(b for g in comp.get("broadcasts", []) for b in g.get("names", [])), "venue": (comp.get("venue") or {}).get("fullName"),
                        "line": odds.get("details"), "total": odds.get("overUnder")})
        return out
    with ThreadPoolExecutor(6) as pool:
        for rows in pool.map(week_games, range(1, REGULAR_SEASON_WEEKS + 1)):
            games += rows
    return sorted(games, key=lambda g: (g["w"], g["kickoff"], g["id"]))


def depth_charts(espn_to_sleeper, by_name=None):
    """Each team's ESPN depth chart (offense, defense, special teams) plus its record and standing."""
    teams = sources.get_json(f"{ESPN_SITE}/teams") or {}
    ids = [t["team"]["id"] for sp in teams.get("sports", []) for lg in sp.get("leagues", []) for t in lg.get("teams", [])]
    def one(tid):
        d = sources.get_json(f"{ESPN_SITE}/teams/{tid}/depthcharts") or {}
        t = d.get("team") or {}
        code = sources.team_code(t.get("abbreviation", ""))
        # ESPN id first; Sleeper doesn't have every player's ESPN id, so fall back to name and team
        sid = lambda a: espn_to_sleeper.get(str(a.get("id"))) or (by_name or {}).get((books.norm_name(a.get("displayName") or ""), code))
        groups = []
        for g in d.get("depthchart", []):
            name = g.get("name") or ""
            kind = "special" if "Special" in name else "offense" if any(k in g.get("positions", {}) for k in ("qb", "wr1")) else "defense"
            keys = list(g.get("positions", {}))
            if kind == "offense":
                keys = [k for k in OFFENSE_ORDER if k in keys] + [k for k in keys if k not in OFFENSE_ORDER]
            rows = []
            for k in keys:
                pos = g["positions"][k]
                rows.append({"slot": SLOT_NAMES.get(k, k.upper()), "label": pos["position"].get("displayName"),
                             "players": [{"n": a.get("displayName"), "id": sid(a)} for a in pos.get("athletes", [])][:4]})
            groups.append({"kind": kind, "formation": name, "rows": rows})
        groups.sort(key=lambda g: ["offense", "defense", "special"].index(g["kind"]))
        return code, {
            "name": t.get("displayName"), "short": t.get("name"), "color": t.get("color"), "record": t.get("recordSummary"),
            "standing": t.get("standingSummary"), "depth": groups}
    out = {}
    with ThreadPoolExecutor(8) as pool:
        for code, info in pool.map(one, ids):
            if code:
                out[code] = info
    return out


def load_stats_through(season, last_week, refresh_from):
    """Last season plus this season's weeks 1..last_week."""
    weeks_by_key = {}
    for w in range(1, REGULAR_SEASON_WEEKS + 1):
        weeks_by_key[(season - 1, w)] = week_stats(season - 1, w, refresh=refresh_from == 1)
    for w in range(1, last_week + 1):
        weeks_by_key[(season, w)] = week_stats(season, w, refresh=w >= refresh_from)
    return weeks_by_key


def point_in_time(weeks_by_key, season, week, players):
    """History and defense ranks using only weeks strictly before `week` of `season`."""
    before = {k: v for k, v in weeks_by_key.items() if k[0] < season or (k[0] == season and k[1] < week)}
    this_season = {w: rows for (s, w), rows in before.items() if s == season}
    return build_history(before), defense_ranks(this_season, players), len(this_season)


def run(refresh_all=False):
    state = sources.nfl_state()
    season = int(state["season"])
    week = int(state["week"]) if state.get("season_type") == "regular" else 1
    week = max(1, min(week, REGULAR_SEASON_WEEKS))
    print(f"Season {season}, week {week}")

    players, kdef = load_players()
    games = load_games(season, week)
    proj = projections(season, week)
    weeks_by_key = load_stats_through(season, week - 1, refresh_from=1 if refresh_all else week - 2)
    this_week_stats = week_stats(season, week, refresh=True)
    history, ranks, weeks_played = point_in_time(weeks_by_key, season, week, players)
    this_season = {w: rows for (s_, w), rows in weeks_by_key.items() if s_ == season and w < week}
    mx = Matchups(this_season, {pid: p["pos"] for pid, p in players.items()})

    previous = {}
    prev_path = OUT / "week.json"
    if prev_path.exists():
        old = json.loads(prev_path.read_text())
        if old.get("season") == season and old.get("week") == week:
            previous = old
    now = iso(now_utc())
    started = {g["id"] for g in games if g["state"] != "pre" or g["kickoff"] <= now}

    try:
        espn_ids = {}
        book = books.week_lines(season, week, [g["id"] for g in games], players, espn_ids)
    except Exception as err:  # a DraftKings outage shouldn't stop the build; lines fall back to ours
        print("DraftKings lines unavailable:", err)
        book, espn_ids = {}, {}
    print(f"DraftKings lines for {len(book)} players")
    try:
        picks_lines = books.sleeper_lines()
    except Exception as err:
        print("Sleeper Picks lines unavailable:", err)
        picks_lines = {}
    print(f"Sleeper Picks lines for {len(picks_lines)} players")
    # DraftKings game lines; a game that has started keeps the lines it had at kickoff if ESPN drops them
    prev_odds = {g["id"]: g.get("odds") for g in previous.get("games", [])}
    with ThreadPoolExecutor(8) as pool:
        for g, odds in zip(games, pool.map(lambda g: books.game_odds(g["id"]), games)):
            g["odds"] = odds or prev_odds.get(g["id"])

    entries, picks = assemble(season, week, players, games, proj, history, ranks, weeks_played, previous, started, book, picks_lines, mx)
    entries += kdef_entries(season, kdef, games, proj, history, previous, started)

    # Our game projection (see gamemodel.py); a started game keeps the numbers it had at kickoff
    pos_team = {pid: (p["pos"], p["team"]) for pid, p in {**players, **kdef}.items()}
    ours = gamemodel.finish(games, gamemodel.rollup(proj, pos_team), league_points_per_team(season, week))
    prev_games = {g["id"]: g for g in previous.get("games", [])}
    for g in games:
        old = prev_games.get(g["id"], {})
        if g["id"] in started and old.get("ours"):
            g["ours"], g["ours_late"] = old["ours"], old.get("ours_late", False)
        else:
            g["ours"] = ours.get(g["id"])
            g["ours_late"] = g["id"] in started
    try:
        reports = news.injury_report([{**e, "espn_id": espn_ids.get(e["id"]) or (players.get(e["id"]) or kdef.get(e["id"]) or {}).get("espn_id")} for e in entries])
    except Exception as err:
        print("ESPN injury report unavailable:", err)
        reports = {}
    for e in entries:
        if e["id"] in reports:
            e["inj_report"] = reports[e["id"]]
        else:
            e.pop("inj_report", None)
    print(f"ESPN injury notes for {len(reports)} players")
    # this season's injury timeline, recorded build by build (newest last)
    timeline = news.update_timeline(CACHE / "injury_timeline.json", entries, reports, now)
    for e in entries:
        if len(timeline.get(e["id"], [])) > 0 and (e.get("injury") or e.get("inj_report") or len(timeline[e["id"]]) > 1):
            e["inj_timeline"] = timeline[e["id"]]
        else:
            e.pop("inj_timeline", None)
    for e in entries:
        # ESPN's id lets the page match DraftKings' live lines to the player
        espn = espn_ids.get(e["id"]) or players.get(e["id"], {}).get("espn_id")
        if espn:
            e["espn_id"] = str(espn)
        # Yahoo's id lets a synced Yahoo league's rosters match players exactly
        yahoo = (players.get(e["id"]) or kdef.get(e["id"]) or {}).get("yahoo_id")
        if yahoo:
            e["yahoo_id"] = str(yahoo)
        actual = this_week_stats.get(e["id"])
        e["actual"] = {"stats": actual["stats"], "ppr": scoring.fantasy_points(actual["stats"], e["pos"]),
                       "half": scoring.fantasy_points(actual["stats"], e["pos"], half=True)} if actual and played(actual) else None

    data = {
        "generated_at": now,
        "season": season,
        "week": week,
        "games": games,
        "players": entries,
        "picks": picks,
        "defense": ranks,
        "categories": {k: {"label": v["label"], "lean": v["lean"]} for k, v in scoring.CATEGORIES.items()},
        "scoring": scoring.PPR,
        "scoring_k": scoring.K_SCORING,
        "scoring_def": scoring.DEF_SCORING,
        "def_tiers": {"pts": scoring.PTS_ALLOWED, "yds": scoring.YDS_ALLOWED},
        "log_stats": LOG_STATS,
    }
    dump(OUT / "week.json", data)
    dump(OUT / "future.json", future_weeks(season, week, entries, {**players, **kdef}, FREE_AGENTS))
    # The page polls this small file to tell when a new build is out
    dump(OUT / "meta.json", {"generated_at": now, "season": season, "week": week})
    dump(ARCHIVE / f"{season}_w{week:02d}.json", archive_of(season, week, entries, picks, now, games))

    # The whole season's schedule and every team's depth chart, for future weeks, team pages and game previews
    try:
        espn_to_sleeper = {str(p["espn_id"]): pid for pid, p in EVERYONE.items() if p.get("espn_id")}
        by_name = {(books.norm_name(p["name"]), p["team"]): pid for pid, p in EVERYONE.items() if p.get("team")}
        dump(OUT / "teams.json", {"season": season, "week": week, "generated_at": now,
                                  "schedule": season_schedule(season), "teams": depth_charts(espn_to_sleeper, by_name)})
    except Exception as err:   # ESPN being down shouldn't stop the build; the page keeps the last file
        print("Schedule and depth charts unavailable:", err)
    # Finished weeks for the week picker; older ones are final, so they're only built once
    for w in range(1, week):
        path = WEEKS / f"{season}_w{w:02d}.json"
        if not path.exists() or w >= week - 2:
            dump(path, past_week(season, w, weeks_by_key[(season, w)]))
    stats_for = lambda s, w: this_week_stats if (s, w) == (season, week) else week_stats(s, w, refresh=False)
    finals = {}
    def scores_for(s_, w):
        if (s_, w) not in finals:
            board = games if (s_, w) == (season, week) else load_games(s_, w)
            finals[(s_, w)] = {g["id"]: (g["home_score"], g["away_score"]) for g in board if g["state"] == "post"}
        return finals[(s_, w)]
    record = grade.grade_all(ARCHIVE, stats_for, current=(season, week), final_games={g["id"] for g in games if g["state"] == "post"},
                             scores_for=scores_for)
    dump(OUT / "track_record.json", record)
    print(f"{len(entries)} players, {len(games)} games, {len(picks['over'])} top overs, {len(picks['under'])} top unders")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-all", action="store_true", help="refetch every cached week (both seasons)")
    args = parser.parse_args()
    run(refresh_all=args.refresh_all)
