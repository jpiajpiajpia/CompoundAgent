"""Stage 03 -- Extract stock mentions from transcript segments.

The whole difficulty of this stage is that most company mentions are not picks.
In one hand-checked segment, ten companies were named and exactly two were
recommendations: Netflix and Spotify (both with explicit position disclosures).
The other eight were a chart comparison, two names used as an example of
"stocks I have no bias about", and a back-reference to an earlier topic.

A keyword counter would have reported ten. So the schema below is built around
separating real opinions from incidental mentions, and the prompt is written to
make that distinction the model's primary job.

Model choice: defaults to Claude Opus 5. The system prompt is held byte-stable
and cached, since it is resent on all ~550 segment calls.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Dict, List, Optional

from . import segment
from .db import RunLog, utcnow

MODEL = os.environ.get("HARNESS_EXTRACT_MODEL", "claude-opus-5")

# Byte-stable: any edit invalidates the prompt cache across every call.
SYSTEM_PROMPT = """You extract stock and ETF mentions from financial podcast transcripts.

The shows are The Compound's "What Are Your Thoughts?" (two hosts, Josh Brown and
Michael Batnick, trading topical segments) and "The Compound and Friends" (the same
hosts with a guest). Transcripts are YouTube auto-captions: there are no speaker
names, proper nouns are frequently garbled ("T. Rowe Price" appears as "Troll
Price"), and punctuation is unreliable. Infer companies from context when the
transcription is mangled, but only when you are confident.

YOUR PRIMARY JOB IS SEPARATING OPINIONS FROM INCIDENTAL MENTIONS.

These hosts name companies constantly without recommending them. Most mentions
are not picks. Record every company you find, but classify each one honestly:

- is_thesis: they made a substantive argument about the company as an investment.
  A real reason to own or avoid it, not just a remark.
- is_news_recap: they are relaying what happened (earnings, a deal, a price move)
  without expressing a view on whether to own it.
- is_hypothetical: the company is an illustration for some other point, a
  comparison in a chart, or a back-reference to a discussion elsewhere in the
  episode. "I have no bias about Micron because I don't use their products" is
  hypothetical -- Micron is an example, not a pick.

A mention can be several of these at once, or none. Set them independently.

STANCE reflects their view of the company as an investment right now:
strong_bull, bull, neutral, bear, strong_bear. Use neutral for pure news recaps
and for genuinely balanced discussion. Do not infer bullishness from enthusiasm
about a product or from the company merely being successful.

ACTION_LANGUAGE is the single highest-value signal in this data, because it is
skin in the game. These hosts disclose their own positions out loud, often with
entry prices and sometimes with stops ("I bought it the day after earnings at
67", "I do have a stop in on Spotify"). Capture it precisely:
  buying   - bought recently, added, or said they are buying
  owns     - holds it, no recent transaction mentioned
  watching - interested, on a list, considering
  sold     - exited or trimmed
  shorting - short the name
  none     - no personal position mentioned

HORIZON: trade (days/weeks), swing (weeks/months), long_term (years),
unspecified.

TICKER: use the US-listed ticker in capitals. Leave null for private companies
(SpaceX, OpenAI, Anthropic), for non-US listings, and whenever you are unsure --
a null is far better than a wrong ticker, since a wrong ticker causes a real
trade in the wrong stock. Set resolve_confidence honestly.

QUOTE: the shortest verbatim span that justifies your stance and action_language.
Copy it exactly from the transcript; never paraphrase.

If a segment contains no company mentions at all, return an empty list."""


def _schemas():
    """Build the response schema at call time.

    Stages 01 and 02 are deliberately stdlib-only, so pydantic and anthropic are
    imported here rather than at module scope -- otherwise a machine without the
    optional dependencies could not run ingest or status either.
    """
    from pydantic import BaseModel, Field
    from typing import List as _List, Optional as _Optional

    class Mention(BaseModel):
        ticker: _Optional[str] = Field(None, description="US ticker in caps, or null if unsure")
        company: str = Field(description="Company or fund name as discussed")
        resolve_confidence: float = Field(description="0-1 confidence the ticker is correct")
        stance: str = Field(description="strong_bull|bull|neutral|bear|strong_bear")
        action_language: str = Field(description="buying|owns|watching|sold|shorting|none")
        horizon: str = Field(description="trade|swing|long_term|unspecified")
        is_thesis: bool
        is_news_recap: bool
        is_hypothetical: bool
        quote: str = Field(description="Shortest verbatim span justifying the classification")
        reasoning: str = Field(description="One sentence on why this classification")

    class SegmentExtraction(BaseModel):
        mentions: _List[Mention]

    return SegmentExtraction


def extract_segment(client, schema, seg: Dict, episode_title: str, show: str):
    user = (
        "Show: {show}\nEpisode: {title}\nSegment timestamp: {mm}:{ss}\n\n"
        "Transcript segment:\n---\n{text}\n---"
    ).format(
        show=show, title=episode_title,
        mm=int(seg["t_start"]) // 60, ss=int(seg["t_start"]) % 60,
        text=seg["text"],
    )
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=8000,
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    return resp.parsed_output, resp.usage


def store_mentions(conn: sqlite3.Connection, episode_id: str, seg: Dict, result) -> int:
    now = utcnow()
    n = 0
    for m in result.mentions:
        conn.execute(
            """INSERT INTO mentions
               (episode_id, speaker, speaker_role, ticker, resolved_name, resolve_conf,
                t_start, t_end, quote, stance, action_language, horizon,
                is_thesis, is_news_recap, is_hypothetical, extracted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (episode_id, None, None, m.ticker, m.company, m.resolve_confidence,
             int(seg["t_start"]), int(seg["t_end"]), m.quote, m.stance,
             m.action_language, m.horizon, int(m.is_thesis), int(m.is_news_recap),
             int(m.is_hypothetical), now),
        )
        n += 1
    conn.commit()
    return n


def run(conn: sqlite3.Connection, limit_episodes: Optional[int] = None,
        show: Optional[str] = None, dry_run: bool = False) -> Dict:
    """Extract mentions for transcribed episodes that haven't been done yet."""
    q = """SELECT id, show, title, transcript_path FROM episodes
           WHERE transcript_path IS NOT NULL AND state='transcribed'"""
    params: tuple = ()
    if show:
        q += " AND show=?"
        params = (show,)
    q += " ORDER BY published_at DESC"
    if limit_episodes:
        q += " LIMIT %d" % limit_episodes

    episodes = conn.execute(q, params).fetchall()
    stats = {"model": MODEL, "episodes": 0, "segments": 0, "mentions": 0,
             "real_picks": 0, "in_tokens": 0, "out_tokens": 0, "cached_tokens": 0,
             "errors": []}

    if dry_run:
        for ep in episodes:
            segs = segment.segment_episode(ep["transcript_path"])["segments"]
            stats["episodes"] += 1
            stats["segments"] += len(segs)
        return stats

    import anthropic  # deferred: dry_run must work without it

    schema = _schemas()
    client = anthropic.Anthropic()

    with RunLog(conn, "extract") as log:
        for ep in episodes:
            segs = segment.segment_episode(ep["transcript_path"])["segments"]
            stats["episodes"] += 1
            got = 0
            for seg in segs:
                try:
                    result, usage = extract_segment(
                        client, schema, seg, ep["title"], ep["show"])
                except anthropic.APIStatusError as exc:
                    stats["errors"].append({"episode": ep["id"], "error": str(exc)[:150]})
                    continue
                stats["segments"] += 1
                stats["in_tokens"] += usage.input_tokens
                stats["out_tokens"] += usage.output_tokens
                stats["cached_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
                got += store_mentions(conn, ep["id"], seg, result)
                stats["real_picks"] += sum(
                    1 for m in result.mentions
                    if m.is_thesis and not m.is_hypothetical and not m.is_news_recap)
            stats["mentions"] += got
            conn.execute("UPDATE episodes SET state='extracted', updated_at=? WHERE id=?",
                         (utcnow(), ep["id"]))
            conn.commit()
        log.payload = stats

    return stats
