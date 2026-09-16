# Odds-Search

Prop screening for the Pick & Spin promo: read what the app offers from a screen
recording, price only those props off DraftKings and FanDuel, and let `wheel.py`
rank them and build tickets.

Python 3 + `requests` only. ffmpeg for frame extraction (the copy bundled with
BlueStacks is found automatically). Read-only against the books.

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
