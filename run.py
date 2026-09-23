#!/usr/bin/env python3
"""
run.py -- one command, availability first.

    python run.py --week 2 --video "C:/path/Recording.mp4"
    python run.py --week 2                      # reuse the week folder's reads / available.txt
    python run.py --week 2 --available my.txt   # skip the video stage

Stages (each prints a summary and the run stops if a stage returns nothing):

  1. video      recording -> frames/ -> reads.jsonl (Claude reads the frames)
                -> available.txt            [video.py]
  2. need       derive the (market, game) pairs the app actually offers
  3. fetch      DK: only those markets (league-wide, one call each)
                FD: only those games x only the tabs those markets live on
  4. match      app prop <-> book record on normalized player AND EXACT line.
                No nearby-line substitution, ever. Misses are listed with a reason.
  5. wheel      devig, rank, availability filter, structure, tickets, P(zero)
                [wheel.py functions, unmodified]

Everything lands in seasons/<season>/week-NN/:
  frames/ frames.json reads.jsonl available.txt props.json matched.json
  unmatched.txt ranked.csv board.txt summary.json
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fetchers                                              # noqa: E402
import video                                                 # noqa: E402
from sanity import check as sanity_check                     # noqa: E402
from wheel import (Promo, Leg, Quote, auto_side, choose_structure,  # noqa: E402
                   evaluate, DEVIG_METHODS, american_to_implied, opportunity_of)


# Yardage lines are set by moving the number, not the price (FanDuel is -114
# both ways on every one), so they devig to ~50% and carry no information.
# The count markets are where the price carries the skew. NFL only; MLB and
# WNBA have no yardage equivalent.
YARDAGE = {"pass_yards", "rush_yards", "receiving_yards"}
# Priced on one side only at the books ("yes" = over 0.5). Rule: the quoted
# side can be used, and only if the app offers that same side. The other side
# is never inferred from it.
ONE_SIDED = fetchers.ONE_SIDED
# In the app's vocabulary but not priced by DK or FD; reported, not fetched.
UNPRICED = {"targets": "targets are not priced at DK or FD"}
# Suhas's preference order per sport; shown as a column, the board is still
# sorted by true probability. MLB and WNBA orders are a starting guess.
PREF_BY_SPORT = {
    "nfl": ["receptions", "pass_tds", "targets", "anytime_td", "pass_attempts",
            "rush_attempts", "interceptions", "field_goals_made", "completions"],
    "mlb": ["strikeouts", "pitching_outs", "hits_allowed"],
    "wnba": ["points", "rebounds", "assists", "pts_reb", "pts_ast",
             "reb_ast", "pra", "threes"],
}
PREF = PREF_BY_SPORT["nfl"]      # rebound to the running sport in main()
BOARD_ROWS = 20


def stage(n, title):
    print(f"\n{'=' * 70}\nSTAGE {n}: {title}\n{'=' * 70}")


def stop(msg):
    print(f"\nSTOP: {msg}")
    sys.exit(1)


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------

def load_available(path: str) -> list:
    """Player | market | line | sides | game  ->  list of dicts."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) < 3:
                print(f"  available.txt: skipping malformed line {ln!r}")
                continue
            market, cleaned = video.normalize_market(parts[1])
            if market is None:
                print(f"  available.txt: unknown market {parts[1]!r} on {parts[0]!r}; skipped")
                continue
            sides = parts[3].lower().replace(" ", "") if len(parts) > 3 and parts[3] else "over,under"
            sides = {s for s in sides.split(",") if s in ("over", "under")} or {"over", "under"}
            game = parts[4] if len(parts) > 4 else ""
            out.append({"player": parts[0], "key": fetchers.normalize_player(parts[0]),
                        "market": market, "line": float(parts[2]), "sides": sides, "game": game})
    return out


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def match(avail: list, records: list):
    """-> (matched records with app display names, unmatched [(prop, reason)])"""
    by_key = {(r["player_key"], r["market"], round(r["line"], 2)): r for r in records}
    by_pm: dict = {}
    players = set()
    for r in records:
        by_pm.setdefault((r["player_key"], r["market"]), []).append(r)
        players.add(r["player_key"])

    matched, unmatched = [], []
    for a in avail:
        if a["market"] in UNPRICED:
            unmatched.append((a, UNPRICED[a["market"]]))
            continue
        k = (a["key"], a["market"], round(a["line"], 2))
        r = by_key.get(k)
        if r is not None:
            m = dict(r)
            m["player"] = a["player"]            # app spelling, so wheel's filter matches
            m["app_sides"] = sorted(a["sides"])
            matched.append(m)
            continue
        alts = by_pm.get((a["key"], a["market"]))
        if alts:
            seen = "; ".join(f"{q['book']} {x['line']}" for x in alts for q in x["quotes"])
            unmatched.append((a, f"line mismatch: app {a['line']}, books {seen}. "
                                 f"No two-way alternate line exists at either book, so no estimate."))
        elif a["key"] in players:
            mk = sorted({r["market"] for r in records if r["player_key"] == a["key"]})
            unmatched.append((a, f"player found but not for {a['market']} (books have {mk})"))
        else:
            unmatched.append((a, "player not found at DK or FD for the fetched games/markets"))
    return matched, unmatched


