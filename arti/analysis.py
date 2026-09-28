from __future__ import annotations

from datetime import timedelta, timezone
from math import log1p
from statistics import median

from .models import Analysis, Change, EvidenceBundle, EvidenceItem, Market, Signal

WINDOWS = (5, 60, 1440)


def analyze(market: Market, history: list[Market], *, jump_pp: float = 8, max_stale_minutes: int = 120) -> Analysis:
    issues = []
    now = market.observed_at
    if market.status.lower() not in ("open", "active"):
        issues.append("market_not_open")
    if market.yes_probability is None:
        issues.append("missing_yes_price")
    if market.source_updated_at and now - market.source_updated_at > timedelta(minutes=max_stale_minutes):
        issues.append("stale_source_update")
    if market.close_time and market.close_time <= now:
        issues.append("past_close_time")
    spread = market.yes_ask - market.yes_bid if market.yes_bid is not None and market.yes_ask is not None else None
    if spread is not None and spread >= 0.5 and (market.volume_24h or 0) == 0:
        issues.append("unusable_wide_spread")
    excluded = any(x in issues for x in ("market_not_open", "missing_yes_price", "stale_source_update", "past_close_time", "unusable_wide_spread"))
    changes = {}
    valid = [h for h in history if h.yes_probability is not None and h.observed_at < now]
    for minutes in WINDOWS:
        target = now - timedelta(minutes=minutes)
        tolerance = timedelta(minutes=max(2, minutes * 0.25))
        candidates = [h for h in valid if abs(h.observed_at - target) <= tolerance]
        if not candidates:
            continue
        old = min(candidates, key=lambda h: abs(h.observed_at - target))
        actual = (now - old.observed_at).total_seconds() / 60
        changes[f"{minutes}m"] = Change(
            window_minutes=minutes, start_probability=old.yes_probability,
            end_probability=market.yes_probability, change_pp=(market.yes_probability - old.yes_probability) * 100,
            actual_minutes=actual, start_at=old.observed_at, end_at=now,
            start_price_source=old.price_source, end_price_source=market.price_source,
        )
    signals = []
    for key, change in changes.items():
        if abs(change.change_pp) >= jump_pp:
            signals.append(Signal(id=f"signal:probability_jump:{key}", type="probability_jump", severity="high",
                evidence={"change_pp": change.change_pp, "start_probability": change.start_probability,
                          "end_probability": change.end_probability, "actual_minutes": change.actual_minutes,
                          "threshold_pp": jump_pp}))
    if market.yes_bid is None or market.yes_ask is None or (market.yes_bid == 0 and market.yes_ask == 0):
        issues.append("missing_quotes")
        signals.append(Signal(id="signal:quote_quality", type="quote_quality", severity="low",
                              evidence={"yes_bid": market.yes_bid, "yes_ask": market.yes_ask, "observed_at": now.isoformat()}))
    elif market.yes_ask - market.yes_bid >= 0.1:
        signals.append(Signal(id="signal:wide_spread", type="quote_quality", severity="medium",
                              evidence={"yes_bid": market.yes_bid, "yes_ask": market.yes_ask,
                                        "spread": market.yes_ask - market.yes_bid, "threshold": 0.1}))
    if "stale_source_update" in issues:
        signals.append(Signal(id="signal:stale_quote", type="quote_quality", severity="medium",
                              evidence={"source_updated_at": market.source_updated_at.isoformat(), "observed_at": now.isoformat()}))
    if changes and any(abs(c.change_pp) >= jump_pp for c in changes.values()):
        if (market.volume_24h is not None and market.volume_24h < 10) or (market.liquidity is not None and market.liquidity < 10):
            signals.append(Signal(id="signal:low_liquidity_jump", type="low_liquidity_jump", severity="medium",
                                  evidence={"volume_24h": market.volume_24h, "volume_unit": market.volume_unit,
                                            "liquidity": market.liquidity, "liquidity_unit": market.liquidity_unit}))
        if market.close_time and timedelta(0) < market.close_time - now <= timedelta(hours=24):
            signals.append(Signal(id="signal:near_close_move", type="near_close_move", severity="medium",
                                  evidence={"hours_remaining": (market.close_time-now).total_seconds()/3600,
                                            "changes_pp": {k: c.change_pp for k, c in changes.items()}}))
    # Compare equal-cadence cumulative volume increments within this one market only.
    volumes = [h for h in history if h.volume_total is not None and h.volume_unit == market.volume_unit]
    volumes = sorted(volumes, key=lambda h: h.observed_at) + [market]
    if market.volume_total is not None and market.volume_unit and len(volumes) >= 5:
        pairs = list(zip(volumes, volumes[1:]))
        latest_minutes = (pairs[-1][1].observed_at - pairs[-1][0].observed_at).total_seconds() / 60
        comparable = [
            (b.volume_total - a.volume_total, (b.observed_at-a.observed_at).total_seconds()/60)
            for a, b in pairs
            if a.volume_total is not None and b.volume_total is not None
            and b.volume_total >= a.volume_total and b.observed_at > a.observed_at
        ]
        baseline = [delta for delta, minutes in comparable[:-1] if latest_minutes > 0 and latest_minutes/2 <= minutes <= latest_minutes*2]
        latest_pair = pairs[-1]
        if (len(baseline) >= 3 and latest_pair[1].volume_total >= latest_pair[0].volume_total
                and latest_minutes > 0):
            recent = latest_pair[1].volume_total - latest_pair[0].volume_total
            typical = median(baseline)
            if recent >= 10 and recent >= max(3 * typical, typical + 10):
                signals.append(Signal(id="signal:volume_spike", type="volume_spike", severity="medium",
                                      evidence={"recent_delta": recent, "baseline_median": typical,
                                                "baseline_samples": len(baseline), "interval_minutes": latest_minutes,
                                                "unit": market.volume_unit, "multiplier_threshold": 3}))
    if not valid:
        issues.append("insufficient_history")
    if market.volume_24h is None:
        issues.append("missing_volume_24h")
    if market.liquidity is None:
        issues.append("missing_liquidity")
    score = None
    if not excluded:
        score = min(100, round(
            20 + min(25, 5 * log1p(market.volume_24h or 0))
            + (15 * max(0, 1 - spread / 0.2) if spread is not None else 0)
            + min(15, 3 * log1p(market.liquidity or 0))
            + (10 if market.close_time and timedelta(0) < market.close_time-now <= timedelta(days=7) else 0)
            + min(15, max((abs(c.change_pp) for c in changes.values()), default=0)), 1))
    return Analysis(attention_score=score, changes=changes, signals=signals,
                    data_quality="excluded" if excluded else "limited" if issues else "adequate", quality_issues=issues)


def evidence_bundle(market: Market, history: list[Market], analysis: Analysis) -> EvidenceBundle:
    items = [EvidenceItem(id="metric:yes_probability", value=market.yes_probability, unit="probability", source=market.price_source or "missing")]
    for key, change in analysis.changes.items():
        items.append(EvidenceItem(id=f"change:{key}", value=change.change_pp, unit="percentage_points",
                                  window_minutes=change.window_minutes, source="snapshot_history"))
    for signal in analysis.signals:
        items.append(EvidenceItem(id=signal.id, value=signal.type, source="deterministic_rule"))
    return EvidenceBundle(market=market, as_of=market.observed_at, history=history, analysis=analysis, evidence_items=items)
