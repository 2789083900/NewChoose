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


def _candles(inst_id, bar, limit, endpoint, getter):
    rows = _data(getter(_url(endpoint, instId=inst_id, bar=bar, limit=min(100, int(limit)))))
    parsed = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            raise RuntimeError("invalid OKX candle row")
        parsed.append({"time": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                       "low": float(row[3]), "close": float(row[4]),
                       "volume": float(row[5]) if len(row) > 5 else 0.0})
    return sw.filter_closed_klines(sorted(parsed, key=lambda item: item["time"]),
                                   next(key for key, value in BAR_MAP.items() if value == bar))


def _funding(inst_id, limit, getter):
    result = []
    rows = _data(getter(_url("/api/v5/public/funding-rate-history", instId=inst_id, limit=min(100, int(limit)))))
    for row in rows:
        result.append({"time": int(row["fundingTime"]), "funding_rate": float(row["fundingRate"]),
                       "mark_price": None})
    return sorted(result, key=lambda item: item["time"])


def _specs(inst_id, getter):
    rows = _data(getter(_url("/api/v5/public/instruments", instType="SWAP", instId=inst_id)))
    if not rows:
        raise RuntimeError(f"OKX instrument not found: {inst_id}")
    row = rows[0]
    return {"symbol": inst_id.replace("-USDT-SWAP", "USDT"), "status": row.get("state"),
            "contract_type": "PERPETUAL", "base_asset": row.get("baseCcy"),
            "quote_asset": "USDT", "price_tick": float(row["tickSz"]),
            "quantity_step": float(row["lotSz"]), "min_quantity": float(row["minSz"]),
            "min_notional": 0.0}


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
    contract = collect("contract_klines", lambda: _candles(inst_id, bar, limit, "/api/v5/market/candles", getter), [])
    mark = collect("mark_price_klines", lambda: _candles(inst_id, bar, limit, "/api/v5/market/mark-price-candles", getter), [])
    index_inst_id = inst_id.replace("-SWAP", "")
    index = collect("index_price_klines", lambda: _candles(index_inst_id, bar, limit, "/api/v5/market/index-candles", getter), [])
    snapshot = {"schema_version": 1, "venue": "okx", "market_type": binance.MARKET_TYPE,
                "symbol": symbol_value, "interval": interval_value, "fetched_at_epoch_ms": fetched,
                "contract_klines": contract, "mark_price_klines": mark, "index_price_klines": index,
                "funding_rates": collect("funding_rates", lambda: _funding(inst_id, limit, getter), []),
                "open_interest": [],
                "collection": {"requested_kline_bars": int(limit), "funding_events_limit": min(100, int(limit)),
                                "open_interest_limit": 0, "open_interest_source_unavailable": True}}
    if include_contract_specs:
        snapshot["contract_specs"] = collect("contract_specs", lambda: _specs(inst_id, getter), None)
    latest = {}
    for name in ("contract_klines", "mark_price_klines", "index_price_klines", "funding_rates"):
        rows = snapshot.get(name) or []
        if rows:
            latest[name] = max(int(row["time"]) for row in rows)
    snapshot["data_health"] = {"component_errors": errors, "complete": not errors,
                               "latest_data_times": latest,
                               "data_lag_minutes": {name: round(max(0, fetched - ts) / 60000, 2)
                                                    for name, ts in latest.items()}}
    return snapshot
