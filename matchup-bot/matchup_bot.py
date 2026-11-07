#!/usr/bin/env python3
"""
Matchup Bot
===========
Scans upcoming NFL, college football, NBA and men's college basketball games for
lopsided unit-vs-unit matchups -- e.g. an elite run game facing a bottom-tier run
defense, or a top pass rush facing a leaky offensive line -- and pings your phone.

How it works
  1. Pulls every completed game this season from ESPN's public JSON feeds (no key).
  2. Builds per-game offense and defense numbers for each team, for every metric.
  3. Adjusts them for strength of schedule (optional, on by default).
  4. Ranks every team's offense and defense in every metric.
  5. For each upcoming game, compares each offense with the defense it faces.
     If one side is top-tier and the other bottom-tier, that's a mismatch.
  6. Sends new mismatches to ntfy (phone push) and/or Discord, and writes an
     HTML report plus team-rank CSVs to the reports folder.

Only the Python standard library is used. Python 3.11+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

if sys.version_info < (3, 11):
    sys.exit("Matchup Bot needs Python 3.11 or newer: https://www.python.org/downloads/")
import tomllib  # noqa: E402  (stdlib in 3.11+)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VERSION = "1.0"
BASE_DIR = Path(os.environ.get("MATCHUP_BOT_HOME", Path(__file__).resolve().parent))
CACHE_DIR = BASE_DIR / "cache"
REPORT_DIR = BASE_DIR / "site"
STATE_FILE = BASE_DIR / "state" / "sent_alerts.json"
LOG_FILE = BASE_DIR / "logs" / "last_run.log"
CONFIG_FILE = BASE_DIR / "config.toml"

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
USER_AGENT = "Mozilla/5.0 (compatible; matchup-bot/1.0)"

log = logging.getLogger("matchup_bot")

LEAGUES = {
    "nfl": {"short": "NFL", "name": "NFL", "sport": "football", "path": "football/nfl",
            "groups": None, "emoji": "🏈", "tag": "football", "season_start": (8, 1)},
    "ncaaf": {"short": "CFB", "name": "College Football", "sport": "football", "path": "football/college-football",
              "groups": "80", "emoji": "🏈", "tag": "football", "season_start": (8, 1)},
    "nba": {"short": "NBA", "name": "NBA", "sport": "basketball", "path": "basketball/nba",
            "groups": None, "emoji": "🏀", "tag": "basketball", "season_start": (10, 1)},
    "ncaab": {"short": "CBB", "name": "College Basketball", "sport": "basketball",
              "path": "basketball/mens-college-basketball",
              "groups": "50", "emoji": "🏀", "tag": "basketball", "season_start": (10, 1)},
}

DEFAULT_LEAGUE_CFG = {
    "nfl":   dict(enabled=True, strong_pct=80, weak_pct=20, min_games=3, lookahead_days=7,
                  hide_if_overall_gap_over=100, max_mismatches_per_game=3),
    "ncaaf": dict(enabled=True, strong_pct=90, weak_pct=10, min_games=4, lookahead_days=7,
                  hide_if_overall_gap_over=70, max_mismatches_per_game=3),
    "nba":   dict(enabled=True, strong_pct=80, weak_pct=20, min_games=5, lookahead_days=1,
                  hide_if_overall_gap_over=100, max_mismatches_per_game=3),
    "ncaab": dict(enabled=True, strong_pct=90, weak_pct=10, min_games=3, lookahead_days=1,
                  hide_if_overall_gap_over=70, max_mismatches_per_game=3),
}


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────────
_PAIR_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[-/]\s*(-?\d+(?:\.\d+)?)\s*$")


def _num(v):
    """ESPN values arrive as '24', '1,234', 24, or {'value': 24}. Returns float or None."""
    if isinstance(v, dict):
        v = v.get("value", v.get("displayValue"))
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _pair(v):
    """'22/35', '22-35', '3-21' -> (22.0, 35.0)."""
    if v is None:
        return None
    m = _PAIR_RE.match(str(v))
    return (float(m.group(1)), float(m.group(2))) if m else None


def _parse_stats(stats: dict, singles: dict, pairs: dict) -> dict:
    out = {}
    for name, key in singles.items():
        val = _num(stats.get(name))
        if val is not None:
            out[key] = val
    for name, (k1, k2) in pairs.items():
        p = _pair(stats.get(name))
        if p:
            out[k1], out[k2] = p
    return out


FOOTBALL_SINGLES = {
    "totalYards": "yards", "rushingYards": "rush_yds", "rushingAttempts": "rush_att",
    "netPassingYards": "pass_yds", "turnovers": "tov", "firstDowns": "first_downs",
    "totalOffensivePlays": "plays", "totalDrives": "drives", "interceptions": "ints",
    "fumblesLost": "fum_lost",
}
FOOTBALL_PAIRS = {
    "completionAttempts": ("cmp", "att"), "sacksYardsLost": ("sacks", "sack_yds"),
    "thirdDownEff": ("third_conv", "third_att"), "fourthDownEff": ("fourth_conv", "fourth_att"),
    "redZoneAttempts": ("rz_td", "rz_att"),
}
BASKETBALL_SINGLES = {
    "offensiveRebounds": "oreb", "defensiveRebounds": "dreb", "totalRebounds": "reb",
    "assists": "ast", "steals": "stl", "blocks": "blk", "fouls": "pf",
    "pointsInPaint": "paint", "fastBreakPoints": "fastbreak", "turnoverPoints": "pts_off_tov",
}
BASKETBALL_PAIRS = {
    "fieldGoalsMade-fieldGoalsAttempted": ("fgm", "fga"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("tpm", "tpa"),
    "freeThrowsMade-freeThrowsAttempted": ("ftm", "fta"),
}


def parse_football(stats: dict) -> dict:
    r = _parse_stats(stats, FOOTBALL_SINGLES, FOOTBALL_PAIRS)
    if "tov" not in r and ("ints" in r or "fum_lost" in r):
        r["tov"] = r.get("ints", 0.0) + r.get("fum_lost", 0.0)
    if "plays" not in r and "rush_att" in r and "att" in r:
        r["plays"] = r["rush_att"] + r["att"] + r.get("sacks", 0.0)
    if "yards" not in r and "rush_yds" in r and "pass_yds" in r:
        r["yards"] = r["rush_yds"] + r["pass_yds"]
    return r


def parse_basketball(stats: dict) -> dict:
    r = _parse_stats(stats, BASKETBALL_SINGLES, BASKETBALL_PAIRS)
    tov = _num(stats.get("totalTurnovers"))
    if tov is None:
        tov = _num(stats.get("turnovers"))
    if tov is not None:
        r["tov"] = tov
    if "dreb" not in r and "reb" in r and "oreb" in r:
        r["dreb"] = r["reb"] - r["oreb"]
    return r


def parse_box(summary: dict, sport: str):
    """Return {team_id: stat_line} from an ESPN summary, or None if no usable box score."""
    teams = ((summary or {}).get("boxscore") or {}).get("teams") or []
    out = {}
    for t in teams:
        tid = str((t.get("team") or {}).get("id", "")).strip()
        stats = {s.get("name"): s.get("displayValue") for s in (t.get("statistics") or [])
                 if isinstance(s, dict) and s.get("name")}
        if not tid or not stats:
            continue
        line = parse_football(stats) if sport == "football" else parse_basketball(stats)
        if line:
            out[tid] = line
    return out if len(out) == 2 else None


def slim_event(ev: dict):
    """Reduce an ESPN scoreboard event to what the bot needs."""
    try:
        comp = ev["competitions"][0]
        st = ((ev.get("status") or comp.get("status") or {}).get("type") or {})
        teams = []
        for c in comp["competitors"]:
            t = c.get("team") or {}
            teams.append({
                "id": str(c.get("id") or t.get("id")),
                "abbr": t.get("abbreviation") or t.get("shortDisplayName") or "?",
                "name": t.get("displayName") or t.get("name") or t.get("abbreviation") or "?",
                "home_away": c.get("homeAway", ""),
                "score": _num(c.get("score")),
            })
        if len(teams) != 2:
            return None
        teams.sort(key=lambda t: 0 if t["home_away"] == "away" else 1)  # away first
        return {
            "id": str(ev["id"]),
            "date": ev.get("date") or comp.get("date"),
            "season_type": (ev.get("season") or {}).get("type"),
            "state": st.get("state"),
            "completed": bool(st.get("completed")),
            "neutral": bool(comp.get("neutralSite")),
            "teams": teams,
        }
    except (KeyError, IndexError, TypeError):
        return None


def season_ok(ev) -> bool:
    return ev.get("season_type") in (None, 2, 3)  # regular season + postseason


def local_dt(iso: str | None):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def fmt_time(d) -> str:
    if d is None:
        return "TBD"
    hour = d.hour % 12 or 12
    return f"{d:%a} {d:%m/%d} {hour}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


# ─────────────────────────────────────────────────────────────────────────────
# Network + cache
# ─────────────────────────────────────────────────────────────────────────────
def fetch_json(url: str, retries: int = 3):
    """GET a JSON document. Returns None on 404 or repeated failure."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    log.warning("Request failed: %s (%s)", url, last)
    return None


