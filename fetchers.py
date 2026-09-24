#!/usr/bin/env python3
"""
fetchers.py -- pull two-way player-prop prices from DraftKings and FanDuel
and emit the record shape wheel.py consumes:

    {"player": "Tee Higgins", "team": "CIN", "game": "CLE @ CIN",
     "market": "receptions", "line": 4.5,
     "quotes": [{"book": "dk", "over": -143, "under": 110},
                {"book": "fd", "over": -140, "under": 108}]}

Two-way markets need both sides; a lone side of a two-way market is dropped,
never guessed. Markets in ONE_SIDED (anytime TD) are priced by the books on
one side only; their quotes carry that side alone ("under": None) and run.py
uses them only when the app offers that same side, never the mirror.

Records from different books are merged ONLY on (normalized player, market,
line). Different lines are different bets and are never merged.

Endpoints (discovered 2026-09-16 from the live sites; they change without
notice, so the parsers print what they see when something looks off):

  DK  https://sportsbook-nash.draftkings.com/api/sportscontent/dkusva/v1/
        leagues/{league}                                   -> events, subcategory ids
        leagues/{league}/categories/{cat}/subcategories/{sub} -> markets + selections
      Only the "... O/U" subcategories are two-way. The plain "Receptions",
      "Pass TDs" etc. subcategories are one-sided "N+" milestone ladders.
      The old sportsbook.draftkings.com/sites/US-SB/api/v5 endpoints are 403.

  FD  https://sbapi.{state}.sportsbook.fanduel.com/api/
        content-managed-page?page=CUSTOM&customPageId=nfl  -> events
        event-page?eventId=..&tab=passing-props|receiving-props|rushing-props
      Market types look like PLAYER_X_RECEPTIONS_HIGH / _MEDIUM / _LOW; the
      suffix is a line tier, not a side. Each has Over/Under runners with the
      line in runner.handicap. PLAYER_X_ALT_* are ladders and are skipped.

Usage:
    python fetchers.py --sport nfl --books dk,fd --out wk3.json
    python fetchers.py --odds-api --odds-api-key KEY --out wk3.json   (fallback)

Standard library + requests only. Read-only. No auth, no posting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import time

import requests

# --------------------------------------------------------------------------
# shared vocabulary
# --------------------------------------------------------------------------

NFL_MARKETS = (
    "receptions", "pass_tds", "rush_attempts", "pass_yards",
    "receiving_yards", "completions", "pass_attempts", "rush_yards",
    "anytime_td", "interceptions", "field_goals_made",
)
# MLB: only the pitcher line is worth pricing. Batter props are 4-event
# markets, far too noisy for a 3-leg parlay that needs 56% a leg.
MLB_MARKETS = ("strikeouts", "hits_allowed", "pitching_outs")
# WNBA: the counting stats and the combos the books post two-way.
WNBA_MARKETS = ("points", "rebounds", "assists", "threes",
                "pts_ast", "pts_reb", "reb_ast", "pra")

SPORT_MARKETS = {"nfl": NFL_MARKETS, "mlb": MLB_MARKETS, "wnba": WNBA_MARKETS}
MARKETS = NFL_MARKETS          # back-compat default for NFL-only callers
ALL_MARKETS = tuple(dict.fromkeys(sum(SPORT_MARKETS.values(), ())))

# Markets the books price on ONE side only ("yes" / over 0.5). Their quotes
# carry only that side: {"book": "dk", "over": -150, "under": None}. A
# one-sided price says nothing about the other side and is never mirrored.
ONE_SIDED = {"anytime_td"}


def markets_for(sport: str) -> tuple:
    s = sport.lower()
    if s not in SPORT_MARKETS:
        raise NotImplementedError(f"unknown sport {sport!r}; known: {list(SPORT_MARKETS)}")
    return SPORT_MARKETS[s]

# The Odds API market keys -> shared vocabulary. Used both as a standalone
# fallback and, more usefully, to fill single-book gaps left by DK and FD.
ODDS_API_MARKET = {
    # NFL
    "player_receptions": "receptions",
    "player_pass_tds": "pass_tds",
    "player_rush_attempts": "rush_attempts",
    "player_pass_yds": "pass_yards",
    "player_reception_yds": "receiving_yards",
    "player_pass_completions": "completions",
    "player_pass_attempts": "pass_attempts",
    "player_rush_yds": "rush_yards",
    "player_pass_interceptions": "interceptions",
    "player_field_goals": "field_goals_made",
    "player_anytime_td": "anytime_td",
    # MLB
    "pitcher_strikeouts": "strikeouts",
    "pitcher_hits_allowed": "hits_allowed",
    "pitcher_outs": "pitching_outs",
}

# Bookmaker keys we accept, mapped to short codes for the board. dk/fd are
# scraped directly and are only taken from here when the direct path is off;
# merge_records keeps the first quote per book, so the direct one wins.
# Checked live 2026-09-24: BetMGM posts receptions but NOT interceptions,
# field goals or completions. Fanatics is the only book in the feed posting
# MLB hits allowed and pitching outs. Bet365 is not carried in the us region.
ODDS_API_BOOK = {
    "draftkings": "dk", "fanduel": "fd", "betmgm": "mgm", "betrivers": "br",
    "fanatics": "fan", "williamhill_us": "wh", "bovada": "bov", "betonlineag": "bol",
}

# Legal US books first; the offshore pair is opt-in because they carry more vig
# and often mirror a US line rather than adding an independent opinion.
ODDS_API_DEFAULT_BOOKS = ("mgm", "br", "fan", "wh")

# Markets where the feed carries no book beyond DK/FD, so a gap-fill request
# bills a credit and returns nothing usable. Observed 2026-09-24; recheck if a
# book starts posting them.
ODDS_API_NO_EXTRA = {"field_goals_made"}
ODDS_API_SPORT_KEY = {"nfl": "americanfootball_nfl", "mlb": "baseball_mlb",
                      "wnba": "basketball_wnba"}

NFL_TEAMS = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}
MLB_TEAMS = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Athletics": "ATH", "Oakland Athletics": "ATH",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD",
    "San Francisco Giants": "SF", "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}
WNBA_TEAMS = {
    "Atlanta Dream": "ATL", "Chicago Sky": "CHI", "Connecticut Sun": "CON",
    "Dallas Wings": "DAL", "Golden State Valkyries": "GSV", "Indiana Fever": "IND",
    "Las Vegas Aces": "LVA", "Los Angeles Sparks": "LAS", "Minnesota Lynx": "MIN",
    "New York Liberty": "NYL", "Phoenix Mercury": "PHX", "Seattle Storm": "SEA",
    "Washington Mystics": "WAS",
}
TEAMS = {"nfl": NFL_TEAMS, "mlb": MLB_TEAMS, "wnba": WNBA_TEAMS}
# DraftKings' own shortName differs from FanDuel's for a few clubs. Left
# unmapped these produce two different game strings for one game, which breaks
# the per-game fetch filter. merge_records logs any pair that is still adrift.
DK_TEAM_ALIAS = {
    "nfl": {},
    "mlb": {"A's": "ATH", "SFG": "SF", "WAS": "WSH"},
    "wnba": {"NY": "NYL", "LV": "LVA", "LA": "LAS", "GS": "GSV", "CONN": "CON"},
}
# FanDuel logo slugs: ".../images/team/nfl/buffalo_bills_jersey.png"
NFL_SLUG = {k.lower().replace(" ", "_"): v for k, v in NFL_TEAMS.items()}
TEAM_SLUG = {sp: {k.lower().replace(" ", "_").replace(".", ""): v for k, v in t.items()}
             for sp, t in TEAMS.items()}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Nickname / spelling differences between books that no rule can derive.
# Keys and values are normalized forms. When merge_records logs a
# "possible same player under different names" line, add the pair here.
PLAYER_ALIASES = {
    "joshua palmer": "josh palmer",          # DK Joshua, FD Josh
    "cameron skattebo": "cam skattebo",      # DK Cameron, FD Cam
}

# normalized key -> display name(s) seen, so output can show the book's spelling
DISPLAY_NAMES: dict = {}


def normalize_player(name: str) -> str:
    """'D.J. Moore Jr.' -> 'dj moore'. Strips suffixes, punctuation, case."""
    s = name.strip().lower()
    s = re.sub(r"[.'’\-]", "", s)          # D.J. -> DJ, Amon-Ra -> AmonRa, D'Andre -> DAndre
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = s.split()
    while len(toks) > 1 and toks[-1] in _SUFFIXES:
        toks.pop()
    key = " ".join(toks)
    key = PLAYER_ALIASES.get(key, key)
    if key:
        seen = DISPLAY_NAMES.setdefault(key, [])
        if name.strip() not in seen:
            seen.append(name.strip())
    return key


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP with disk cache and polite delay
# --------------------------------------------------------------------------

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def read_env_key(name: str = "ODDS_API_KEY") -> str:
    """Read a secret from the gitignored .env, so it never lands in a command line."""
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def browser_headers(origin: str) -> dict:
    return {
        "User-Agent": CHROME_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",   # no br: requests cannot decode Brotli without an extra package
        "Origin": origin,
        "Referer": origin + "/",
        "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Connection": "keep-alive",
    }


class BotBlocked(RuntimeError):
    """403 / challenge page. Report it; do not work around it."""


class Client:
    def __init__(self, book: str, origin: str, max_age_min: float = 30.0,
                 refresh: bool = False, delay: tuple = (1.0, 2.0)):
        self.book = book
        self.sess = requests.Session()
        self.sess.headers.update(browser_headers(origin))
        self.max_age = dt.timedelta(minutes=max_age_min)
        self.refresh = refresh
        self.delay = delay
        self.hits = 0
        self.cached = 0
        self.credits = 0          # metered APIs: units actually billed
        self.remaining = None     # units left on the plan, per the last response
        os.makedirs(os.path.join(CACHE_DIR, book), exist_ok=True)

    def _path(self, url: str) -> str:
        h = hashlib.sha1(url.encode()).hexdigest()[:12]
        tail = re.sub(r"[^A-Za-z0-9]+", "_", url.split("://", 1)[-1])[-60:]
        return os.path.join(CACHE_DIR, self.book, f"{tail}_{h}.json")

    def get_json(self, url: str) -> dict:
        path = self._path(url)
        if not self.refresh and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    blob = json.load(fh)
                fetched = dt.datetime.fromisoformat(blob["fetched_at"])
                if dt.datetime.now(dt.timezone.utc) - fetched <= self.max_age:
                    self.cached += 1
                    return blob["body"]
            except Exception:
                pass
        if self.hits:
            time.sleep(random.uniform(*self.delay))
        self.hits += 1
        r = self.sess.get(url, timeout=30)
        if r.status_code in (403, 429) or "Access Denied" in r.text[:500]:
            raise BotBlocked(f"{self.book}: HTTP {r.status_code} from {url}\n"
                             f"{r.text[:200]}")
        r.raise_for_status()
        # The Odds API bills per market that actually returns data and reports
        # the tally in headers; unposted markets cost nothing.
        try:
            self.credits += int(r.headers.get("x-requests-last", 0) or 0)
            if "x-requests-remaining" in r.headers:
                self.remaining = int(float(r.headers["x-requests-remaining"]))
        except (TypeError, ValueError):
            pass
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError(f"{self.book}: non-JSON response from {url}: "
                               f"{r.text[:200]!r}")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"url": url,
                       "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "status": r.status_code, "body": body}, fh)
        return body


def parse_american(s):
    """DK sends '−152' with a Unicode minus (U+2212), '+119', or 'EVEN'."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    t = str(s).strip().replace("−", "-").replace("+", "")
    if t.upper() in ("EVEN", "EV"):
        return 100
    try:
        return int(t)
    except ValueError:
        return None


