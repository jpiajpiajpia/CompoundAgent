"""Stage 03 -- Extract stock mentions from transcript segments.

Extraction runs in a Claude session rather than against a platform API key: the
harness emits batches of segments to read, and loads the structured results back
into the ledger. That keeps the whole build key-free and means the same model
doing the reading can see the surrounding project context.

The hard part is not finding company names -- it is separating recommendations
from incidental mentions. In one hand-checked segment ten companies were named
and exactly two were picks; the rest were a chart comparison, two names cited as
examples of stocks the host has no view on, and a back-reference. So every
mention carries three independent flags for what kind of mention it is.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from . import paths, segment
from .db import utcnow

STANCES = {"strong_bull", "bull", "neutral", "bear", "strong_bear"}
ACTIONS = {"buying", "owns", "watching", "sold", "shorting", "none"}
HORIZONS = {"trade", "swing", "long_term", "unspecified"}
TIER_ORDER = {"disclosure": 0, "opinion": 1, "plain": 2}


# ------------------------------------------------------------------- emit
def collect_segments(conn: sqlite3.Connection, tier: Optional[str] = None,
                     show: Optional[str] = None,
                     limit: Optional[int] = None) -> List[Dict]:
    """Gather unextracted segments, best signal first."""
    q = """SELECT id, show, title, published_at, transcript_path FROM episodes
           WHERE transcript_path IS NOT NULL"""
    params: tuple = ()
    if show:
        q += " AND show=?"
        params = (show,)
    q += " ORDER BY published_at DESC"

    done = {(r[0], int(r[1])) for r in conn.execute(
        "SELECT episode_id, t_start FROM mentions")}
    extracted_eps = {r[0] for r in conn.execute(
        "SELECT id FROM episodes WHERE state='extracted'")}

    out: List[Dict] = []
    for ep in conn.execute(q, params).fetchall():
        for s in segment.segment_episode(ep["transcript_path"])["segments"]:
            t = segment.classify_tier(s["text"])
            if tier and t != tier:
                continue
            if (ep["id"], int(s["t_start"])) in done:
                continue
            if ep["id"] in extracted_eps:
                continue
            n_disc = len(segment.DISCLOSURE.findall(s["text"]))
            n_op = len(segment.OPINION.findall(s["text"]))
            out.append({
                "episode_id": ep["id"], "show": ep["show"], "title": ep["title"],
                "published_at": ep["published_at"][:10], "tier": t,
                "t_start": int(s["t_start"]), "t_end": int(s["t_end"]),
                "words": s["words"], "companies_hinted": s["companies_hinted"],
                "n_disclosures": n_disc, "n_opinions": n_op,
                # signal per word read -- a 4000-word segment with two disclosures
                # is worse value than a 300-word one with two.
                "density": round((n_disc * 2 + n_op) / max(s["words"], 1) * 1000, 3),
                "text": s["text"],
            })

    # Best signal per word read, not longest first.
    out.sort(key=lambda s: (TIER_ORDER[s["tier"]], -s["density"]))
    return out[:limit] if limit else out


def emit_batch(segments: List[Dict], dest: Path) -> Path:
    """Write a batch to disk in a form that is straightforward to read."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, s in enumerate(segments):
        lines.append("=" * 78)
        lines.append("SEGMENT {i}  |  {ep}  |  {tier}  |  {mm}:{ss}  |  {w} words".format(
            i=i, ep=s["episode_id"], tier=s["tier"].upper(),
            mm=s["t_start"] // 60, ss=s["t_start"] % 60, w=s["words"]))
        lines.append("SHOW: {}   EPISODE: {}".format(s["show"], s["title"][:70]))
        lines.append("HINTED: {}".format(", ".join(s["companies_hinted"]) or "-"))
        lines.append("SIGNAL: {} disclosures, {} opinions (density {})".format(
            s["n_disclosures"], s["n_opinions"], s["density"]))
        lines.append("=" * 78)
        lines.append(s["text"])
        lines.append("")
    dest.write_text("\n".join(lines), encoding="utf-8")
    (dest.with_suffix(".index.json")).write_text(
        json.dumps([{k: v for k, v in s.items() if k != "text"} for s in segments],
                   indent=2), encoding="utf-8")
    return dest


# ------------------------------------------------------------------- load
def validate(rec: Dict) -> List[str]:
    errs = []
    if rec.get("stance") not in STANCES:
        errs.append("bad stance: %r" % rec.get("stance"))
    if rec.get("action_language") not in ACTIONS:
        errs.append("bad action_language: %r" % rec.get("action_language"))
    if rec.get("horizon") not in HORIZONS:
        errs.append("bad horizon: %r" % rec.get("horizon"))
    if not rec.get("company"):
        errs.append("missing company")
    if not rec.get("quote"):
        errs.append("missing quote")
    t = rec.get("ticker")
    if t is not None and (not t.isupper() or not (1 <= len(t) <= 5)):
        errs.append("suspicious ticker: %r" % t)
    return errs


def load_extractions(conn: sqlite3.Connection, records: List[Dict]) -> Dict:
    """Insert validated mentions. Rejects rather than guesses on bad input."""
    stats = {"accepted": 0, "rejected": 0, "errors": [], "episodes_touched": set()}
    now = utcnow()

    for rec in records:
        errs = validate(rec)
        if errs:
            stats["rejected"] += 1
            if len(stats["errors"]) < 10:
                stats["errors"].append({"company": rec.get("company"), "problems": errs})
            continue
        conn.execute(
            """INSERT INTO mentions
               (episode_id, speaker, speaker_role, ticker, resolved_name, resolve_conf,
                t_start, t_end, quote, stance, action_language, horizon,
                is_thesis, is_news_recap, is_hypothetical, extracted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["episode_id"], rec.get("speaker"), rec.get("speaker_role"),
             rec.get("ticker"), rec["company"], rec.get("resolve_confidence"),
             rec.get("t_start"), rec.get("t_end"), rec["quote"], rec["stance"],
             rec["action_language"], rec["horizon"],
             int(rec.get("is_thesis", False)), int(rec.get("is_news_recap", False)),
             int(rec.get("is_hypothetical", False)), now))
        stats["accepted"] += 1
        stats["episodes_touched"].add(rec["episode_id"])

    conn.commit()
    stats["episodes_touched"] = sorted(stats["episodes_touched"])
    return stats
