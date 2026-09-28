import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from arti.adapters import kalshi_record, polymarket_record
from arti.analysis import analyze, evidence_bundle
from arti.decision import assess
from arti.models import Market
from arti.storage import SnapshotStore

NOW = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)


def market(at=NOW, price=0.6, volume=100):
    return Market(market_id="kalshi:T", platform="kalshi", source_id="T", title="Question?", status="open",
                  yes_probability=price, yes_bid=price - 0.01, yes_ask=price + 0.01,
                  price_source="bid_ask_midpoint", volume_total=volume, volume_24h=20,
                  volume_unit="contracts", liquidity=100, liquidity_unit="contracts_open_interest",
                  observed_at=at, source_updated_at=at, source_url="https://example.com")


def test_polymarket_yes_mapping_and_rejection():
    raw = {"id": "1", "question": "Q", "outcomes": '["No", "Yes"]', "outcomePrices": '["0.7", "0.3"]'}
    result = polymarket_record(raw, NOW)
    assert result.yes_probability == 0.3
    assert result.price_source == "outcome_price"
    assert polymarket_record({**raw, "outcomes": '["A", "B"]'}, NOW) is None


def test_kalshi_midpoint_fallback_and_units():
    raw = {"ticker": "T", "title": "Q", "status": "open", "yes_bid_dollars": "0.50",
           "yes_ask_dollars": "0.60", "last_price_dollars": "0.40", "volume_fp": "12.00"}
    result = kalshi_record(raw, NOW)
    assert result.yes_probability == pytest.approx(0.55)
    assert result.volume_unit == "contracts"
    assert kalshi_record({**raw, "yes_ask_dollars": None}, NOW).price_source == "last_price"


def test_snapshot_window_and_history_insufficient(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots.db")
    old = market(NOW - timedelta(minutes=5), 0.5)
    current = market()
    assert "insufficient_history" in analyze(current, []).quality_issues
    store.save([old])
    history = store.history(current.market_id, current.observed_at)
    result = analyze(current, history)
    assert result.changes["5m"].change_pp == pytest.approx(10)
    assert result.signals[0].id == "signal:probability_jump:5m"
    far = market(NOW - timedelta(minutes=20), 0.4)
    assert "5m" not in analyze(current, [far]).changes


def test_volume_spike_requires_comparable_baseline():
    history = [market(NOW - timedelta(minutes=20 - i * 5), 0.5, volume)
               for i, volume in enumerate((100, 102, 104, 106))]
    current = market(NOW, 0.6, 130)
    result = analyze(current, history)
    spike = next(signal for signal in result.signals if signal.type == "volume_spike")
    assert spike.evidence["recent_delta"] == 24
    assert spike.evidence["baseline_samples"] == 3
    reset = market(NOW, 0.6, 5)
    assert not any(s.type == "volume_spike" for s in analyze(reset, history).signals)


class FakeClient:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(next(self.payloads)), refusal=None))])


def test_single_and_debate_contracts():
    item = market()
    bundle = evidence_bundle(item, [], analyze(item, []))
    base = {"as_of": NOW.isoformat(), "status": "watch", "summary": "Observe.",
            "supporting_evidence": ["metric:yes_probability"], "counter_evidence": [],
            "open_questions": [], "risks": [], "cited_signals": []}
    single, turns = assess(bundle, "single", FakeClient([{**base, "mode": "single"}]), "test")
    assert single.mode == "single" and turns == []
    phases = ["opening", "challenge", "reply"]
    payloads = [{"phase": phase, "position": "uncertain", "claims": [{"text": "price", "evidence_ids": ["metric:yes_probability"]}],
                 "limitations": [], "questions": []} for phase in phases]
    verdict, turns = assess(bundle, "debate", FakeClient(payloads + [{**base, "mode": "debate"}]), "test")
    assert verdict.mode == "debate" and [t.phase for t in turns] == phases
    with pytest.raises(ValueError, match="unknown evidence"):
        assess(bundle, "single", FakeClient([{**base, "mode": "single", "supporting_evidence": ["invented"]}]), "test")
