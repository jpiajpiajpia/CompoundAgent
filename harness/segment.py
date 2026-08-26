"""Stage 03a -- Find the parts of an episode worth extracting from.

Roughly two-thirds of a typical episode is macro talk with no company in it.
Feeding whole transcripts to a model is wasteful and dilutes precision, so we
first locate the pockets where companies are actually discussed.

Also splits on the '>>' markers YouTube inserts at speaker changes. That gives
turn boundaries without names -- on a two-host show, alternating turns recover
much of "who said it" for free.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from . import paths

# Broad net: company names and obvious ticker-shaped tokens. Precision comes
# later from the model; this stage only needs recall to locate segments.
COMPANY_HINT = re.compile(
    r"\b(Nvidia|Palantir|Apple|Tesla|Amazon|Microsoft|Google|Alphabet|Meta|Netflix|"
    r"Broadcom|Micron|Oracle|Robinhood|Coinbase|AMD|Costco|Airbnb|Workday|Schwab|"
    r"IBM|Uber|Lyft|Disney|Walmart|Target|Ford|Intel|Qualcomm|Salesforce|Adobe|"
    r"Shopify|Snowflake|Datadog|Cloudflare|Crowdstrike|Arista|Vertiv|Eaton|Caterpillar|"
    r"Goldman|Morgan Stanley|JPMorgan|Berkshire|Exxon|Chevron|Pfizer|Merck|Lilly|"
    r"SpaceX|OpenAI|Anthropic|Paramount|Warner|Comcast|Verizon|Starbucks|Nike|"
    r"Boeing|Lockheed|Palo Alto|Fortinet|ServiceNow|Snap|Pinterest|Roblox|Unity|"
    r"Spotify|Block|PayPal|Visa|Mastercard|Rocket|Zillow|Carvana|Chipotle|Cava)\b",
    re.I,
)
TICKERISH = re.compile(r"\b(?:QQQ|SPY|IWM|SMH|XL[A-Z]|TQQQ|SOXL|ARKK|GLD|TLT|VIX)\b")


def load_cues(transcript_path: str) -> List[Dict]:
    return json.load(open(paths.ROOT / transcript_path, encoding="utf-8"))["cues"]


def split_turns(cues: List[Dict]) -> List[Dict]:
    """Group cues into speaker turns using the '>>' change markers."""
    turns: List[Dict] = []
    cur = {"t": cues[0]["t"] if cues else 0.0, "text": ""}
    for cue in cues:
        parts = cue["text"].split(">>")
        for i, part in enumerate(parts):
            if i > 0:  # a '>>' means the speaker changed here
                if cur["text"].strip():
                    turns.append({"t": cur["t"], "text": cur["text"].strip()})
                cur = {"t": cue["t"], "text": ""}
            cur["text"] += " " + part
    if cur["text"].strip():
        turns.append({"t": cur["t"], "text": cur["text"].strip()})
    return turns


def assign_speakers(turns: List[Dict], n_speakers: int = 2) -> List[Dict]:
    """Alternate speaker labels across turns.

    Honest about what this is: an approximation. It holds up on the two-host
    Tuesday show and degrades on guest episodes, so downstream consumers should
    treat speaker as a weak feature, never as ground truth.
    """
    for i, t in enumerate(turns):
        t["speaker_idx"] = i % n_speakers
    return turns


def find_dense_segments(turns: List[Dict], window_turns: int = 12,
                        min_hits: int = 2, merge_gap: int = 6) -> List[Dict]:
    """Return contiguous stretches of turns where companies are discussed."""
    hits = []
    for i, t in enumerate(turns):
        n = len(COMPANY_HINT.findall(t["text"])) + len(TICKERISH.findall(t["text"]))
        if n:
            hits.append(i)
    if not hits:
        return []

    # cluster hit indices that sit close together
    clusters: List[List[int]] = [[hits[0]]]
    for h in hits[1:]:
        if h - clusters[-1][-1] <= merge_gap:
            clusters[-1].append(h)
        else:
            clusters.append([h])

    segments = []
    for cl in clusters:
        lo = max(0, cl[0] - 2)                      # a little lead-in for context
        hi = min(len(turns) - 1, cl[-1] + 2)
        text = " ".join(t["text"] for t in turns[lo:hi + 1])
        names = sorted({m.title() for m in COMPANY_HINT.findall(text)})
        if len(cl) < min_hits:
            continue
        segments.append({
            "t_start": turns[lo]["t"],
            "t_end": turns[hi]["t"],
            "turn_range": [lo, hi],
            "words": len(text.split()),
            "companies_hinted": names,
            "text": text,
        })
    return segments


def segment_episode(transcript_path: str, n_speakers: int = 2) -> Dict:
    cues = load_cues(transcript_path)
    turns = assign_speakers(split_turns(cues), n_speakers)
    segs = find_dense_segments(turns)
    total_words = sum(len(t["text"].split()) for t in turns)
    seg_words = sum(s["words"] for s in segs)
    return {
        "turns": len(turns),
        "total_words": total_words,
        "segments": segs,
        "segment_words": seg_words,
        "coverage_pct": round(100.0 * seg_words / max(total_words, 1), 1),
    }


# --------------------------------------------------------------- tiering
# Not all segments are worth the same. A host stating their own position is the
# strongest signal available in this data -- it is skin in the game, often with
# an entry price attached. Explicit like/dislike is next. Segments that merely
# name companies with no first-person view are mostly news recap.
#
# Measured across the corpus: 110 disclosure / 195 opinion / 248 plain.

DISCLOSURE = re.compile(
    r"\b(i (bought|own|sold|added|picked up|grabbed|started a position|"
    r"took a position|trimmed|shorted)"
    r"|i'?m (buying|selling|long|short|adding|in this)"
    r"|i have a (stop|position)"
    r"|we (bought|own|added)"
    r"|my (position|stop|cost basis)"
    r"|(bought|added) (it|this|more|to it)"
    r"|still (own|long|in) (it|this))\b", re.I)

OPINION = re.compile(
    r"\b(i (like|love|hate|don'?t like)"
    r"|i think (it|this|they)"
    r"|best (stock|idea|name)"
    r"|my (favorite|top) (stock|pick|name)"
    r"|would (buy|own)"
    r"|i'?d (buy|own|avoid))\b", re.I)


def classify_tier(text: str) -> str:
    """disclosure > opinion > plain."""
    if DISCLOSURE.search(text):
        return "disclosure"
    if OPINION.search(text):
        return "opinion"
    return "plain"
