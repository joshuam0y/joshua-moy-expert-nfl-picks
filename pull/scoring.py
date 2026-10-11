"""PPR scoring and the prop categories.

PPR uses Sleeper's standard settings. ppr_points() reproduces Sleeper's own pts_ppr exactly on every
player-week checked (tools/verify.py re-checks it on each run).
"""
import math

PPR = {
    "pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2,
    "rush_yd": 0.1, "rush_td": 6, "rush_2pt": 2,
    "rec": 1, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2,
    "fum_lost": -2,
    # Special teams plays by offensive players, which Sleeper's standard PPR also counts
    "st_td": 6, "st_fum_rec": 1, "st_ff": 1,
    # A fumble recovered by an offensive player and returned for a TD, and a kick blocked by one
    "fum_rec_td": 6, "idp_blk_kick": 2,
}

# Kickers and team defenses use ESPN's standard scoring (they aren't in the Sleeper league this was built for).
# Checked against ESPN's own default-league totals: every kicker and defense matched in 2026 weeks 1-3.
K_SCORING = {
    "fgm_0_19": 3, "fgm_20_29": 3, "fgm_30_39": 3, "fgm_40_49": 4, "fgm_50_59": 5, "fgm_60p": 6,
    "xpm": 1, "fgmiss": -1,                      # ESPN doesn't take a point off for a missed extra point
}
DEF_SCORING = {
    "sack": 1, "int": 2, "fum_rec": 2, "def_st_fum_rec": 2, "blk_kick": 2, "safe": 2, "def_td": 6, "def_st_td": 6,
}
# (most allowed, points) tiers
PTS_ALLOWED = [(0, 5), (6, 4), (13, 3), (17, 1), (27, 0), (34, -1), (45, -3), (10 ** 6, -5)]
YDS_ALLOWED = [(99, 5), (199, 3), (299, 2), (349, 0), (399, -1), (449, -3), (499, -5), (549, -6), (10 ** 6, -7)]

# Stats some fantasy leagues score beyond the standard ones: first downs, sacks taken, incompletions, long-play
# and long-touchdown bonuses, return yards, and kicker and defense detail. Kept so the page can score any synced
# Sleeper, ESPN or Yahoo league's settings exactly.
LEAGUE_STATS = {
    "pass_inc", "pass_sack", "pass_fd", "pass_td_40p", "pass_td_50p", "pass_cmp_40p", "pass_int_td", "fum",
    "rush_fd", "rush_td_40p", "rush_td_50p", "rush_40p",
    "rec_fd", "rec_td_40p", "rec_td_50p", "rec_40p", "rec_0_4", "rec_5_9", "rec_10_19", "rec_20_29", "rec_30_39",
    "bonus_fd_qb", "bonus_fd_rb", "bonus_fd_wr", "bonus_fd_te", "bonus_rush_td_qb", "kr_yd", "pr_yd",
    "fga", "fgm", "fgmiss_0_19", "fgmiss_20_29", "fgmiss_30_39", "fgmiss_40_49", "fgmiss_50_59", "fgmiss_50p", "fgmiss_60p",
    "xpa", "xpmiss", "fgm_yds", "fgm_yds_over_30",
    "def_2pt", "def_pass_def", "def_3_and_out", "def_4_and_stop", "def_forced_punts", "def_kr_td", "def_pr_td", "def_fum_td",
    "ff", "qb_hit", "tkl_loss", "sack_yd", "int_ret_yd", "fum_ret_yd", "def_kr_yd", "def_pr_yd", "tkl", "tkl_solo", "tkl_ast",
}

# The raw stats kept from Sleeper (everything else is dropped to keep files small)
STAT_KEYS = sorted(set(PPR) | set(K_SCORING) | set(DEF_SCORING) | LEAGUE_STATS
                   | {"pass_cmp", "pass_att", "rush_att", "rec_tgt", "gp", "off_snp", "pts_ppr", "pts_half_ppr", "idp_fum_rec",
                      "pts_allow", "yds_allow", "tm_off_snp", "fgm_50p"})


def ppr_points(stats):
    total = sum(w * (stats.get(k) or 0) for k, w in PPR.items())
    if stats.get("idp_fum_rec"):
        # Sleeper scores a fumble-return TD as defense (not PPR) when the recovery is credited as a defensive one
        total -= PPR["fum_rec_td"] * (stats.get("fum_rec_td") or 0)
    return round(total, 2)


def _tier(value, tiers):
    return next(points for most, points in tiers if value <= most)


