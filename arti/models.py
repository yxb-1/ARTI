from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Market(StrictModel):
    market_id: str
    platform: Literal["polymarket", "kalshi"]
    source_id: str
    title: str
    status: str
    close_time: datetime | None = None
    source_updated_at: datetime | None = None
    yes_probability: float | None = Field(default=None, ge=0, le=1)
    yes_bid: float | None = Field(default=None, ge=0, le=1)
    yes_ask: float | None = Field(default=None, ge=0, le=1)
    price_source: str | None = None
    volume_total: float | None = Field(default=None, ge=0)
    volume_24h: float | None = Field(default=None, ge=0)
    volume_unit: str | None = None
    liquidity: float | None = Field(default=None, ge=0)
    liquidity_unit: str | None = None
    observed_at: datetime
    source_url: str

    @model_validator(mode="after")
    def check_identity(self):
        if self.market_id != f"{self.platform}:{self.source_id}":
            raise ValueError("market_id must contain platform and source_id")
        if self.yes_bid is not None and self.yes_ask is not None and self.yes_bid > self.yes_ask:
            raise ValueError("crossed YES quotes")
        return self


class Change(StrictModel):
    window_minutes: int
    start_probability: float
    end_probability: float
    change_pp: float
    actual_minutes: float
    start_at: datetime
    end_at: datetime
    start_price_source: str | None
    end_price_source: str | None


class Signal(StrictModel):
    id: str
    type: str
    severity: Literal["low", "medium", "high"]
    evidence: dict


class Analysis(StrictModel):
    attention_score: float | None
    changes: dict[str, Change]
    signals: list[Signal]
    data_quality: Literal["adequate", "limited", "excluded"]
    quality_issues: list[str]


class EvidenceItem(StrictModel):
    id: str
    value: float | str | None
    unit: str | None = None
    window_minutes: int | None = None
    source: str


class EvidenceBundle(StrictModel):
    market: Market
    as_of: datetime
    history: list[Market]
    analysis: Analysis
    evidence_items: list[EvidenceItem]


class AgentAssessment(StrictModel):
    mode: Literal["single", "debate"]
    as_of: datetime
    status: Literal["watch", "investigate", "avoid", "insufficient_evidence"]
    summary: str
    supporting_evidence: list[str]
    counter_evidence: list[str]
    open_questions: list[str]
    risks: list[str]
    cited_signals: list[str]


class Claim(StrictModel):
    text: str
    evidence_ids: list[str]


class DebateTurn(StrictModel):
    phase: Literal["opening", "challenge", "reply"]
    position: str
    claims: list[Claim]
    limitations: list[str]
    questions: list[str]


class TurnRecord(StrictModel):
    role: Literal["R", "C"]
    phase: str
    model: str
    created_at: datetime
    content: DebateTurn


class Report(StrictModel):
    market: Market
    analysis: Analysis
    assessment: AgentAssessment | None = None
    assessment_error: str | None = None
    debate_turns: list[TurnRecord] = Field(default_factory=list)