def in_window(when: dt.datetime, days: float) -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    return now - dt.timedelta(hours=4) <= when <= now + dt.timedelta(days=days)


def parse_iso(s: str) -> dt.datetime:
    # DK: 2026-09-18T00:15:00.0000000Z  FD: 2026-09-18T00:15:00.000Z
    s = re.sub(r"(\.\d{1,6})\d*", r"\1", s).replace("Z", "+00:00")
    return dt.datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# DraftKings
# --------------------------------------------------------------------------

DK_BASE = "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusva/v1"
DK_ORIGIN = "https://sportsbook.draftkings.com"
DK_LEAGUE = {"nfl": "88808", "mlb": "84240", "wnba": "94682"}

# Resolved by subcategory NAME from the league payload at run time; the ids
# below are the values observed 2026-09-16 and are only a fallback.
DK_SUBCAT = {
    "pass_yards":      ("Pass Yards O/U",     (1000, 9524)),
    "pass_tds":        ("Pass TDs O/U",       (1000, 9525)),
    "pass_attempts":   ("Pass Attempts O/U",  (1000, 9517)),
    "completions":     ("Completions O/U",    (1000, 9522)),
    "receptions":      ("Receptions O/U",     (1342, 14115)),
    "receiving_yards": ("Rec Yards O/U",      (1342, 14114)),
    "rush_yards":      ("Rush Yards O/U",     (1001, 9514)),
    "rush_attempts":   ("Rush Attempts O/U",  (1001, 9518)),
    "anytime_td":      ("TD Scorer",          (1003, 12438)),   # one-sided "yes" list per game
    "interceptions":   ("Interceptions O/U",  (1000, 15937)),
    "field_goals_made": ("FG Made O/U",       (1743, 17061)),   # Special Teams Props
}
# Only the "... O/U" subcategories are two-way; the bare ones are N+ ladders.
DK_SUBCAT_MLB = {                                                  # all cat 1031 Pitcher Props
    "strikeouts":      ("Strikeouts Thrown O/U", (1031, 15221)),
    "hits_allowed":    ("Hits Allowed O/U",      (1031, 9886)),
    "pitching_outs":   ("Outs Recorded O/U",     (1031, 17413)),
}
DK_SUBCAT_WNBA = {
    "points":          ("Points O/U",          (1215, 12488)),
    "rebounds":        ("Rebounds O/U",        (1216, 12492)),
    "assists":         ("Assists O/U",         (1217, 12495)),
    "threes":          ("Threes O/U",          (1218, 12497)),
    "pts_ast":         ("Pts + Ast O/U",       (583, 9973)),
    "pts_reb":         ("Pts + Reb O/U",       (583, 9976)),
    "reb_ast":         ("Ast + Reb O/U",       (583, 9974)),
    "pra":             ("Pts + Reb + Ast O/U", (583, 5001)),
}
DK_SUBCATS = {"nfl": DK_SUBCAT, "mlb": DK_SUBCAT_MLB, "wnba": DK_SUBCAT_WNBA}
DK_ONE_SIDED_MARKET_NAME = {"anytime_td": "anytime td scorer"}   # market.name to keep in that subcategory


