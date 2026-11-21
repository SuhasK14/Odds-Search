#!/usr/bin/env python3
"""
QB completions: where does Boom's line differ from the books, and what is that worth?

Boom sets its own line. When it is lower than the book's (Boom 22.5, book 23.5) the
over is easier than the book prices it; when higher, the under is. Completions is an
integer count, so one point of line is about 8-10 points of probability.

  python qb_completions.py --week 11            # value board for one week
  python qb_completions.py --history            # how often and how far Boom differs, all weeks

Method: take the book's devigged P(over) at its line, fit a normal to completions
with that P and a standard deviation, then read off P(over) at Boom's line. The SD is
an ASSUMPTION (default 4.8), not measured. Check: MGM priced Bo Nix over 20.5 at 54.5%
and Geno Smith over 19.5 at 54.5% (week 11), where this model said 55.9% and 56.6% at
SD 4.3 and 54.6% and 55.4% at SD 5.0, so SD 4.8 is about right but slightly generous.
Read-only: uses saved props.json and reads.jsonl, spends no credits.
"""
from __future__ import annotations
import argparse, collections, io, json, os, sys
from statistics import NormalDist, mean

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import fetchers, video                                          # noqa: E402
from wheel import american_to_implied as imp                    # noqa: E402
N = NormalDist()


def p_over_at(boom, book, p_book, sd):
    mu = book - sd * N.inv_cdf(1 - p_book)
    return 1 - N.cdf((boom - mu) / sd)


def load(week):
    d = os.path.join(HERE, "seasons", "2026-nfl", f"week-{week:02d}")
    boom = {}
    for ln in io.open(os.path.join(d, "reads.jsonl"), encoding="utf-8"):
        if ln.startswith("#") or not ln.strip():
            continue
        r = json.loads(ln)
        if video.normalize_market(r["market"])[0] == "completions":
            boom[fetchers.normalize_player(r["player"])] = (r["player"], float(r["line"]))
    book = {}
    try:
        for r in json.load(io.open(os.path.join(d, "props.json"), encoding="utf-8")):
            if r["market"] != "completions":
                continue
            ps = []
            for q in r["quotes"]:
                if q.get("under") is not None:
                    a, b = imp(q["over"]), imp(q["under"]); ps.append(a / (a + b))
            if ps:
                book[r["player_key"]] = (float(r["line"]), sum(ps) / len(ps), len(ps))
    except FileNotFoundError:
        pass
    return boom, book


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", type=int); ap.add_argument("--history", action="store_true")
    ap.add_argument("--sd", type=float, default=4.8, help="assumed SD of QB completions")
    ap.add_argument("--breakeven", type=float, default=0.53, help="0.53 = 6x/20x wheel")
    a = ap.parse_args()
    if a.history:
        gaps = []
        for w in range(2, 30):
            if not os.path.isdir(os.path.join(HERE, "seasons", "2026-nfl", f"week-{w:02d}")):
                continue
            boom, book = load(w)
            g = [boom[k][1] - book[k][0] for k in boom if k in book]
            gaps += g
            print(f"week {w:>2}: {len(g):>2} QBs priced, Boom lower {sum(x<0 for x in g)}, higher {sum(x>0 for x in g)}")
        print(f"\nall: Boom == book {sum(x==0 for x in gaps)/len(gaps):.0%}, lower {sum(x<0 for x in gaps)/len(gaps):.0%}, "
              f"higher {sum(x>0 for x in gaps)/len(gaps):.0%}; mean gap {mean(gaps):+.2f}")
        return
    if not a.week:
        ap.error("give --week N or --history")
    boom, book = load(a.week)
    rows = []
    for k, (name, lb) in boom.items():
        if k not in book:
            continue
        lk, pk, nb = book[k]; po = p_over_at(lb, lk, pk, a.sd)
        side, pv = ("OVER", po) if po >= 0.5 else ("UNDER", 1 - po)
        rows.append((abs(lb - lk), pv - a.breakeven, name, lb, lk, pk, nb, side, pv))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    print(f"week {a.week}, SD {a.sd}, break-even {a.breakeven:.1%}. Lines that differ first.\n")
    print(f"{'QB':<20}{'Boom':>6}{'Book':>6}{'book P(over)':>14}   best side @ Boom   edge   books")
    for _, edge, name, lb, lk, pk, nb, side, pv in rows:
        flag = "  <-- line gap" if lb != lk else ""
        print(f"{name:<20}{lb:>6.1f}{lk:>6.1f}{pk*100:>13.1f}%   {side:<5} {pv*100:5.1f}%   {edge*100:+5.1f}   {nb}{flag}")


if __name__ == "__main__":
    main()