def post_json(url: str, payload: dict, headers: dict | None = None) -> bool:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    h.update(headers or {})
    try:
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001
        log.warning("Notification POST failed: %s", e)
        return False


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def scoreboard_url(lg: dict, day: dt.date) -> str:
    q = {"dates": day.strftime("%Y%m%d"), "limit": "1000"}
    if lg["groups"]:
        q["groups"] = lg["groups"]
    return f"{ESPN}/{lg['path']}/scoreboard?{urllib.parse.urlencode(q)}"


_BOARD_MEMO: dict = {}


def get_scoreboard(key: str, day: dt.date, today: dt.date):
    """Events for one date. Past dates whose games are all final are cached forever."""
    if (key, day) in _BOARD_MEMO:
        return _BOARD_MEMO[(key, day)]
    cache = CACHE_DIR / key / "scoreboards" / f"{day:%Y%m%d}.json"
    if cache.exists():
        cached = _read_json(cache)
        if cached is not None:
            return cached
    data = fetch_json(scoreboard_url(LEAGUES[key], day))
    if data is None:
        return None
    events = [e for e in (slim_event(ev) for ev in data.get("events") or []) if e]
    if day < today and all(e["state"] == "post" for e in events):
        _write_json(cache, events)
    _BOARD_MEMO[(key, day)] = events
    return events


def get_scoreboards(key: str, days: list, today: dt.date, workers: int) -> dict:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda d: get_scoreboard(key, d, today), days))
    failed = sum(r is None for r in results)
    if failed:
        log.warning("%s: %d of %d scoreboard dates failed to load", key.upper(), failed, len(days))
    return {d: (r or []) for d, r in zip(days, results)}