def _dk_events(payload: dict, sport: str = "nfl") -> dict:
    """event id -> {"game": "DET @ BUF", "home": "BUF", "away": "DET", "start": datetime}"""
    alias = DK_TEAM_ALIAS.get(sport.lower(), {})
    canon = set(TEAMS.get(sport.lower(), {}).values())
    out = {}
    for e in payload.get("events", []):
        home = away = ""
        for p in e.get("participants", []):
            abbr = (p.get("metadata") or {}).get("shortName") or p.get("name", "").split()[0]
            abbr = alias.get(abbr, abbr)
            if canon and abbr not in canon:
                _dk_unknown_abbr.add((sport.lower(), abbr, p.get("name", "")))
            if p.get("venueRole") == "Home":
                home = abbr
            elif p.get("venueRole") == "Away":
                away = abbr
        try:
            start = parse_iso(e["startEventDate"])
        except Exception:
            start = None
        out[str(e["id"])] = {"game": f"{away} @ {home}" if home and away else e.get("name", ""),
                             "home": home, "away": away, "start": start,
                             "name": e.get("name", "")}
    return out


def fetch_dk(sport: str, markets=MARKETS, days: float = 7, max_age_min: float = 30,
             refresh: bool = False, games=None) -> list:
    """
    Return one record per (player, market, line) with a single 'dk' quote.
    DK serves each market league-wide in one request, so `games` (a set of
    'AWAY @ HOME' strings) only filters the records, it does not save calls.
    """
    league = DK_LEAGUE.get(sport.lower())
    if not league:
        raise NotImplementedError(f"DK: no league id for sport {sport!r}; add it to DK_LEAGUE")
    subcats = DK_SUBCATS.get(sport.lower(), {})
    c = Client("dk", DK_ORIGIN, max_age_min, refresh)

    nav = c.get_json(f"{DK_BASE}/leagues/{league}")
    events = _dk_events(nav, sport)
    by_name = {}
    for s in nav.get("subcategories", []):
        by_name[s.get("name", "").strip().lower()] = (s.get("categoryId"), s.get("id"))

    records = []
    for mkt in markets:
        if mkt not in subcats:
            log(f"dk: no subcategory mapping for {sport} market {mkt!r}, skipping")
            continue
        name, fallback = subcats[mkt]
        cat, sub = by_name.get(name.lower(), fallback)
        if name.lower() not in by_name:
            log(f"dk: subcategory {name!r} not in nav payload; using fallback ids {fallback}")
        data = c.get_json(f"{DK_BASE}/leagues/{league}/categories/{cat}/subcategories/{sub}")
        events.update(_dk_events(data, sport))

        mk_by_id = {m["id"]: m for m in data.get("markets", [])
                    if str(m.get("subcategoryId")) == str(sub)}

        if mkt in ONE_SIDED:
            keep = DK_ONE_SIDED_MARKET_NAME[mkt]
            mk_by_id = {k: m for k, m in mk_by_id.items()
                        if m.get("name", "").strip().lower() == keep}
            n = 0
            for sel in data.get("selections", []):
                m = mk_by_id.get(sel.get("marketId"))
                if not m:
                    continue
                price = parse_american(sel.get("displayOdds", {}).get("american"))
                parts = [p for p in sel.get("participants", []) if p.get("type") == "Player"]
                player = parts[0]["name"] if parts else sel.get("label", "")
                role = parts[0].get("venueRole", "") if parts else ""
                if price is None or not player:
                    continue
                ev = events.get(str(m.get("eventId")), {})
                if ev.get("start") and not in_window(ev["start"], days):
                    continue
                if games is not None and ev.get("game") not in games:
                    continue
                team = ev.get("home", "") if role == "HomePlayer" else ev.get("away", "") if role == "AwayPlayer" else ""
                records.append({
                    "player": player.strip(), "player_key": normalize_player(player),
                    "team": team, "game": ev.get("game", ""), "market": mkt, "line": 0.5,
                    "quotes": [{"book": "dk", "over": price, "under": None, "line": 0.5}],
                })
                n += 1
            log(f"dk: {mkt:<16} {n:4d} one-sided (yes) prices  (subcategory {sub})")
            continue

        groups: dict = {}
        for sel in data.get("selections", []):
            m = mk_by_id.get(sel.get("marketId"))
            if not m:
                continue
            pts = sel.get("points")
            side = (sel.get("outcomeType") or sel.get("label") or "").strip().lower()
            if pts is None or side not in ("over", "under"):
                continue
            groups.setdefault((m["id"], float(pts)), {})[side] = sel

        n = 0
        for (mid, line), sides in groups.items():
            if "over" not in sides or "under" not in sides:
                continue
            o = parse_american(sides["over"].get("displayOdds", {}).get("american"))
            u = parse_american(sides["under"].get("displayOdds", {}).get("american"))
            if o is None or u is None:
                log(f"dk: unparseable odds in market {mid}: "
                    f"{sides['over'].get('displayOdds')} / {sides['under'].get('displayOdds')}")
                continue
            m = mk_by_id[mid]
            parts = [p for p in sides["over"].get("participants", []) if p.get("type") == "Player"]
            if parts:
                player = parts[0]["name"]
                role = parts[0].get("venueRole", "")
            else:
                player = re.sub(r"\s+" + re.escape(name) + r"$", "", m.get("name", ""), flags=re.I)
                role = ""
            ev = events.get(str(m.get("eventId")), {})
            if ev.get("start") and not in_window(ev["start"], days):
                continue
            if games is not None and ev.get("game") not in games:
                continue
            team = ev.get("home", "") if role == "HomePlayer" else ev.get("away", "") if role == "AwayPlayer" else ""
            records.append({
                "player": player.strip(), "player_key": normalize_player(player),
                "team": team, "game": ev.get("game", ""), "market": mkt, "line": line,
                "quotes": [{"book": "dk", "over": o, "under": u, "line": line}],
            })
            n += 1
        log(f"dk: {mkt:<16} {n:4d} two-way lines  (subcategory {sub})")
    for sp, ab, nm in sorted(_dk_unknown_abbr):
        log(f"dk: unmapped {sp} team abbreviation {ab!r} ({nm}); add it to DK_TEAM_ALIAS")
    log(f"dk: {c.hits} requests, {c.cached} served from cache")
    return records


