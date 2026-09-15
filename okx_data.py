#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public OKX USDT-margined perpetual data adapter.

The adapter emits the same normalized snapshot contract as derivatives_data.
It is public-data only and never accepts credentials.
"""

import time
import urllib.parse

import derivatives_data as binance
import signal_watch as sw


BASE_URL = "https://www.okx.com"
BAR_MAP = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
           "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "8h": "8H",
           "12h": "12H", "1d": "1D", "3d": "3D", "1w": "1W", "1M": "1M"}


def instrument_id(symbol):
    value = binance.normalize_symbol(symbol)
    if not value.endswith("USDT"):
        raise ValueError("OKX adapter currently supports USDT perpetuals only")
    return f"{value[:-4]}-USDT-SWAP"


def _url(path, **params):
    clean = {key: value for key, value in params.items() if value is not None}
    return f"{BASE_URL}{path}?{urllib.parse.urlencode(clean)}"


def _get(url, timeout=None, attempts=None, backoff_seconds=None):
    return sw.http_get_json(
        url, timeout=timeout or binance.PERPETUAL_HTTP_TIMEOUT,
        attempts=attempts or binance.PERPETUAL_HTTP_ATTEMPTS,
        backoff_seconds=(binance.PERPETUAL_HTTP_BACKOFF_SECONDS
                         if backoff_seconds is None else backoff_seconds),
    )


def _data(document):
    if not isinstance(document, dict) or str(document.get("code")) != "0":
        raise RuntimeError(f"OKX API error: {document}")
    return document.get("data") or []


def _candles(inst_id, bar, limit, endpoint, getter, after=None):
    rows = _data(getter(_url(endpoint, instId=inst_id, bar=bar, limit=min(100, int(limit)), after=after)))
    parsed = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            raise RuntimeError("invalid OKX candle row")
        parsed.append({"time": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                       "low": float(row[3]), "close": float(row[4]),
                       "volume": float(row[5]) if len(row) > 5 else 0.0})
    return sorted(parsed, key=lambda item: item["time"])


def _paged_candles(inst_id, bar, limit, endpoint, getter, interval, closed_only=True):
    target = max(1, int(limit))
    collected = {}
    cursor = None
    for _ in range((target + 99) // 100 + 2):
        page = _candles(inst_id, bar, min(100, target - len(collected)), endpoint, getter, cursor)
        if not page:
            break
        before = len(collected)
        for row in page:
            collected[int(row["time"])] = row
        if len(collected) == before:
            break
        earliest = min(int(row["time"]) for row in page)
        cursor = str(earliest - 1)
        if len(collected) >= target or len(page) < 100:
            break
    rows = sorted(collected.values(), key=lambda item: item["time"])[-target:]
    return sw.filter_closed_klines(rows, interval) if closed_only else rows


def _funding(inst_id, limit, getter):
    """Page OKX funding history backwards until the requested target is met."""
    target = max(1, int(limit))
    collected = {}
    cursor = None
    for _ in range((target + 99) // 100 + 2):
        rows = _data(getter(_url(
            "/api/v5/public/funding-rate-history", instId=inst_id,
            limit=min(100, target - len(collected)), before=cursor,
        )))
        if not rows:
            break
        before_count = len(collected)
        for row in rows:
            if not isinstance(row, dict) or "fundingTime" not in row or "fundingRate" not in row:
                raise RuntimeError("invalid OKX funding-rate row")
            collected[int(row["fundingTime"])] = {
                "time": int(row["fundingTime"]),
                "funding_rate": float(row["fundingRate"]),
                "mark_price": None,
            }
        if len(collected) == before_count:
            break
        earliest = min(collected)
        cursor = str(earliest - 1)
        if len(collected) >= target:
            break
    return sorted(collected.values(), key=lambda item: item["time"])[-target:]


def _specs(inst_id, getter):
    rows = _data(getter(_url("/api/v5/public/instruments", instType="SWAP", instId=inst_id)))
    if not rows:
        raise RuntimeError(f"OKX instrument not found: {inst_id}")
    row = rows[0]
    contract_value = float(row.get("ctVal") or 0)
    if contract_value <= 0:
        raise RuntimeError(f"OKX instrument is missing a valid contract value: {inst_id}")
    if row.get("ctType") not in (None, "", "linear") or row.get("settleCcy") not in (None, "", "USDT"):
        raise RuntimeError(f"OKX contract is not linear USDT-settled: {inst_id}")
    inferred_base = inst_id.split("-", 1)[0]
    base_asset = row.get("baseCcy") or row.get("ctValCcy") or inferred_base
    contract_currency = row.get("ctValCcy") or base_asset
    if base_asset != inferred_base or contract_currency != base_asset:
        raise RuntimeError(f"OKX contract value is not denominated in the base asset: {inst_id}")
    native_step = float(row["lotSz"])
    native_minimum = float(row["minSz"])
    return {"symbol": inst_id.replace("-USDT-SWAP", "USDT"), "status": row.get("state"),
            "contract_type": "PERPETUAL", "base_asset": base_asset,
            "quote_asset": "USDT", "price_tick": float(row["tickSz"]),
            "quantity_step": native_step * contract_value,
            "min_quantity": native_minimum * contract_value,
            "min_notional": 0.0, "contract_value": contract_value,
            "contract_value_currency": contract_currency,
            "native_quantity_step_contracts": native_step,
            "native_min_quantity_contracts": native_minimum}


def _open_interest(inst_id, getter):
    rows = _data(getter(_url("/api/v5/public/open-interest", instType="SWAP", instId=inst_id)))
    if not rows:
        return []
    row = rows[0]
    if not isinstance(row, dict):
        return []
    if row.get("ts") in (None, ""):
        raise RuntimeError("OKX open-interest row is missing timestamp")
    timestamp = int(row["ts"])
    oi = float(row.get("oi") or 0)
    oi_value = row.get("oiUsd")
    return [{"time": timestamp, "open_interest": oi,
             "open_interest_value": float(oi_value) if oi_value not in (None, "") else None}]


def fetch_perpetual_snapshot(symbol, interval="4h", limit=500, http_get=None,
                             closed_only=True, include_contract_specs=False,
                             allow_partial=False, request_timeout=None,
                             request_attempts=None, request_backoff_seconds=None):
    symbol_value = binance.normalize_symbol(symbol)
    interval_value = binance.validate_interval(interval)
    inst_id = instrument_id(symbol_value)
    getter = http_get or (lambda url: _get(url, request_timeout, request_attempts, request_backoff_seconds))
    errors = {}

    def collect(name, callback, default):
        try:
            return callback()
        except Exception as exc:
            if not allow_partial:
                raise
            errors[name] = str(exc)
            return default

    bar = BAR_MAP[interval_value]
    fetched = int(time.time() * 1000)
    contract = collect("contract_klines", lambda: _paged_candles(inst_id, bar, limit, "/api/v5/market/history-candles", getter, interval_value, closed_only), [])
    mark = collect("mark_price_klines", lambda: _paged_candles(inst_id, bar, limit, "/api/v5/market/history-mark-price-candles", getter, interval_value, closed_only), [])
    index_inst_id = inst_id.replace("-SWAP", "")
    index = collect("index_price_klines", lambda: _paged_candles(index_inst_id, bar, limit, "/api/v5/market/history-index-candles", getter, interval_value, closed_only), [])
    snapshot = {"schema_version": 1, "venue": "okx", "market_type": binance.MARKET_TYPE,
                "symbol": symbol_value, "interval": interval_value, "fetched_at_epoch_ms": fetched,
                "contract_klines": contract, "mark_price_klines": mark, "index_price_klines": index,
                "funding_rates": collect("funding_rates", lambda: _funding(inst_id, limit, getter), []),
                "open_interest": collect("open_interest", lambda: _open_interest(inst_id, getter), []),
                "collection": {"requested_kline_bars": int(limit), "funding_events_limit": int(limit),
                "funding_history_paginated": True, "closed_only": bool(closed_only),
                "provider": "okx", "interval_ms": sw.INTERVAL_MS[interval_value],
                "open_interest_limit": 1, "open_interest_coverage": "latest_only"}}
    if include_contract_specs:
        snapshot["contract_specs"] = collect("contract_specs", lambda: _specs(inst_id, getter), None)
    specs = snapshot.get("contract_specs") or {}
    contract_value = float(specs.get("contract_value") or 0)
    if contract_value > 0:
        for candle in snapshot["contract_klines"]:
            candle["volume_contracts"] = float(candle.get("volume") or 0)
            candle["volume"] = candle["volume_contracts"] * contract_value
        for observation in snapshot["open_interest"]:
            observation["open_interest_contracts"] = float(observation.get("open_interest") or 0)
            observation["open_interest"] = observation["open_interest_contracts"] * contract_value
        snapshot["collection"]["contract_volume_unit"] = "base_asset"
        snapshot["collection"]["open_interest_unit"] = "base_asset"
        snapshot["collection"]["native_derivatives_unit"] = "contracts"
    else:
        snapshot["collection"]["contract_volume_unit"] = "contracts"
        snapshot["collection"]["open_interest_unit"] = "contracts"
    latest = {}
    latest_bar_open_times = {}
    interval_ms = sw.INTERVAL_MS[interval_value]
    for name in ("contract_klines", "mark_price_klines", "index_price_klines", "funding_rates", "open_interest"):
        rows = snapshot.get(name) or []
        if rows:
            latest_open = max(int(row["time"]) for row in rows)
            if name.endswith("klines"):
                latest_bar_open_times[name] = latest_open
            latest[name] = latest_open + (interval_ms if name.endswith("klines") else 0)
    snapshot["data_health"] = {"component_errors": errors, "complete": not errors,
                               "latest_data_times": latest,
                               "latest_bar_open_times": latest_bar_open_times,
                               "data_lag_minutes": {name: round(max(0, fetched - ts) / 60000, 2)
                                                    for name, ts in latest.items()}}
    for field in ("funding_rates", "open_interest"):
        times = sorted({int(row["time"]) for row in (snapshot.get(field) or [])
                        if isinstance(row, dict) and row.get("time") not in (None, "")})
        gaps = [right - left for left, right in zip(times, times[1:])]
        snapshot["data_health"][f"{field}_observation_count"] = len(times)
        snapshot["data_health"][f"{field}_max_gap_ms"] = max(gaps, default=0)
        expected_gap = (binance.FUNDING_SETTLEMENT_INTERVAL_MS
                        if field == "funding_rates" else interval_ms)
        snapshot["data_health"][f"{field}_gap_count"] = sum(gap > expected_gap * 3 for gap in gaps)
    return snapshot