def get_game(key: str, ev: dict, today: dt.date):
    """Box-score lines for a completed game: {'id', 'teams': {tid: line}}. Cached."""
    lg = LEAGUES[key]
    cache = CACHE_DIR / key / "games" / f"{ev['id']}.json"
    if cache.exists():
        rec = _read_json(cache)
        if rec and not rec.get("nobox"):
            return rec
        if rec and rec.get("nobox"):
            fetched = dt.date.fromisoformat(rec.get("fetched", "2000-01-01"))
            game_day = (local_dt(ev.get("date")) or dt.datetime.now().astimezone()).date()
            if (today - fetched).days < 1 or (today - game_day).days > 4:
                return None  # don't hammer ESPN for games that never get a box score
    data = fetch_json(f"{ESPN}/{lg['path']}/summary?event={ev['id']}")
    if data is None:
        return None
    lines = parse_box(data, lg["sport"])
    if not lines or set(lines) != {t["id"] for t in ev["teams"]}:
        _write_json(cache, {"nobox": True, "fetched": today.isoformat()})
        return None
    for t in ev["teams"]:
        if t["score"] is not None:
            lines[t["id"]]["pts"] = t["score"]
    rec = {"id": ev["id"], "date": ev.get("date"), "teams": lines}
    _write_json(cache, rec)
    return rec


def get_games(key: str, events: list, today: dt.date, workers: int) -> list:
    todo = len([e for e in events if not (CACHE_DIR / key / "games" / f"{e['id']}.json").exists()])
    if todo:
        log.info("%s: downloading %d new box scores (cached after this)...", key.upper(), todo)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        games = list(pool.map(lambda e: get_game(key, e, today), events))
    return [g for g in games if g]


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
#   Each metric is computed per team per game from the offense's line (o) and the
#   opposing team's line (x). The same function, applied to the opponent's offense,
#   gives what a defense allowed. Rates carry a weight (their denominator) so
#   season averages are pooled rather than averages of noisy per-game rates.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    off_unit: str
    def_unit: str
    higher_better: bool      # from the offense's point of view
    fmt: str                 # ".1f", ".2f", ".0f" or "pct"
    fn: Callable

    def show(self, v) -> str:
        if v is None:
            return "–"
        if self.fmt == "pct":
            return f"{v * 100:.1f}%"
        return format(v, self.fmt)


def _cnt(v):
    return (float(v), 1.0)


def _rate(n, d, scale=1.0):
    if d is None or d <= 0:
        return None
    return (scale * n / d, float(d))


def _poss(o, x):
    def one(t):
        return t["fga"] - t["oreb"] + t["tov"] + 0.44 * t["fta"]
    vals = []
    for t in (o, x):
        try:
            vals.append(one(t))
        except (KeyError, TypeError):
            pass
    return sum(vals) / len(vals) if vals else None


FOOTBALL_METRICS = [
    Metric("points", "Points per game", "scoring offense", "scoring defense", True, ".1f",
           lambda o, x: _cnt(o["pts"])),
    Metric("ypp", "Yards per play", "offense", "defense", True, ".2f",
           lambda o, x: _rate(o["yards"], o["plays"])),
    Metric("ppd", "Points per drive", "offense (per drive)", "defense (per drive)", True, ".2f",
           lambda o, x: _rate(o["pts"], o["drives"])),
    Metric("rush_ypg", "Rushing yards per game", "run game", "run defense", True, ".0f",
           lambda o, x: _cnt(o["rush_yds"])),
    Metric("rush_ypc", "Yards per carry", "run game", "run defense", True, ".2f",
           lambda o, x: _rate(o["rush_yds"], o["rush_att"])),
    Metric("pass_ypg", "Net passing yards per game", "pass offense", "pass defense", True, ".0f",
           lambda o, x: _cnt(o["pass_yds"])),
    Metric("net_ypa", "Net yards per pass attempt", "pass offense", "pass defense", True, ".2f",
           lambda o, x: _rate(o["pass_yds"], o["att"] + o.get("sacks", 0.0))),
    Metric("cmp_pct", "Completion %", "passing accuracy", "pass coverage", True, "pct",
           lambda o, x: _rate(o["cmp"], o["att"])),
    Metric("sack_rate", "Sack rate", "pass protection", "pass rush", False, "pct",
           lambda o, x: _rate(o["sacks"], o["att"] + o["sacks"])),
    Metric("third_down", "3rd-down conversion %", "3rd-down offense", "3rd-down defense", True, "pct",
           lambda o, x: _rate(o["third_conv"], o["third_att"])),
    Metric("red_zone", "Red-zone TD %", "red-zone offense", "red-zone defense", True, "pct",
           lambda o, x: _rate(o["rz_td"], o["rz_att"])),
    Metric("turnovers", "Turnovers per game", "ball security", "takeaways", False, ".2f",
           lambda o, x: _cnt(o["tov"])),
    Metric("first_downs", "First downs per game", "chain-moving", "getting off the field", True, ".1f",
           lambda o, x: _cnt(o["first_downs"])),
]