# --------------------------------------------------------------------------
# FanDuel
# --------------------------------------------------------------------------

FD_ORIGIN = "https://sportsbook.fanduel.com"
# _ak is the public app key embedded in FanDuel's web client. If requests start
# failing with 401/403, open the site, watch the sbapi XHRs, and update it.
FD_AK = "FhMFpcPWXMeyZxOx"
FD_PAGE = {"nfl": "nfl", "mlb": "mlb", "wnba": "wnba"}
FD_TAB_NFL = {
    "pass_yards": "passing-props", "pass_tds": "passing-props",
    "completions": "passing-props", "pass_attempts": "passing-props",
    "receptions": "receiving-props", "receiving_yards": "receiving-props",
    "rush_yards": "rushing-props", "rush_attempts": "rushing-props",
    "anytime_td": "td-scorer-props",
    "interceptions": "passing-props",
    # field_goals_made: FD had no player FG line as of 2026-09-16 (its "kicking-props"
    # tab serves 4th-quarter markets), so it is DK-only until a type name is seen.
}
FD_TAB_MLB = {"strikeouts": "pitcher-props", "pitching_outs": "pitcher-props"}
# hits_allowed: FD posts no pitcher hits-allowed market (checked across tabs
# 2026-09-23), so it is DK-only, like NFL field goals made.
FD_TAB_WNBA = {
    "points": "player-points", "rebounds": "player-rebounds",
    "assists": "player-assists", "threes": "player-threes",
    "pts_ast": "player-combos", "pts_reb": "player-combos",
    "reb_ast": "player-combos", "pra": "player-combos",
}
FD_TABS = {"nfl": FD_TAB_NFL, "mlb": FD_TAB_MLB, "wnba": FD_TAB_WNBA}
FD_TAB = FD_TAB_NFL          # back-compat

