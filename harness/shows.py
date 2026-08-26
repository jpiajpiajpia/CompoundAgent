"""Which show is this episode?

The Compound publishes several formats through one RSS feed. They differ enough
that a single extraction prompt would underperform on all of them, so each
episode is classified at ingest time.

Weekday is the primary signal because the schedule is stable. Title shape is
recorded alongside it so episodes can be reclassified later without re-fetching
anything -- if the schedule shifts, we re-run classify() over stored rows.
"""
from __future__ import annotations

import json
from typing import Dict

from . import paths

_cfg: Dict = json.loads(paths.SHOWS_CONFIG.read_text())
CLASSIFICATION = _cfg["classification"]
SHOW_WEIGHTS = {k: v for k, v in _cfg["show_weights"].items() if k != "comment"}
FEEDS = [f for f in _cfg["feeds"] if f.get("enabled")]


def looks_like_guest_episode(title: str) -> bool:
    """Guest interviews name the guest in the title: '...with Michael Santoli'."""
    return any(m in title for m in CLASSIFICATION["guest_markers"])


def looks_like_topic_list(title: str) -> bool:
    """What Are Your Thoughts? titles enumerate the segment topics, comma
    separated: 'Treasury yields break out, Workday rumors, offsides...'"""
    return title.count(",") >= CLASSIFICATION["topic_list_min_commas"]


def classify(weekday: str, title: str, feed_id: str = "compound") -> str:
    """Return a show key. Weekday decides; title shape overrides when it
    disagrees strongly, which covers holiday shifts and one-off schedules."""
    if feed_id == "animal_spirits":
        return "animal_spirits"

    show = CLASSIFICATION["weekday_map"].get(weekday, "compound_other")

    # A comma-heavy title with no named guest is the WAYT format regardless of
    # which day it landed on.
    if looks_like_topic_list(title) and not looks_like_guest_episode(title):
        return "wayt"

    # Conversely a named guest on a Tuesday is an interview, not a topic list.
    if show == "wayt" and looks_like_guest_episode(title) and not looks_like_topic_list(title):
        return "tcaf"

    return show


def weight(show: str) -> float:
    return SHOW_WEIGHTS.get(show, 0.5)
