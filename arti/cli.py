from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .adapters import fetch_markets
from .analysis import analyze, evidence_bundle
from .decision import assess
from .models import Report
from .storage import SnapshotStore

ROOT = Path(__file__).resolve().parents[2]


def run(args):
    load_dotenv(ROOT / ".env")
    store = SnapshotStore(args.db)
    client = None
    model = args.model or os.getenv("ARTI_MODEL")
    if args.mode != "none" and model and os.getenv("DASHSCOPE_API_KEY") and os.getenv("DASHSCOPE_BASE_URL"):
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["DASHSCOPE_API_KEY"], base_url=os.environ["DASHSCOPE_BASE_URL"], timeout=30, max_retries=0)
    reports = []
    for _ in range(args.poll_count):
        markets, fetch_errors = [], []
        for platform in args.platforms:
            print(f"Fetching {platform} markets (limit {args.limit})...", file=sys.stderr, flush=True)
            batch, errors = fetch_markets(platform, args.limit)
            markets.extend(batch)
            fetch_errors.extend(errors)
        for error in fetch_errors:
            print(error, file=sys.stderr)
        histories = {market.market_id: store.history(market.market_id, market.observed_at) for market in markets}
        store.save(markets)
        for index, market in enumerate(markets, 1):
            print(f"Analyzing {index}/{len(markets)}: {market.market_id}", file=sys.stderr, flush=True)
            history = histories[market.market_id]
            analysis = analyze(market, history, jump_pp=args.jump_pp)
            report = Report(market=market, analysis=analysis)
            if args.mode != "none":
                if client is None:
                    report.assessment_error = "model unavailable: configure ARTI_MODEL and DASHSCOPE_API_KEY/BASE_URL"
                elif analysis.data_quality != "excluded":
                    try:
                        print(f"Assessing {market.market_id} ({args.mode})...", file=sys.stderr, flush=True)
                        report.assessment, report.debate_turns = assess(evidence_bundle(market, history, analysis), args.mode, client, model)
                    except Exception as exc:
                        report.assessment_error = f"{type(exc).__name__}: {exc}"
                else:
                    report.assessment_error = "market excluded by data quality filter"
            reports.append(report)
        if args.poll_count > 1 and _ < args.poll_count - 1:
            time.sleep(args.interval)
    reports.sort(key=lambda x: (x.market.platform, -(x.analysis.attention_score or 0)))
    result = {"reports": [r.model_dump(mode="json") for r in reports], "fetch_errors": fetch_errors}
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0 if reports else 1


def main():
    parser = argparse.ArgumentParser(description="ARTI public prediction market monitor")
    parser.add_argument("--platforms", nargs="+", choices=["polymarket", "kalshi"], default=["polymarket", "kalshi"])
    parser.add_argument("--mode", choices=["none", "single", "debate"], default="none")
    parser.add_argument("--model", help="model name; defaults to ARTI_MODEL from environment")
    parser.add_argument("--limit", type=int, default=20, help="market count per platform")
    parser.add_argument("--db", default=str(ROOT / "ARTI" / "arti.sqlite3"))
    parser.add_argument("--output")
    parser.add_argument("--jump-pp", type=float, default=8)
    parser.add_argument("--poll-count", type=int, default=1)
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    if args.limit < 1 or args.poll_count < 1 or args.interval < 1 or args.jump_pp <= 0:
        parser.error("limit, poll-count, interval, and jump-pp must be positive")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
