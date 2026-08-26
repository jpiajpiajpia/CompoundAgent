"""Stage 02a -- YouTube catalog and episode matching.

Transcripts come from YouTube's auto-captions, which means every podcast episode
has to be paired with its video. Two things make that harder than it sounds:

  * YouTube re-headlines episodes. The podcast calls one "Bubble bursts in 2027,
    Nvidia earnings preview, Materials sector set-up"; YouTube calls the same
    show "AI Darlings Crash in a Teflon Tape | WAYT?". Title matching scores 84%
    on TCAF and 5% on WAYT, so titles cannot be the key.
  * The main channel is full of short clips cut from episodes.

Both are solved by pulling each show's own playlist (no clips) and matching on
publish date plus runtime, which are near-identical between the two platforms.
Title similarity is kept only as a tiebreaker.

Requires yt-dlp. YouTube blocks its default web client, so every call pins
player_client (android) -- see config/shows.json.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import paths
from .db import utcnow

_cfg = json.loads(paths.SHOWS_CONFIG.read_text())["youtube"]
PLAYLISTS: Dict[str, str] = _cfg["playlists"]
PLAYER_CLIENT: str = _cfg["player_client"]
MAX_DATE_DELTA: int = _cfg["match"]["max_date_delta_days"]
MAX_DUR_DELTA: float = _cfg["match"]["max_duration_delta_pct"]

_PRINT_FMT = "%(id)s\t%(upload_date)s\t%(duration)s\t%(title)s"


class YtDlpError(RuntimeError):
    pass


def _run(args: List[str], timeout: int = 900) -> str:
    proc = subprocess.run(
        ["python3", "-m", "yt_dlp", "--no-warnings", "--socket-timeout", "30",
         "--extractor-args", "youtube:player_client=" + PLAYER_CLIENT] + args,
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise YtDlpError((proc.stderr or "").strip()[:400])
    return proc.stdout


# ------------------------------------------------------------------- catalog
def fetch_playlist(show: str, limit: Optional[int] = None) -> List[Dict]:
    """Full (non-flat) extract -- flat mode omits upload_date and duration,
    which are exactly the two fields the matcher needs."""
    playlist_id = PLAYLISTS[show]
    args = ["--skip-download", "--print", _PRINT_FMT]
    if limit:
        args += ["--playlist-end", str(limit)]
    args.append("https://www.youtube.com/playlist?list=" + playlist_id)

    videos = []
    for line in _run(args).splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4 or parts[0] in ("NA", ""):
            continue
        vid, up, dur, title = parts[0], parts[1], parts[2], "\t".join(parts[3:])
        videos.append({
            "video_id": vid,
            "show": show,
            "playlist_id": playlist_id,
            "title": title.strip(),
            "upload_date": (f"{up[:4]}-{up[4:6]}-{up[6:8]}" if up and up != "NA" else None),
            "duration_sec": (int(dur) if dur.isdigit() else None),
        })
    return videos


def store_videos(conn: sqlite3.Connection, videos: List[Dict]) -> int:
    now = utcnow()
    n = 0
    for v in videos:
        conn.execute(
            """INSERT INTO youtube_videos
                 (video_id, show, playlist_id, title, upload_date, duration_sec, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET
                 title=excluded.title,
                 upload_date=excluded.upload_date,
                 duration_sec=excluded.duration_sec""",
            (v["video_id"], v["show"], v["playlist_id"], v["title"],
             v["upload_date"], v["duration_sec"], now),
        )
        n += 1
    conn.commit()
    return n


# ------------------------------------------------------------------ matching
def normalize_title(t: str) -> str:
    t = re.sub(r"\s*\|\s*(TCAF|WAYT\??|Animal Spirits)\s*\d*\s*$", "", t, flags=re.I)
    t = re.sub(r"\s+[wW]ith\s+.*$", "", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return " ".join(t.split())


def _score(ep_date: str, ep_dur: Optional[int],
           v_date: Optional[str], v_dur: Optional[int]) -> Optional[float]:
    """0..1 confidence, or None if the pair is disqualified outright."""
    if not v_date:
        return None
    d0 = datetime.strptime(ep_date[:10], "%Y-%m-%d")
    d1 = datetime.strptime(v_date, "%Y-%m-%d")
    days = abs((d1 - d0).days)
    if days > MAX_DATE_DELTA:
        return None

    date_score = 1.0 - (days / (MAX_DATE_DELTA + 1))

    if ep_dur and v_dur:
        delta = abs(v_dur - ep_dur) / max(ep_dur, 1)
        if delta > MAX_DUR_DELTA:
            return None
        dur_score = 1.0 - (delta / MAX_DUR_DELTA)
        return 0.5 * date_score + 0.5 * dur_score

    return 0.6 * date_score  # date-only matches are inherently less certain


def match_episodes(conn: sqlite3.Connection, show: Optional[str] = None) -> Dict:
    """Pair unmatched episodes with catalog videos. Idempotent."""
    q = "SELECT id, show, published_at, duration_sec, title FROM episodes WHERE youtube_id IS NULL"
    params: Tuple = ()
    if show:
        q += " AND show=?"
        params = (show,)

    stats = {"considered": 0, "matched": 0, "unmatched": 0, "by_show": {}}
    taken = {r[0] for r in conn.execute(
        "SELECT youtube_id FROM episodes WHERE youtube_id IS NOT NULL")}

    for ep in conn.execute(q, params).fetchall():
        stats["considered"] += 1
        bucket = stats["by_show"].setdefault(ep["show"], {"matched": 0, "unmatched": 0})

        cands = conn.execute(
            "SELECT video_id, title, upload_date, duration_sec FROM youtube_videos WHERE show=?",
            (ep["show"],),
        ).fetchall()

        best, best_score = None, 0.0
        for v in cands:
            if v["video_id"] in taken:
                continue
            s = _score(ep["published_at"], ep["duration_sec"],
                       v["upload_date"], v["duration_sec"])
            if s is None:
                continue
            # title similarity only nudges; it is unreliable on WAYT
            s += 0.08 * difflib.SequenceMatcher(
                None, normalize_title(ep["title"]), normalize_title(v["title"])).ratio()
            if s > best_score:
                best, best_score = v, s

        if best is not None and best_score >= 0.45:
            conn.execute(
                "UPDATE episodes SET youtube_id=?, match_score=?, match_method=?, updated_at=? WHERE id=?",
                (best["video_id"], round(best_score, 3), "date+duration", utcnow(), ep["id"]),
            )
            taken.add(best["video_id"])
            stats["matched"] += 1
            bucket["matched"] += 1
        else:
            stats["unmatched"] += 1
            bucket["unmatched"] += 1

    conn.commit()
    return stats


def refresh_catalog(conn: sqlite3.Connection, limit: Optional[int] = None) -> Dict:
    out = {}
    for show in PLAYLISTS:
        try:
            vids = fetch_playlist(show, limit=limit)
            out[show] = store_videos(conn, vids)
        except (YtDlpError, subprocess.TimeoutExpired) as exc:
            out[show] = {"error": repr(exc)[:200]}
    return out