def load_picks(path: str) -> list:
    """
    Hand-picked legs, one per line:  Player | market | line
    The line is optional; leave it off and the only priced line for that
    player and market is used. Anything else is a hard error, never a guess.
    """
    picks = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split("|")]
            if len(parts) < 2:
                stop(f"picks file: cannot read {ln!r}; want 'Player | market | line'")
            market, cleaned = video.normalize_market(parts[1])
            if market in (None, "skip"):
                stop(f"picks file: unknown market {cleaned!r} on {parts[0]!r}")
            line = float(parts[2]) if len(parts) > 2 and parts[2] else None
            picks.append({"raw": ln, "key": fetchers.normalize_player(parts[0]),
                          "player": parts[0], "market": market, "line": line})
    return picks


def select_picks(legs: list, picks: list):
    """Match each pick to exactly one priced leg. Ambiguity is an error."""
    chosen, problems = [], []
    for p in picks:
        cands = [l for l in legs
                 if fetchers.normalize_player(l.player) == p["key"] and l.market == p["market"]
                 and (p["line"] is None or abs(l.line - p["line"]) < 1e-9)]
        if not cands:
            near = [f"{l.market} {l.line}" for l in legs
                    if fetchers.normalize_player(l.player) == p["key"]]
            problems.append(f"{p['raw']}  -> not on the priced board"
                            + (f"; that player has {near}" if near else ""))
        elif len(cands) > 1:
            problems.append(f"{p['raw']}  -> matches {len(cands)} lines "
                            f"{[l.line for l in cands]}; add the line to the pick")
        else:
            chosen.append(cands[0])
    return chosen, problems


def unmatched_bucket(p, why: str) -> str:
    if p["market"] in UNPRICED:
        return f"{p['market']}: not priced at DK or FD"
    if why.startswith("line mismatch"):
        return "book line differs from the app line (no substitution)"
    if why.startswith("player found but not for"):
        return "market not posted yet by the books for that player"
    if why.startswith("player not found"):
        return "player not on either book's board yet"
    if "app does not offer it" in why or "app only offers" in why:
        return "app does not offer the side the books favour"
    return why


def print_unmatched_summary(unmatched):
    """One line per reason with a count and a few names, not the full list."""
    buckets: dict = {}
    for p, why in unmatched:
        buckets.setdefault(unmatched_bucket(p, why), []).append(f"{p['player']} {p['market']} {p['line']}")
    print(f"unmatched ({len(unmatched)}):")
    for reason, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        eg = "; ".join(items[:3]) + (", ..." if len(items) > 3 else "")
        print(f"   {len(items):3d}  {reason}  e.g. {eg}")


# --------------------------------------------------------------------------
# board
# --------------------------------------------------------------------------

def pref_rank(market: str) -> int:
    return PREF.index(market) + 1 if market in PREF else len(PREF) + 1


