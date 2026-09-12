#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public market-data access for linear USDT-margined perpetual research.

Only Binance USD-M public endpoints are represented here. This module does
not accept API credentials and contains no account or order endpoints.
"""

import math
import re
import time
import urllib.parse
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import backtest_data
import signal_watch as sw


BASE_URL = "https://fapi.binance.com"
MARKET_TYPE = "linear_perpetual"
PERPETUAL_HTTP_TIMEOUT = 15
PERPETUAL_HTTP_ATTEMPTS = 4
PERPETUAL_HTTP_BACKOFF_SECONDS = 0.5
SUPPORTED_INTERVALS = set(sw.INTERVAL_MS)
OPEN_INTEREST_PERIODS = {
    "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"
}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]{5,30}$")


def normalize_symbol(symbol):
    value = str(symbol or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(value):
        raise ValueError(f"invalid perpetual symbol: {symbol!r}")
    return value


def validate_interval(interval):
    value = str(interval or "").strip()
    if value not in SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported perpetual interval: {interval!r}")
    return value


def _limit(value, maximum=1500):
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return number


def _history_limit(value, maximum=20000):
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f"history limit must be between 1 and {maximum}")
    return number


def _url(path, **params):
    clean = {key: value for key, value in params.items() if value is not None}
    return f"{BASE_URL}{path}?{urllib.parse.urlencode(clean)}"


def perpetual_http_get_json(url, timeout=PERPETUAL_HTTP_TIMEOUT,
                            attempts=PERPETUAL_HTTP_ATTEMPTS,
                            backoff_seconds=PERPETUAL_HTTP_BACKOFF_SECONDS):
    """Use a longer timeout for the slower USD-M public endpoints.

    Spot monitoring keeps its short timeout so a derivatives outage cannot
    delay the normal scan.  Tests and callers may still inject ``http_get``.
    """
    return sw.http_get_json(url, timeout=timeout, attempts=attempts,
                            backoff_seconds=backoff_seconds)


def parse_kline_rows(rows):
    if not isinstance(rows, list):
        raise RuntimeError("perpetual kline response must be a list")
    parsed = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            raise RuntimeError("invalid perpetual kline row")
        parsed.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]) if len(row) > 5 and row[5] not in (None, "") else 0.0,
        })
    return sorted(parsed, key=lambda item: item["time"])


def _fetch_klines(path, symbol, interval, limit=500, end_time=None,
                   parameter="symbol", http_get=None, closed_only=True):
    getter = http_get or sw.http_get_json
    symbol_value = normalize_symbol(symbol)
    interval_value = validate_interval(interval)
    params = {
        parameter: symbol_value,
        "interval": interval_value,
        "limit": _limit(limit),
        "endTime": int(end_time) if end_time is not None else None,
    }
    rows = parse_kline_rows(getter(_url(path, **params)))
    return sw.filter_closed_klines(rows, interval_value) if closed_only else rows


def fetch_contract_klines(symbol, interval, limit=500, end_time=None,
                          http_get=None, closed_only=True):
    return _fetch_klines(
        "/fapi/v1/klines", symbol, interval, limit, end_time,
        http_get=http_get, closed_only=closed_only,
    )


def fetch_mark_price_klines(symbol, interval, limit=500, end_time=None,
                            http_get=None, closed_only=True):
    return _fetch_klines(
        "/fapi/v1/markPriceKlines", symbol, interval, limit, end_time,
        http_get=http_get, closed_only=closed_only,
    )


def fetch_index_price_klines(symbol, interval, limit=500, end_time=None,
                             http_get=None, closed_only=True):
    return _fetch_klines(
        "/fapi/v1/indexPriceKlines", symbol, interval, limit, end_time,
        parameter="pair", http_get=http_get, closed_only=closed_only,
    )


def fetch_kline_history(kind, symbol, interval, limit=500, end_time=None,
                        http_get=None, closed_only=True):
    """Page backwards through a public kline endpoint and deduplicate rows."""
    routes = {
        "contract": ("/fapi/v1/klines", "symbol"),
        "mark": ("/fapi/v1/markPriceKlines", "symbol"),
        "index": ("/fapi/v1/indexPriceKlines", "pair"),
    }
    if kind not in routes:
        raise ValueError(f"unsupported perpetual kline kind: {kind!r}")
    total = _history_limit(limit)
    path, parameter = routes[kind]
    cursor = int(end_time) if end_time is not None else None
    collected = {}
    while len(collected) < total:
        page_limit = min(1500, total - len(collected))
        rows = _fetch_klines(
            path, symbol, interval, page_limit, cursor, parameter,
            http_get=http_get, closed_only=False,
        )
        if not rows:
            break
        before = len(collected)
        for row in rows:
            collected[int(row["time"])] = row
        if len(collected) == before:
            break
        earliest = min(int(row["time"]) for row in rows)
        next_cursor = earliest - 1
        if cursor is not None and next_cursor >= cursor:
            break
        cursor = next_cursor
    result = sorted(collected.values(), key=lambda item: item["time"])[-total:]
    return sw.filter_closed_klines(result, validate_interval(interval)) if closed_only else result


def fetch_funding_rates(symbol, start_time=None, end_time=None, limit=1000,
                        http_get=None):
    getter = http_get or sw.http_get_json
    rows = getter(_url(
        "/fapi/v1/fundingRate",
        symbol=normalize_symbol(symbol),
        startTime=int(start_time) if start_time is not None else None,
        endTime=int(end_time) if end_time is not None else None,
        limit=_limit(limit, 1000),
    ))
    if not isinstance(rows, list):
        raise RuntimeError("funding-rate response must be a list")
    result = []
    for row in rows:
        if not isinstance(row, dict) or "fundingTime" not in row or "fundingRate" not in row:
            raise RuntimeError("invalid funding-rate row")
        result.append({
            "time": int(row["fundingTime"]),
            "funding_rate": float(row["fundingRate"]),
            "mark_price": float(row["markPrice"]) if row.get("markPrice") not in (None, "") else None,
        })
    return sorted(result, key=lambda item: item["time"])


def fetch_funding_history(symbol, limit=1000, end_time=None, http_get=None):
    """Page backwards through funding settlements for reproducible history."""
    total = _history_limit(limit)
    cursor = int(end_time) if end_time is not None else None
    collected = {}
    while len(collected) < total:
        page_limit = min(1000, total - len(collected))
        rows = fetch_funding_rates(
            symbol, end_time=cursor, limit=page_limit, http_get=http_get
        )
        if not rows:
            break
        before = len(collected)
        for row in rows:
            collected[int(row["time"])] = row
        if len(collected) == before:
            break
        earliest = min(int(row["time"]) for row in rows)
        next_cursor = earliest - 1
        if cursor is not None and next_cursor >= cursor:
            break
        cursor = next_cursor
    return sorted(collected.values(), key=lambda item: item["time"])[-total:]


def fetch_open_interest_history(symbol, period="4h", limit=500, start_time=None,
                                end_time=None, http_get=None):
    getter = http_get or sw.http_get_json
    if period not in OPEN_INTEREST_PERIODS:
        raise ValueError(f"unsupported open-interest period: {period!r}")
    rows = getter(_url(
        "/futures/data/openInterestHist",
        symbol=normalize_symbol(symbol), period=period, limit=_limit(limit, 500),
        startTime=int(start_time) if start_time is not None else None,
        endTime=int(end_time) if end_time is not None else None,
    ))
    if not isinstance(rows, list):
        raise RuntimeError("open-interest response must be a list")
    result = []
    for row in rows:
        if not isinstance(row, dict) or "timestamp" not in row:
            raise RuntimeError("invalid open-interest row")
        result.append({
            "time": int(row["timestamp"]),
            "open_interest": float(row["sumOpenInterest"]),
            "open_interest_value": float(row["sumOpenInterestValue"]),
        })
    return sorted(result, key=lambda item: item["time"])


def parse_contract_specs(document, symbol):
    """Extract the public Binance USD-M contract constraints for one symbol."""
    symbol_value = normalize_symbol(symbol)
    if not isinstance(document, dict) or not isinstance(document.get("symbols"), list):
        raise RuntimeError("exchange-info response must contain symbols")
    match = next((item for item in document["symbols"]
                  if isinstance(item, dict) and item.get("symbol") == symbol_value), None)
    if not match:
        raise RuntimeError(f"contract symbol not found: {symbol_value}")
    if match.get("contractType") != "PERPETUAL":
        raise RuntimeError(f"{symbol_value} is not a perpetual contract")
    if match.get("quoteAsset") != "USDT":
        raise RuntimeError(f"{symbol_value} is not USDT-margined")
    filters = {item.get("filterType"): item for item in (match.get("filters") or [])
               if isinstance(item, dict) and item.get("filterType")}
    price_filter = filters.get("PRICE_FILTER") or {}
    lot_filter = filters.get("LOT_SIZE") or {}
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    try:
        tick = float(price_filter["tickSize"])
        step = float(lot_filter["stepSize"])
        min_qty = float(lot_filter["minQty"])
        min_notional = float(notional_filter.get("notional", notional_filter.get("minNotional", 0)) or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid contract filters for {symbol_value}") from exc
    if tick <= 0 or step <= 0 or min_qty < 0 or min_notional < 0:
        raise RuntimeError(f"invalid contract constraints for {symbol_value}")
    return {
        "symbol": symbol_value,
        "status": match.get("status"),
        "contract_type": match.get("contractType"),
        "base_asset": match.get("baseAsset"),
        "quote_asset": match.get("quoteAsset"),
        "price_tick": tick,
        "quantity_step": step,
        "min_quantity": min_qty,
        "min_notional": min_notional,
    }


def fetch_contract_specs(symbol, http_get=None):
    getter = http_get or sw.http_get_json
    value = normalize_symbol(symbol)
    document = getter(_url("/fapi/v1/exchangeInfo", symbol=value))
    return parse_contract_specs(document, value)


def quantize_quantity(quantity, specs):
    """Round quantity down to the exchange step without creating extra risk."""
    value = Decimal(str(quantity))
    step = Decimal(str(specs["quantity_step"]))
    if value <= 0 or step <= 0:
        return 0.0
    return float((value / step).to_integral_value(rounding=ROUND_FLOOR) * step)


def quantize_price(price, specs, side):
    """Round a fill price in the adverse direction for a marketable order."""
    value = Decimal(str(price))
    tick = Decimal(str(specs["price_tick"]))
    if value <= 0 or tick <= 0:
        raise ValueError("price and price tick must be > 0")
    rounding = ROUND_CEILING if str(side).lower() == "buy" else ROUND_FLOOR
    return float((value / tick).to_integral_value(rounding=rounding) * tick)


def quantity_is_executable(quantity, price, specs):
    """Check the public minimum quantity and notional constraints."""
    qty = float(quantity)
    notional = float(price) * qty
    return (
        qty >= float(specs.get("min_quantity", 0) or 0)
        and notional >= float(specs.get("min_notional", 0) or 0)
    )


def validate_perpetual_snapshot(snapshot, interval, now_ms=None,
                                max_staleness_intervals=3):
    """Return errors for malformed, mixed-market or stale research data."""
    errors = []
    interval_value = validate_interval(interval)
    interval_ms = sw.INTERVAL_MS[interval_value]
    if not isinstance(snapshot, dict):
        return ["snapshot must be an object"]
    if snapshot.get("market_type") != MARKET_TYPE:
        errors.append(f"market_type must be {MARKET_TYPE}")
    specs = snapshot.get("contract_specs")
    if specs is not None:
        if not isinstance(specs, dict):
            errors.append("contract_specs must be an object")
        else:
            if specs.get("symbol") != snapshot.get("symbol"):
                errors.append("contract_specs symbol mismatch")
            if specs.get("contract_type") != "PERPETUAL":
                errors.append("contract_specs must describe a perpetual contract")
            if specs.get("quote_asset") != "USDT":
                errors.append("contract_specs must be USDT-margined")
            for field in ("price_tick", "quantity_step"):
                try:
                    value = float(specs[field])
                except (KeyError, TypeError, ValueError):
                    errors.append(f"contract_specs contains invalid {field}")
                    continue
                if not math.isfinite(value) or value <= 0:
                    errors.append(f"contract_specs contains invalid {field}")
            for field in ("min_quantity", "min_notional"):
                try:
                    value = float(specs.get(field, 0))
                except (TypeError, ValueError):
                    errors.append(f"contract_specs contains invalid {field}")
                    continue
                if not math.isfinite(value) or value < 0:
                    errors.append(f"contract_specs contains invalid {field}")
    series_by_name = {
        "contract_klines": snapshot.get("contract_klines"),
        "mark_price_klines": snapshot.get("mark_price_klines"),
        "index_price_klines": snapshot.get("index_price_klines"),
    }
    for name, rows in series_by_name.items():
        if not isinstance(rows, list) or not rows:
            errors.append(f"{name} is empty")
            continue
        quality = backtest_data.validate_klines(rows, interval_ms)
        if not quality["continuous"]:
            errors.append(f"{name} quality failed: {quality}")
        timestamps = [int(row.get("time", -1)) for row in rows if isinstance(row, dict)]
        if len(timestamps) != len(set(timestamps)):
            errors.append(f"{name} contains duplicate timestamps")
        for row in rows:
            try:
                values = [float(row[field]) for field in ("open", "high", "low", "close")]
            except (KeyError, TypeError, ValueError):
                errors.append(f"{name} contains non-numeric OHLC")
                break
            if not all(math.isfinite(value) and value > 0 for value in values):
                errors.append(f"{name} contains non-finite OHLC")
                break
    contract_rows = series_by_name["contract_klines"]
    contract_times = []
    if isinstance(contract_rows, list) and all(
        isinstance(row, dict) and "time" in row for row in contract_rows
    ):
        try:
            contract_times = [int(row["time"]) for row in contract_rows]
        except (TypeError, ValueError):
            contract_times = []
    if contract_times:
        for name in ("mark_price_klines", "index_price_klines"):
            rows = series_by_name[name]
            if isinstance(rows, list) and rows:
                times = [int(row["time"]) for row in rows if isinstance(row, dict) and "time" in row]
                if times != contract_times:
                    errors.append(f"{name} timestamps do not align with contract_klines")
    contract = series_by_name["contract_klines"]
    if isinstance(contract, list) and contract:
        current = int(time.time() * 1000) if now_ms is None else int(now_ms)
        last_close = int(contract[-1]["time"]) + interval_ms
        if current - last_close > interval_ms * max(1, int(max_staleness_intervals)):
            errors.append("contract_klines are stale")
    funding = snapshot.get("funding_rates")
    if funding is not None and not isinstance(funding, list):
        errors.append("funding_rates must be a list")
    elif isinstance(funding, list):
        funding_times = []
        for event in funding:
            if not isinstance(event, dict):
                errors.append("funding_rates contains a non-object")
                continue
            try:
                timestamp = int(event["time"])
                rate = float(event["funding_rate"])
            except (KeyError, TypeError, ValueError):
                errors.append("funding_rates contains an invalid event")
                continue
            funding_times.append(timestamp)
            if not math.isfinite(rate) or abs(rate) > 1:
                errors.append("funding_rates contains an invalid rate")
            if event.get("mark_price") not in (None, ""):
                try:
                    if not math.isfinite(float(event["mark_price"])) or float(event["mark_price"]) <= 0:
                        errors.append("funding_rates contains an invalid mark price")
                except (TypeError, ValueError):
                    errors.append("funding_rates contains an invalid mark price")
        if len(funding_times) != len(set(funding_times)):
            errors.append("funding_rates contains duplicate timestamps")
    open_interest = snapshot.get("open_interest")
    if open_interest is not None and not isinstance(open_interest, list):
        errors.append("open_interest must be a list")
    elif isinstance(open_interest, list):
        oi_times = []
        for event in open_interest:
            if not isinstance(event, dict):
                errors.append("open_interest contains a non-object")
                continue
            for field in ("time", "open_interest", "open_interest_value"):
                try:
                    value = float(event[field])
                except (KeyError, TypeError, ValueError):
                    errors.append(f"open_interest contains invalid {field}")
                    continue
                if not math.isfinite(value) or (field != "time" and value < 0):
                    errors.append(f"open_interest contains invalid {field}")
                if field == "time":
                    oi_times.append(int(value))
        if len(oi_times) != len(set(oi_times)):
            errors.append("open_interest contains duplicate timestamps")
    return errors


def fetch_perpetual_snapshot(symbol, interval="4h", limit=500, http_get=None,
                             closed_only=True, include_contract_specs=False,
                             allow_partial=False, request_timeout=None,
                             request_attempts=None, request_backoff_seconds=None):
    symbol_value = normalize_symbol(symbol)
    interval_value = validate_interval(interval)
    history_limit = _history_limit(limit)
    if http_get:
        getter = http_get
    else:
        getter = lambda url: perpetual_http_get_json(
            url,
            timeout=request_timeout or PERPETUAL_HTTP_TIMEOUT,
            attempts=request_attempts or PERPETUAL_HTTP_ATTEMPTS,
            backoff_seconds=(PERPETUAL_HTTP_BACKOFF_SECONDS
                             if request_backoff_seconds is None else request_backoff_seconds),
        )
    component_errors = {}

    def collect(name, callback, default):
        try:
            return callback()
        except Exception as exc:
            if not allow_partial:
                raise
            component_errors[name] = str(exc)
            return default

    fetched_at = int(time.time() * 1000)
    snapshot = {
        "schema_version": 1,
        "venue": "binance",
        "market_type": MARKET_TYPE,
        "symbol": symbol_value,
        "interval": interval_value,
        "fetched_at_epoch_ms": fetched_at,
        "contract_klines": collect("contract_klines", lambda: fetch_kline_history(
            "contract", symbol_value, interval_value, history_limit,
            http_get=getter, closed_only=closed_only
        ), []),
        "mark_price_klines": collect("mark_price_klines", lambda: fetch_kline_history(
            "mark", symbol_value, interval_value, history_limit,
            http_get=getter, closed_only=closed_only
        ), []),
        "index_price_klines": collect("index_price_klines", lambda: fetch_kline_history(
            "index", symbol_value, interval_value, history_limit,
            http_get=getter, closed_only=closed_only
        ), []),
        "funding_rates": collect("funding_rates", lambda: fetch_funding_history(
            symbol_value, limit=history_limit, http_get=getter
        ), []),
        "open_interest": collect("open_interest", lambda: fetch_open_interest_history(
            symbol_value, period=interval_value, limit=min(500, history_limit), http_get=getter
        ), []),
        "collection": {
            "requested_kline_bars": history_limit,
            "funding_events_limit": history_limit,
            "open_interest_limit": min(500, history_limit),
            "open_interest_history_limited_by_source": history_limit > 500,
        },
    }
    if include_contract_specs:
        snapshot["contract_specs"] = collect(
            "contract_specs", lambda: fetch_contract_specs(symbol_value, http_get=getter), None
        )
    latest = {}
    for name in ("contract_klines", "mark_price_klines", "index_price_klines", "funding_rates", "open_interest"):
        rows = snapshot.get(name) or []
        if rows:
            latest[name] = max(int(row["time"]) for row in rows if isinstance(row, dict) and "time" in row)
    snapshot["data_health"] = {
        "component_errors": component_errors,
        "complete": not component_errors,
        "latest_data_times": latest,
        "data_lag_minutes": {name: round(max(0, fetched_at - ts) / 60000, 2) for name, ts in latest.items()},
    }
    return snapshot