# Per-sport marketType -> our market. NFL uses PLAYER_X_<STAT>_<TIER>; MLB and
# WNBA use a per-player slot letter, PITCHER_<L>_... / PLAYER_<L>_..._WNBA.
# Discovered live 2026-09-23; anything unmatched is reported, never guessed.
FD_TYPE_MLB = {
    "TOTAL_STRIKEOUTS": "strikeouts",
    "OUTS_RECORDED_SB": "pitching_outs",
}
# Markets whose FD runners carry the line in the runner name ("Name Over 15.5")
# with handicap 0, instead of the usual MOVING_HANDICAP shape.
FD_LINE_IN_RUNNER = {"pitching_outs"}
_FD_NAME_LINE = re.compile(r"(Over|Under)\s+([0-9]+(?:\.[0-9]+)?)\s*$", re.I)
FD_TYPE_WNBA = {
    "TOTAL_POINTS": "points", "TOTAL_REBOUNDS": "rebounds",
    "TOTAL_ASSISTS": "assists", "TOTAL_THREES": "threes",
    "TOTAL_MADE_3_POINT_FIELD_GOALS": "threes",
    "TOTAL_THREE_POINTERS": "threes", "TOTAL_MADE_THREES": "threes",
    "TOTAL_POINTS_+_ASSISTS": "pts_ast", "TOTAL_POINTS_+_REBOUNDS": "pts_reb",
    "TOTAL_REBOUNDS_+_ASSISTS": "reb_ast",
    "TOTAL_POINTS_+_REBOUNDS_+_ASSISTS": "pra",
}
_FD_SLOT_MLB = re.compile(r"^PITCHER_[A-Z]_(?P<stat>[A-Z0-9_+]+)$")
_FD_SLOT_WNBA = re.compile(r"^PLAYER_[A-Z]_(?P<stat>[A-Z0-9_+]+)_WNBA$")
# Known player-shaped types we deliberately skip: one-sided "N+" ladders and
# stats outside the vocabulary. Listing them keeps the unmapped report useful.
FD_IGNORE_STAT = {"STRIKEOUTS", "HITS_ALLOWED", "WALKS_ALLOWED",
                  "EARNED_RUNS_ALLOWED", "TOTAL_STEALS", "TOTAL_BLOCKS",
                  "TOTAL_TURNOVERS", "TOTAL_STEALS_+_BLOCKS"}
_dk_unknown_abbr: set = set()


def _fd_market_from_type(sport: str, mt: str):
    """(market | None, is_candidate). is_candidate marks player-shaped types."""
    s = sport.lower()
    if s == "mlb":
        g = _FD_SLOT_MLB.match(mt)
        if not g:
            return None, False
        stat = g.group("stat")
        return FD_TYPE_MLB.get(stat), stat not in FD_IGNORE_STAT
    if s == "wnba":
        g = _FD_SLOT_WNBA.match(mt)
        if not g:
            return None, False
        stat = g.group("stat")
        if "QUARTER" in stat or "HALF" in stat:      # period props, not full game
            return None, False
        return FD_TYPE_WNBA.get(stat), stat not in FD_IGNORE_STAT
    g = _FD_TYPE.match(mt)
    if not g:
        return None, mt.startswith("PLAYER_X_")
    return FD_STAT.get(g.group("stat")), True


FD_ONE_SIDED_TYPE = {"ANY_TIME_TOUCHDOWN_SCORER": "anytime_td"}
# PLAYER_X_<STAT>_<TIER>  ->  shared vocabulary. Anything else that matches the
# PLAYER_X_ pattern is reported as unmapped so new stat names are noticed.
FD_STAT = {
    "RECEPTIONS": "receptions", "RECEIVING_YARDS": "receiving_yards",
    "PASSING_YARDS": "pass_yards", "PASSING_TOUCHDOWNS": "pass_tds",
    "RUSHING_YARDS": "rush_yards",
    "RUSHING_ATTEMPTS": "rush_attempts", "RUSH_ATTEMPTS": "rush_attempts",
    "PASSING_ATTEMPTS": "pass_attempts", "PASS_ATTEMPTS": "pass_attempts",
    "COMPLETIONS": "completions", "PASS_COMPLETIONS": "completions",
    "PASSING_COMPLETIONS": "completions",
    "INTERCEPTIONS": "interceptions", "INTERCEPTIONS_THROWN": "interceptions",
    "FIELD_GOALS_MADE": "field_goals_made", "FIELD_GOALS": "field_goals_made",
}
_FD_TYPE = re.compile(r"^PLAYER_X_(?P<stat>[A-Z_]+?)_(?P<tier>HIGH|MEDIUM|LOW)$")


def _fd_game(name: str, sport: str = "nfl") -> tuple:
    """
    'Detroit Lions @ Buffalo Bills' -> ('DET @ BUF', 'DET', 'BUF')
    MLB event names carry the probable starter, which is stripped:
    'Toronto Blue Jays (M Scherzer) @ Baltimore Orioles (C Bassitt)'.
    """
    name = re.sub(r"\s*\([^)]*\)", "", name).strip()
    if " @ " in name:
        a, h = name.split(" @ ", 1)
    elif " v " in name:
        h, a = name.split(" v ", 1)
    else:
        return name, "", ""
    tm = TEAMS.get(sport.lower(), NFL_TEAMS)
    aa, hh = tm.get(a.strip(), a.strip()), tm.get(h.strip(), h.strip())
    return f"{aa} @ {hh}", aa, hh