def print_board(ranked, tickets, name, res, promo, a):
    """Top BOARD_ROWS legs by true probability, then the tickets wheel.py chose."""
    one_sided = any(l.notes == "1-sided" for l in ranked)
    if a.picks:
        print(f"{len(ranked)} hand-picked legs "
              f"(break-even {promo.breakeven_leg*100:.1f}%, multiplier {promo.multiplier:.2f}x).")
    else:
        print(f"{len(ranked)} legs priced; top {min(BOARD_ROWS, len(ranked))} shown. "
              f"Tickets use legs at or above {a.threshold*100:.0f}% "
              f"(break-even {promo.breakeven_leg*100:.1f}%, multiplier {promo.multiplier:.2f}x).")
    if one_sided:
        print(f"1-sided legs: true_p = implied(yes price) / {a.one_sided_overround:.2f}. "
              f"That divisor is an assumption (--one-sided-overround), not a measurement.")
    print(f"pref = Suhas's market preference rank: " +
          ", ".join(f"{i+1}={m}" for i, m in enumerate(PREF)))
    print(f"\n{'#':<3}{'leg':<44}{'true':>7}{'spr':>6}{'bk':>4}{'opp':>5}{'pref':>6}  flags")
    print("-" * 82)
    for i, l in enumerate(ranked[:BOARD_ROWS], 1):
        flags = []
        if l.notes == "1-sided":
            flags.append("1-sided")
        if l.spread > 0.03:
            flags.append("thin")
        if l.books < 2:
            flags.append("1 book")
        print(f"{i:<3}{l.label()[:43]:<44}{l.true_p*100:>6.1f}%{l.spread*100:>5.1f}"
              f"{l.books:>4}{l.opportunity:>5}{pref_rank(l.market):>6}  {' '.join(flags)}")

    print(f"\nSTRUCTURE: {name}")
    if not tickets:
        print("No slate. Unused entries cost nothing; bad legs cost money.")
        return
    print(f"\nTICKETS ({len(tickets)})")
    for i, t in enumerate(tickets, 1):
        print(f"  {i:>2}. " + "  |  ".join(l.label()[:28] for l in t))
    print(f"\nEV per entry      {res['roi']*100:+.1f}%")
    print(f"P(zero return)    {res['p_zero']*100:.1f}%")
    print(f"P(profit)         {res['p_profit']*100:.1f}%")
    print(f"Kelly (quarter)   {res['kelly_quarter']*100:.1f}% of bankroll  <-- use this")
    print("\noutcome distribution (tickets cashing -> probability)")
    for w, p in res["dist"].items():
        if p > 0.0005:
            print(f"  {w:>2} cash  {p*100:>5.1f}%   return {w*res['multiplier']:>6.1f} on {res['tickets']} staked")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--season", default="2026-nfl")
    ap.add_argument("--sport", help="nfl | mlb | wnba (default: taken from --season)")
    ap.add_argument("--video", help="screen recording of the promo app")
    ap.add_argument("--available", help="use this availability file instead of the video")
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--tile", default="4x2", help="frames per image for reading; 4x2 = 8 seconds per image")
    ap.add_argument("--width", type=int, default=600)
    ap.add_argument("--games", default="", help="comma list like 'DET @ BUF' when the app does not show the game")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--books", default="dk,fd")
    ap.add_argument("--fd-state", default="il")
    ap.add_argument("--max-age", type=float, default=30)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.56)
    ap.add_argument("--max-spread", type=float, default=0.05)
    ap.add_argument("--min-opportunity", type=int, default=0)
    ap.add_argument("--devig", default="multiplicative", choices=list(DEVIG_METHODS))
    ap.add_argument("--entries", type=int, default=10)
    ap.add_argument("--include-yardage", action="store_true",
                    help="also price pass/rush/receiving yards (skipped by default: no edge)")
    ap.add_argument("--picks", help="file of hand-picked legs (Player | market | line); "
                                    "builds tickets from exactly these, ignoring --threshold")
    ap.add_argument("--one-sided-overround", type=float, default=1.08,
                    help="vig assumed baked into a one-sided price: true_p = implied / this. "
                         "An assumption, not a measurement; shown on the board.")
    a = ap.parse_args()

    sport = (a.sport or (a.season.split("-", 1)[1] if "-" in a.season else "nfl")).lower()
    if sport not in fetchers.SPORT_MARKETS:
        stop(f"unknown sport {sport!r}; known: {list(fetchers.SPORT_MARKETS)}")
    global PREF
    PREF = PREF_BY_SPORT[sport]
    wk = os.path.join(HERE, "seasons", a.season, f"week-{a.week:02d}")
    os.makedirs(wk, exist_ok=True)
    print(f"week folder: {wk}")
    summary = {"week": a.week, "season": a.season}

    # ---- stage 1: video -> available.txt ---------------------------------
    stage(1, "availability from the promo app")
    avail_path = os.path.join(wk, "available.txt")
    reads_path = os.path.join(wk, "reads.jsonl")
    if a.available:
        avail_path = a.available
        print(f"using {avail_path}")
    else:
        if a.video:
            video.extract(a.video, wk, a.fps, a.tile, a.width)
        if os.path.exists(reads_path):
            video.compile_dir(wk)
        elif not os.path.exists(avail_path):
            if not a.video and not os.path.isdir(os.path.join(wk, "frames")):
                stop("nothing to read: pass --video REC.mp4 or --available FILE")
            stop(f"frames are in {os.path.join(wk, 'frames')}. Read them and write {reads_path}, "
                 f"then rerun the same command.")
        else:
            print(f"using existing {avail_path}")
    avail = load_available(avail_path)
    summary["props_in_app"] = len(avail)
    print(f"\n{len(avail)} props offered by the app")
    if not a.include_yardage:
        n0 = len(avail)
        avail = [x for x in avail if x["market"] not in YARDAGE]
        print(f"{n0 - len(avail)} yardage props set aside (--include-yardage to price them); "
              f"{len(avail)} count-market props remain")
    if not avail:
        stop("no props read from the app")

    # ---- stage 2: what do we need -----------------------------------------
    stage(2, "what to fetch")
    markets = sorted({x["market"] for x in avail if x["market"] in fetchers.markets_for(sport)})
    app_games = {x["game"] for x in avail if x["game"]}
    if a.games:
        app_games |= {g.strip() for g in a.games.split(",") if g.strip()}
    print(f"markets needed: {markets}")
    print(f"games known from the app: {sorted(app_games) or 'none (will resolve from DK)'}")

    # ---- stage 3: fetch ------------------------------------------------------
    stage(3, "fetch only what is needed")
    books = [b.strip() for b in a.books.split(",") if b.strip()]
    lists = []
    try:
        dk = []
        if "dk" in books:
            dk = fetchers.fetch_dk(sport, markets, a.days, a.max_age, a.refresh,
                                   games=app_games or None)
            lists.append(dk)
        need_keys = {x["key"] for x in avail}
        games = set(app_games)
        for r in dk:
            if r["player_key"] in need_keys and r["game"]:
                games.add(r["game"])
        unresolved = need_keys - {r["player_key"] for r in dk}
        if unresolved and not app_games:
            print(f"  {len(unresolved)} app player(s) not seen at DK; game unknown, FD will not be searched for them: "
                  f"{sorted(unresolved)}")
        print(f"games to fetch at FD: {sorted(games) or 'none'}")
        if "fd" in books and games:
            lists.append(fetchers.fetch_fd(sport, markets, a.days, a.max_age, a.refresh,
                                           a.fd_state, games=games))
    except fetchers.BotBlocked as e:
        stop(f"blocked by a book, not retrying:\n{e}")
    records = fetchers.merge_records(*lists)
    with open(os.path.join(wk, "props.json"), "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=1, ensure_ascii=False)
    print(fetchers.summarize(records))
    summary["book_records"] = len(records)
    if not records:
        stop("books returned nothing for the needed markets and games")

    # ---- stage 4: match ----------------------------------------------------
    stage(4, "match app props to book lines (player + exact line)")
    matched, unmatched = match(avail, records)
    with open(os.path.join(wk, "matched.json"), "w", encoding="utf-8") as fh:
        json.dump(matched, fh, indent=1, ensure_ascii=False)
    two = sum(1 for m in matched if len(m["quotes"]) >= 2)
    print(f"{len(avail)} app props -> {len(matched)} matched ({two} at both books, "
          f"{len(matched) - two} at one), {len(unmatched)} unmatched")
    for m in matched:
        print(f"   OK  {m['player']:<24} {m['market']:<16} {m['line']:>6}  "
              f"{'+'.join(q['book'] for q in m['quotes'])}")
    if unmatched:
        print_unmatched_summary(unmatched)
    summary.update({"matched": len(matched), "matched_two_books": two,
                    "unmatched": [(p["player"], p["market"], p["line"], why) for p, why in unmatched]})
    if not matched:
        stop("nothing matched; see unmatched.txt")
    crit, flags = sanity_check(matched, min_books=2, max_spread=0.03)
    if crit:
        for c in crit:
            print(f"   CRIT {c}")
        stop("sanity check failed on the matched records")
    for cat, msg in flags:
        if cat in ("thin", "vig"):
            print(f"   FLAG {msg}")

    # ---- stage 5: wheel ----------------------------------------------------
    stage(5, "wheel")
    promo = Promo(max_entries=a.entries)
    legs = []
    for m in matched:
        leg = Leg(player=m["player"], team=m.get("team", ""), game=m.get("game", ""),
                  market=m["market"], line=float(m["line"]))
        if m["market"] in ONE_SIDED:
            # side is whatever the book quoted (over / yes); strip an assumed vig
            ps = [american_to_implied(q["over"]) / a.one_sided_overround for q in m["quotes"]]
            leg.side = "over"
            leg.true_p = sum(ps) / len(ps)
            leg.spread = max(ps) - min(ps)
            leg.books = len(ps)
            leg.opportunity = opportunity_of(leg.market)
            leg.notes = "1-sided"
            leg.quotes = [Quote(q["book"], q["over"], 0) for q in m["quotes"]]
        else:
            leg.quotes = [Quote(q["book"], q["over"], q["under"]) for q in m["quotes"]]
            leg = auto_side(leg, a.devig)
        legs.append(leg)

    # availability filter: the side we would take has to be tappable in the app
    tappable = {(x["key"], x["market"], round(x["line"], 2), s) for x in avail for s in x["sides"]}
    alive, dead = [], []
    for l in legs:
        k = (fetchers.normalize_player(l.player), l.market, round(l.line, 2), l.side)
        (alive if k in tappable else dead).append(l)
    for l in dead:
        why = (f"book quotes only the {l.side} side and the app does not offer it"
               if l.notes == "1-sided" else
               f"favoured side is {l.side}, app only offers the other side")
        unmatched.append(({"player": l.player, "market": l.market, "line": l.line, "sides": set()}, why))
        print(f"   --  {l.player:<24} {l.market:<16} {l.line:>6}  {why}")
    legs = alive

    ranked = sorted(legs, key=lambda x: -x.true_p)
    ranked_path = os.path.join(wk, "ranked.csv")
    with open(ranked_path, "w", encoding="utf-8") as fh:
        fh.write("player,game,market,line,side,true_p,spread,books,opportunity,pref,one_sided\n")
        for l in ranked:
            fh.write(f"{l.player},{l.game},{l.market},{l.line},{l.side},"
                     f"{l.true_p:.4f},{l.spread:.4f},{l.books},{l.opportunity},"
                     f"{pref_rank(l.market)},{int(l.notes == '1-sided')}\n")

    if a.picks:
        pool, problems = select_picks(legs, load_picks(a.picks))
        for msg in problems:
            print(f"   PICK  {msg}")
        if problems:
            stop("some picks could not be resolved; fix the picks file and rerun")
        print(f"using {len(pool)} hand-picked legs from {a.picks}")
    else:
        pool = [l for l in legs if l.true_p >= a.threshold and l.spread <= a.max_spread
                and l.opportunity >= a.min_opportunity]
    pool.sort(key=lambda x: -x.true_p)
    name, tickets = choose_structure(pool, promo)
    res = None
    if tickets:
        used = sorted({id(l): l for t in tickets for l in t}.values(), key=lambda x: -x.true_p)
        res = evaluate(tickets, used, promo)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_board(pool if a.picks else ranked, tickets, name, res, promo, a)
    board = buf.getvalue()
    print(board)
    with open(os.path.join(wk, "board.txt"), "w", encoding="utf-8") as fh:
        fh.write(board)

    summary.update({
        "legs_priced": len(legs), "legs_clear": len(pool), "threshold": a.threshold,
        "structure": name, "tickets": [[l.label() for l in t] for t in tickets],
        "p_zero": res["p_zero"] if res else None, "roi": res["roi"] if res else None,
        "kelly_quarter": res["kelly_quarter"] if res else None,
        "board": [{"leg": l.label(), "true_p": round(l.true_p, 4), "spread": round(l.spread, 4),
                   "books": l.books, "opportunity": l.opportunity}
                  for l in sorted(legs, key=lambda x: -x.true_p)],
    })
    with open(os.path.join(wk, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, ensure_ascii=False)

    print(f"\nRUN SUMMARY  week {a.week}")
    print(f"  props read from app      {len(avail)}")
    print(f"  matched a book           {len(matched)}  ({two} at both books)")
    print(f"  unmatched / dead         {len(unmatched)}")
    print(f"  structure                {name}")
    if res:
        print(f"  tickets                  {len(tickets)}")
        print(f"  P(zero return)           {res['p_zero']*100:.1f}%")
    print(f"  files                    {wk}")


if __name__ == "__main__":
    main()