BASKETBALL_METRICS = [
    Metric("ortg", "Points per 100 possessions", "offense", "defense", True, ".1f",
           lambda o, x: _rate(o["pts"], _poss(o, x), 100)),
    Metric("efg", "Effective FG%", "shooting", "shooting defense", True, "pct",
           lambda o, x: _rate(o["fgm"] + 0.5 * o["tpm"], o["fga"])),
    Metric("three_pct", "3-point %", "3-point shooting", "3-point defense", True, "pct",
           lambda o, x: _rate(o["tpm"], o["tpa"])),
    Metric("three_rate", "3-point attempt rate", "3-point volume", "limiting 3-point looks", True, "pct",
           lambda o, x: _rate(o["tpa"], o["fga"])),
    Metric("two_pct", "2-point %", "interior scoring", "interior defense", True, "pct",
           lambda o, x: _rate(o["fgm"] - o["tpm"], o["fga"] - o["tpa"])),
    Metric("tov_rate", "Turnover rate", "ball security", "turnover pressure", False, "pct",
           lambda o, x: _rate(o["tov"], _poss(o, x))),
    Metric("oreb_pct", "Offensive rebound %", "offensive rebounding", "defensive rebounding", True, "pct",
           lambda o, x: _rate(o["oreb"], o["oreb"] + x["dreb"])),
    Metric("ft_rate", "Free-throw rate (FTA/FGA)", "getting to the line", "defending without fouling",
           True, "pct", lambda o, x: _rate(o["fta"], o["fga"])),
    Metric("paint", "Paint points per 100 poss.", "paint scoring", "paint defense", True, ".1f",
           lambda o, x: _rate(o["paint"], _poss(o, x), 100)),
    Metric("fastbreak", "Fast-break points per 100 poss.", "transition offense", "transition defense",
           True, ".1f", lambda o, x: _rate(o["fastbreak"], _poss(o, x), 100)),
    Metric("blocked", "Share of 2s blocked", "finishing at the rim", "rim protection", False, "pct",
           lambda o, x: _rate(x["blk"], o["fga"] - o["tpa"])),
    Metric("ast_rate", "Assist rate (AST/FGM)", "ball movement", "defending ball movement", True, "pct",
           lambda o, x: _rate(o["ast"], o["fgm"])),
]

POWER_METRIC = {"football": "points", "basketball": "ortg"}
_PTS_METRIC = Metric("_pts", "Points", "offense", "defense", True, ".1f", lambda o, x: _cnt(o["pts"]))


def eval_metric(m: Metric, o: dict, x: dict):
    try:
        r = m.fn(o, x)
    except (KeyError, TypeError, ZeroDivisionError, ValueError):
        return None
    if r is None:
        return None
    v, w = r
    if not (math.isfinite(v) and math.isfinite(w)) or w <= 0:
        return None
    return v, w


# ─────────────────────────────────────────────────────────────────────────────
# Ratings: season averages, strength-of-schedule adjustment, ranks
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Ratings:
    teams: set
    games_played: dict
    metrics: list
    off_raw: dict = field(default_factory=dict)   # metric -> team -> value
    def_raw: dict = field(default_factory=dict)
    off_adj: dict = field(default_factory=dict)
    def_adj: dict = field(default_factory=dict)
    off_rank: dict = field(default_factory=dict)
    def_rank: dict = field(default_factory=dict)
    off_pct: dict = field(default_factory=dict)
    def_pct: dict = field(default_factory=dict)
    n: dict = field(default_factory=dict)
    rel_off: dict = field(default_factory=dict)   # metric -> reliability 0..1
    rel_def: dict = field(default_factory=dict)
    power: dict = field(default_factory=dict)
    power_rank: dict = field(default_factory=dict)
    power_pct: dict = field(default_factory=dict)

    def usable(self, mkey: str, min_rel: float) -> bool:
        return self.rel_off.get(mkey, 0) >= min_rel and self.rel_def.get(mkey, 0) >= min_rel


def _wmean(pairs):
    sw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / sw if sw > 0 else None


def _rank(values: dict, descending: bool):
    order = sorted(values, key=lambda t: values[t], reverse=descending)
    n = len(order)
    rank = {t: i + 1 for i, t in enumerate(order)}
    pct = {t: (100.0 * (n - r) / (n - 1) if n > 1 else 50.0) for t, r in rank.items()}
    return rank, pct, n


def signal_stats(groups: dict):
    """How much of the spread between teams in a stat is real vs. small-sample noise.
    groups: team -> [(value, weight)]. Model: Var(per-game value) = sigma2 / weight.
    Returns (reliability 0..1, true_var tau2, sigma2)."""
    means, sumw, ss, dof = {}, {}, 0.0, 0
    for t, pairs in groups.items():
        sw = sum(w for _, w in pairs)
        if sw <= 0:
            continue
        m = sum(v * w for v, w in pairs) / sw
        means[t], sumw[t] = m, sw
        ss += sum(w * (v - m) ** 2 for v, w in pairs)
        dof += len(pairs) - 1
    if len(means) < 4 or dof <= 0:
        return 0.0, 0.0, 0.0
    sigma2 = ss / dof
    vals = list(means.values())
    grand = sum(vals) / len(vals)
    observed = sum((v - grand) ** 2 for v in vals) / (len(vals) - 1)
    noise = sum(sigma2 / sumw[t] for t in means) / len(means)
    tau2 = max(0.0, observed - noise)
    return (tau2 / observed if observed > 0 else 0.0), tau2, sigma2


def shrink(values: dict, groups: dict, mu: float, tau2: float, sigma2: float) -> dict:
    """Pull small-sample teams toward the league average (empirical Bayes)."""
    out = {}
    for t, v in values.items():
        sw = sum(w for _, w in groups.get(t, []))
        err = sigma2 / sw if sw > 0 else float("inf")
        b = tau2 / (tau2 + err) if (tau2 + err) > 0 and math.isfinite(err) else 0.0
        out[t] = mu + (v - mu) * b
    return out


