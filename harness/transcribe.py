"""Stage 02b -- Transcripts from YouTube auto-captions.

Downloads the auto-generated English caption track for each matched episode and
normalizes it into timestamped cues. Timestamps matter downstream: a mention's
position and the airtime around it feed the ranker, so cues are kept rather than
flattened into one blob.

Caveat worth carrying forward: auto-captions garble proper nouns ("T. Rowe Price"
comes through as "Troll Price"). Ticker resolution has to be robust to that, and
it is a reason to revisit paid transcription if extraction precision disappoints.
There are no speaker labels here at all -- that is the known cost of the free route.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import paths
from .db import RunLog, utcnow
from .youtube import PLAYER_CLIENT, YtDlpError


def _parse_json3(raw: dict) -> List[Dict]:
    cues = []
    for ev in raw.get("events", []):
        if "segs" not in ev:
            continue
        text = "".join(s.get("utf8", "") for s in ev["segs"]).strip()
        if not text or text == "\n":
            continue
        cues.append({"t": round((ev.get("tStartMs") or 0) / 1000.0, 2), "text": text})
    return cues


def fetch_transcript(video_id: str, timeout: int = 180) -> Optional[List[Dict]]:
    """Return normalized cues, or None when the video has no usable captions."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "cap")
        proc = subprocess.run(
            ["python3", "-m", "yt_dlp", "--skip-download",
             "--write-auto-subs", "--sub-langs", "en", "--sub-format", "json3",
             "-o", out + ".%(ext)s", "--no-warnings", "--socket-timeout", "30",
             "--extractor-args", "youtube:player_client=" + PLAYER_CLIENT,
             "https://www.youtube.com/watch?v=" + video_id],
            capture_output=True, text=True, timeout=timeout,
        )
        files = [Path(tmp) / f for f in os.listdir(tmp) if f.endswith(".json3")]
        if not files:
            err = (proc.stderr or "").strip().splitlines()
            raise YtDlpError(err[-1][:200] if err else "no caption file produced")
        raw = json.loads(files[0].read_text(encoding="utf-8"))
    cues = _parse_json3(raw)
    return cues or None


def run(conn: sqlite3.Connection, limit: Optional[int] = None,
        delay: float = 1.0) -> Dict:
    """Fetch transcripts for matched episodes that don't have one yet."""
    paths.ensure_dirs()
    q = """SELECT id, youtube_id, title FROM episodes
           WHERE youtube_id IS NOT NULL AND transcript_path IS NULL
           ORDER BY published_at DESC"""
    if limit:
        q += " LIMIT %d" % limit

    stats = {"attempted": 0, "ok": 0, "empty": 0, "failed": 0,
             "words": 0, "errors": []}

    with RunLog(conn, "transcribe") as log:
        for ep in conn.execute(q).fetchall():
            stats["attempted"] += 1
            try:
                cues = fetch_transcript(ep["youtube_id"])
            except (YtDlpError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                stats["failed"] += 1
                if len(stats["errors"]) < 6:
                    stats["errors"].append({"episode": ep["id"], "error": repr(exc)[:150]})
                continue

            if not cues:
                stats["empty"] += 1
                conn.execute(
                    "UPDATE episodes SET transcript_kind='none', updated_at=? WHERE id=?",
                    (utcnow(), ep["id"]))
                conn.commit()
                continue

            words = sum(len(c["text"].split()) for c in cues)
            dest = paths.TRANSCRIPTS / (ep["id"] + ".json")
            dest.write_text(json.dumps(
                {"episode_id": ep["id"], "video_id": ep["youtube_id"],
                 "source": "youtube_auto_captions", "cues": cues},
                ensure_ascii=False), encoding="utf-8")

            conn.execute(
                """UPDATE episodes SET transcript_path=?, transcript_kind='captions',
                       word_count=?, state='transcribed', updated_at=? WHERE id=?""",
                (str(dest.relative_to(paths.ROOT)), words, utcnow(), ep["id"]))
            conn.commit()
            stats["ok"] += 1
            stats["words"] += words
            time.sleep(delay)  # be a polite client

        log.payload = stats

    return stats