def _fd_team(runner: dict, sport: str = "nfl") -> str:
    logo = runner.get("logo") or runner.get("secondaryLogo") or ""
    s = sport.lower()
    m = re.search(rf"/team/{s}/([a-z0-9_]+?)(?:_jersey)?\.png", logo)
    return TEAM_SLUG.get(s, {}).get(m.group(1), "") if m else ""


def fetch_fd(sport: str, markets=MARKETS, days: float = 7, max_age_min: float = 30,
             refresh: bool = False, state: str = "il", games=None) -> list:
    """
    Return one record per (player, market, line) with a single 'fd' quote.
    FD is one request per game per tab, so `games` (a set of 'AWAY @ HOME'
    strings) limits the calls to those games only.
    """
    page = FD_PAGE.get(sport.lower())
    if not page:
        raise NotImplementedError(f"FD: no page id for sport {sport!r}; add it to FD_PAGE")
    base = f"https://sbapi.{state}.sportsbook.fanduel.com/api"
    c = Client("fd", FD_ORIGIN, max_age_min, refresh)

    nav = c.get_json(f"{base}/content-managed-page?page=CUSTOM&customPageId={page}"
                     f"&pbHorizontal=false&_ak={FD_AK}&timezone=America%2FChicago")
    wanted_games = []
    for e in nav.get("attachments", {}).get("events", {}).values():
        nm = e.get("name", "")
        if " @ " not in nm and " v " not in nm:
            continue
        try:
            start = parse_iso(e["openDate"])
        except Exception:
            continue
        if not in_window(start, days):
            continue
        if games is not None and _fd_game(nm, sport)[0] not in games:
            continue
        wanted_games.append((start, e["eventId"], nm))
    wanted_games.sort()
    log(f"fd: {len(wanted_games)} games in the next {days:g} days"
        + (f" (restricted to {sorted(games)})" if games is not None else ""))

    tab_map = FD_TABS.get(sport.lower(), {})
    tabs = sorted({tab_map[m] for m in markets if m in tab_map})
    wanted = set(markets)
    records, unmapped, counts = [], set(), {}
    for start, eid, nm in wanted_games:
        game, away, home = _fd_game(nm, sport)
        for tab in tabs:
            data = c.get_json(f"{base}/event-page?_ak={FD_AK}&eventId={eid}&tab={tab}"
                              f"&useCombinedTouchdownsVirtualMarket=true&usePulse=true&useQuickBets=true")
            for m in data.get("attachments", {}).get("markets", {}).values():
                mt = m.get("marketType", "")
                if mt in FD_ONE_SIDED_TYPE:
                    mkt = FD_ONE_SIDED_TYPE[mt]
                    if mkt not in wanted or m.get("marketStatus") not in (None, "OPEN"):
                        continue
                    for r in m.get("runners", []):
                        if r.get("runnerStatus") not in (None, "ACTIVE"):
                            continue
                        try:
                            price = int(r["winRunnerOdds"]["americanDisplayOdds"]["americanOdds"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        player = r.get("runnerName", "").strip()
                        if not player:
                            continue
                        records.append({
                            "player": player, "player_key": normalize_player(player),
                            "team": _fd_team(r, sport), "game": game, "market": mkt, "line": 0.5,
                            "quotes": [{"book": "fd", "over": price, "under": None, "line": 0.5}],
                        })
                        counts[mkt] = counts.get(mkt, 0) + 1
                    continue
                if "_ALT_" in mt:
                    continue
                mkt, candidate = _fd_market_from_type(sport, mt)
                if mkt is None:
                    if candidate:
                        unmapped.add(mt)
                    continue
                if mkt not in wanted:
                    continue
                if m.get("marketStatus") not in (None, "OPEN"):
                    continue
                groups: dict = {}
                line_in_name = mkt in FD_LINE_IN_RUNNER
                for r in m.get("runners", []):
                    if r.get("runnerStatus") not in (None, "ACTIVE"):
                        continue
                    rn = r.get("runnerName", "")
                    if line_in_name:
                        # "Matthew Liberatore Over 15.5": handicap is 0 and the
                        # line rides in the runner name instead.
                        g2 = _FD_NAME_LINE.search(rn)
                        if not g2:
                            continue
                        side, hc = g2.group(1).lower(), float(g2.group(2))
                    else:
                        side = ((r.get("result") or {}).get("type") or "").lower()
                        if side not in ("over", "under"):
                            low = rn.lower()
                            side = ("over" if low.endswith(" over")
                                    else "under" if low.endswith(" under") else "")
                        hc = r.get("handicap")
                    if not side or hc is None:
                        continue
                    groups.setdefault(float(hc), {})[side] = r
                for line, sides in groups.items():
                    if "over" not in sides or "under" not in sides:
                        continue
                    try:
                        o = int(sides["over"]["winRunnerOdds"]["americanDisplayOdds"]["americanOdds"])
                        u = int(sides["under"]["winRunnerOdds"]["americanDisplayOdds"]["americanOdds"])
                    except (KeyError, TypeError, ValueError):
                        log(f"fd: unparseable odds in {m.get('marketName')}")
                        continue
                    if line_in_name:
                        # marketName here is "Matthew Liberatore Outs Recorded",
                        # with no " - " separator, so the runner name is the
                        # only reliable source: "Matthew Liberatore Over 15.5".
                        player = _FD_NAME_LINE.sub("", sides["over"].get("runnerName", "")).strip()
                    else:
                        player = m.get("marketName", "").split(" - ")[0].strip()
                    if not player:
                        player = re.sub(r"\s+(Over|Under)$", "", sides["over"].get("runnerName", ""))
                    records.append({
                        "player": player, "player_key": normalize_player(player),
                        "team": _fd_team(sides["over"], sport), "game": game, "market": mkt,
                        "line": line,
                        "quotes": [{"book": "fd", "over": o, "under": u, "line": line}],
                    })
                    counts[mkt] = counts.get(mkt, 0) + 1
    for mkt in markets:
        log(f"fd: {mkt:<16} {counts.get(mkt, 0):4d} " + ("one-sided (yes) prices" if mkt in ONE_SIDED else "two-way lines"))
    if unmapped:
        log("fd: PLAYER_X market types seen but not mapped (add to FD_STAT if wanted):")
        for t in sorted(unmapped):
            log(f"      {t}")
    log(f"fd: {c.hits} requests, {c.cached} served from cache")
    return records


# --------------------------------------------------------------------------
# The Odds API fallback
# --------------------------------------------------------------------------

def fetch_odds_api(sport: str, markets=MARKETS, api_key: str = "", regions: str = "us",
                   days: float = 7, max_age_min: float = 30, refresh: bool = False,
                   books=ODDS_API_DEFAULT_BOOKS, only=None, budget=None,
                   games=None) -> list:
    """
    Same output shape, from https://the-odds-api.com.

    Metered: one credit per (event x market x region) that actually returns
    data. Unposted markets are free, and the disk cache means a rerun inside
    `max_age_min` costs nothing. Two levers keep a run cheap:

      only    {(game, market)} -- ask only for these pairs, one request per
              game carrying just the markets still wanted there. This is the
              gap-fill path: DK and FD are free, so spend credits only where
              they left a single book.
      budget  stop once this many credits have been billed this run, rather
              than silently eating a monthly allowance.

    `books` filters to short codes (see ODDS_API_BOOK). dk/fd are excluded by
    default because the direct scrapers already have them, fresher.
    """
    if not api_key:
        raise RuntimeError("The Odds API needs --odds-api-key or ODDS_API_KEY")
    sport = sport.lower()
    sport_key = ODDS_API_SPORT_KEY.get(sport, sport)
    teams = TEAMS.get(sport, NFL_TEAMS)
    inv = {v: k for k, v in ODDS_API_MARKET.items()}
    wanted = [m for m in markets if m in inv]
    if not wanted:
        log(f"oddsapi: none of {list(markets)} are carried by the API")
        return []

    c = Client("oddsapi", "https://the-odds-api.com", max_age_min, refresh, delay=(0.2, 0.5))
    base = "https://api.the-odds-api.com/v4/sports"
    events = c.get_json(f"{base}/{sport_key}/events?apiKey={api_key}")   # free

    records, skipped_budget = [], 0
    for ev in events:
        try:
            start = parse_iso(ev["commence_time"])
        except Exception:
            start = None
        if start and not in_window(start, days):
            continue
        away = teams.get(ev.get("away_team", ""), ev.get("away_team", ""))
        home = teams.get(ev.get("home_team", ""), ev.get("home_team", ""))
        game = f"{away} @ {home}"
        if games and game not in games:
            continue

        # Narrow the market list per game when gap-filling.
        if only is not None:
            need = [m for m in wanted if (game, m) in only]
            if not need:
                continue
        else:
            need = wanted

        if budget is not None and c.credits >= budget:
            skipped_budget += 1
            continue

        api_markets = ",".join(inv[m] for m in need)
        url = (f"{base}/{sport_key}/events/{ev['id']}/odds?apiKey={api_key}&regions={regions}"
               f"&markets={api_markets}&oddsFormat=american")
        try:
            data = c.get_json(url)
        except Exception as e:
            log(f"oddsapi: skip event {ev.get('id')}: {e}")
            continue

        for bm in data.get("bookmakers", []):
            book = ODDS_API_BOOK.get(bm.get("key"), bm.get("key"))
            if books and book not in books:
                continue
            for mk in bm.get("markets", []):
                mkt = ODDS_API_MARKET.get(mk.get("key"))
                if not mkt or mkt not in need:
                    continue

                if mkt in ONE_SIDED:
                    # Yes/No on the player, no line. Keep the "yes" only; the
                    # "no" side is never used to infer the other direction.
                    for oc in mk.get("outcomes", []):
                        nm = oc.get("description") or ""
                        if not nm or (oc.get("name") or "").lower() != "yes":
                            continue
                        try:
                            price = int(oc.get("price"))
                        except (TypeError, ValueError):
                            continue
                        records.append({
                            "player": nm, "player_key": normalize_player(nm), "team": "",
                            "game": game, "market": mkt, "line": 0.5,
                            "quotes": [{"book": book, "over": price,
                                        "under": None, "line": 0.5}],
                        })
                    continue

                sides: dict = {}
                for oc in mk.get("outcomes", []):
                    nm = oc.get("description") or ""
                    pt = oc.get("point")
                    side = (oc.get("name") or "").lower()
                    if pt is None or side not in ("over", "under") or not nm:
                        continue
                    sides.setdefault((nm, float(pt)), {})[side] = oc.get("price")
                for (nm, pt), s in sides.items():
                    if "over" not in s or "under" not in s:
                        continue
                    try:
                        o, u = int(s["over"]), int(s["under"])
                    except (TypeError, ValueError):
                        continue
                    records.append({
                        "player": nm, "player_key": normalize_player(nm), "team": "",
                        "game": game, "market": mkt, "line": pt,
                        "quotes": [{"book": book, "over": o, "under": u, "line": pt}],
                    })

    rem = "?" if c.remaining is None else c.remaining
    log(f"oddsapi: {len(records)} quotes from {c.hits} requests ({c.cached} cached), "
        f"{c.credits} credits spent, {rem} left this month")
    if skipped_budget:
        log(f"oddsapi: BUDGET {budget} reached, {skipped_budget} game(s) not queried")
    return records


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------

def merge_records(*record_lists) -> list:
    """
    Merge on (player_key, market, line). The line is part of the key, so
    different lines can never collapse into one record. If the same book shows
    up twice for one key, the first quote wins and the duplicate is reported.
    """
    merged: dict = {}
    dupes = 0
    conflicts = []
    for recs in record_lists:
        for r in recs:
            key = (r["player_key"], r["market"], round(float(r["line"]), 2))
            if key not in merged:
                merged[key] = {"player": r["player"], "player_key": r["player_key"],
                               "team": r.get("team", ""), "game": r.get("game", ""),
                               "market": r["market"], "line": float(r["line"]),
                               "quotes": []}
            m = merged[key]
            for q in r["quotes"]:
                if abs(float(q.get("line", r["line"])) - m["line"]) > 1e-9:
                    raise AssertionError(f"line mismatch inside merge for {key}: {q}")
                if any(x["book"] == q["book"] for x in m["quotes"]):
                    dupes += 1
                    continue
                m["quotes"].append(q)
            for f in ("team", "game"):
                if not m[f] and r.get(f):
                    m[f] = r[f]
                elif m[f] and r.get(f) and m[f] != r[f]:
                    conflicts.append((key, f, m[f], r[f]))
    if dupes:
        log(f"merge: {dupes} duplicate same-book quotes ignored (first seen kept)")
    for key, f, a, b in conflicts[:20]:
        log(f"merge: {f} conflict for {key}: {a!r} vs {b!r} (kept {a!r})")
    out = list(merged.values())
    for m in out:
        names = DISPLAY_NAMES.get(m["player_key"], [])
        if len(names) > 1:
            m["aliases"] = names
    out.sort(key=lambda m: (m["game"], m["market"], m["player_key"], m["line"]))

    # Near-miss report: single-book records in the same game + market that
    # share a last name but not a key, each from a different book. Almost
    # always a nickname (Joshua/Josh). Add the pair to PLAYER_ALIASES.
    single: dict = {}
    for m in out:
        if len(m["quotes"]) == 1 and m["player_key"]:
            k = (m["game"], m["market"], m["player_key"].split()[-1])
            single.setdefault(k, {}).setdefault(m["quotes"][0]["book"], set()).add(m["player_key"])
    for (game, mkt, _), by_book in sorted(single.items()):
        keys = set().union(*by_book.values())
        if len(by_book) >= 2 and len(keys) >= 2:
            log(f"merge: possible same player under different names in {game} {mkt}: "
                f"{sorted(keys)}  -> add to PLAYER_ALIASES if so")
    return out


def summarize(records: list) -> str:
    by_books: dict = {}
    by_mkt: dict = {}
    for r in records:
        n = len(r["quotes"])
        by_books[n] = by_books.get(n, 0) + 1
        by_mkt[r["market"]] = by_mkt.get(r["market"], 0) + 1
    lines = [f"{len(records)} records"]
    for n in sorted(by_books):
        lines.append(f"  {by_books[n]:4d} with {n} book(s)")
    for m in MARKETS:
        if m in by_mkt:
            lines.append(f"  {by_mkt[m]:4d} {m}")
    return "\n".join(lines)


# --------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--books", default="dk,fd",
                    help="comma list; dk,fd are scraped directly. With --odds-api: "
                         + ",".join(sorted(set(ODDS_API_BOOK.values()))))
    ap.add_argument("--markets", default="", help="default: every market for --sport")
    ap.add_argument("--days", type=float, default=7, help="only games starting within N days")
    ap.add_argument("--max-age", type=float, default=30, help="reuse cached responses younger than N minutes")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    ap.add_argument("--fd-state", default="il", help="FanDuel sbapi state subdomain (il, nj, pa, ...)")
    ap.add_argument("--odds-api", action="store_true", help="use The Odds API instead of the book scrapers")
    ap.add_argument("--odds-api-key", default=os.environ.get("ODDS_API_KEY", "") or read_env_key())
    ap.add_argument("--odds-api-budget", type=int, default=None,
                    help="stop after this many credits are billed this run")
    ap.add_argument("--out", default="props.json")
    a = ap.parse_args()

    if a.sport.lower() not in SPORT_MARKETS:
        ap.error(f"unknown sport {a.sport!r}; choose from {list(SPORT_MARKETS)}")
    allowed = markets_for(a.sport)
    markets = [m.strip() for m in a.markets.split(",") if m.strip()] or list(allowed)
    bad = [m for m in markets if m not in allowed]
    if bad:
        ap.error(f"unknown {a.sport} markets {bad}; choose from {list(allowed)}")
    books = [b.strip() for b in a.books.split(",") if b.strip()]

    lists = []
    try:
        if a.odds_api:
            lists.append(fetch_odds_api(a.sport, markets, a.odds_api_key, days=a.days,
                                        max_age_min=a.max_age, refresh=a.refresh,
                                        books=books, budget=a.odds_api_budget))
        else:
            if "dk" in books:
                lists.append(fetch_dk(a.sport, markets, a.days, a.max_age, a.refresh))
            if "fd" in books:
                lists.append(fetch_fd(a.sport, markets, a.days, a.max_age, a.refresh, a.fd_state))
    except BotBlocked as e:
        log(f"\nBLOCKED: {e}\nNot retrying. Use --odds-api as the fallback.")
        sys.exit(2)
    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else ""
        log(f"\nHTTP error: {e}\n{body}")
        sys.exit(3)
    except requests.RequestException as e:
        log(f"\nnetwork error: {e}")
        sys.exit(3)

    records = merge_records(*lists)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=1, ensure_ascii=False)
    print(summarize(records))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
