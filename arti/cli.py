from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, ValidationError

from .adapters import fetch_markets
from .analysis import analyze, evidence_bundle
from .decision import assess
from .models import Report, StrictModel
from .storage import SnapshotStore

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "ARTI" / "config.json"


class RunSettings(StrictModel):
    platforms: list[Literal["polymarket", "kalshi"]] = Field(min_length=1)
    mode: Literal["none", "single", "debate"]
    model: str | None
    limit: int = Field(gt=0)
    db: str
    output: str
    jump_pp: float = Field(gt=0)
    poll_count: int = Field(gt=0)
    interval: int = Field(gt=0)


def _workspace_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def _provider_rejected_content(exc: Exception) -> bool:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        code = body.get("code")
        nested = body.get("error")
        if code == "data_inspection_failed" or (isinstance(nested, dict) and nested.get("code") == "data_inspection_failed"):
            return True
    return "data_inspection_failed" in str(exc)


def run(args):
    load_dotenv(ROOT / ".env")
    store = SnapshotStore(_workspace_path(args.db))
    client = None
    model = args.model or os.getenv("ARTI_MODEL")
    if args.mode != "none" and model and os.getenv("DASHSCOPE_API_KEY") and os.getenv("DASHSCOPE_BASE_URL"):
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["DASHSCOPE_API_KEY"], base_url=os.environ["DASHSCOPE_BASE_URL"], timeout=30, max_retries=0)
    reports = []
    all_fetch_errors = []
    skipped_assessments = []
    for _ in range(args.poll_count):
        candidates, fetch_errors = [], []
        for platform in args.platforms:
            pool_size = max(args.limit, 500 if platform == "kalshi" else 100)
            print(f"Fetching {platform} candidates (up to {pool_size})...", file=sys.stderr, flush=True)
        with ThreadPoolExecutor(max_workers=len(args.platforms)) as pool:
            for batch, errors in pool.map(
                lambda platform: fetch_markets(platform, max(args.limit, 500 if platform == "kalshi" else 100)), args.platforms
            ):
                candidates.extend(batch)
                fetch_errors.extend(errors)
        for error in fetch_errors:
            print(error, file=sys.stderr)
        all_fetch_errors.extend(fetch_errors)
        histories = {market.market_id: store.history(market.market_id, market.observed_at) for market in candidates}
        analyses = {market.market_id: analyze(market, histories[market.market_id], jump_pp=args.jump_pp) for market in candidates}
        store.save(candidates)
        for platform in args.platforms:
            ranked = sorted(
                (market for market in candidates if market.platform == platform and analyses[market.market_id].data_quality != "excluded"),
                key=lambda market: -(analyses[market.market_id].attention_score or 0),
            )
            selected = 0
            attempts = 0
            max_attempts = args.limit if args.mode == "none" else args.limit + 3
            for market in ranked:
                if selected >= args.limit or attempts >= max_attempts:
                    break
                attempts += 1
                print(f"Analyzing {platform} candidate {attempts}: {market.market_id}", file=sys.stderr, flush=True)
                history = histories[market.market_id]
                analysis = analyses[market.market_id]
                report = Report(market=market, analysis=analysis)
                if args.mode != "none" and client is None:
                    report.assessment_error = "model unavailable: configure model and DASHSCOPE_API_KEY/BASE_URL"
                elif args.mode != "none":
                    try:
                        print(f"Assessing {market.market_id} ({args.mode})...", file=sys.stderr, flush=True)
                        report.assessment, report.debate_turns = assess(evidence_bundle(market, history, analysis), args.mode, client, model)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        if _provider_rejected_content(exc):
                            report.assessment_error = error
                            skipped_assessments.append(report.model_dump(mode="json"))
                            print(f"Provider rejected {market.market_id}; trying next candidate", file=sys.stderr, flush=True)
                            continue
                        report.assessment_error = error
                reports.append(report)
                selected += 1
            print(f"Selected {selected} of {sum(m.platform == platform for m in candidates)} {platform} candidates", file=sys.stderr, flush=True)
        if args.poll_count > 1 and _ < args.poll_count - 1:
            time.sleep(args.interval)
    reports.sort(key=lambda x: (x.market.platform, -(x.analysis.attention_score or 0)))
    settings = {key: str(getattr(args, key)) if isinstance(getattr(args, key), Path) else getattr(args, key)
                for key in RunSettings.model_fields}
    result = {"settings": settings, "reports": [r.model_dump(mode="json") for r in reports],
              "skipped_assessments": skipped_assessments, "fetch_errors": all_fetch_errors}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    output_path = _workspace_path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output + "\n", encoding="utf-8")
    print(f"Report saved to {output_path}", file=sys.stderr)
    return 0 if reports else 1


def parse_args(argv=None):
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    config_args, _ = config_parser.parse_known_args(argv)
    config_path = Path(config_args.config).expanduser()
    try:
        settings = RunSettings.model_validate_json(config_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        config_parser.error(f"cannot load config {config_path}: {exc}")
    parser = argparse.ArgumentParser(description="ARTI public prediction market monitor")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="JSON settings file")
    parser.add_argument("--platforms", nargs="+", choices=["polymarket", "kalshi"], default=settings.platforms)
    parser.add_argument("--mode", choices=["none", "single", "debate"], default=settings.mode)
    parser.add_argument("--model", default=settings.model, help="model name")
    parser.add_argument("--limit", type=int, default=settings.limit, help="selected markets per platform")
    parser.add_argument("--db", default=settings.db)
    parser.add_argument("--output", default=settings.output, help="JSON report path")
    parser.add_argument("--jump-pp", type=float, default=settings.jump_pp)
    parser.add_argument("--poll-count", type=int, default=settings.poll_count)
    parser.add_argument("--interval", type=int, default=settings.interval)
    args = parser.parse_args(argv)
    if args.limit < 1 or args.poll_count < 1 or args.interval < 1 or args.jump_pp <= 0:
        parser.error("limit, poll-count, interval, and jump-pp must be positive")
    return args


def main():
    args = parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
