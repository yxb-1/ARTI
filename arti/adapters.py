"""Read only public market API adapters."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Market


def _number(value):
    if value is None or value == "":
        return None
    try:
        result = float(value)
        return result if result >= 0 and result != float("inf") else None
    except (TypeError, ValueError):
        return None


def _price(value):
    n = _number(value)
    return n if n is not None and n <= 1 else None


def _time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None


def _array(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _get(url, params):
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "ARTI/0.1"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            if attempt == 2 or (exc.code < 500 and exc.code != 429):
                raise
            time.sleep(1 + attempt)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def polymarket_record(raw: dict, observed_at: datetime) -> Market | None:
    outcomes, prices = _array(raw.get("outcomes")), _array(raw.get("outcomePrices"))
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    labels = [str(x).strip().lower() for x in outcomes]
    if sorted(labels) != ["no", "yes"]:
        return None
    source_id = str(raw.get("id") or "")
    if not source_id:
        return None
    yes_price = _price(prices[labels.index("yes")])
    # Gamma bestBid/bestAsk may describe a non-YES outcome; outcomePrices is explicit.
    return Market(
        market_id=f"polymarket:{source_id}", platform="polymarket", source_id=source_id,
        title=raw.get("question") or "", status="closed" if raw.get("closed") else "open",
        close_time=_time(raw.get("endDate")), source_updated_at=_time(raw.get("updatedAt")),
        yes_probability=yes_price, price_source="outcome_price" if yes_price is not None else None,
        volume_total=_number(raw.get("volumeNum") or raw.get("volume")),
        volume_24h=_number(raw.get("volume24hr")), volume_unit="USD",
        liquidity=_number(raw.get("liquidityNum") or raw.get("liquidity")), liquidity_unit="USD",
        observed_at=observed_at,
        source_url=f"https://polymarket.com/event/{raw['slug']}" if raw.get("slug") else f"https://gamma-api.polymarket.com/markets/{source_id}",
    )


def kalshi_record(raw: dict, observed_at: datetime) -> Market | None:
    source_id = raw.get("ticker")
    if not source_id or raw.get("market_type", "binary") != "binary":
        return None
    bid, ask, last = (_price(raw.get(k)) for k in ("yes_bid_dollars", "yes_ask_dollars", "last_price_dollars"))
    bid_size = _number(raw.get("yes_bid_size_fp"))
    ask_size = _number(raw.get("yes_ask_size_fp"))
    if bid is not None and ask is not None and bid <= ask and (bid > 0 or ask > 0):
        probability, source = (bid + ask) / 2, "bid_ask_midpoint"
    else:
        probability = last if last is not None and last > 0 else None
        source = "last_price" if probability is not None else None
        if bid is not None and ask is not None and bid > ask:
            bid = ask = None
    return Market(
        market_id=f"kalshi:{source_id}", platform="kalshi", source_id=source_id,
        title=raw.get("title") or raw.get("subtitle") or "", status=raw.get("status") or "unknown",
        close_time=_time(raw.get("close_time")), source_updated_at=_time(raw.get("updated_time")),
        yes_probability=probability, yes_bid=bid, yes_ask=ask, price_source=source,
        volume_total=_number(raw.get("volume_fp")), volume_24h=_number(raw.get("volume_24h_fp")),
        volume_unit="contracts", liquidity=min(bid_size, ask_size) if bid_size is not None and ask_size is not None else None,
        liquidity_unit="contracts_at_best_quotes",
        observed_at=observed_at, source_url=f"https://kalshi.com/markets/{source_id}",
    )


def fetch_markets(platform: str, limit: int = 100) -> tuple[list[Market], list[str]]:
    if limit < 1:
        raise ValueError("limit must be positive")
    observed_at = datetime.now(timezone.utc)
    records, errors = [], []
    cursor = None
    offset = 0
    pages = 0
    while len(records) < limit and pages < 5:
        try:
            if platform == "polymarket":
                page = _get("https://gamma-api.polymarket.com/markets", {"active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false", "limit": min(100, limit - len(records)), "offset": offset})
                raw_items = page
                offset += len(raw_items)
                next_cursor = None
            elif platform == "kalshi":
                params = {"status": "open", "mve_filter": "exclude", "limit": min(1000, limit - len(records))}
                if cursor:
                    params["cursor"] = cursor
                page = _get("https://external-api.kalshi.com/trade-api/v2/markets", params)
                raw_items, next_cursor = page["markets"], page.get("cursor")
            else:
                raise ValueError(f"unknown platform: {platform}")
        except Exception as exc:
            errors.append(f"{platform} request failed: {type(exc).__name__}: {exc}")
            break
        for raw in raw_items:
            try:
                item = (polymarket_record if platform == "polymarket" else kalshi_record)(raw, observed_at)
                if item:
                    records.append(item)
            except Exception as exc:
                errors.append(f"{platform} record skipped: {type(exc).__name__}: {exc}")
        pages += 1
        if not raw_items or (platform == "kalshi" and not next_cursor):
            break
        if platform == "kalshi":
            if next_cursor == cursor:
                break
            cursor = next_cursor
        if len(records) >= limit:
            break
    return records[:limit], errors
