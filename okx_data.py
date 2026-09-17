#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Public OKX USDT-margined perpetual data adapter.

The adapter emits the same normalized snapshot contract as derivatives_data.
It is public-data only and never accepts credentials.
"""

import hashlib
import json
import math
import os
import time
import urllib.parse

import derivatives_data as binance
import derivatives_risk
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


def _position_tiers(inst_id, contract_value, getter):
    """Return official isolated-margin tiers in normalized base-asset units."""
    family = inst_id.removesuffix("-SWAP")
    endpoint = "/api/v5/public/position-tiers"
    rows = _data(getter(_url(
        endpoint, instType="SWAP", tdMode="isolated", instFamily=family,
    )))
    tiers = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("invalid OKX position-tier row")
        try:
            max_contracts = float(row["maxSz"])
            rate = float(row["mmr"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid OKX position-tier row") from exc
        if (row.get("instFamily") not in (None, "", family)
                or not math.isfinite(max_contracts) or max_contracts <= 0
                or not math.isfinite(rate) or not 0 <= rate < 1):
            raise RuntimeError("invalid OKX position-tier row")
        tiers.append({
            "max_quantity": max_contracts * float(contract_value),
            "maintenance_margin_rate": rate,
        })
    normalized = derivatives_risk.normalize_maintenance_margin_tiers(tiers)
    if not normalized:
        raise RuntimeError(f"OKX position tiers not found: {inst_id}")
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return {
        "tiers": normalized,
        "source": f"{BASE_URL}{endpoint}",
        "source_parameters": {
            "instType": "SWAP", "tdMode": "isolated", "instFamily": family,
        },
        "retrieved_at_epoch_ms": int(time.time() * 1000),
        "tier_version": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "native_cap_unit": "contracts",
        "normalized_cap_unit": "base_asset",
        "contract_value": float(contract_value),
    }


def _tier_cache_path(cache_dir, symbol):
    if not cache_dir:
        return None
    safe_symbol = binance.normalize_symbol(symbol).lower()
    return os.path.join(cache_dir, f"okx-risk-tiers-{safe_symbol}.json")


def _load_position_tier_cache(path, inst_id, contract_value, now_ms):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as file:
            cached = json.load(file)
        if not isinstance(cached, dict) or int(cached.get("schema_version") or 0) != 1:
            return None
        binding = cached.get("binding")
        if not isinstance(binding, dict):
            return None
        metadata = {key: value for key, value in binding.items() if key != "tiers"}
        expected_family = inst_id.removesuffix("-SWAP")
        source_parameters = metadata.get("source_parameters") or {}
        retrieved = int(metadata.get("retrieved_at_epoch_ms") or 0)
        cached_contract_value = float(metadata.get("contract_value") or 0)
        if (metadata.get("source") != f"{BASE_URL}/api/v5/public/position-tiers"
                or source_parameters.get("instType") != "SWAP"
                or source_parameters.get("tdMode") != "isolated"
                or source_parameters.get("instFamily") != expected_family
                or metadata.get("native_cap_unit") != "contracts"
                or metadata.get("normalized_cap_unit") != "base_asset"
                or not math.isclose(cached_contract_value, float(contract_value), rel_tol=0, abs_tol=1e-12)
                or retrieved <= 0 or retrieved > int(now_ms)):
            return None
        tiers = derivatives_risk.normalize_maintenance_margin_tiers(binding.get("tiers") or [])
        canonical = json.dumps(tiers, sort_keys=True, separators=(",", ":"))
        expected_version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        if metadata.get("tier_version") != expected_version:
            return None
        return {"tiers": tiers, **metadata,
                "cache_age_minutes": round((int(now_ms) - retrieved) / 60000, 2)}
    except (OSError, ValueError, TypeError):
        return None


def _save_position_tier_cache(path, binding):
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    payload = {"schema_version": 1, "binding": binding}
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)


def _resolve_position_tiers(inst_id, symbol, contract_value, getter, cache_dir=None,
                            cache_ttl_minutes=360, cache_max_stale_minutes=1440,
                            now_ms=None):
    """Resolve official tiers with a validated fresh/stale last-known-good cache."""
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    cache_path = _tier_cache_path(cache_dir, symbol)
    cached = _load_position_tier_cache(cache_path, inst_id, contract_value, current_ms)
    ttl = max(0.0, float(cache_ttl_minutes))
    max_stale = max(ttl, float(cache_max_stale_minutes))
    if cached and cached["cache_age_minutes"] <= ttl:
        return {**cached, "cache_status": "fresh_cache", "refresh_error_category": None}
    try:
        live = _position_tiers(inst_id, contract_value, getter)
        live = {**live, "cache_status": "live_refresh", "cache_age_minutes": 0.0,
                "refresh_error_category": None}
        _save_position_tier_cache(cache_path, {
            key: value for key, value in live.items()
            if key not in {"cache_status", "cache_age_minutes", "refresh_error_category"}
        })
        return live
    except Exception as exc:
        if cached and cached["cache_age_minutes"] <= max_stale:
            return {**cached, "cache_status": "stale_fallback",
                    "refresh_error_category": type(exc).__name__}
        raise


def _open_interest(inst_id, period, limit, getter):
    """Page OKX's official contract-level open-interest history."""
    target = min(1440, max(1, int(limit)))
    collected = {}
    cursor = None
    for _ in range((target + 99) // 100 + 2):
        rows = _data(getter(_url(
            "/api/v5/rubik/stat/contracts/open-interest-history",
            instId=inst_id, period=period, limit=min(100, target - len(collected)),
            end=cursor,
        )))
        if not rows:
            break
        before_count = len(collected)
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 4:
                raise RuntimeError("invalid OKX open-interest history row")
            try:
                timestamp = int(row[0])
                contracts = float(row[1])
                base_value = float(row[2])
                usd_value = float(row[3])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid OKX open-interest history row") from exc
            if (timestamp <= 0 or any(
                    not math.isfinite(value) or value < 0
                    for value in (contracts, base_value, usd_value))):
                raise RuntimeError("invalid OKX open-interest history row")
            collected[timestamp] = {
                "time": timestamp,
                "open_interest": base_value,
                "open_interest_value": usd_value,
                "open_interest_contracts": contracts,
            }
        if len(collected) == before_count:
            break
        cursor = str(min(int(row[0]) for row in rows) - 1)
        if len(collected) >= target:
            break
    return sorted(collected.values(), key=lambda item: item["time"])[-target:]


def fetch_perpetual_snapshot(symbol, interval="4h", limit=500, http_get=None,
                             closed_only=True, include_contract_specs=False,
                             allow_partial=False, request_timeout=None,
                             request_attempts=None, request_backoff_seconds=None,
                             open_interest_limit=None, risk_tier_cache_dir=None,
                             risk_tier_cache_ttl_minutes=360,
                             risk_tier_cache_max_stale_minutes=1440):
    symbol_value = binance.normalize_symbol(symbol)
    interval_value = binance.validate_interval(interval)
    inst_id = instrument_id(symbol_value)
    getter = http_get or (lambda url: _get(url, request_timeout, request_attempts, request_backoff_seconds))
    errors = {}
    oi_limit = int(limit if open_interest_limit is None else open_interest_limit)
    if oi_limit < 1:
        raise ValueError("open_interest_limit must be >= 1")

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
                "open_interest": collect(
                    "open_interest", lambda: _open_interest(inst_id, bar, oi_limit, getter), []
                ),
                "collection": {"requested_kline_bars": int(limit), "funding_events_limit": int(limit),
                "funding_history_paginated": True, "closed_only": bool(closed_only),
                "provider": "okx", "interval_ms": sw.INTERVAL_MS[interval_value],
                "open_interest_limit": min(1440, oi_limit),
                "open_interest_coverage": "historical",
                "open_interest_history_paginated": True,
                "open_interest_history_limited_by_source": oi_limit > 1440,
                "open_interest_unit": "base_asset",
                "native_derivatives_unit": "contracts"}}
    if include_contract_specs:
        snapshot["contract_specs"] = collect("contract_specs", lambda: _specs(inst_id, getter), None)
    specs = snapshot.get("contract_specs") or {}
    contract_value = float(specs.get("contract_value") or 0)
    if include_contract_specs and contract_value > 0:
        try:
            tier_getter = http_get or (lambda url: _get(
                url, min(float(request_timeout or binance.PERPETUAL_HTTP_TIMEOUT), 10.0),
                1, 0.0,
            ))
            tier_binding = _resolve_position_tiers(
                inst_id, symbol_value, contract_value, tier_getter,
                cache_dir=risk_tier_cache_dir,
                cache_ttl_minutes=risk_tier_cache_ttl_minutes,
                cache_max_stale_minutes=risk_tier_cache_max_stale_minutes,
            )
            snapshot["maintenance_margin_tiers"] = tier_binding["tiers"]
            snapshot["maintenance_margin_tier_metadata"] = {
                key: value for key, value in tier_binding.items() if key != "tiers"
            }
            if tier_binding.get("cache_status") == "stale_fallback":
                errors["maintenance_margin_tiers_refresh"] = (
                    tier_binding.get("refresh_error_category") or "unknown_error"
                )
        except Exception as exc:
            errors["maintenance_margin_tiers"] = str(exc)
    if contract_value > 0:
        for candle in snapshot["contract_klines"]:
            candle["volume_contracts"] = float(candle.get("volume") or 0)
            candle["volume"] = candle["volume_contracts"] * contract_value
        for observation in snapshot["open_interest"]:
            if observation.get("open_interest_contracts") in (None, ""):
                observation["open_interest_contracts"] = float(
                    observation.get("open_interest") or 0
                )
                observation["open_interest"] = (
                    observation["open_interest_contracts"] * contract_value
                )
        snapshot["collection"]["contract_volume_unit"] = "base_asset"
    else:
        snapshot["collection"]["contract_volume_unit"] = "contracts"
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