def adjust_for_schedule(obs, teams, mu, iters=30):
    """Additive opponent adjustment.
    obs: list of (off_team, def_team, value, weight).
    Offense rating = what a team produces vs an average defense;
    defense rating = what it allows vs an average offense."""
    by_off, by_def = defaultdict(list), defaultdict(list)
    for o, d, v, w in obs:
        by_off[o].append((d, v, w))
        by_def[d].append((o, v, w))
    O = {t: _wmean([(v, w) for _, v, w in by_off[t]]) for t in teams if by_off[t]}
    D = {t: _wmean([(v, w) for _, v, w in by_def[t]]) for t in teams if by_def[t]}
    for _ in range(iters):
        newO = {t: _wmean([(v - (D.get(d, mu) - mu), w) for d, v, w in by_off[t]]) for t in O}
        newD = {t: _wmean([(v - (O.get(o, mu) - mu), w) for o, v, w in by_def[t]]) for t in D}
        # re-center so the league average stays at mu (removes the free +c/-c drift)
        for vals in (newO, newD):
            if vals:
                shift = mu - sum(vals.values()) / len(vals)
                for t in vals:
                    vals[t] += shift
        O = {t: 0.5 * O[t] + 0.5 * newO[t] for t in O}
        D = {t: 0.5 * D[t] + 0.5 * newD[t] for t in D}
    return O, D


def build_ratings(games: list, metrics: list, min_games: int, adjust: bool) -> Ratings:
    counts = Counter(t for g in games for t in g["teams"])
    universe = {t for t, c in counts.items() if c >= min_games}
    games = [g for g in games if all(t in universe for t in g["teams"])]
    counts = Counter(t for g in games for t in g["teams"])
    teams = {t for t, c in counts.items() if c >= min_games}
    R = Ratings(teams=teams, games_played=dict(counts), metrics=[])
    if len(teams) < 4:
        return R

    for m in metrics + [_PTS_METRIC]:
        obs = []
        for g in games:
            a, b = list(g["teams"])
            for o, d in ((a, b), (b, a)):
                r = eval_metric(m, g["teams"][o], g["teams"][d])
                if r:
                    obs.append((o, d, r[0], r[1]))
        if not games or len(obs) < 0.6 * 2 * len(games):
            continue  # stat not reported for this league (e.g. sacks in some college box scores)
        mu = _wmean([(v, w) for *_, v, w in obs])
        by_off, by_def = defaultdict(list), defaultdict(list)
        for o, d, v, w in obs:
            by_off[o].append((v, w))
            by_def[d].append((v, w))
        off_raw = {t: _wmean(by_off[t]) for t in teams if by_off.get(t)}
        def_raw = {t: _wmean(by_def[t]) for t in teams if by_def.get(t)}
        if adjust:
            off_adj, def_adj = adjust_for_schedule(obs, universe, mu)
            off_adj = {t: off_adj[t] for t in off_raw if t in off_adj}
            def_adj = {t: def_adj[t] for t in def_raw if t in def_adj}
        else:
            off_adj, def_adj = dict(off_raw), dict(def_raw)
        rel_o, tau_o, sig_o = signal_stats({t: by_off[t] for t in teams if by_off.get(t)})
        rel_d, tau_d, sig_d = signal_stats({t: by_def[t] for t in teams if by_def.get(t)})
        if m is not _PTS_METRIC:
            off_adj = shrink(off_adj, by_off, mu, tau_o, sig_o)
            def_adj = shrink(def_adj, by_def, mu, tau_d, sig_d)
        R.rel_off[m.key], R.rel_def[m.key] = rel_o, rel_d
        R.off_raw[m.key], R.def_raw[m.key] = off_raw, def_raw
        R.off_adj[m.key], R.def_adj[m.key] = off_adj, def_adj
        # offense: higher value is better when higher_better; defense is good when opponents do badly
        R.off_rank[m.key], R.off_pct[m.key], R.n[m.key] = _rank(off_adj, descending=m.higher_better)
        R.def_rank[m.key], R.def_pct[m.key], _ = _rank(def_adj, descending=not m.higher_better)
        if m is not _PTS_METRIC:
            R.metrics.append(m)
    return R


def compute_power(R: Ratings, sport: str) -> None:
    key = POWER_METRIC[sport] if POWER_METRIC[sport] in R.off_adj else "_pts"
    if key not in R.off_adj:
        return
    R.power = {t: R.off_adj[key][t] - R.def_adj[key][t]
               for t in R.teams if t in R.off_adj[key] and t in R.def_adj[key]}
    R.power_rank, R.power_pct, _ = _rank(R.power, descending=True)


# ─────────────────────────────────────────────────────────────────────────────
# Mismatch detection
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Mismatch:
    metric: Metric
    off_team: str
    def_team: str
    off_rank: int
    def_rank: int
    n: int
    off_pct: float
    def_pct: float
    off_val: float
    def_val: float

    @property
    def gap(self) -> float:
        return abs(self.off_pct - self.def_pct)

    @property
    def offense_favored(self) -> bool:
        return self.off_pct >= self.def_pct

    @property
    def edge_team(self) -> str:
        return self.off_team if self.offense_favored else self.def_team

    def key(self, league: str, event_id: str) -> str:
        # keyed on the unit pairing, not the exact stat, so "run game vs run D" alerts once
        # even if yards/game and yards/carry trade places as the top stat between runs
        return f"{league}:{event_id}:{self.metric.off_unit}|{self.metric.def_unit}:{self.off_team}"


@dataclass
class GameAlert:
    event: dict
    mismatches: list
    overall_gap: float | None

    @property
    def best_gap(self) -> float:
        return max(m.gap for m in self.mismatches)


