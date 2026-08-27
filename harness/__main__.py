"""CLI entry point:  python3 -m harness <command>"""
from __future__ import annotations

import argparse
import json
import sys

from . import backtest, db, extract, ingest, transcribe, youtube


def cmd_ingest(args) -> int:
    with db.session() as conn:
        summary = ingest.run(conn, weeks=args.weeks)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_catalog(args) -> int:
    with db.session() as conn:
        out = youtube.refresh_catalog(conn, limit=args.limit)
        total = conn.execute("SELECT COUNT(*) FROM youtube_videos").fetchone()[0]
    print(json.dumps({"stored_per_show": out, "catalog_total": total}, indent=2))
    return 0


def cmd_match(args) -> int:
    with db.session() as conn:
        stats = youtube.match_episodes(conn, show=args.show)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_transcribe(args) -> int:
    with db.session() as conn:
        stats = transcribe.run(conn, limit=args.limit, delay=args.delay)
    print(json.dumps(stats, indent=2))
    return 0


def cmd_batch(args) -> int:
    from pathlib import Path
    with db.session() as conn:
        segs = extract.collect_segments(conn, tier=args.tier, show=args.show,
                                        limit=args.limit)
    if not segs:
        print("No unextracted segments match.")
        return 0
    dest = Path(args.out)
    extract.emit_batch(segs, dest)
    from collections import Counter
    print(json.dumps({
        "batch_file": str(dest),
        "segments": len(segs),
        "words": sum(s["words"] for s in segs),
        "by_tier": dict(Counter(s["tier"] for s in segs)),
        "by_show": dict(Counter(s["show"] for s in segs)),
        "episodes": len({s["episode_id"] for s in segs}),
    }, indent=2))
    return 0


def cmd_load(args) -> int:
    records = json.loads(open(args.file, encoding="utf-8").read())
    if isinstance(records, dict):
        records = records.get("mentions", [])
    with db.session() as conn:
        stats = extract.load_extractions(conn, records)
        if args.mark_extracted:
            for ep in stats["episodes_touched"]:
                conn.execute("UPDATE episodes SET state='extracted' WHERE id=?", (ep,))
            conn.commit()
    print(json.dumps(stats, indent=2))
    return 0


def cmd_backtest(args) -> int:
    import math, statistics as st
    with db.session() as conn:
        rows = conn.execute("""SELECT m.ticker, e.published_at, m.stance FROM mentions m
                               JOIN episodes e ON e.id=m.episode_id
                               WHERE m.is_thesis=1 AND m.is_hypothetical=0 AND m.is_news_recap=0
                                 AND m.ticker IS NOT NULL AND m.stance IN ('bull','strong_bull')""").fetchall()
    picks = [{"ticker": r["ticker"], "date": r["published_at"][:10], "stance": r["stance"]}
             for r in rows]
    res = backtest.run(picks)
    ex = [t["excess"] for t in res["trades"] if t["excess"] is not None]
    out = {k: v for k, v in res.items() if k != "trades"}
    if len(ex) > 1:
        se = st.stdev(ex) / math.sqrt(len(ex))
        out["t_stat"] = round(st.mean(ex) / se, 2)
        out["significant"] = abs(out["t_stat"]) > 2
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_status(args) -> int:
    with db.session() as conn:
        total = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        if not total:
            print("No episodes ingested yet. Run:  python3 -m harness ingest")
            return 0

        print("episodes: {}\n".format(total))

        print("{:<16} {:>6}  {:<12} {:<12}".format("SHOW", "COUNT", "EARLIEST", "LATEST"))
        rows = conn.execute(
            """SELECT show, COUNT(*) n, MIN(published_at) lo, MAX(published_at) hi
               FROM episodes GROUP BY show ORDER BY n DESC"""
        )
        for r in rows:
            print("{:<16} {:>6}  {:<12} {:<12}".format(
                r["show"], r["n"], r["lo"][:10], r["hi"][:10]))

        print("\nby state:")
        for r in conn.execute(
            "SELECT state, COUNT(*) n FROM episodes GROUP BY state ORDER BY n DESC"
        ):
            print("  {:<14} {}".format(r["state"], r["n"]))

        m = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE youtube_id IS NOT NULL").fetchone()[0]
        cat = conn.execute("SELECT COUNT(*) FROM youtube_videos").fetchone()[0]
        print("\nyoutube: {} videos catalogued, {}/{} episodes matched".format(cat, m, total))

        print("\nmost recent:")
        for r in conn.execute(
            """SELECT published_at, show, title FROM episodes
               ORDER BY published_at DESC LIMIT 8"""
        ):
            print("  {}  {:<15} {}".format(
                r["published_at"][:10], r["show"], r["title"][:56]))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="harness", description="Compound Signal Harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="poll feeds and record new episodes")
    pi.add_argument("--weeks", type=int, default=None,
                    help="only ingest the last N weeks (default: whole feed)")
    pi.set_defaults(func=cmd_ingest)

    pc = sub.add_parser("catalog", help="pull per-show YouTube playlists")
    pc.add_argument("--limit", type=int, default=80,
                    help="videos per playlist (default 80, ~18 months of weekly shows)")
    pc.set_defaults(func=cmd_catalog)

    pm = sub.add_parser("match", help="pair episodes with their YouTube videos")
    pm.add_argument("--show", default=None)
    pm.set_defaults(func=cmd_match)

    pt = sub.add_parser("transcribe", help="download captions for matched episodes")
    pt.add_argument("--limit", type=int, default=None)
    pt.add_argument("--delay", type=float, default=1.0)
    pt.set_defaults(func=cmd_transcribe)

    pb = sub.add_parser("batch", help="emit segments to read, best signal first")
    pb.add_argument("--tier", default=None, help="disclosure | opinion | plain")
    pb.add_argument("--show", default=None, help="wayt | tcaf")
    pb.add_argument("--limit", type=int, default=10,
                    help="segments per batch; keep small, they get long")
    pb.add_argument("--out", default="runs/batch.txt")
    pb.set_defaults(func=cmd_batch)

    pl = sub.add_parser("load", help="load extracted mentions back into the ledger")
    pl.add_argument("--file", required=True, help="JSON array of mention records")
    pl.add_argument("--mark-extracted", action="store_true")
    pl.set_defaults(func=cmd_load)

    pk = sub.add_parser("backtest", help="replay picks against real prices")
    pk.set_defaults(func=cmd_backtest)

    ps = sub.add_parser("status", help="what is in the ledger")
    ps.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
