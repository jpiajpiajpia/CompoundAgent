# Compound Signal Harness

Extracts stock and ETF picks from The Compound's podcasts, ranks them weekly,
and manages a small stop-protected book through Robinhood.

Full design: `docs/plan.html`

## Status

**Phase 0 — validating the premise.** No broker connection, no money at risk.

- [x] Stage 01 Ingest — RSS discovery, show classification, dedupe
- [x] Stage 02 Transcribe — YouTube playlist catalog, episode matching, captions
- [~] Stage 03 Extract mentions — in progress, 100 loaded from 38 segments
- [ ] Stage 04 Resolve tickers
- [ ] Backtest replay

## Usage

    python3 -m harness ingest --weeks 52   # backfill podcast episodes
    python3 -m harness catalog             # pull per-show YouTube playlists
    python3 -m harness match               # pair episodes with their videos
    python3 -m harness transcribe          # download captions
    python3 -m harness batch --tier disclosure --limit 14 --out runs/batchNN.txt
    python3 -m harness load --file runs/batchNN.extracted.json --mark-extracted
    python3 -m harness status              # what's in the ledger

Extraction runs in a Claude session, not against a platform API key. `batch`
emits segments to read; `load` validates and stores the results.

## Rebuilding the ledger

`data/harness.db` is disposable — it rebuilds from tracked inputs:

    python3 -m harness ingest --weeks 52 && python3 -m harness catalog && python3 -m harness match
    for f in runs/*.extracted.json; do python3 -m harness load --file "$f" --mark-extracted; done

Transcripts (`data/transcripts/`) and extractions (`runs/*.extracted.json`) are
both committed. The extractions are hand-classified judgement and are the one
thing here that cannot be regenerated.

Every command is idempotent — safe to re-run.

## How episodes are matched to YouTube

Transcripts come from YouTube auto-captions, so each podcast episode must be
paired with its video. Two obstacles shaped the approach:

* **YouTube re-headlines episodes.** The podcast calls one *"Bubble bursts in
  2027, Nvidia earnings preview, Materials sector set-up"*; YouTube calls the
  same show *"AI Darlings Crash in a Teflon Tape | WAYT?"*. Title matching
  scores 84% on TCAF and **5%** on WAYT, so titles cannot be the key.
* **The main channel is mostly clips.** Short segments cut from episodes vastly
  outnumber full shows.

Both are solved by pulling each show's own playlist (no clips, and show
classification comes free) and matching on **publish date + runtime**, which are
near-identical across platforms. Title similarity is only a tiebreaker.

Result: TCAF 51/51, WAYT 37/44. The `compound_other` format has no playlist of
its own and is currently unmatched.

## Known limitations

* **No speaker labels.** YouTube auto-captions are one undivided stream. Knowing
  whether Josh, a co-host, or a guest said something is a real signal we're
  giving up for now — it costs ~$35 of paid transcription to recover.
* **Proper nouns get garbled.** "T. Rowe Price" transcribes as "Troll Price".
  Ticker resolution must tolerate this.
* **YouTube blocks yt-dlp's default client.** Every call pins
  `player_client=android` (see `config/shows.json`). If extraction breaks, that
  setting is the first thing to check.

## Environment

System Python is 3.9.6. `yt-dlp` warns that 3.9 is deprecated but works. Later
stages will want 3.11+:

    curl -LsSf https://astral.sh/uv/install.sh | sh
