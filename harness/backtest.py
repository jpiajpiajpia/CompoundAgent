"""Phase 0 -- Replay the picks against real prices.

Answers the only question that matters before funding anything: did acting on
these picks, under the exact stop rules we intend to trade, beat simply holding
the S&P over the same periods?

Deliberately honest about the simulation:
  * Entry is the NEXT session's open after the episode published. You cannot
    buy during the episode.
  * The stop is checked against each day's LOW, not its close, so intraday
    breaches count.
  * If a session OPENS below the stop, the exit fills at that open, not at the
    stop price. That is the gap-through case, and pretending otherwise would
    flatter the results.
  * The high water mark that drives the ratchet is taken off CLOSES, matching
    the live design -- a single wick should not ratchet the stop.
  * Each pick is benchmarked against SPY bought and sold on the same two dates,
    so the comparison holds holding-period constant.

No ranker is applied here. This is the naive 'act on every bullish thesis'
baseline that the plan says a real ranker must beat.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from . import paths

INITIAL_STOP_PCT = 0.07
BREAKEVEN_TRIGGER_PCT = 0.07
TRAIL_PCT = 0.07
TIME_STOP_SESSIONS = 20


def load_prices() -> Dict[str, List[Dict]]:
    return json.loads((paths.DATA / "prices" / "daily.json").read_text())


def _entry_index(bars: List[Dict], after_date: str) -> Optional[int]:
    """First session strictly after the episode date."""
    for i, b in enumerate(bars):
        if b["d"] > after_date:
            return i
    return None


def simulate(bars: List[Dict], entry_i: int,
             time_stop: int = TIME_STOP_SESSIONS) -> Dict:
    entry = bars[entry_i]["o"]
    stop = entry * (1 - INITIAL_STOP_PCT)
    hwm = entry
    ratcheted = False

    last = min(entry_i + time_stop, len(bars) - 1)
    for i in range(entry_i, last + 1):
        b = bars[i]

        # Gap-through: the session opened already below the stop.
        if i > entry_i and b["o"] <= stop:
            return {"exit_date": b["d"], "exit": b["o"], "reason": "gap_through",
                    "sessions": i - entry_i, "entry": entry, "ratcheted": ratcheted}

        # Intraday breach.
        if i > entry_i and b["l"] <= stop:
            return {"exit_date": b["d"], "exit": stop, "reason": "stop",
                    "sessions": i - entry_i, "entry": entry, "ratcheted": ratcheted}

        # Ratchet off the close, after the stop check for that day.
        if b["c"] > hwm:
            hwm = b["c"]
        if hwm >= entry * (1 + BREAKEVEN_TRIGGER_PCT):
            new_stop = max(entry, hwm * (1 - TRAIL_PCT))
            if new_stop > stop:
                stop = new_stop
                ratcheted = True

    b = bars[last]
    reason = "time_stop" if last == entry_i + time_stop else "still_open"
    return {"exit_date": b["d"], "exit": b["c"], "reason": reason,
            "sessions": last - entry_i, "entry": entry, "ratcheted": ratcheted}


def benchmark(spy: List[Dict], entry_date: str, exit_date: str) -> Optional[float]:
    """SPY return over exactly the same two dates."""
    e = next((b for b in spy if b["d"] == entry_date), None)
    x = next((b for b in spy if b["d"] == exit_date), None)
    if not e or not x:
        return None
    return (x["c"] - e["o"]) / e["o"]


def run(picks: List[Dict]) -> Dict:
    prices = load_prices()
    spy = prices.get("SPY", [])
    trades, skipped = [], []

    for p in picks:
        bars = prices.get(p["ticker"])
        if not bars:
            skipped.append({**p, "why": "no price data"})
            continue
        ei = _entry_index(bars, p["date"])
        if ei is None:
            skipped.append({**p, "why": "no session after episode"})
            continue

        r = simulate(bars, ei)
        ret = (r["exit"] - r["entry"]) / r["entry"]
        bench = benchmark(spy, bars[ei]["d"], r["exit_date"])
        trades.append({
            "ticker": p["ticker"], "episode_date": p["date"], "stance": p["stance"],
            "entry_date": bars[ei]["d"], "entry": round(r["entry"], 2),
            "exit_date": r["exit_date"], "exit": round(r["exit"], 2),
            "reason": r["reason"], "sessions": r["sessions"],
            "ratcheted": r["ratcheted"],
            "ret": ret, "spy_ret": bench,
            "excess": (ret - bench) if bench is not None else None,
        })

    n = len(trades)
    wins = [t for t in trades if t["ret"] > 0]
    rets = [t["ret"] for t in trades]
    excess = [t["excess"] for t in trades if t["excess"] is not None]
    return {
        "trades": trades, "skipped": skipped, "n": n,
        "win_rate": len(wins) / n if n else 0,
        "mean_ret": sum(rets) / n if n else 0,
        "total_ret": sum(rets),
        "mean_excess": sum(excess) / len(excess) if excess else 0,
        "beat_spy": sum(1 for e in excess if e > 0),
        "by_reason": {r: sum(1 for t in trades if t["reason"] == r)
                      for r in {t["reason"] for t in trades}},
    }
