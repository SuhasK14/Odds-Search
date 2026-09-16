#!/usr/bin/env python3
"""
sanity.py -- pre-bet checks on a props JSON file (the shape wheel.py reads).

    python sanity.py wk3.json [--min-books 2] [--max-spread 0.03]

Checks
  critical (exit 1):
    * malformed record: missing player/market/line, quote without BOTH sides,
      non-integer odds, odds in (-100, 100) exclusive, non-numeric line
    * duplicate (player, market, line) keys
    * devigged over_p + under_p != 1.0 for any quote
    * same book quoted twice in one record
  flagged (reported, exit 0 on their own):
    * fewer than --min-books quotes
    * cross-book devigged probabilities disagree by more than --max-spread
    * a (player, market) whose line at one book differs from the line at the
      other book (usually a parse error, occasionally a real market difference)
    * implied over + under <= 1 (negative vig: almost always a parse error)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wheel import american_to_implied, devig_multiplicative  # noqa: E402
from fetchers import ONE_SIDED  # noqa: E402

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(name: str) -> str:
    s = re.sub(r"[.'\u2019\-]", "", name.strip().lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    t = s.split()
    while len(t) > 1 and t[-1] in _SUFFIXES:
        t.pop()
    return " ".join(t)


def check(records: list, min_books: int = 2, max_spread: float = 0.03):
    """Returns (critical: list[str], flags: list[(category, str)])."""
    crit, flags = [], []
    seen = {}
    lines_by_pm: dict = {}          # (player, market) -> {line: set(books)}

    for i, r in enumerate(records):
        tag = f"#{i} {r.get('player')!r} {r.get('market')} {r.get('line')}"
        try:
            player, market, line = r["player"], r["market"], float(r["line"])
        except (KeyError, TypeError, ValueError) as e:
            crit.append(f"{tag}: malformed record ({e})")
            continue
        if line != line or line <= 0:
            crit.append(f"{tag}: implausible line {line}")
        key = (r.get("player_key") or norm(player), market, round(line, 2))
        if key in seen:
            crit.append(f"{tag}: duplicate key, also record #{seen[key]}")
        seen[key] = i

        quotes = r.get("quotes") or []
        books = [q.get("book") for q in quotes]
        if len(set(books)) != len(books):
            crit.append(f"{tag}: same book quoted more than once {books}")
        ps = []
        for q in quotes:
            if market in ONE_SIDED:
                # one-sided market: exactly one side, and it must be the over/yes
                if q.get("over") is None or q.get("under") is not None:
                    crit.append(f"{tag}: one-sided market with unexpected sides {q}")
                    continue
                try:
                    o = int(q["over"])
                except (TypeError, ValueError):
                    crit.append(f"{tag}: non-integer odds {q}")
                    continue
                if -100 < o < 100:
                    crit.append(f"{tag}: odds outside American range {q}")
                    continue
                ps.append((q["book"], american_to_implied(o)))   # raw implied; run.py strips vig
                continue
            if "over" not in q or "under" not in q or q["over"] is None or q["under"] is None:
                crit.append(f"{tag}: one-sided quote {q}")
                continue
            try:
                o, u = int(q["over"]), int(q["under"])
            except (TypeError, ValueError):
                crit.append(f"{tag}: non-integer odds {q}")
                continue
            if -100 < o < 100 or -100 < u < 100:
                crit.append(f"{tag}: odds outside American range {q}")
                continue
            if "line" in q and abs(float(q["line"]) - line) > 1e-9:
                crit.append(f"{tag}: quote line {q['line']} != record line {line}")
            oi, ui = american_to_implied(o), american_to_implied(u)
            if oi + ui <= 1.0:
                flags.append(("vig", f"{tag}: {q['book']} implied sum {oi+ui:.3f} <= 1 (negative vig)"))
            p_over = devig_multiplicative(oi, ui)
            p_under = devig_multiplicative(ui, oi)
            if abs(p_over + p_under - 1.0) > 1e-9:
                crit.append(f"{tag}: {q['book']} devig sums to {p_over+p_under:.6f}")
            ps.append((q["book"], p_over))
        if len(ps) < min_books:
            flags.append(("books", f"{tag}: only {len(ps)} book(s) {[b for b, _ in ps]}"))
        if len(ps) >= 2:
            spread = max(p for _, p in ps) - min(p for _, p in ps)
            if spread > max_spread:
                detail = ", ".join(f"{b} {p*100:.1f}%" for b, p in ps)
                flags.append(("thin", f"{tag}: cross-book spread {spread*100:.1f} pts ({detail})"))
        pm = lines_by_pm.setdefault((key[0], market), {})
        pm.setdefault(round(line, 2), set()).update(b for b, _ in ps)

    for (player, market), by_line in lines_by_pm.items():
        if len(by_line) < 2:
            continue
        all_books = set().union(*by_line.values())
        if len(all_books) < 2:
            continue
        lone = {ln: bks for ln, bks in by_line.items() if len(bks) == 1}
        if lone:
            desc = "; ".join(f"{ln} only at {','.join(sorted(b))}" for ln, b in sorted(lone.items()))
            flags.append(("lines", f"{player!r} {market}: {desc}"))
    return crit, flags


FLAG_TITLES = {
    "thin":  "thin: cross-book devigged probabilities disagree",
    "lines": "lines differ across books (routine for yardage; check receptions / TDs)",
    "vig":   "negative vig (implied over + under <= 1): almost certainly a parse error",
    "books": "fewer books than --min-books",
}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--min-books", type=int, default=2)
    ap.add_argument("--max-spread", type=float, default=0.03)
    ap.add_argument("--verbose", action="store_true",
                    help="also list every record with fewer than --min-books quotes")
    a = ap.parse_args()

    with open(a.path, encoding="utf-8") as fh:
        records = json.load(fh)
    if not isinstance(records, list):
        print("CRITICAL: top-level JSON is not a list")
        sys.exit(1)
    crit, flags = check(records, a.min_books, a.max_spread)

    n_two = sum(1 for r in records if len(r.get("quotes") or []) >= a.min_books)
    print(f"{a.path}: {len(records)} records, {n_two} with >= {a.min_books} books, "
          f"{len(records) - n_two} with fewer")

    by_cat: dict = {}
    for cat, msg in flags:
        by_cat.setdefault(cat, []).append(msg)
    for cat in ("vig", "thin", "lines", "books"):
        msgs = by_cat.get(cat, [])
        if not msgs:
            continue
        print(f"\n{len(msgs):4d} {FLAG_TITLES[cat]}")
        if cat != "books" or a.verbose:
            for m in msgs:
                print(f"      {m}")
    if crit:
        print(f"\n{len(crit):4d} CRITICAL")
        for c in crit:
            print(f"      {c}")

    print(f"\n{len(flags)} flagged, {len(crit)} critical -> "
          f"{'DO NOT BET' if crit else 'ok to proceed (review flags)'}")
    sys.exit(1 if crit else 0)


if __name__ == "__main__":
    main()
