# Odds-Search

Prop screening for the Pick & Spin promo: read what the app offers from a screen
recording, price only those props off DraftKings and FanDuel, and let `wheel.py`
rank them and build tickets.

Python 3 + `requests` only. ffmpeg for frame extraction. Read-only against the books.

**On this machine** `python` and `python3` resolve to the Microsoft Store stub and
fail. Use the real interpreter explicitly, and set `PYTHONIOENCODING=utf-8` when
output contains non-ASCII (the console is cp1252):

```bash
"$LOCALAPPDATA/Programs/Python/Python312/python.exe" run.py --week 3
```

ffmpeg is not on PATH either; `video.py` finds the copy bundled with BlueStacks at
`C:\Program Files\BlueStacks_nxt\ffmpeg.exe`. That build has no `mpdecimate`
filter, so duplicate frames are removed at the record level instead.

## Weekly run

```bash
python run.py --week 3 --video "C:/path/to/recording.MP4"
```

That extracts frames into `seasons/2026-nfl/week-03/frames/` and stops. Have
Claude read the frames and write `reads.jsonl` next to them, then rerun without
`--video`:

```bash
python run.py --week 3
```

Add `--refresh` to ignore cached book responses (they are reused for 30 minutes).
Everything for the week lands in `seasons/<season>/week-NN/`.

## Files

| file | job |
|---|---|
| `wheel.py` | devig, rank, ticket structure, exact EV. Unchanged from the original. |
| `fetchers.py` | DraftKings + FanDuel fetchers, disk cache, merge on (player, market, exact line) |
| `video.py` | recording -> frames -> `available.txt` (record-level dedupe, market normalization) |
| `run.py` | orchestrator: availability first, fetch only what is needed, match, board |
| `sanity.py` | pre-bet checks on a props file |
| `fixtures/week-01/` | acceptance fixture: `python wheel.py --json fixtures/week-01/props.json --threshold 0.55 --entries 10` |

## Sports

| sport | markets priced | notes |
|---|---|---|
| `nfl` | receptions, pass TDs, rush/pass attempts, completions, interceptions, field goals made, anytime TD | yardage ignored by default |
| `mlb` | pitcher strikeouts, hits allowed, pitching outs | hits allowed is DK-only; batter props stay out, they are 4-event markets too noisy for a 56% leg |
| `wnba` | points, rebounds, assists, threes, Pts+Reb, Pts+Ast, Reb+Ast, PRA | |

```bash
python fetchers.py --sport wnba --days 2 --out wnba.json
python run.py --week 1 --season 2026-wnba --video REC.mp4
```

League and subcategory ids were discovered live (NFL 2026-09-16, MLB and WNBA
2026-09-23) and are in `fetchers.py`. DraftKings abbreviates a few clubs
differently from FanDuel (Athletics, Giants, Nationals, Liberty); `DK_TEAM_ALIAS`
reconciles them and any new mismatch is logged rather than silently split.

## Bet log

`bets/<date>-<promo>.json` records what was actually placed: the legs with their
true probability at bet time, the ticket list, the promo terms, and the expected
distribution. Fill each leg's `result` after the games and grade it.

Two promos are in play and they are **not** interchangeable:

| wheel | legs/ticket | multiplier | break-even per leg |
|---|---|---|---|
| 6x (95%) / 20x (5%) | 3 | 6.70x | 53.0% |
| 3x (95%) / 10x (5%) | 2 | 3.35x | 54.6% |

A leg good for one is not automatically good for the other, and the ticket
structure differs: the 3-leg wheel wants leave-two-out on 5 (10 tickets), the
2-leg wheel wants every pair of 5 (also 10 tickets).

## Reading the frames

`run.py` extracts frames and stops, because reading them is a vision step. The
frames are 4x2 tiles: eight consecutive one-second screenshots per image, read
left to right along the top row, then the bottom row. Write one JSON object per
prop per image into `reads.jsonl` beside the frames:

```json
{"frame": "007.png", "player": "Tee Higgins", "market": "Receptions", "line": 4.5,
 "sides": "over,under", "confidence": "high", "game": "CLE @ CIN"}
```

`sides` is which buttons are actually tappable; some props offer only Over.
`market` can be the app's own wording, `video.py` normalizes it. Mark anything
uncertain `"confidence": "low"` and it gets printed rather than trusted.

Boom app conventions worth knowing:

- A card header reads `TEAM vs OPP` on home players and `TEAM @ OPP` on away
  players. Always write `game` as `AWAY @ HOME`.
- Boom's abbreviations differ from the books': `AZ` to `ARI`, WNBA `NY` to `NYL`,
  NFL `WSH` to `WAS`. Normalize while reading.
- Every `View more (N)` sheet must be opened in the recording or those props are
  invisible; the count in the label tells you how many are hidden.
- Occasional label errors appear (a player shown under the wrong game).
  Transcribe what is displayed and flag it rather than correcting it.

## What the books actually post

Checked live 2026-09-23. A market missing here is not a bug, it is the book.

| | DraftKings | FanDuel |
|---|---|---|
| NFL receptions, rec/rush/pass yards, pass TDs, anytime TD | yes | yes |
| NFL completions, pass attempts, rush attempts, interceptions, FG made | yes | **no** |
| MLB strikeouts, pitching outs | yes | yes |
| MLB hits allowed | yes | **no** |
| WNBA points, rebounds, assists, threes, all combos | yes | yes |
| targets (any sport), WNBA three-point attempts | **no** | **no** |

**Timing matters more than anything else.** DraftKings posts NFL player props
close to game day. On the Tuesday before a Thursday opener it had five of sixteen
games; the Wednesday before that it had none at all, with zero `O/U`
subcategories in the league payload. A thin board usually means you ran too
early, not that the parser broke. Run Friday or Saturday for a full Sunday slate.
MLB and WNBA post the day of.

## Rules encoded in run.py

- Merge and match on player **and exact line**. A nearby line is never substituted.
- Two-way markets need both sides. One-sided markets (anytime TD) use the quoted
  side only, and only if the app offers that side; the other side is never inferred.
  The vig stripped from a single price is `--one-sided-overround` (default 1.08) and
  is printed on the board.
- Yardage props are ignored (`--include-yardage` to price them).
- Board shows the top 20 by true probability with a market-preference column.

## Recording for the cheapest read

The frame read is the only step that costs model tokens, and cost scales with
the number of images, so:

- Scroll at a steady one screen per second. No need to pause longer than that.
- Open every "View more" sheet, since props behind an unopened sheet are invisible.
- Skip players who only have yardage, fantasy-point, or longest-play props.
- Defaults (`--fps 1 --tile 4x2 --width 600`) pack 8 seconds of recording into one
  legible image. A 4.5-minute recording is about 34 images.