def find_mismatches(upcoming: list, R: Ratings, lc: dict, disabled: set, min_rel: float):
    alerts, skipped = [], []
    disabled = set(disabled) | {m.key for m in R.metrics if not R.usable(m.key, min_rel)}
    for ev in upcoming:
        a, b = ev["teams"][0]["id"], ev["teams"][1]["id"]
        if a not in R.teams or b not in R.teams:
            skipped.append(ev)
            continue
        overall_gap = None
        if a in R.power_pct and b in R.power_pct:
            overall_gap = abs(R.power_pct[a] - R.power_pct[b])
            if overall_gap > lc["hide_if_overall_gap_over"]:
                continue
        found = []
        for m in R.metrics:
            if m.key in disabled:
                continue
            for off_t, def_t in ((a, b), (b, a)):
                if off_t not in R.off_pct[m.key] or def_t not in R.def_pct[m.key]:
                    continue
                po, pd = R.off_pct[m.key][off_t], R.def_pct[m.key][def_t]
                if max(po, pd) >= lc["strong_pct"] and min(po, pd) <= lc["weak_pct"]:
                    found.append(Mismatch(
                        metric=m, off_team=off_t, def_team=def_t,
                        off_rank=R.off_rank[m.key][off_t], def_rank=R.def_rank[m.key][def_t],
                        n=R.n[m.key], off_pct=po, def_pct=pd,
                        off_val=R.off_raw[m.key][off_t], def_val=R.def_raw[m.key][def_t]))
        if found:
            found.sort(key=lambda mm: mm.gap, reverse=True)
            best, seen = [], set()
            for mm in found:  # one line per unit pairing (keeps the biggest gap)
                k = (mm.metric.off_unit, mm.metric.def_unit, mm.off_team)
                if k not in seen:
                    seen.add(k)
                    best.append(mm)
            alerts.append(GameAlert(ev, best[: lc["max_mismatches_per_game"]], overall_gap))
    alerts.sort(key=lambda g: (-g.best_gap, g.overall_gap if g.overall_gap is not None else 100))
    return alerts, skipped


# ─────────────────────────────────────────────────────────────────────────────
# Text formatting
# ─────────────────────────────────────────────────────────────────────────────
def game_title(ev: dict) -> str:
    a, h = ev["teams"]
    return f"{a['abbr']} {'vs' if ev['neutral'] else '@'} {h['abbr']}"


def mismatch_line(mm: Mismatch, names: dict) -> str:
    m = mm.metric
    off = f"{names[mm.off_team]} {m.off_unit} #{mm.off_rank} ({m.show(mm.off_val)})"
    dfn = f"{names[mm.def_team]} {m.def_unit} #{mm.def_rank} ({m.show(mm.def_val)})"
    first, second = (off, dfn) if mm.offense_favored else (dfn, off)
    return f"{m.label}: {first} vs {second}"


def close_game_tag(g: GameAlert) -> str:
    return " ⚡ evenly matched overall" if g.overall_gap is not None and g.overall_gap <= 25 else ""


# ─────────────────────────────────────────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────────────────────────────────────────
def build_message(lg: dict, alerts: list, names: dict, max_games: int, n_teams: int, max_bytes=3800):
    title = f"{lg['emoji']} {lg['name']}: {sum(len(g.mismatches) for g in alerts)} new mismatch" \
            f"{'es' if sum(len(g.mismatches) for g in alerts) != 1 else ''} in {len(alerts)} game" \
            f"{'s' if len(alerts) != 1 else ''}"
    parts = [f"Ranks out of ~{n_teams} teams, schedule-adjusted."]
    shown = 0
    for g in alerts[:max_games]:
        block = [f"\n{game_title(g.event)} · {fmt_time(local_dt(g.event['date']))}{close_game_tag(g)}"]
        block += [f"• {mismatch_line(mm, names)}" for mm in g.mismatches]
        candidate = "\n".join(parts + block)
        if len(candidate.encode("utf-8")) > max_bytes:
            break
        parts += block
        shown += 1
    rest = len(alerts) - shown
    if rest > 0:
        parts.append(f"\n+{rest} more game{'s' if rest != 1 else ''} in the report.")
    return title, "\n".join(parts)