def kicker_points(stats):
    s = dict(stats)
    # Sleeper's 50+ count includes 60+, and it reports only one of the two splits; derive the other. Projections
    # have neither split, so their 50+ kicks count as 50-59.
    fifty = s.get("fgm_50p") or 0
    if s.get("fgm_60p") is None:
        s["fgm_60p"] = fifty - s["fgm_50_59"] if s.get("fgm_50_59") is not None else 0
    if s.get("fgm_50_59") is None:
        s["fgm_50_59"] = max(0, fifty - s["fgm_60p"])
    return sum(w * (s.get(k) or 0) for k, w in K_SCORING.items())


def defense_points(stats):
    return (sum(w * (stats.get(k) or 0) for k, w in DEF_SCORING.items())
            + _tier(stats.get("pts_allow") or 0, PTS_ALLOWED) + _tier(stats.get("yds_allow") or 0, YDS_ALLOWED))


def fantasy_points(stats, pos, season=None, half=False):
    """Fantasy points: Sleeper's standard PPR (or half PPR) for skill players, ESPN's standard scoring for K and DEF."""
    if pos == "K":
        return round(ppr_points(stats) + kicker_points(stats), 2)
    if pos == "DEF":
        return round(defense_points(stats), 2)
    return round(ppr_points(stats) - (0.5 * (stats.get("rec") or 0) if half else 0), 2)


def stat_value(key, stats):
    """The value of one prop category from a stat line (Sleeper field names)."""
    s = stats
    if key == "rush_rec_yd":
        return (s.get("rush_yd") or 0) + (s.get("rec_yd") or 0)
    if key == "anytime_td":
        return (s.get("rush_td") or 0) + (s.get("rec_td") or 0)
    if key == "fpts":
        return ppr_points(s)
    return s.get(key) or 0


# label; lean: how far our projection must be from the line to call a lean; scale: a typical spread, used to compare
# gaps across categories when ranking the top picks; min_pick: the smallest line that can be a top pick
CATEGORIES = {
    "pass_yd": {"label": "Passing Yards", "lean": 8, "scale": 30, "min_pick": 150.5},
    "pass_td": {"label": "Passing TDs", "lean": 0.15, "scale": 0.6, "min_pick": 0.5},
    "pass_cmp": {"label": "Completions", "lean": 0.8, "scale": 2.5, "min_pick": 12.5},
    "pass_att": {"label": "Pass Attempts", "lean": 1.0, "scale": 3, "min_pick": 20.5},
    "pass_int": {"label": "Interceptions", "lean": 0.12, "scale": 0.5, "min_pick": 0.5},
    "rush_yd": {"label": "Rushing Yards", "lean": 4, "scale": 12, "min_pick": 15.5},
    "rush_att": {"label": "Rush Attempts", "lean": 0.8, "scale": 3, "min_pick": 6.5},
    "rec": {"label": "Receptions", "lean": 0.3, "scale": 1.2, "min_pick": 1.5},
    "rec_yd": {"label": "Receiving Yards", "lean": 4, "scale": 12, "min_pick": 15.5},
    "rush_rec_yd": {"label": "Rush + Rec Yards", "lean": 5, "scale": 15, "min_pick": 25.5},
    "anytime_td": {"label": "Anytime TD", "lean": 0, "scale": 1, "min_pick": None},
    "fpts": {"label": "Fantasy Points (PPR)", "lean": 1.0, "scale": 4, "min_pick": None},
}

POSITION_CATEGORIES = {
    "QB": ["pass_yd", "pass_td", "pass_cmp", "pass_att", "pass_int", "rush_yd", "fpts"],
    "RB": ["rush_yd", "rush_att", "rec", "rec_yd", "rush_rec_yd", "anytime_td", "fpts"],
    "WR": ["rec", "rec_yd", "rush_rec_yd", "anytime_td", "fpts"],
    "TE": ["rec", "rec_yd", "anytime_td", "fpts"],
}

# Categories that can be a Top Over/Under pick (yes/no TD props and fantasy points are shown, not ranked)
PICKABLE = {"pass_yd", "pass_td", "pass_cmp", "pass_att", "pass_int", "rush_yd", "rush_att", "rec", "rec_yd", "rush_rec_yd"}


def round_to_half(x):
    """floor(x) + 0.5: lines always end in .5, so there are no pushes. (round() would use banker's rounding.)"""
    return math.floor(x) + 0.5


def td_probability(expected_tds):
    """Chance of at least one touchdown if TDs are Poisson with this mean."""
    return 1 - math.exp(-max(expected_tds, 0))
