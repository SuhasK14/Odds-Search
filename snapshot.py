#!/usr/bin/env python3
"""
Price history for player props, independent of the Boom board.

run.py is availability-first: it reads what the app offers and prices only
that. This is the opposite. It fetches everything DK and FanDuel post for a
sport, devigs it, and appends a timestamped row per prop. Nothing about Boom
is needed, so it can run from Wednesday onward, long before the promo board
goes up on Saturday.

Two modes:

  snapshot.py --sport nfl
      fetch and append one snapshot. Free: DK and FD only, never the Odds API.

  snapshot.py --report --sport nfl
      show how each prop's price moved across the snapshots taken so far.
      With --available <file> it is restricted to the props Boom actually
      offers, so on Saturday you can cross-check the board against its history.

Storage is snapshots/<sport>.jsonl, one JSON object per prop per snapshot.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetchers                                              # noqa: E402
from wheel import american_to_implied                        # noqa: E402

SNAP_DIR = os.path.join(HERE, "snapshots")

# Yardage is never bet (lines are moved, not priced), so it is skipped by
# default to keep the FanDuel tab count down. --all-markets brings it back.
YARDAGE = {"pass_yards", "rush_yards", "receiving_yards"}


def devig_over(rec: dict, one_sided_overround: float) -> tuple:
    """Consensus P(over) across the books on this record, plus the spread."""
    ps = []
    for q in rec["quotes"]:
        if q.get("under") is None:
            ps.append(american_to_implied(q["over"]) / one_sided_overround)
        else:
            o = american_to_implied(q["over"])
            u = american_to_implied(q["under"])
            ps.append(o / (o + u))
    if not ps:
        return None, None
    return sum(ps) / len(ps), (max(ps) - min(ps))


def take(sport: str, days: float, state: str, all_markets: bool,
         one_sided_overround: float) -> int:
    markets = list(fetchers.markets_for(sport))
    if not all_markets:
        markets = [m for m in markets if m not in YARDAGE]

    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    fetchers.log(f"snapshot {stamp}  sport={sport}  markets={len(markets)}")

    lists = []
    dk = fetchers.fetch_dk(sport, markets, days, max_age_min=0, refresh=True)
    lists.append(dk)
    games = {r["game"] for r in dk if r.get("game")}
    fetchers.log(f"snapshot: {len(games)} games seen at DK")
    if games:
        lists.append(fetchers.fetch_fd(sport, markets, days, max_age_min=0,
                                       refresh=True, state=state, games=games))
    records = fetchers.merge_records(*lists)

    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, f"{sport}.jsonl")
    n = 0
    with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
        for r in records:
            p, spread = devig_over(r, one_sided_overround)
            if p is None:
                continue
            fh.write(json.dumps({
                "at": stamp, "player": r["player"], "player_key": r["player_key"],
                "game": r.get("game", ""), "market": r["market"], "line": r["line"],
                "p_over": round(p, 4), "spread": round(spread, 4),
                "books": len(r["quotes"]),
                "quotes": [{"book": q["book"], "over": q["over"], "under": q["under"]}
                           for q in r["quotes"]],
            }) + "\n")
            n += 1
    fetchers.log(f"snapshot: wrote {n} props to {path}")
    return n


def load(sport: str) -> list:
    path = os.path.join(SNAP_DIR, f"{sport}.jsonl")
    if not os.path.exists(path):
        sys.exit(f"no snapshots yet at {path}; run without --report first")
    out = []
    for ln in io.open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


def report(sport: str, available: str, since: str, min_move: float, top: int):
    rows = load(sport)
    if since:
        rows = [r for r in rows if r["at"][:10] >= since]
    if not rows:
        sys.exit("no snapshots in that window")

    keep = None
    if available:
        keep = set()
        for ln in io.open(available, encoding="utf-8"):
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) >= 3:
                try:
                    keep.add((fetchers.normalize_player(parts[0]), parts[1],
                              round(float(parts[2]), 2)))
                except ValueError:
                    continue

    series: dict = {}
    for r in rows:
        k = (r["player_key"], r["market"], round(float(r["line"]), 2))
        if keep is not None and k not in keep:
            continue
        series.setdefault(k, {"player": r["player"], "game": r["game"],
                              "market": r["market"], "line": r["line"],
                              "pts": []})["pts"].append((r["at"], r["p_over"], r["books"]))

    stamps = sorted({r["at"] for r in rows})
    print(f"{len(series)} props across {len(stamps)} snapshots "
          f"({stamps[0][:16]} -> {stamps[-1][:16]})"
          + (f", filtered to {os.path.basename(available)}" if available else ""))

    movers = []
    for k, v in series.items():
        v["pts"].sort()
        if len(v["pts"]) < 2:
            continue
        first, last = v["pts"][0][1], v["pts"][-1][1]
        movers.append((abs(last - first), last - first, v))
    movers.sort(key=lambda m: m[0], reverse=True)

    shown = [m for m in movers if m[0] * 100 >= min_move][:top]
    if not shown:
        print(f"\nnothing moved by {min_move:g} points or more yet.")
        return

    print(f"\nbiggest movers in P(over)   (drift = last - first)\n")
    print(f"  {'player':<22}{'market':<16}{'line':>6}{'first':>8}{'last':>8}"
          f"{'drift':>8}   path")
    for _, drift, v in shown:
        path = " ".join(f"{p*100:.0f}" for _, p, _ in v["pts"])
        print(f"  {v['player'][:21]:<22}{v['market']:<16}{v['line']:>6}"
              f"{v['pts'][0][1]*100:>7.1f}%{v['pts'][-1][1]*100:>7.1f}%"
              f"{drift*100:>+7.1f}   {path}")

    print("\n  A big positive drift means the over got bet up between snapshots.")
    print("  Whether that is news or just flow is the part the numbers cannot tell you.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="nfl", help="nfl | mlb | wnba")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--fd-state", default="il")
    ap.add_argument("--all-markets", action="store_true",
                    help="include yardage as well (more FanDuel tabs, slower)")
    ap.add_argument("--one-sided-overround", type=float, default=1.08)
    ap.add_argument("--report", action="store_true", help="show the price history instead")
    ap.add_argument("--available", default="",
                    help="report only props in this available.txt (the Boom board)")
    ap.add_argument("--since", default="", help="report only snapshots from this date, YYYY-MM-DD")
    ap.add_argument("--min-move", type=float, default=1.0,
                    help="report only props that drifted at least this many points")
    ap.add_argument("--top", type=int, default=40)
    a = ap.parse_args()

    if a.sport.lower() not in fetchers.SPORT_MARKETS:
        ap.error(f"unknown sport {a.sport!r}; choose from {list(fetchers.SPORT_MARKETS)}")

    if a.report:
        report(a.sport.lower(), a.available, a.since, a.min_move, a.top)
        return

    try:
        take(a.sport.lower(), a.days, a.fd_state, a.all_markets, a.one_sided_overround)
    except fetchers.BotBlocked as e:
        fetchers.log(f"BLOCKED, not retrying:\n{e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