def send_ntfy(cfg: dict, title: str, body: str, tag: str) -> bool:
    topic = (cfg.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    server = (cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    return post_json(server + "/", {"topic": topic, "title": title, "message": body,
                                    "tags": [tag], "priority": 3})


def send_discord(cfg: dict, title: str, body: str) -> bool:
    hook = (cfg.get("discord_webhook") or "").strip()
    if not hook:
        return False
    text = f"**{title}**\n{body}"
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    ok = True
    for c in chunks:
        ok &= post_json(hook, {"content": c})
        time.sleep(0.5)
    return ok


def notify(cfg: dict, title: str, body: str, tag: str) -> bool:
    sent_any = False
    sent_any |= send_ntfy(cfg, title, body, tag)
    sent_any |= send_discord(cfg, title, body)
    return sent_any


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────
def write_rank_csv(key: str, R: Ratings, names_full: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{key}_team_ranks.csv"
    header = ["team", "games", "power_rank"]
    for m in R.metrics:
        header += [f"{m.key}_off", f"{m.key}_off_rank", f"{m.key}_def_allowed", f"{m.key}_def_rank"]
    rows = []
    for t in sorted(R.teams, key=lambda t: R.power_rank.get(t, 9999)):
        row = [names_full.get(t, t), R.games_played.get(t, 0), R.power_rank.get(t, "")]
        for m in R.metrics:
            row += [round(R.off_raw[m.key].get(t, float("nan")), 4), R.off_rank[m.key].get(t, ""),
                    round(R.def_raw[m.key].get(t, float("nan")), 4), R.def_rank[m.key].get(t, "")]
        rows.append(row)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


SITE_TEMPLATE = BASE_DIR / "site_template.html"


def site_payload(results: list, run_at: dt.datetime) -> dict:
    leagues = []
    for res in results:
        lg, R = res["league"], res.get("ratings")
        names, full = res.get("names", {}), res.get("names_full", {})
        games = []
        for g in res.get("alerts") or []:
            ev = g.event
            a, h = ev["teams"]
            rows = []
            for mm in g.mismatches:
                m = mm.metric
                off = {"abbr": names.get(mm.off_team, "?"), "unit": m.off_unit, "rank": mm.off_rank,
                       "value": m.show(mm.off_val), "pct": round(mm.off_pct, 1)}
                dfn = {"abbr": names.get(mm.def_team, "?"), "unit": m.def_unit, "rank": mm.def_rank,
                       "value": m.show(mm.def_val), "pct": round(mm.def_pct, 1)}
                strong, weak = (off, dfn) if mm.offense_favored else (dfn, off)
                rows.append({"metric": m.label, "gap": round(mm.gap), "n": mm.n, "strong": strong,
                             "weak": weak, "new": mm.key(res["key"], ev["id"]) in res["new_keys"]})
            games.append({
                "id": ev["id"], "when": fmt_time(local_dt(ev["date"])), "neutral": ev["neutral"],
                "away": {"abbr": a["abbr"], "name": full.get(a["id"], a["name"]),
                         "rank": R.power_rank.get(a["id"]) if R else None},
                "home": {"abbr": h["abbr"], "name": full.get(h["id"], h["name"]),
                         "rank": R.power_rank.get(h["id"]) if R else None},
                "close": g.overall_gap is not None and g.overall_gap <= 25,
                "mismatches": rows})
        leagues.append({"key": res["key"], "name": lg["name"], "short": lg["short"],
                        "note": res.get("note", ""), "n_teams": res.get("n_teams"),
                        "csv": f"{res['key']}_team_ranks.csv" if res.get("csv") else None,
                        "games": games})
    hour = run_at.hour % 12 or 12
    return {"generated": run_at.isoformat(), "date": run_at.date().isoformat(),
            "day": run_at.strftime("%A"), "date_label": f"{run_at:%A}, {run_at:%B} {run_at.day}",
            "time_label": f"{hour}:{run_at:%M} {'AM' if run_at.hour < 12 else 'PM'}",
            "leagues": leagues}


def build_site(results: list, run_at: dt.datetime) -> Path:
    """Write site/index.html: the morning board (data embedded, so it also opens from disk)."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    data = json.dumps(site_payload(results, run_at), ensure_ascii=False).replace("</", "<\\/")
    page = SITE_TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/{}", data)
    out = REPORT_DIR / "index.html"
    out.write_text(page, encoding="utf-8")
    (REPORT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Config + state
# ─────────────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as f:
            cfg = tomllib.load(f)
    else:
        log.warning("config.toml not found; using defaults (no notifications).")
    cfg.setdefault("notify", {})
    for env, k in (("NTFY_TOPIC", "ntfy_topic"), ("DISCORD_WEBHOOK", "discord_webhook")):
        if os.environ.get(env, "").strip():  # GitHub secrets arrive as env vars
            cfg["notify"][k] = os.environ[env].strip()
    cfg.setdefault("analysis", {})
    cfg.setdefault("metrics", {})
    cfg.setdefault("leagues", {})
    for k, d in DEFAULT_LEAGUE_CFG.items():
        merged = dict(d)
        merged.update(cfg["leagues"].get(k, {}))
        cfg["leagues"][k] = merged
    return cfg


def load_state() -> dict:
    st = _read_json(STATE_FILE) or {}
    st.setdefault("sent", {})
    cutoff = (dt.date.today() - dt.timedelta(days=21)).isoformat()
    st["sent"] = {k: v for k, v in st["sent"].items() if v >= cutoff}
    return st


def season_start(key: str, lc: dict, today: dt.date) -> dt.date:
    if lc.get("season_start"):
        return dt.date.fromisoformat(str(lc["season_start"]))
    m, d = LEAGUES[key]["season_start"]
    start = dt.date(today.year, m, d)
    return start if start <= today else dt.date(today.year - 1, m, d)


# ─────────────────────────────────────────────────────────────────────────────
# Main flow
# ─────────────────────────────────────────────────────────────────────────────
def run_league(key: str, cfg: dict, today: dt.date, workers: int) -> dict:
    lg, lc = LEAGUES[key], cfg["leagues"][key]
    res = {"key": key, "league": lg, "alerts": [], "new_keys": set(), "note": ""}

    window = [today + dt.timedelta(days=i) for i in range(max(1, int(lc["lookahead_days"])))]
    boards = get_scoreboards(key, window, today, workers)
    upcoming = {e["id"]: e for evs in boards.values() for e in evs
                if e["state"] == "pre" and season_ok(e)}
    if not upcoming:
        res["note"] = "No upcoming games in the scan window."
        log.info("%s: no upcoming games in the next %d day(s).", key.upper(), len(window))
        return res

    start = season_start(key, lc, today)
    hist_days = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
    hist = get_scoreboards(key, hist_days, today, workers)
    finals = {e["id"]: e for evs in hist.values() for e in evs
              if e["state"] == "post" and e["completed"] and season_ok(e)}
    names_abbr, names_full = {}, {}
    for e in list(finals.values()) + list(upcoming.values()):
        for t in e["teams"]:
            names_abbr[t["id"]], names_full[t["id"]] = t["abbr"], t["name"]

    games = get_games(key, list(finals.values()), today, workers)
    log.info("%s: %d completed games with box scores, %d upcoming.", key.upper(), len(games), len(upcoming))
    if finals and not games:
        log.warning("%s: found %d finals but parsed 0 box scores - ESPN's format may have changed.",
                    key.upper(), len(finals))

    metrics = FOOTBALL_METRICS if lg["sport"] == "football" else BASKETBALL_METRICS
    R = build_ratings(games, metrics, int(lc["min_games"]),
                      bool(cfg["analysis"].get("opponent_adjust", True)))
    compute_power(R, lg["sport"])
    if len(R.teams) < 4:
        res["note"] = f"Not enough data yet - teams need {lc['min_games']}+ games to be ranked."
        log.info("%s: %s", key.upper(), res["note"])
        return res

    write_rank_csv(key, R, names_full)
    res["csv"] = True
    disabled = set(cfg["metrics"].get("disabled", []))
    min_rel = float(cfg["analysis"].get("min_reliability", 0.3))
    alerts, skipped = find_mismatches(list(upcoming.values()), R, lc, disabled, min_rel)
    n_teams = len(R.teams)
    noisy = [m.label for m in R.metrics if m.key not in disabled and not R.usable(m.key, min_rel)]
    active = len([m for m in R.metrics if m.key not in disabled]) - len(noisy)
    res.update(alerts=alerts, ratings=R, names=names_abbr, names_full=names_full, n_teams=n_teams)
    res["note"] = (f"{len(upcoming)} upcoming games scanned, {n_teams} teams ranked, "
                   f"{active} metrics in use"
                   + (f"; {len(skipped)} games skipped (a team has under {lc['min_games']} games)"
                      if skipped else "")
                   + (f". Too noisy to trust yet: {', '.join(noisy)}" if noisy else "") + ".")
    log.info("%s: reliability %s", key.upper(), ", ".join(
        f"{m.key}={min(R.rel_off[m.key], R.rel_def[m.key]):.2f}" for m in R.metrics))
    log.info("%s: %d games with mismatches.", key.upper(), len(alerts))
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Flag lopsided matchups in NFL, NCAAF, NBA and NCAAB.")
    ap.add_argument("--leagues", help="comma list, e.g. nfl,nba (default: all enabled in config)")
    ap.add_argument("--dry-run", action="store_true", help="don't send notifications or mark alerts sent")
    ap.add_argument("--resend", action="store_true", help="send everything, even alerts sent before")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    ap.add_argument("--no-open", action="store_true", help="don't open the report in a browser")
    ap.add_argument("--scheduled", action="store_true", help="for Task Scheduler (implies --no-open)")
    ap.add_argument("--workers", type=int, default=6, help="parallel downloads (default 6)")
    args = ap.parse_args(argv)

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")])
    log.info("Matchup Bot v%s", VERSION)
    cfg = load_config()
    ncfg = cfg["notify"]
    has_channel = bool((ncfg.get("ntfy_topic") or "").strip() or (ncfg.get("discord_webhook") or "").strip())

    if args.test_notify:
        ok = notify(ncfg, "✅ Matchup Bot is connected",
                    "You'll get alerts here when the bot finds lopsided matchups.", "white_check_mark")
        log.info("Test notification %s.", "sent" if ok else "FAILED - check ntfy_topic / discord_webhook")
        return 0 if ok else 1
    if not has_channel and not args.dry_run:
        log.warning("No ntfy_topic or discord_webhook set in config.toml - results go to the report only.")

    keys = [k.strip().lower() for k in args.leagues.split(",")] if args.leagues else \
        [k for k in LEAGUES if cfg["leagues"][k].get("enabled", True)]
    bad = [k for k in keys if k not in LEAGUES]
    if bad:
        log.error("Unknown league(s): %s. Use: %s", ", ".join(bad), ", ".join(LEAGUES))
        return 2

    today = dt.date.today()
    run_at = dt.datetime.now().astimezone()
    state = load_state()
    results = []
    for key in keys:
        try:
            res = run_league(key, cfg, today, args.workers)
        except Exception:  # noqa: BLE001 - one league failing shouldn't kill the rest
            log.exception("%s failed", key.upper())
            res = {"key": key, "league": LEAGUES[key], "alerts": [], "new_keys": set(),
                   "note": "This league failed to run - see logs/last_run.log."}
        results.append(res)

        fresh = []
        for g in res["alerts"]:
            new_mm = [mm for mm in g.mismatches
                      if args.resend or mm.key(key, g.event["id"]) not in state["sent"]]
            if new_mm:
                fresh.append(GameAlert(g.event, new_mm, g.overall_gap))
                res["new_keys"].update(mm.key(key, g.event["id"]) for mm in new_mm)
        if not fresh:
            continue
        title, body = build_message(res["league"], fresh, res["names"],
                                    int(ncfg.get("max_games_per_alert", 8)), res["n_teams"])
        print("\n" + "=" * 70 + f"\n{title}\n{body}\n" + "=" * 70)
        if args.dry_run or not has_channel:
            continue
        if notify(ncfg, title, body, res["league"]["tag"]):
            for k in res["new_keys"]:
                state["sent"][k] = today.isoformat()
            log.info("%s: notification sent.", key.upper())

    if not args.dry_run:
        _write_json(STATE_FILE, state)
    report = build_site(results, run_at)
    log.info("Report: %s", report)
    if not (args.no_open or args.scheduled):
        try:
            webbrowser.open(report.as_uri())
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
