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

## Third and fourth books (The Odds API)

DK and FD leave a lot of legs on one book, and a single price has to be devigged
with an assumed 1.08 overround, which flatters it. `--odds-api` fills those gaps
after the free scrapers have run:

```bash
python run.py --week 3 --season 2026-nfl --odds-api
```

The key lives in `.env` (gitignored) as `ODDS_API_KEY=...`; nothing reads it from
the command line. Billing is **1 credit per game-market that returns data** —
markets nobody posts are free, and the 30-minute disk cache means a rerun costs
nothing. The free plan is 500 credits a month; a full NFL gap-fill run is about
30, so budget roughly 15 runs. `--odds-api-budget` (default 40) stops a run
before it eats the allowance.

**Credits are only spent on legs that already look playable.** Before anything
is bought, DK/FD are priced as usual; a single-book leg is cross-referenced only
if all of these hold:

- it is the only book on that prop, and
- it already prices at or above `--odds-api-min-prob` (default 55%) on that one
  book, and
- the app offers the side we would actually take, and
- it is in the top `--odds-api-top` (default 8) such legs by probability.

A leg at 45% does not get more interesting with a second quote, so it never
costs a credit. On NFL week 3 this took the run from 31 credits to 1.

Raise the bar with `--odds-api-min-prob 0.56`, or drop it to about 0.53 to also
audit the band just under the break-even. The trade-off is real: a single-book
leg between 53% and the threshold keeps its assumed-vig number, which is
optimistic, so treat the `1 book` flag on the board as "unverified" rather than
"verified good".

A credit that comes back empty is still informative. Week 3 bought one on
TEN @ NYG receptions and learned FanDuel is the only book in the feed posting
that game at all, which is why Ridley and Ayomanor stay single-book.

Coverage as checked live 2026-09-24:

| | who adds a second price |
|---|---|
| NFL interceptions | BetRivers (BetMGM does **not** post these) |
| NFL receptions, pass TDs, attempts | BetMGM |
| NFL completions | BetMGM, sparsely |
| NFL field goals made | nobody — DK only, so it is skipped to save the credit |
| MLB hits allowed, pitching outs | Fanatics only |
| MLB strikeouts | Fanatics, Bovada |

Default extra books are `mgm,br,fan,wh` — legal US only. Bovada and BetOnline
are off by default: more vig, and they often mirror a US line rather than adding
an independent opinion. They do, however, have the **widest** coverage of the QB
count markets (attempts, completions, interceptions, rush attempts), well beyond
BetRivers. If a run keeps coming back single-book on those, turn them on:

```bash
python run.py --week 3 --odds-api --odds-api-books mgm,br,fan,wh,bov,bol
```

**Bet365 is not usable.** Every entry point returns 403 behind a Cloudflare
challenge, and their sportsbook API is websocket-based behind a JS-derived
token. It is also absent from the Odds API's `us` region. Do not spend time
retrying it.

Adding a second price usually *lowers* a single-book leg, because the assumed
vig was doing the flattering. On week 3 both Kirk Cousins and Bo Nix
interceptions fell about 1.5 points once BetRivers was included, dropping them
out of contention. That is the feature working.

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
