"""Stage 01 -- Ingest.

Poll the podcast feeds, discover new episodes, write them to the ledger.
Deduplicates on the feed's GUID so re-running is always safe.

Deliberately stdlib-only: this stage has to be the most reliable thing in the
system and it does not need a dependency tree to parse XML.
"""
from __future__ import annotations

import re
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional

from . import shows
from .db import RunLog, utcnow

USER_AGENT = "compound-harness/0.1 (+personal research)"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


# ------------------------------------------------------------------ fetching
def fetch_feed(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ------------------------------------------------------------------ parsing
def _duration_seconds(raw: Optional[str]) -> Optional[int]:
    """itunes:duration is either seconds or [HH:]MM:SS."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def _slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-")


def parse_items(xml_bytes: bytes, feed_id: str) -> List[Dict]:
    """Turn a feed document into normalized episode dicts, newest first."""
    channel = ET.fromstring(xml_bytes).find("channel")
    if channel is None:
        return []

    out: List[Dict] = []
    for item in channel.findall("item"):
        pub_raw = item.findtext("pubDate")
        if not pub_raw:
            continue
        try:
            published = parsedate_to_datetime(pub_raw)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        published = published.astimezone(timezone.utc)

        title = (item.findtext("title") or "").strip()
        guid = (item.findtext("guid") or "").strip() or f"{feed_id}:{pub_raw}:{title}"
        weekday = published.strftime("%a")

        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None

        out.append(
            {
                "guid": guid,
                "feed": feed_id,
                "show": shows.classify(weekday, title, feed_id),
                "published_at": published.replace(microsecond=0).isoformat(),
                "published_dt": published,
                "weekday": weekday,
                "title": title,
                "subtitle": (item.findtext(ITUNES + "subtitle") or "").strip() or None,
                "audio_url": audio_url,
                "duration_sec": _duration_seconds(item.findtext(ITUNES + "duration")),
                "link": item.findtext("link"),
            }
        )

    out.sort(key=lambda e: e["published_dt"], reverse=True)
    return out


# ------------------------------------------------------------------ storing
def _episode_id(conn: sqlite3.Connection, ep: Dict) -> str:
    """Stable, human-readable primary key: show-date, disambiguated on clash."""
    base = "{}-{}".format(ep["show"], ep["published_dt"].strftime("%Y-%m-%d"))
    candidate, n = base, 1
    while True:
        row = conn.execute(
            "SELECT guid FROM episodes WHERE id=?", (candidate,)
        ).fetchone()
        if row is None or row["guid"] == ep["guid"]:
            return candidate
        n += 1
        candidate = "{}-{}".format(base, n)


def upsert_episodes(conn: sqlite3.Connection, episodes: Iterable[Dict]) -> Dict[str, int]:
    """Insert unseen episodes. Existing GUIDs are left untouched so that
    downstream state (transcripts, extraction) is never clobbered by a re-poll."""
    stats = {"seen": 0, "inserted": 0, "skipped": 0}
    now = utcnow()

    for ep in episodes:
        stats["seen"] += 1
        exists = conn.execute(
            "SELECT 1 FROM episodes WHERE guid=?", (ep["guid"],)
        ).fetchone()
        if exists:
            stats["skipped"] += 1
            continue

        conn.execute(
            """INSERT INTO episodes
               (id, show, feed, guid, published_at, weekday, title, subtitle,
                audio_url, duration_sec, link, state, discovered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'discovered',?,?)""",
            (
                _episode_id(conn, ep), ep["show"], ep["feed"], ep["guid"],
                ep["published_at"], ep["weekday"], ep["title"], ep["subtitle"],
                ep["audio_url"], ep["duration_sec"], ep["link"], now, now,
            ),
        )
        stats["inserted"] += 1

    conn.commit()
    return stats


# ------------------------------------------------------------------ entry point
def run(conn: sqlite3.Connection, weeks: Optional[int] = None) -> Dict:
    """Poll every enabled feed. `weeks` limits how far back to ingest;
    None means the whole feed (used once, for the Phase 0 backfill)."""
    cutoff = None
    if weeks is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    summary: Dict = {"feeds": {}, "totals": {"seen": 0, "inserted": 0, "skipped": 0}}

    with RunLog(conn, "ingest") as log:
        for feed in shows.FEEDS:
            url = feed.get("url")
            if not url:
                summary["feeds"][feed["feed_id"]] = {"error": "no url configured"}
                continue
            try:
                raw = fetch_feed(url)
            except (urllib.error.URLError, TimeoutError) as exc:
                summary["feeds"][feed["feed_id"]] = {"error": repr(exc)}
                continue

            items = parse_items(raw, feed["feed_id"])
            if cutoff is not None:
                items = [i for i in items if i["published_dt"] >= cutoff]

            stats = upsert_episodes(conn, items)
            summary["feeds"][feed["feed_id"]] = stats
            for k in summary["totals"]:
                summary["totals"][k] += stats[k]

        log.payload = summary

    return summary
