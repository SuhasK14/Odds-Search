#!/usr/bin/env python3
"""
video.py -- screen recording of the promo app  ->  available.txt

Two stages, because reading the frames is a vision step and this machine has
no OCR binary and no Claude API key. The reader is Claude in the session:

  1. extract   ffmpeg samples the recording at --fps, drops frames identical to
               the previous one, scales, and tiles --tile consecutive frames
               into one image so fewer images have to be read.
                   python video.py extract REC.mp4 --out seasons/2026-nfl/week-02

  2. (read)    Claude reads frames/*.png and writes reads.jsonl next to them,
               one JSON object per prop per frame:
                 {"frame": "0007.png", "player": "Tee Higgins",
                  "market": "receptions", "line": 4.5, "sides": "over,under",
                  "confidence": "high", "game": "CLE @ CIN", "note": ""}
               sides   = which buttons look tappable ("over", "under", "over,under")
               game    = optional; only if the screen shows it
               confidence "low" = print it rather than trust it

  3. compile   reads.jsonl -> available.txt in the format wheel.py parses:
                   Player Name | market | line | sides | game
               Dedupe is at the RECORD level (player, market, line), never the
               frame level: the same prop seen in ten scrolling frames is one
               record; sides are unioned across sightings. Market names are
               normalized to wheel.py's vocabulary. Anything read with low
               confidence, any unknown market, and any player+market seen at
               two different lines is printed for a human decision.
                   python video.py compile seasons/2026-nfl/week-02

Standard library only. ffmpeg is found via --ffmpeg, $FFMPEG, PATH, or the
copy bundled with BlueStacks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetchers import ALL_MARKETS, normalize_player  # noqa: E402

FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG", ""),
    shutil.which("ffmpeg") or "",
    r"C:\Program Files\BlueStacks_nxt\ffmpeg.exe",
]

# What the promo app (and a human transcribing it) might call each market.
MARKET_SYNONYMS = {
    "receptions": ["receptions", "reception", "rec", "recs", "catches", "total receptions"],
    "pass_tds": ["pass_tds", "pass tds", "passing tds", "pass td", "passing td",
                 "passing touchdowns", "pass touchdowns", "td passes", "touchdown passes"],
    "rush_attempts": ["rush_attempts", "rush attempts", "rushing attempts", "rush att",
                      "carries", "rushes", "attempts (rush)"],
    "pass_yards": ["pass_yards", "pass yards", "passing yards", "pass yds", "passing yds"],
    "receiving_yards": ["receiving_yards", "receiving yards", "rec yards", "rec yds",
                        "receiving yds", "reception yards"],
    "completions": ["completions", "pass completions", "completed passes", "comp"],
    "pass_attempts": ["pass_attempts", "pass attempts", "passing attempts", "pass att",
                      "attempts (pass)"],
    "rush_yards": ["rush_yards", "rush yards", "rushing yards", "rush yds", "rushing yds"],
    # one-sided at the books ("yes" only); matches the app's Over 0.5 rush+rec TDs
    "anytime_td": ["anytime_td", "anytime td", "anytime touchdown", "anytime td scorer",
                   "td scorer", "rushing + receiving tds", "rush + rec tds",
                   "rushing + receiving touchdowns", "receiving + rushing tds"],
    # not priced at DK or FD; captured so run.py reports it rather than losing it
    "targets": ["targets", "target"],
    "interceptions": ["interceptions", "interception", "ints", "interceptions thrown",
                      "int thrown", "ints thrown"],
    "field_goals_made": ["field_goals_made", "field goals made", "fg made", "field goals",
                         "fgs made", "fgm"],
    # ---- MLB ----
    "strikeouts": ["strikeouts", "strikeout", "ks", "pitcher strikeouts",
                   "strikeouts thrown", "total strikeouts", "so"],
    # ---- WNBA ----
    "points": ["points", "pts", "total points"],
    "rebounds": ["rebounds", "reb", "rebs", "total rebounds"],
    "assists": ["assists", "ast", "asts", "total assists"],
    "threes": ["threes", "3pt made", "three pointers made", "made threes",
               "3 pointers made", "threes made", "3s"],
    "pts_ast": ["pts_ast", "pts + ast", "points + assists", "pts+ast", "p+a",
                "points assists"],
    "pts_reb": ["pts_reb", "pts + reb", "points + rebounds", "pts+reb", "p+r",
                "points rebounds"],
    "reb_ast": ["reb_ast", "reb + ast", "rebounds + assists", "reb+ast", "r+a",
                "ast + reb", "assists + rebounds"],
    "pra": ["pra", "pts + reb + ast", "points + rebounds + assists", "pts+reb+ast",
            "p+r+a", "pts reb ast"],
}
_SYN = {s: m for m, ss in MARKET_SYNONYMS.items() for s in ss}

# Props the app offers that no book prices two-way in our vocabulary. They are
# transcribed for the record and counted as skipped, not flagged as unknown.
NON_VOCAB = [
    "fantasy points", "longest reception", "longest rush",
    "longest pass completion", "completion percentage", "passing + rushing",
    "rushing + receiving", "game high",
    "in 1st",            # "Completions in 1st 10 Attempts", "Rec Yds in 1st 2 Receptions", ...
    "first reception", "first rush", "first pass", "first catch", "first carry",
    "kicker points", "kicking points", "extra points made", "punts", "sacks",
    "tackles", "assists",
]
VOCAB = set(ALL_MARKETS) | {"anytime_td", "targets"}


def normalize_market(raw: str):
    """Return (vocab_name | "skip" | None, cleaned)."""
    s = re.sub(r"[^a-z0-9()+ _]", " ", raw.strip().lower())
    s = re.sub(r"\s+", " ", s).strip()
    if s in _SYN:
        return _SYN[s], s
    t = s.replace(" ", "_")
    if t in VOCAB:
        return t, s
    if any(k in s for k in NON_VOCAB):
        return "skip", s
    return None, s


def find_ffmpeg(explicit: str = "") -> str:
    for c in [explicit] + FFMPEG_CANDIDATES:
        if c and os.path.exists(c):
            return c
    sys.exit("ffmpeg not found. Install it (winget install Gyan.FFmpeg) or pass --ffmpeg PATH.")


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------

def extract(video: str, out_dir: str, fps: float = 1.0, tile: str = "4x2",
            width: int = 600, ffmpeg: str = "") -> dict:
    """
    Defaults are tuned for token cost when Claude reads the frames: 1 fps is
    enough for a steady one-screen-per-second scroll, and a 4x2 tile of 600px
    frames (2400x2600) is still fully legible after the viewer downsizes it,
    so one image covers 8 seconds of recording. A 4.5-minute recording becomes
    ~34 images instead of ~270.
    """
    ff = find_ffmpeg(ffmpeg)
    frames = os.path.join(out_dir, "frames")
    if os.path.isdir(frames):
        for f in os.listdir(frames):
            if f.endswith(".png"):
                os.remove(os.path.join(frames, f))
    os.makedirs(frames, exist_ok=True)

    # mpdecimate (drop frames identical to the previous one) is missing from
    # some builds, e.g. the BlueStacks copy; record-level dedupe covers it.
    have = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    vf = [f"fps={fps}"]
    if " mpdecimate " in have:
        vf.append("mpdecimate")
    vf.append(f"scale={width}:-2")
    if tile and tile != "1x1":
        vf.append(f"tile={tile}")
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-i", video,
           "-vf", ",".join(vf), "-vsync", "vfr", os.path.join(frames, "%04d.png")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg failed:\n{r.stderr}")
    n = len([f for f in os.listdir(frames) if f.endswith(".png")])

    # duration, for the manifest
    p = subprocess.run([ff, "-hide_banner", "-i", video], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None

    manifest = {"video": os.path.abspath(video), "duration_s": dur, "fps": fps,
                "tile": tile, "width": width, "images": n, "ffmpeg": ff}
    with open(os.path.join(out_dir, "frames.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    print(f"extract: {dur:.1f}s of video -> {n} images in {frames} "
          f"(fps {fps}, tile {tile}, width {width})" if dur else
          f"extract: {n} images in {frames}")
    if n == 0:
        sys.exit("extract: no frames produced")
    return manifest


# --------------------------------------------------------------------------
# compile
# --------------------------------------------------------------------------

def load_reads(path: str) -> list:
    reads = []
    with open(path, encoding="utf-8") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                reads.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"reads.jsonl line {ln}: bad JSON ({e}); skipped")
    return reads


def compile_reads(reads: list):
    """
    -> (records, problems)
    records: list of dicts {player, market, line, sides, game, frames, confidence}
    problems: list of strings for a human to look at
    """
    problems = []
    skipped: Counter = Counter()
    groups: dict = defaultdict(list)
    for r in reads:
        player = str(r.get("player", "")).strip()
        try:
            line = float(r.get("line"))
        except (TypeError, ValueError):
            problems.append(f"{r.get('frame')}: unreadable line for {player!r}: {r.get('line')!r}")
            continue
        market, cleaned = normalize_market(str(r.get("market", "")))
        if market == "skip":
            skipped[(normalize_player(player), cleaned, line)] += 1
            continue
        if market is None:
            problems.append(f"{r.get('frame')}: unknown market {cleaned!r} for {player!r} {line}")
            continue
        key = normalize_player(player)
        if not key:
            problems.append(f"{r.get('frame')}: empty player name")
            continue
        groups[(key, market, line)].append({**r, "player": player, "line": line, "market": market})

    records = []
    by_pm: dict = defaultdict(set)
    for (key, market, line), rs in groups.items():
        names = Counter(x["player"] for x in rs)
        display = names.most_common(1)[0][0]
        if len(names) > 1:
            problems.append(f"{display}: spelled {len(names)} ways across frames {dict(names)}; using {display!r}")
        sides = set()
        for x in rs:
            for s in str(x.get("sides", "over,under")).lower().replace(" ", "").split(","):
                if s in ("over", "under"):
                    sides.add(s)
        if not sides:
            problems.append(f"{display} {market} {line}: no tappable side recorded; dropped")
            continue
        games = Counter(x.get("game") for x in rs if x.get("game"))
        game = games.most_common(1)[0][0] if games else ""
        conf = "low" if any(str(x.get("confidence", "high")).lower() == "low" for x in rs) else "high"
        notes = [x["note"] for x in rs if x.get("note")]
        if conf == "low":
            problems.append(f"LOW CONFIDENCE: {display} | {market} | {line} | {','.join(sorted(sides))}"
                            f"  frames {[x.get('frame') for x in rs]}  {'; '.join(notes)}")
        records.append({"player": display, "market": market, "line": line,
                        "sides": ",".join(s for s in ("over", "under") if s in sides),
                        "game": game, "frames": sorted({x.get("frame") for x in rs}),
                        "confidence": conf})
        by_pm[(key, market)].add(line)
    for (key, market), lines in by_pm.items():
        if len(lines) > 1:
            problems.append(f"CHECK: {key} {market} seen at {sorted(lines)} -- two real props, or one misread?")
    records.sort(key=lambda x: (x["game"], x["market"], x["player"], x["line"]))
    if skipped:
        kinds = Counter(k[1] for k in skipped)
        problems.append(f"skipped {len(skipped)} app props with no two-way book market: "
                        + ", ".join(f"{k} x{v}" for k, v in kinds.most_common()))
    return records, problems


def write_available(records: list, path: str, source: str = ""):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# promo app availability, compiled by video.py\n")
        if source:
            fh.write(f"# source: {source}\n")
        fh.write("# Player Name | market | line | sides | game\n")
        for r in records:
            fh.write(f"{r['player']} | {r['market']} | {r['line']} | {r['sides']}"
                     f"{' | ' + r['game'] if r['game'] else ''}\n")


def compile_dir(out_dir: str) -> list:
    reads_path = os.path.join(out_dir, "reads.jsonl")
    if not os.path.exists(reads_path):
        sys.exit(f"compile: {reads_path} not found. Read frames/*.png and write it first.")
    reads = load_reads(reads_path)
    records, problems = compile_reads(reads)
    src = ""
    mf = os.path.join(out_dir, "frames.json")
    if os.path.exists(mf):
        with open(mf, encoding="utf-8") as fh:
            src = json.load(fh).get("video", "")
    write_available(records, os.path.join(out_dir, "available.txt"), src)
    print(f"compile: {len(reads)} reads over {len({r.get('frame') for r in reads})} images "
          f"-> {len(records)} props")
    for r in records:
        print(f"   {r['player']:<24} {r['market']:<16} {r['line']:>6}  {r['sides']:<11} {r['game']}")
    if problems:
        print(f"\n{len(problems)} thing(s) to look at:")
        for p in problems:
            print(f"   {p}")
    return records


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("video")
    e.add_argument("--out", required=True)
    e.add_argument("--fps", type=float, default=1.0)
    e.add_argument("--tile", default="4x2", help="ffmpeg tile layout (1x1 = no tiling)")
    e.add_argument("--width", type=int, default=600)
    e.add_argument("--ffmpeg", default="")
    c = sub.add_parser("compile")
    c.add_argument("out")
    a = ap.parse_args()
    if a.cmd == "extract":
        os.makedirs(a.out, exist_ok=True)
        extract(a.video, a.out, a.fps, a.tile, a.width, a.ffmpeg)
    else:
        compile_dir(a.out)


if __name__ == "__main__":
    main()
