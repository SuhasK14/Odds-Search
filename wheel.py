#!/usr/bin/env python3
"""
wheel.py -- prop screening and ticket construction for a fixed-multiplier
3-leg parlay promo ("Pick & Spin" style).

The promo pays a fixed multiplier regardless of the price of each leg, so the
only thing that matters is each leg's TRUE probability. Book prices are used
purely to estimate that probability; the app's own price (if it even shows one)
is irrelevant to selection.

Pipeline:
    1. load two-way prices from sportsbooks
    2. devig each market to a true probability
    3. compare across books, flag disagreement
    4. filter / rank
    5. pick a ticket structure based on how many legs clear the bar
    6. emit tickets + exact outcome distribution, EV, and Kelly sizing
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import sys
from dataclasses import dataclass, field, asdict


# --------------------------------------------------------------------------
# promo configuration
# --------------------------------------------------------------------------

@dataclass
class Promo:
    """Wheel terms. VERIFY THESE EVERY WEEK -- they are the whole ballgame."""
    legs_per_ticket: int = 3
    wheel: tuple = ((6.0, 0.95), (20.0, 0.05))   # (multiplier, probability)
    max_entries: int = 10

    @property
    def multiplier(self) -> float:
        return sum(m * p for m, p in self.wheel)

    @property
    def breakeven_leg(self) -> float:
        """True probability each leg needs, assuming independent legs."""
        return self.multiplier ** (-1.0 / self.legs_per_ticket)


# --------------------------------------------------------------------------
# odds conversion and devigging
# --------------------------------------------------------------------------

def american_to_implied(odds: float) -> float:
    odds = float(odds)
    if odds < 0:
        return -odds / (-odds + 100.0)
    return 100.0 / (odds + 100.0)


def decimal_to_implied(dec: float) -> float:
    return 1.0 / float(dec)


def devig_multiplicative(over_imp: float, under_imp: float) -> float:
    """Proportional devig. Simple, but overstates heavy favorites."""
    return over_imp / (over_imp + under_imp)


def devig_power(over_imp: float, under_imp: float, tol=1e-10) -> float:
    """
    Power devig: solve for k such that over^k + under^k == 1.
    Handles lopsided markets better than proportional. Preferred when a leg
    sits far from 50/50.
    """
    lo, hi = 0.01, 10.0
    for _ in range(200):
        k = (lo + hi) / 2.0
        s = over_imp ** k + under_imp ** k
        if abs(s - 1.0) < tol:
            break
        if s > 1.0:
            lo = k
        else:
            hi = k
    return over_imp ** k


DEVIG_METHODS = {
    "multiplicative": devig_multiplicative,
    "power": devig_power,
}


# --------------------------------------------------------------------------
# opportunity count -- the variance lever
# --------------------------------------------------------------------------
# Rough number of independent chances the stat gets in one game. Variance of a
# counting stat relative to its mean scales as ~1/sqrt(n), so a 56% estimate on
# a 40-event market is far more trustworthy than 56% on a 5-event market.

OPPORTUNITY = {
    # soccer
    "passes": 45, "touches": 60, "passes_completed": 45,
    # basketball
    "points": 20, "rebounds": 10, "assists": 8, "pra": 30, "threes": 7,
    "pts_ast": 28, "pts_reb": 30, "reb_ast": 18,
    # tennis
    "games": 22, "aces": 8,
    # football
    "pass_attempts": 32, "completions": 22, "pass_yards": 32,
    "rush_attempts": 15, "rush_yards": 15,
    "receptions": 6, "receiving_yards": 6, "pass_tds": 32,
    # baseball
    "pitching_outs": 18, "strikeouts": 18,
    "hits": 4, "total_bases": 4, "runs": 4, "rbis": 4, "singles": 4,
    # hockey
    "shots": 4, "saves": 28,
}

MIN_OPPORTUNITY_DEFAULT = 6


def opportunity_of(market: str) -> int:
    key = market.lower().replace(" ", "_").replace("+", "_")
    if key in OPPORTUNITY:
        return OPPORTUNITY[key]
    for k, v in OPPORTUNITY.items():
        if k in key:
            return v
    return MIN_OPPORTUNITY_DEFAULT


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

@dataclass
class Quote:
    book: str
    over: float          # american odds
    under: float


@dataclass
class Leg:
    player: str
    team: str = ""
    game: str = ""       # used for correlation; same game -> correlated
    market: str = ""
    line: float = 0.0
    side: str = "over"   # which side we are taking
    quotes: list = field(default_factory=list)

    true_p: float = 0.0
    spread: float = 0.0  # max-min devigged p across books
    books: int = 0
    opportunity: int = 0
    notes: str = ""

    def key(self):
        return (self.player.lower(), self.market.lower(), float(self.line))

    def label(self):
        return f"{self.player} {self.side.upper()} {self.line} {self.market}"


def price_leg(leg: Leg, method="multiplicative") -> Leg:
    """Devig every book quote, take the mean, record cross-book spread."""
    fn = DEVIG_METHODS[method]
    ps = []
    for q in leg.quotes:
        o, u = american_to_implied(q.over), american_to_implied(q.under)
        p_over = fn(o, u)
        ps.append(p_over if leg.side == "over" else 1.0 - p_over)
    if not ps:
        return leg
    leg.true_p = sum(ps) / len(ps)
    leg.spread = max(ps) - min(ps)
    leg.books = len(ps)
    leg.opportunity = opportunity_of(leg.market)
    return leg


def auto_side(leg: Leg, method="multiplicative") -> Leg:
    """Take whichever side is the favorite. Fixed payout means never take a dog."""
    fn = DEVIG_METHODS[method]
    q = leg.quotes[0]
    p_over = fn(american_to_implied(q.over), american_to_implied(q.under))
    leg.side = "over" if p_over >= 0.5 else "under"
    return price_leg(leg, method)


# --------------------------------------------------------------------------
# input adapters
# --------------------------------------------------------------------------

def load_json(path: str) -> list:
    """
    Generic loader. Expects a list of records:
      {"player":..., "game":..., "market":..., "line":...,
       "quotes":[{"book":"dk","over":-156,"under":121}, ...]}
    Write a small shim per source that emits this shape.
    """
    with open(path) as fh:
        raw = json.load(fh)
    legs = []
    for r in raw:
        legs.append(Leg(
            player=r["player"], team=r.get("team", ""), game=r.get("game", ""),
            market=r["market"], line=float(r["line"]),
            quotes=[Quote(q.get("book", "?"), q["over"], q["under"])
                    for q in r["quotes"]],
        ))
    return legs


def load_csv(path: str) -> list:
    """player,game,market,line,book,over,under  (one row per book)"""
    buckets = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            k = (row["player"], row["market"], row["line"])
            if k not in buckets:
                buckets[k] = Leg(player=row["player"], game=row.get("game", ""),
                                 market=row["market"], line=float(row["line"]))
            buckets[k].quotes.append(
                Quote(row.get("book", "?"), float(row["over"]), float(row["under"])))
    return list(buckets.values())


def load_odds_api(sport: str, api_key: str, markets: str, regions="us") -> list:
    """The Odds API. Player props live in the paid tiers."""
    import urllib.request, urllib.parse
    base = "https://api.the-odds-api.com/v4/sports"
    ev_url = f"{base}/{sport}/events?apiKey={api_key}"
    events = json.loads(urllib.request.urlopen(ev_url).read())
    legs = {}
    for ev in events:
        q = urllib.parse.urlencode({
            "apiKey": api_key, "regions": regions,
            "markets": markets, "oddsFormat": "american",
        })
        url = f"{base}/{sport}/events/{ev['id']}/odds?{q}"
        try:
            data = json.loads(urllib.request.urlopen(url).read())
        except Exception as e:
            print(f"  skip {ev.get('id')}: {e}", file=sys.stderr)
            continue
        game = f"{ev.get('away_team','?')} @ {ev.get('home_team','?')}"
        for bm in data.get("bookmakers", []):
            for mk in bm.get("markets", []):
                sides = {}
                for oc in mk.get("outcomes", []):
                    nm = oc.get("description") or oc.get("name", "")
                    pt = oc.get("point")
                    if pt is None:
                        continue
                    sides.setdefault((nm, pt), {})[oc["name"].lower()] = oc["price"]
                for (nm, pt), s in sides.items():
                    if "over" not in s or "under" not in s:
                        continue
                    k = (nm.lower(), mk["key"], float(pt))
                    if k not in legs:
                        legs[k] = Leg(player=nm, game=game,
                                      market=mk["key"], line=float(pt))
                    legs[k].quotes.append(Quote(bm["key"], s["over"], s["under"]))
    return list(legs.values())


def load_availability(path: str) -> set:
    """
    What the promo app actually offers. One entry per line:
        Player Name | market | line | sides
    e.g.  Tee Higgins | receptions | 4.5 | over,under
    Built from the screen recording. Sides matter: if the app only offers the
    side we do not want, the prop is unusable.
    """
    avail = set()
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) < 3:
                continue
            player, market, line = parts[0], parts[1], float(parts[2])
            sides = parts[3].lower() if len(parts) > 3 else "over,under"
            for s in ("over", "under"):
                if s in sides:
                    avail.add((player.lower(), market.lower(), line, s))
    return avail


# --------------------------------------------------------------------------
# ticket construction
# --------------------------------------------------------------------------

def leave_one_out(legs):
    """4 legs -> 4 tickets. Each leg omitted exactly once."""
    return [tuple(l for l in legs if l is not omit) for omit in legs]


def leave_two_out(legs):
    """5 legs -> C(5,3)=10 tickets. Every PAIR is omitted by exactly one
    ticket, so correlation ordering is irrelevant here."""
    return list(itertools.combinations(legs, 3))


def cyclic(legs):
    """5 legs -> 5 tickets, {i,i+1,i+2}. Each ticket omits an ADJACENT pair,
    so correlated legs belong next to each other in the cycle."""
    n = len(legs)
    return [tuple(legs[(i + j) % n] for j in range(3)) for i in range(n)]


def correlation(a: Leg, b: Leg) -> float:
    """Crude structural proxy. Same game is the dominant term: shared game
    script is what took down Tee Higgins in week 1."""
    if a.game and a.game == b.game:
        return 0.9 if a.team and a.team == b.team else 0.6
    return 0.0


def order_cycle(legs):
    """Brute force the 12 distinct cyclic orderings of 5 legs; maximise
    correlation on adjacent edges so one ticket drops each correlated pair."""
    best, best_score = None, -1.0
    first, rest = legs[0], legs[1:]
    for perm in itertools.permutations(rest):
        order = [first] + list(perm)
        score = sum(correlation(order[i], order[(i + 1) % len(order)])
                    for i in range(len(order)))
        if score > best_score:
            best, best_score = order, score
    return best


def choose_structure(legs, promo: Promo):
    n = len(legs)
    if n >= 10 and promo.max_entries >= 10:
        a, b = legs[:5], legs[5:10]
        return "two cycles of 5", cyclic(order_cycle(a)) + cyclic(order_cycle(b))
    if n >= 5 and promo.max_entries >= 10:
        return "leave-two-out on top 5", leave_two_out(legs[:5])
    if n >= 5:
        return "cyclic on top 5", cyclic(order_cycle(legs[:5]))
    if n >= 4:
        return "leave-one-out on top 4", leave_one_out(legs[:4])
    return f"only {n} qualifying legs -- do not force entries", []


# --------------------------------------------------------------------------
# exact evaluation
# --------------------------------------------------------------------------

def evaluate(tickets, legs, promo: Promo, max_f=0.40):
    """
    Enumerate all 2^n leg outcomes exactly. n is small (<=10), so no simulation
    needed. Correlation is ignored here, which makes these numbers slightly
    optimistic on the joint tail.
    """
    n, m = len(legs), promo.multiplier
    idx = {id(l): i for i, l in enumerate(legs)}
    tix = [[idx[id(l)] for l in t] for t in tickets]
    stake = len(tickets)

    dist = {}
    for mask in range(1 << n):
        p = 1.0
        for i, leg in enumerate(legs):
            p *= leg.true_p if (mask >> i) & 1 else (1.0 - leg.true_p)
        if p <= 0.0:
            continue
        wins = sum(1 for t in tix if all((mask >> i) & 1 for i in t))
        dist[wins] = dist.get(wins, 0.0) + p

    ev_units = sum(w * m * p for w, p in dist.items())
    roi = ev_units / stake - 1.0
    p_zero = dist.get(0, 0.0)
    p_profit = sum(p for w, p in dist.items() if w * m > stake)

    # Kelly on fraction f of bankroll spread across the whole slate
    def growth(f):
        g = 0.0
        for w, p in dist.items():
            ret = 1.0 + f * (w * m / stake - 1.0)
            if ret <= 0:
                return -9e9
            g += p * math.log(ret)
        return g

    lo, hi = 0.0, max_f
    for _ in range(200):
        m1, m2 = lo + (hi - lo) / 3, hi - (hi - lo) / 3
        if growth(m1) < growth(m2):
            lo = m1
        else:
            hi = m2
    f_star = max(0.0, (lo + hi) / 2)

    return {
        "tickets": stake, "legs": n, "multiplier": m,
        "roi": roi, "p_zero": p_zero, "p_profit": p_profit,
        "kelly_full": f_star, "kelly_quarter": f_star / 4,
        "growth": growth(f_star), "dist": dict(sorted(dist.items())),
    }


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def report(legs, tickets, name, res, promo):
    print(f"\n{'='*70}\nSTRUCTURE: {name}")
    print(f"multiplier {promo.multiplier:.2f}x  "
          f"break-even/leg {promo.breakeven_leg*100:.1f}%\n")

    print(f"{'#':<3}{'leg':<46}{'true':>7}{'spr':>6}{'bk':>4}{'opp':>5}")
    print("-" * 70)
    for i, l in enumerate(legs, 1):
        flag = " <-- thin" if l.spread > 0.03 else ""
        print(f"{i:<3}{l.label()[:45]:<46}{l.true_p*100:>6.1f}%"
              f"{l.spread*100:>5.1f}{l.books:>4}{l.opportunity:>5}{flag}")

    if not tickets:
        print("\nNo slate. Unused entries cost nothing; bad legs cost money.")
        return

    print(f"\nTICKETS ({len(tickets)})")
    for i, t in enumerate(tickets, 1):
        print(f"  {i:>2}. " + "  |  ".join(l.label()[:28] for l in t))

    print(f"\nEV per entry      {res['roi']*100:+.1f}%")
    print(f"P(zero return)    {res['p_zero']*100:.1f}%")
    print(f"P(profit)         {res['p_profit']*100:.1f}%")
    print(f"Kelly (full)      {res['kelly_full']*100:.1f}% of bankroll")
    print(f"Kelly (quarter)   {res['kelly_quarter']*100:.1f}%  <-- use this")
    print(f"log growth        {res['growth']:.4f}")
    print("\noutcome distribution (tickets cashing -> probability)")
    for w, p in res["dist"].items():
        if p > 0.0005:
            ret = w * res["multiplier"]
            print(f"  {w:>2} cash  {p*100:>5.1f}%   return {ret:>6.1f}"
                  f" on {res['tickets']} staked")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json"), ap.add_argument("--csv")
    ap.add_argument("--odds-api-key"), ap.add_argument("--sport", default="americanfootball_nfl")
    ap.add_argument("--markets", default="player_receptions,player_pass_tds,"
                                         "player_rush_attempts,player_reception_yds")
    ap.add_argument("--available", help="availability file built from the screen recording")
    ap.add_argument("--threshold", type=float, default=0.56)
    ap.add_argument("--min-opportunity", type=int, default=0)
    ap.add_argument("--max-spread", type=float, default=0.05)
    ap.add_argument("--devig", default="multiplicative", choices=list(DEVIG_METHODS))
    ap.add_argument("--entries", type=int, default=10)
    ap.add_argument("--mult", type=float, nargs=2, action="append",
                    metavar=("MULT", "PROB"), help="override wheel, repeatable")
    ap.add_argument("--out", default="ranked.csv")
    a = ap.parse_args()

    promo = Promo(max_entries=a.entries)
    if a.mult:
        promo.wheel = tuple((m, p) for m, p in a.mult)

    if a.json:
        legs = load_json(a.json)
    elif a.csv:
        legs = load_csv(a.csv)
    elif a.odds_api_key:
        legs = load_odds_api(a.sport, a.odds_api_key, a.markets)
    else:
        ap.error("need --json, --csv, or --odds-api-key")

    legs = [auto_side(l, a.devig) for l in legs if l.quotes]

    if a.available:
        av = load_availability(a.available)
        legs = [l for l in legs
                if (l.player.lower(), l.market.lower(), l.line, l.side) in av]
        print(f"{len(legs)} legs survive app availability")

    with open(a.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["player", "game", "market", "line", "side",
                    "true_p", "spread", "books", "opportunity"])
        for l in sorted(legs, key=lambda x: -x.true_p):
            w.writerow([l.player, l.game, l.market, l.line, l.side,
                        f"{l.true_p:.4f}", f"{l.spread:.4f}", l.books, l.opportunity])
    print(f"wrote {a.out} ({len(legs)} legs)")

    pool = [l for l in legs
            if l.true_p >= a.threshold
            and l.spread <= a.max_spread
            and l.opportunity >= a.min_opportunity]
    pool.sort(key=lambda x: -x.true_p)
    print(f"{len(pool)} legs clear {a.threshold*100:.0f}% "
          f"(break-even {promo.breakeven_leg*100:.1f}%)")

    name, tickets = choose_structure(pool, promo)
    res = evaluate(tickets, sorted({id(l): l for t in tickets for l in t}.values(),
                                   key=lambda x: -x.true_p), promo) if tickets else None
    report(pool[:10], tickets, name, res, promo) if tickets else report(pool, [], name, None, promo)


if __name__ == "__main__":
    main()
