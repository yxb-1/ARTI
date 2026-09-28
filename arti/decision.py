"""Structured single-agent and one-round debate workflows."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .models import AgentAssessment, DebateTurn, EvidenceBundle, TurnRecord


class DebateFailure(Exception):
    def __init__(self, phase: str, cause: Exception, turns: list[TurnRecord]):
        super().__init__(f"{phase}: {type(cause).__name__}: {cause}")
        self.phase = phase
        self.cause = cause
        self.turns = list(turns)

def _prompt_after(heading: str) -> str:
    design = (Path(__file__).resolve().parents[1] / "PROMPTS.md").read_text(encoding="utf-8")
    section = design.split(heading, 1)[1]
    match = re.search(r"```xml\s*(<agent_instructions>.*?</agent_instructions>)\s*```", section, re.DOTALL)
    if not match:
        raise RuntimeError(f"missing prompt: {heading}")
    return match.group(1)


SINGLE = _prompt_after("## 模式一：单 Agent")
RESEARCHER = _prompt_after("### 研究 Agent R：固定 developer 消息")
CRITIC = _prompt_after("### 质疑 Agent C：固定 developer 消息")


def _xml_json(value):
    return escape(json.dumps(value, ensure_ascii=False, default=str), quote=True)


def _validate_ids(ids, bundle):
    valid = {item.id for item in bundle.evidence_items}
    invalid = set(ids) - valid
    if invalid:
        raise ValueError(f"unknown evidence IDs: {sorted(invalid)}")


def _response_schema(schema, bundle: EvidenceBundle):
    output = schema.model_json_schema()
    evidence_ids = sorted(item.id for item in bundle.evidence_items)
    if schema is DebateTurn:
        output["$defs"]["Claim"]["properties"]["evidence_ids"]["items"]["enum"] = evidence_ids
    else:
        for field in ("supporting_evidence", "counter_evidence"):
            output["properties"][field]["items"]["enum"] = evidence_ids
        signal_ids = sorted(signal.id for signal in bundle.analysis.signals)
        if signal_ids:
            output["properties"]["cited_signals"]["items"]["enum"] = signal_ids
        else:
            output["properties"]["cited_signals"]["maxItems"] = 0
    return output


def _call(client, model: str, developer: str, user: str, schema, bundle: EvidenceBundle):
    model_options = {"extra_body": {"enable_thinking": False}} if model.startswith("qwen3.8-") else {}
    response = client.chat.completions.create(
        model=model,
        # DashScope's OpenAI-compatible endpoint accepts system, not developer.
        messages=[{"role": "system", "content": developer}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {
            "name": schema.__name__, "strict": True, "schema": _response_schema(schema, bundle)}},
        temperature=0,
        **model_options,
    )
    choice = response.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ValueError(f"model did not complete: {choice.finish_reason}")
    if getattr(choice.message, "refusal", None):
        raise ValueError("model refused")
    return schema.model_validate_json(choice.message.content)


def assess(bundle: EvidenceBundle, mode: str, client, model: str):
    if mode not in ("single", "debate"):
        raise ValueError("mode must be single or debate")
    payload = _xml_json(bundle.model_dump(mode="json"))
    allowed_ids = _xml_json(sorted(item.id for item in bundle.evidence_items))
    if mode == "single":
        user = f'<case><request>依据本次证据生成 AgentAssessment。引用时只复制 allowed_evidence_ids 中的完整 ID。</request><allowed_evidence_ids format="json">{allowed_ids}</allowed_evidence_ids><evidence_bundle format="json">{payload}</evidence_bundle></case>'
        result = _call(client, model, SINGLE, user, AgentAssessment, bundle)
        _check_assessment(result, bundle, mode)
        return result, []
    turns = []
    for phase, role, prompt, schema in (
        ("opening", "R", RESEARCHER, DebateTurn),
        ("challenge", "C", CRITIC, DebateTurn),
        ("reply", "R", RESEARCHER, DebateTurn),
        ("verdict", "C", CRITIC, AgentAssessment),
    ):
        prior = _xml_json([t.content.model_dump(mode="json") for t in turns])
        user = f'<debate_case><phase>{phase}</phase><request>引用时只复制 allowed_evidence_ids 中的完整 ID。没有对应证据时使用空数组，不创建占位 ID。</request><allowed_evidence_ids format="json">{allowed_ids}</allowed_evidence_ids><evidence_bundle format="json">{payload}</evidence_bundle><prior_turns format="json">{prior}</prior_turns></debate_case>'
        try:
            result = _call(client, model, prompt, user, schema, bundle)
            if phase == "verdict":
                _check_assessment(result, bundle, mode)
                return result, turns
            if result.phase != phase:
                raise ValueError(f"wrong debate phase: {result.phase}")
            for claim in result.claims:
                _validate_ids(claim.evidence_ids, bundle)
            turns.append(TurnRecord(role=role, phase=phase, model=model,
                                    created_at=datetime.now(timezone.utc), content=result))
        except Exception as exc:
            raise DebateFailure(phase, exc, turns) from exc
    raise AssertionError("unreachable")


def _check_assessment(result, bundle, mode):
    if result.mode != mode or result.as_of != bundle.as_of:
        raise ValueError("assessment mode or as_of mismatch")
    _validate_ids(result.supporting_evidence + result.counter_evidence + result.cited_signals, bundle)
    signal_ids = {signal.id for signal in bundle.analysis.signals}
    if set(result.cited_signals) - signal_ids:
        raise ValueError("cited_signals contains non-signal ID")
