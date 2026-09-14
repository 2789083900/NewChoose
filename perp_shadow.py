#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Research-only shadow trading for Binance USD-M perpetual contracts.

The module uses public market data only. It never accepts API credentials and
contains no account or order endpoints. State is intentionally isolated from
the spot watcher.
"""

import argparse
import copy
from glob import glob
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import derivatives_data
import derivatives_risk
import okx_data
import signal_watch as sw


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "signal_watch.config.json")
DEFAULT_STATE_PATH = os.path.join(BASE_DIR, "perp_shadow_state.json")
DEFAULT_STATS_PATH = os.path.join(BASE_DIR, "perp_shadow_stats.json")
SCHEMA_VERSION = 1
FUNDING_SETTLEMENT_INTERVAL_MS = 8 * 60 * 60 * 1000
MARKET_STATE_LABELS = {"trend", "range", "volatility_expansion", "extreme_risk", "unknown"}


def empty_state(account_value=10000.0):
    return {
        "schema_version": SCHEMA_VERSION,
        "market_type": derivatives_data.MARKET_TYPE,
        "research_only": True,
        "equity": float(account_value),
        "open_trades": [],
        "closed_trades": [],
        "rejected_signals": [],
        "seen_signal_ids": [],
        "equity_curve": [],
        "run_count": 0,
        "consecutive_unavailable_runs": 0,
        "last_success_at_epoch_ms": None,
        "last_successful_symbols": [],
        "symbol_health": {},
        "provider_health": {},
        "market_time_by_symbol": {},
        "notification_history": [],
        "market_state_by_symbol": {},
    }


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as file:
            value = json.load(file)
        return value
    except (OSError, ValueError):
        return default


def load_state(path, account_value=10000.0):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as file:
                state = json.load(file)
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read perpetual shadow state: {exc}") from exc
    else:
        state = empty_state(account_value)
    had_schema = isinstance(state, dict) and "schema_version" in state
    if not isinstance(state, dict) or state.get("market_type") not in (None, derivatives_data.MARKET_TYPE):
        raise ValueError("invalid or mixed-market perpetual shadow state")
    if had_schema and state.get("market_type") is None:
        raise ValueError("perpetual shadow state is missing market_type")
    if state.get("market_type") is None:
        state["migration_applied"] = "legacy_missing_market_type"
    try:
        schema_version = int(state.get("schema_version", SCHEMA_VERSION))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid perpetual shadow state schema_version") from exc
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported perpetual shadow state schema_version: {schema_version}"
        )
    state.setdefault("schema_version", SCHEMA_VERSION)
    state["market_type"] = derivatives_data.MARKET_TYPE
    state["research_only"] = True
    state.setdefault("equity", float(account_value))
    for key in ("open_trades", "closed_trades", "rejected_signals", "seen_signal_ids", "equity_curve"):
        state.setdefault(key, [])
        if not isinstance(state[key], list):
            raise ValueError(f"perpetual shadow state field {key} must be a list")
    state.setdefault("notification_history", [])
    if not isinstance(state["notification_history"], list):
        raise ValueError("perpetual shadow state field notification_history must be a list")
    try:
        state["equity"] = float(state["equity"])
    except (TypeError, ValueError) as exc:
        raise ValueError("perpetual shadow state equity must be numeric") from exc
    if not math.isfinite(state["equity"]) or state["equity"] < 0:
        raise ValueError("perpetual shadow state equity must be finite and non-negative")
    if not isinstance(state.get("provider_health"), dict):
        state["provider_health"] = {}
    if not isinstance(state.get("market_time_by_symbol"), dict):
        state["market_time_by_symbol"] = {}
    if not isinstance(state.get("market_state_by_symbol"), dict):
        state["market_state_by_symbol"] = {}
    for trade in state["open_trades"] + state["closed_trades"]:
        if not isinstance(trade, dict):
            raise ValueError("perpetual shadow state contains a non-object trade")
        if trade.get("market_type") != derivatives_data.MARKET_TYPE or trade.get("research_only") is not True:
            raise ValueError("perpetual shadow state contains an invalid trade")
        if trade.get("status") == "open" and not trade.get("initial_entry"):
            baseline = trade.get("entry", trade.get("avg_entry"))
            if baseline not in (None, ""):
                trade["initial_entry"] = float(baseline)
    return state


def _cache_path(cache_dir, symbol, interval, provider=None):
    if not cache_dir:
        return None
    suffix = f"-{str(provider).lower()}" if provider else ""
    return os.path.join(cache_dir, f"{symbol}-{interval}{suffix}.json")


def _save_snapshot_cache(cache_dir, snapshot):
    path = _cache_path(
        cache_dir, snapshot.get("symbol"), snapshot.get("interval"), snapshot.get("venue")
    )
    if path:
        sw.atomic_write_json(path, snapshot)


def _load_snapshot_cache(cache_dir, symbol, interval, max_stale_minutes, provider=None):
    """Load a recent source-specific cache, with legacy path compatibility."""
    paths = []
    if provider:
        paths.append(_cache_path(cache_dir, symbol, interval, provider))
    elif cache_dir:
        paths.extend(glob(os.path.join(cache_dir, f"{symbol}-{interval}-*.json")))
    paths.append(_cache_path(cache_dir, symbol, interval))
    candidates = []
    for path in paths:
        if not path:
            continue
        cached = load_json(path, None)
        if not isinstance(cached, dict):
            continue
        try:
            fetched = int(cached.get("fetched_at_epoch_ms") or 0)
        except (TypeError, ValueError):
            continue
        age_minutes = (int(time.time() * 1000) - fetched) / 60000 if fetched else float("inf")
        if age_minutes < 0 or age_minutes > float(max_stale_minutes):
            continue
        candidates.append((fetched, cached, round(age_minutes, 2)))
    if not candidates:
        return None
    _, cached, age_minutes = max(candidates, key=lambda item: item[0])
    return cached, age_minutes


def _number(config, key, default, minimum=0.0):
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, value)


def shadow_settings(config):
    raw = dict((config or {}).get("derivatives") or {})
    if raw.get("market_type", derivatives_data.MARKET_TYPE) != derivatives_data.MARKET_TYPE:
        raise ValueError("derivatives.market_type must be linear_perpetual")
    if raw.get("research_only", True) is not True:
        raise ValueError("perpetual shadow trading must remain research_only")
    interval = derivatives_data.validate_interval(raw.get("interval", "4h"))
    symbols = [derivatives_data.normalize_symbol(item) for item in raw.get("symbols", ["BTCUSDT", "ETHUSDT"])]
    provider = str(raw.get("provider", "auto")).strip().lower()
    if provider not in {"binance", "okx", "auto"}:
        raise ValueError("derivatives.provider must be auto, binance or okx")
    providers = [str(item).strip().lower() for item in (raw.get("providers") or [])]
    if provider != "auto":
        providers = [provider]
    else:
        providers = providers or ["binance", "okx"]
    if any(item not in {"binance", "okx"} for item in providers):
        raise ValueError("derivatives.providers must contain only binance or okx")
    data_mode = str(raw.get("research_data_mode", "price_only_research")).strip().lower()
    if data_mode not in {"price_only_research", "full_perpetual_research"}:
        raise ValueError("derivatives.research_data_mode must be price_only_research or full_perpetual_research")
    leverage = _number(raw, "max_leverage", 2.0, 0.01)
    maintenance = _number(raw, "maintenance_margin_rate", 0.005)
    liquidation_fee = _number(raw, "liquidation_fee_rate", 0.0)
    risk_fraction = _number(raw, "risk_fraction", 0.005)
    max_open_risk = _number(raw, "max_total_open_risk", 0.03)
    slippage_model = str(raw.get("slippage_model", "volume_impact")).strip().lower()
    if slippage_model not in {"fixed", "volume_impact"}:
        raise ValueError("derivatives.slippage_model must be fixed or volume_impact")
    if leverage > 20 or maintenance >= 1 or liquidation_fee >= 1:
        raise ValueError("derivatives risk parameters are outside supported bounds")
    if risk_fraction > 1 or max_open_risk > 1:
        raise ValueError("derivatives risk fractions must be <= 1")
    return {
        "enabled": bool(raw.get("enabled", False)),
        "research_only": True,
        "symbols": list(dict.fromkeys(symbols)),
        "provider": provider,
        "providers": list(dict.fromkeys(providers)),
        "research_data_mode": data_mode,
        "interval": interval,
        "history_limit": int(_number(raw, "history_limit", 1000, 100)),
        "request_timeout_seconds": _number(raw, "request_timeout_seconds", derivatives_data.PERPETUAL_HTTP_TIMEOUT, 1.0),
        "request_attempts": int(_number(raw, "request_attempts", derivatives_data.PERPETUAL_HTTP_ATTEMPTS, 1)),
        "request_backoff_seconds": _number(raw, "request_backoff_seconds", derivatives_data.PERPETUAL_HTTP_BACKOFF_SECONDS, 0.0),
        "provider_cooldown_seconds": _number(raw, "provider_cooldown_seconds", 900.0, 0.0),
        "provider_failure_threshold": int(_number(raw, "provider_failure_threshold", 1, 1)),
        "cache_max_stale_minutes": _number(raw, "cache_max_stale_minutes", 720.0, 0.0),
        "cache_dir": str(raw.get("cache_dir") or ""),
        "sample_goal_min_trades": int(_number(raw, "sample_goal_min_trades", 30, 1)),
        "sample_goal_preferred_trades": int(_number(raw, "sample_goal_preferred_trades", 50, 1)),
        "system": raw.get("system", "system2"),
        "account_value": _number(raw, "account_value", 10000.0, 1.0),
        "risk_fraction": risk_fraction,
        "leverage": leverage,
        "maintenance_margin_rate": maintenance,
        "liquidation_fee_rate": liquidation_fee,
        "fee_rate": _number(raw, "fee_rate", 0.0004),
        "slippage_rate": _number(raw, "slippage_rate", 0.0005),
        "slippage_model": slippage_model,
        "slippage_impact_coefficient": _number(raw, "slippage_impact_coefficient", 0.001, 0.0),
        "max_slippage_rate": _number(raw, "max_slippage_rate", 0.01, 0.0),
        "max_total_open_risk": max_open_risk,
        "market_state_enabled": bool(raw.get("market_state_enabled", True)),
        "extreme_basis_pct": _number(raw, "extreme_basis_pct", 1.0, 0.0),
        "extreme_funding_rate": _number(raw, "extreme_funding_rate", 0.001, 0.0),
        "extreme_oi_change_pct": _number(raw, "extreme_oi_change_pct", 0.20, 0.0),
        "expansion_atr_ratio": _number(raw, "expansion_atr_ratio", 1.8, 1.0),
        "trend_efficiency_min": _number(raw, "trend_efficiency_min", 0.45, 0.0),
        "trend_move_min_pct": _number(raw, "trend_move_min_pct", 2.0, 0.0),
        "range_risk_multiplier": min(1.0, _number(raw, "range_risk_multiplier", 0.5, 0.0)),
        "expansion_risk_multiplier": min(1.0, _number(raw, "expansion_risk_multiplier", 0.5, 0.0)),
        "filters": raw.get("filters") or {
            "higher_timeframe": False,
            "volume_confirmation": False,
            "volatility_filter": False,
            "anomaly_filter": True,
        },
    }


def classify_market_state(snapshot, settings=None):
    """Classify a perpetual market using only fields already in a snapshot.

    This is a conservative research filter: extreme basis/funding/OI shocks
    pause new entries, while range and volatility-expansion states reduce
    their risk budget. Missing optional series never creates a risk flag.
    """
    settings = settings or {}
    bars = [row for row in (snapshot.get("contract_klines") or []) if isinstance(row, dict)]
    closes = [float(row["close"]) for row in bars if row.get("close") not in (None, "")]
    if len(closes) < 25:
        return {"state": "unknown", "risk_multiplier": 1.0, "flags": [], "metrics": {}}
    window = closes[-20:]
    first = window[0]
    last = window[-1]
    move_pct = abs(last - first) / first * 100 if first else 0.0
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    efficiency = abs(last - first) / path if path else 0.0
    ranges = [abs(float(row["high"]) - float(row["low"])) / float(row["close"])
              for row in bars if row.get("high") not in (None, "") and row.get("low") not in (None, "") and float(row.get("close") or 0) > 0]
    current_atr = sum(ranges[-5:]) / min(5, len(ranges)) if ranges else 0.0
    prior = ranges[-25:-5] if len(ranges) >= 10 else ranges[:-5]
    prior_atr = sorted(prior)[len(prior) // 2] if prior else current_atr
    atr_ratio = current_atr / prior_atr if prior_atr > 0 else 1.0
    flags = []
    mark = _latest_observation(snapshot.get("mark_price_klines"), bars[-1].get("time", 0))
    index = _latest_observation(snapshot.get("index_price_klines"), bars[-1].get("time", 0))
    basis_pct = ((float(mark["close"]) - float(index["close"])) / float(index["close"]) * 100
                 if mark and index and float(index.get("close") or 0) else None)
    if basis_pct is not None and abs(basis_pct) >= float(settings.get("extreme_basis_pct", 1.0)):
        flags.append("basis_extreme")
    funding = sorted(snapshot.get("funding_rates") or [], key=lambda row: int(row.get("time", 0)))
    funding_rate = float(funding[-1]["funding_rate"]) if funding and funding[-1].get("funding_rate") not in (None, "") else None
    if funding_rate is not None and abs(funding_rate) >= float(settings.get("extreme_funding_rate", 0.001)):
        flags.append("funding_extreme")
    oi = sorted(snapshot.get("open_interest") or [], key=lambda row: int(row.get("time", 0)))
    oi_change_pct = None
    if len(oi) >= 2 and float(oi[-2].get("open_interest") or 0) > 0:
        oi_change_pct = (float(oi[-1].get("open_interest")) / float(oi[-2].get("open_interest")) - 1) * 100
        if abs(oi_change_pct) >= float(settings.get("extreme_oi_change_pct", 0.20)) * 100:
            flags.append("oi_shock")
    if flags:
        state = "extreme_risk"
        multiplier = 0.0
    elif atr_ratio >= float(settings.get("expansion_atr_ratio", 1.8)):
        state = "volatility_expansion"
        multiplier = float(settings.get("expansion_risk_multiplier", 0.5))
    elif efficiency >= float(settings.get("trend_efficiency_min", 0.45)) and move_pct >= float(settings.get("trend_move_min_pct", 0.02)):
        state = "trend"
        multiplier = 1.0
    else:
        state = "range"
        multiplier = float(settings.get("range_risk_multiplier", 0.5))
    return {
        "state": state,
        "risk_multiplier": round(max(0.0, min(1.0, multiplier)), 4),
        "flags": flags,
        "metrics": {
            "move_pct": round(move_pct, 4), "trend_efficiency": round(efficiency, 4),
            "atr_ratio": round(atr_ratio, 4), "basis_pct": round(basis_pct, 6) if basis_pct is not None else None,
            "funding_rate": funding_rate, "oi_change_pct": round(oi_change_pct, 4) if oi_change_pct is not None else None,
        },
    }


def _signal_id(symbol, interval, bar_time, direction, system):
    return f"perp|{symbol}|{interval}|{int(bar_time)}|{direction}|{system}"


def parameter_snapshot(settings):
    return {
        key: settings[key] for key in (
            "account_value", "risk_fraction", "leverage",
            "maintenance_margin_rate", "liquidation_fee_rate",
            "fee_rate", "slippage_rate", "slippage_model", "slippage_impact_coefficient",
            "max_slippage_rate", "max_total_open_risk",
            "interval", "system", "market_state_enabled", "extreme_basis_pct",
            "extreme_funding_rate", "extreme_oi_change_pct", "expansion_atr_ratio",
            "trend_efficiency_min", "trend_move_min_pct", "range_risk_multiplier",
            "expansion_risk_multiplier",
        )
    } | {
        "max_units": sw.TURTLE_MAX_UNITS,
        "add_n": 0.5,
        "stop_n": 2.0,
        "filters": settings["filters"],
        "research_data_mode": settings["research_data_mode"],
        "execution_model": "signal_close_next_contract_bar_open",
        "risk_price": "mark_price",
        "margin_mode": "isolated_approximation",
        "sample_collection_phase": "phase_1_btc_eth_shadow_only",
    }


def parameter_checksum(parameters):
    canonical = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _series_time_bounds(rows):
    """Return the earliest/latest timestamps for a validated time series."""
    times = [int(row["time"]) for row in (rows or [])
             if isinstance(row, dict) and row.get("time") not in (None, "")]
    return (min(times), max(times)) if times else None


def _historical_coverage(snapshot, field, interval_ms):
    """Check whether a research series spans the contract-kline window.

    Funding settles on an approximately eight-hour cadence, so its boundary
    tolerance is wider than the per-bar OI series tolerance.
    """
    contract_bounds = _series_time_bounds(snapshot.get("contract_klines"))
    series_bounds = _series_time_bounds(snapshot.get(field))
    if not contract_bounds or not series_bounds:
        return False
    contract_start, contract_last_open = contract_bounds
    contract_end = contract_last_open + int(interval_ms)
    tolerance = (
        FUNDING_SETTLEMENT_INTERVAL_MS
        if field == "funding_rates" else int(interval_ms)
    )
    if field == "open_interest" and series_bounds[1] <= series_bounds[0]:
        return False
    return (
        series_bounds[0] <= contract_start + tolerance
        and series_bounds[1] >= contract_end - tolerance
    )


def _historical_density_ok(snapshot, field, interval_ms):
    """Reject research series with an excessive internal observation gap."""
    times = sorted({int(row["time"]) for row in (snapshot.get(field) or [])
                    if isinstance(row, dict) and row.get("time") not in (None, "")})
    if len(times) < 2:
        return False
    expected = FUNDING_SETTLEMENT_INTERVAL_MS if field == "funding_rates" else int(interval_ms)
    max_gap = max(right - left for left, right in zip(times, times[1:]))
    return max_gap <= expected * 3


def _trade_settings(trade, current):
    frozen = trade.get("parameter_snapshot")
    return frozen if isinstance(frozen, dict) else current


def _open_risk(state, exclude_id=None):
    return sum(
        float(item.get("risk_fraction") or 0) * max(1, len(item.get("units") or []))
        for item in state["open_trades"]
        if item.get("id") != exclude_id
    )


def _used_margin(state, exclude_id=None):
    total = 0.0
    for item in state["open_trades"]:
        if item.get("id") == exclude_id or item.get("status") != "open":
            continue
        total += derivatives_risk.margin_required(
            item["avg_entry"], item["quantity"], item["leverage"]
        )
    return total


def _funding_until(trade, funding, timestamp, state, fallback_mark=None):
    last_time = int(trade.get("last_funding_time") or 0)
    for event in funding:
        event_time = int(event["time"])
        if event_time <= last_time or event_time <= int(trade["entry_time"]) or event_time > timestamp:
            continue
        mark = event.get("mark_price")
        if mark in (None, ""):
            mark = fallback_mark
        if mark in (None, ""):
            trade["last_funding_time"] = event_time
            continue
        cashflow = derivatives_risk.funding_payment(
            derivatives_risk.position_notional(float(mark), trade["quantity"]),
            event["funding_rate"], trade["direction"],
        )
        trade["funding_cashflow"] += cashflow
        state["equity"] += cashflow
        trade["last_funding_time"] = event_time


def _latest_observation(rows, timestamp):
    eligible = [row for row in (rows or []) if int(row.get("time", 0)) <= int(timestamp)]
    return max(eligible, key=lambda row: int(row["time"])) if eligible else None


def _market_context(snapshot, timestamp):
    contract = _latest_observation(snapshot.get("contract_klines"), timestamp)
    mark = _latest_observation(snapshot.get("mark_price_klines"), timestamp)
    index = _latest_observation(snapshot.get("index_price_klines"), timestamp)
    oi = _latest_observation(snapshot.get("open_interest"), timestamp)
    mark_price = float(mark["close"]) if mark else None
    index_price = float(index["close"]) if index else None
    basis_pct = (
        (mark_price - index_price) / index_price * 100
        if mark_price is not None and index_price not in (None, 0) else None
    )
    return {
        "time": int(timestamp),
        "contract_close": float(contract["close"]) if contract else None,
        "mark_close": mark_price,
        "index_close": index_price,
        "basis_pct": round(basis_pct, 6) if basis_pct is not None else None,
        "open_interest": float(oi["open_interest"]) if oi else None,
        "open_interest_value": (
            float(oi["open_interest_value"])
            if oi and oi.get("open_interest_value") not in (None, "") else None
        ),
        "open_interest_time": int(oi["time"]) if oi else None,
    }


def _price_return_pct(trade, price, entry=None):
    entry = float(entry if entry is not None else trade.get("avg_entry"))
    value = float(price)
    if trade["direction"] == "long":
        return (value - entry) / entry * 100
    return (entry - value) / entry * 100


def _update_excursion(trade, favorable_price, adverse_price):
    # Excursion is measured from the first fill so later unit additions do not
    # rewrite the historical price path. PnL itself continues to use avg_entry.
    baseline = trade.get("initial_entry", trade.get("avg_entry"))
    favorable = _price_return_pct(trade, favorable_price, baseline)
    adverse = _price_return_pct(trade, adverse_price, baseline)
    trade["mfe_pct"] = round(max(float(trade.get("mfe_pct") or 0), favorable), 6)
    trade["mae_pct"] = round(min(float(trade.get("mae_pct") or 0), adverse), 6)


def _update_exit_excursion(trade, exit_price):
    """Include the known adverse exit trigger without inventing OHLC order."""
    baseline = trade.get("initial_entry", trade.get("avg_entry"))
    adverse = _price_return_pct(trade, exit_price, baseline)
    trade["mae_pct"] = round(min(float(trade.get("mae_pct") or 0), adverse), 6)


def _marked_equity(state):
    unrealized = 0.0
    for trade in state["open_trades"]:
        if trade.get("status") != "open" or trade.get("last_mark_price") is None:
            continue
        unrealized += _price_return_pct(trade, trade["last_mark_price"]) / 100 * float(trade["avg_entry"]) * float(trade["quantity"])
    return float(state["equity"]) + unrealized, unrealized


def _portfolio_exposure(state):
    """Return current notional, margin and directional exposure for open trades."""
    result = {"margin": 0.0, "notional": 0.0, "long_notional": 0.0,
              "short_notional": 0.0, "risk_fraction": 0.0}
    for trade in state.get("open_trades") or []:
        if trade.get("status") != "open":
            continue
        notional = float(trade.get("avg_entry") or 0) * float(trade.get("quantity") or 0)
        margin = derivatives_risk.margin_required(
            float(trade.get("avg_entry") or 0), float(trade.get("quantity") or 0),
            float(trade.get("leverage") or 1),
        ) if notional > 0 else 0.0
        result["notional"] += notional
        result["margin"] += margin
        result["risk_fraction"] += float(trade.get("risk_fraction") or 0) * max(1, len(trade.get("units") or []))
        side = "long_notional" if trade.get("direction") == "long" else "short_notional"
        result[side] += notional
    result["directional_peak"] = max(result["long_notional"], result["short_notional"])
    return result


def record_equity_snapshot(state):
    marked, unrealized = _marked_equity(state)
    exposure = _portfolio_exposure(state)
    times = [int(item.get("last_processed_bar") or item.get("entry_time") or 0)
             for item in state["open_trades"]]
    timestamp = int(state.get("last_market_time") or max(times + [0]))
    if timestamp <= 0:
        timestamp = int(state.get("updated_at_epoch_ms") or 0)
    point = {
        "time": timestamp,
        "realized_equity": round(float(state["equity"]), 8),
        "marked_equity": round(marked, 8),
        "unrealized_pnl": round(unrealized, 8),
        "open_count": len(state["open_trades"]),
    }
    point.update({
        "open_margin": round(exposure["margin"], 8),
        "open_notional": round(exposure["notional"], 8),
        "long_notional": round(exposure["long_notional"], 8),
        "short_notional": round(exposure["short_notional"], 8),
        "open_risk_fraction": round(exposure["risk_fraction"], 8),
    })
    curve = state.setdefault("equity_curve", [])
    # A provider fallback or delayed response can arrive with an older market
    # timestamp. Never append an out-of-order point to the persisted curve.
    if curve and timestamp < int(curve[-1].get("time") or 0):
        return curve[-1]
    if curve and int(curve[-1].get("time") or 0) == timestamp:
        curve[-1] = point
    else:
        curve.append(point)
    state["equity_curve"] = curve[-2000:]
    return point


def _adverse_fill(raw_price, direction, action, slippage, specs, opening=None):
    price = float(raw_price)
    is_buy = (direction == "long" and action == "entry") or (direction == "short" and action == "exit")
    if opening is not None:
        price = max(price, float(opening)) if is_buy else min(price, float(opening))
    price *= 1 + slippage if is_buy else 1 - slippage
    return derivatives_data.quantize_price(price, specs, "buy" if is_buy else "sell")


def _execution_slippage(settings, quantity, bar=None):
    detail = derivatives_risk.execution_slippage(
        settings["slippage_rate"], quantity,
        (bar or {}).get("volume"),
        settings.get("slippage_model", "fixed"),
        settings.get("slippage_impact_coefficient", 0.001),
        settings.get("max_slippage_rate", 0.01),
    )
    detail["liquidity_proxy_time"] = (bar or {}).get("time")
    detail["liquidity_proxy_volume"] = (bar or {}).get("volume")
    return detail


def _close_trade(trade, raw_exit, reason, exit_time, mark_open, settings, specs, state,
                 market_context=None, execution_bar=None):
    slippage = _execution_slippage(settings, trade["quantity"], execution_bar)
    fill = _adverse_fill(
        raw_exit, trade["direction"], "exit", slippage["rate"], specs,
        opening=mark_open,
    )
    gross = ((fill - trade["avg_entry"]) if trade["direction"] == "long"
             else (trade["avg_entry"] - fill)) * trade["quantity"]
    exit_fee = abs(fill * trade["quantity"]) * settings["fee_rate"]
    state["equity"] += gross - exit_fee
    trade["fees"] += exit_fee
    trade.update({
        "status": "closed",
        "exit_time": int(exit_time),
        "exit": fill,
        "exit_reason": reason,
        "gross_pnl": gross,
        "net_pnl": gross - trade["fees"] + trade["funding_cashflow"],
        "holding_hours": round((int(exit_time) - int(trade["entry_time"])) / 3600000, 4),
        "exit_market_context": market_context or {},
        "exit_slippage": slippage,
    })
    trade["return_pct"] = trade["net_pnl"] / settings["account_value"] * 100
    trade["funding_to_gross_pnl_pct"] = (
        round(abs(trade["funding_cashflow"]) / abs(gross) * 100, 4)
        if gross else 0.0
    )


def _fill_pending(trade, bar, settings, specs, state, snapshot=None, liquidity_bar=None):
    quantity = sw.turtle_unit_quantity(
        state["equity"], trade["n"], float(trade.get("risk_fraction") or settings["risk_fraction"]), 2.0
    )
    quantity = derivatives_data.quantize_quantity(quantity, specs)
    slippage = _execution_slippage(settings, quantity, liquidity_bar)
    entry = _adverse_fill(
        bar["open"], trade["direction"], "entry", slippage["rate"], specs
    )
    margin = derivatives_risk.margin_required(entry, quantity, settings["leverage"])
    available_margin = max(0.0, state["equity"] - _used_margin(state, exclude_id=trade["id"]))
    if (quantity <= 0 or not derivatives_data.quantity_is_executable(quantity, entry, specs)
            or margin > available_margin):
        trade.update({"status": "rejected", "rejection_reason": "contract_or_margin_constraint"})
        state["rejected_signals"].append(trade)
        return False
    fee = entry * quantity * settings["fee_rate"]
    state["equity"] -= fee
    stop = entry - 2 * trade["n"] if trade["direction"] == "long" else entry + 2 * trade["n"]
    trade.update({
        "status": "open", "entry": entry, "avg_entry": entry,
        "initial_entry": entry,
        "entry_time": int(bar["time"]), "quantity": quantity,
        "units": [{"price": entry, "quantity": quantity, "n": trade["n"],
                   "slippage": slippage}],
        "latest_entry": entry, "stop": stop, "fees": fee,
        "funding_cashflow": 0.0, "last_funding_time": 0,
        "mfe_pct": 0.0, "mae_pct": 0.0,
        "last_mark_price": None,
        "entry_market_context": _market_context(snapshot or {}, bar["time"]),
        "entry_slippage": slippage,
        "account_equity_snapshot": float(state["equity"] + fee),
        "estimated_margin": round(margin, 8),
        "estimated_max_loss": round(float(state["equity"] + fee) * float(trade.get("risk_fraction") or settings["risk_fraction"]), 8),
        "remaining_risk_capacity": max(0.0, float(settings["max_total_open_risk"]) - _open_risk(state)),
        # OHLC cannot reveal whether this bar's high/low happened before or
        # after the open fill, so risk management begins on the next bar.
        "last_processed_bar": int(bar["time"]),
    })
    return True


def update_trade(trade, snapshot, settings, state):
    settings = _trade_settings(trade, settings)
    contract = snapshot["contract_klines"]
    mark_map = {int(row["time"]): row for row in snapshot["mark_price_klines"]}
    funding = snapshot.get("funding_rates") or []
    specs = trade.get("contract_specs") or snapshot["contract_specs"]
    if trade["status"] == "pending_entry":
        fill_time = int(trade["fill_time"])
        fill_bar = next((row for row in contract if int(row["time"]) == fill_time), None)
        if not fill_bar:
            if contract and int(contract[-1]["time"]) > fill_time:
                trade.update({"status": "rejected", "rejection_reason": "entry_window_missed"})
                state["rejected_signals"].append(trade)
                return "rejected"
            return "pending"
        fill_index = contract.index(fill_bar)
        liquidity_bar = contract[fill_index - 1] if fill_index > 0 else None
        if not _fill_pending(
            trade, fill_bar, settings, specs, state, snapshot=snapshot,
            liquidity_bar=liquidity_bar,
        ):
            return "rejected"

    start = int(trade.get("last_processed_bar") or 0)
    if trade.get("status") == "open" and start:
        expected = start + sw.INTERVAL_MS[trade["interval"]]
        available = {int(row["time"]) for row in contract}
        if contract and int(contract[-1]["time"]) >= expected and expected not in available:
            raise RuntimeError(
                f"perpetual shadow data gap after {start}: expected bar {expected}"
            )
    for index, bar in enumerate(contract):
        bar_time = int(bar["time"])
        if bar_time < int(trade["entry_time"]) or bar_time <= start:
            continue
        mark = mark_map[bar_time]
        _funding_until(trade, funding, bar_time, state, fallback_mark=mark["close"])
        direction = trade["direction"]
        liq = derivatives_risk.liquidation_price(
            trade["avg_entry"], direction, settings["leverage"],
            settings["maintenance_margin_rate"], settings["liquidation_fee_rate"],
        )
        levels = sw.turtle_levels(contract, index, trade["system"], settings["interval"])
        exit_level = (levels["exit_low"] if direction == "long" else levels["exit_high"]) if levels else None
        high, low = float(mark["high"]), float(mark["low"])
        raw_exit = reason = None
        if direction == "long":
            if low <= liq:
                raw_exit, reason = liq, "liquidation"
            elif low <= trade["stop"]:
                raw_exit, reason = trade["stop"], "stop"
            elif exit_level is not None and low <= exit_level:
                raw_exit, reason = exit_level, "channel_exit"
        else:
            if high >= liq:
                raw_exit, reason = liq, "liquidation"
            elif high >= trade["stop"]:
                raw_exit, reason = trade["stop"], "stop"
            elif exit_level is not None and high >= exit_level:
                raw_exit, reason = exit_level, "channel_exit"
        if raw_exit is not None:
            _update_exit_excursion(trade, raw_exit)
            trade["last_mark_price"] = float(mark["close"])
            _close_trade(
                trade, raw_exit, reason, bar_time, mark["open"], settings, specs, state,
                market_context=_market_context(snapshot, bar_time),
                execution_bar=contract[index - 1] if index > 0 else None,
            )
            return "closed"

        favorable = high if direction == "long" else low
        adverse = low if direction == "long" else high
        _update_excursion(trade, favorable, adverse)

        while len(trade["units"]) < sw.TURTLE_MAX_UNITS:
            next_add = (trade["latest_entry"] + 0.5 * trade["n"] if direction == "long"
                        else trade["latest_entry"] - 0.5 * trade["n"])
            reached = high >= next_add if direction == "long" else low <= next_add
            if not reached:
                break
            quantity = derivatives_data.quantize_quantity(
                sw.turtle_unit_quantity(state["equity"], trade["n"], float(trade.get("risk_fraction") or settings["risk_fraction"]), 2.0), specs
            )
            slippage = _execution_slippage(
                settings, quantity, contract[index - 1] if index > 0 else None
            )
            fill = _adverse_fill(
                next_add, direction, "entry", slippage["rate"], specs,
                opening=mark["open"],
            )
            unit_risk = float(trade.get("risk_fraction") or settings["risk_fraction"])
            added_risk = _open_risk(state, exclude_id=trade["id"]) + unit_risk * (len(trade["units"]) + 1)
            added_margin = derivatives_risk.margin_required(fill, quantity, settings["leverage"])
            available_margin = max(0.0, state["equity"] - _used_margin(state, exclude_id=trade["id"])
                                   - derivatives_risk.margin_required(
                                       trade["avg_entry"], trade["quantity"], trade["leverage"]
                                   ))
            if (not derivatives_data.quantity_is_executable(quantity, fill, specs)
                    or added_risk > settings["max_total_open_risk"]
                    or added_margin > available_margin):
                break
            fee = fill * quantity * settings["fee_rate"]
            state["equity"] -= fee
            total_cost = trade["avg_entry"] * trade["quantity"] + fill * quantity
            trade["quantity"] += quantity
            trade["avg_entry"] = total_cost / trade["quantity"]
            trade["latest_entry"] = fill
            trade["stop"] = fill - 2 * trade["n"] if direction == "long" else fill + 2 * trade["n"]
            trade["fees"] += fee
            trade["units"].append({"price": fill, "quantity": quantity, "n": trade["n"],
                                   "slippage": slippage})
        trade["last_mark_price"] = float(mark["close"])
        trade["last_processed_bar"] = bar_time
    return "open"


def create_signal(snapshot, settings, state):
    bars = snapshot["contract_klines"]
    market_state = classify_market_state(snapshot, settings) if settings.get("market_state_enabled", True) else {
        "state": "disabled", "risk_multiplier": 1.0, "flags": [], "metrics": {},
    }
    direction, reasons, plan = sw.build_turtle_signal(
        bars, settings["system"], state["equity"], settings["risk_fraction"],
        settings["interval"], filter_options=settings["filters"],
    )
    if not direction or not plan:
        return None
    signal_time = int(bars[-1]["time"])
    signal_id = _signal_id(snapshot["symbol"], settings["interval"], signal_time, direction, settings["system"])
    if signal_id in state["seen_signal_ids"]:
        return None
    state["seen_signal_ids"].append(signal_id)
    if market_state["state"] == "extreme_risk":
        state["rejected_signals"].append({
            "id": signal_id, "status": "rejected", "rejection_reason": "market_state_extreme_risk",
            "market_state": market_state, "market_type": derivatives_data.MARKET_TYPE,
            "symbol": snapshot["symbol"], "provider": snapshot.get("venue", "unknown"),
            "research_only": True, "signal_bar_time": signal_time,
        })
        return None
    risk_multiplier = float(market_state.get("risk_multiplier", 1.0))
    effective_risk_fraction = settings["risk_fraction"] * risk_multiplier
    if effective_risk_fraction <= 0 or _open_risk(state) + effective_risk_fraction > settings["max_total_open_risk"]:
        state["rejected_signals"].append({
            "id": signal_id, "status": "rejected", "rejection_reason": "portfolio_risk_limit",
            "market_state": market_state,
            "market_type": derivatives_data.MARKET_TYPE, "symbol": snapshot["symbol"],
            "provider": snapshot.get("venue", "unknown"),
            "research_only": True, "signal_bar_time": signal_time,
        })
        return None
    parameters = parameter_snapshot(settings)
    account_equity = float(state["equity"])
    try:
        estimated_quantity = derivatives_data.quantize_quantity(
            sw.turtle_unit_quantity(account_equity, plan["n"], effective_risk_fraction, 2.0),
            snapshot["contract_specs"],
        )
    except (KeyError, TypeError, ValueError):
        # A malformed/legacy direct caller may not include exchange filters;
        # snapshot validation still rejects it before normal processing.
        estimated_quantity = 0.0
    estimated_margin = derivatives_risk.margin_required(
        float(plan["entry"]), estimated_quantity, settings["leverage"]
    ) if estimated_quantity > 0 else 0.0
    trade = {
        "id": signal_id, "market_type": derivatives_data.MARKET_TYPE,
        "research_only": True, "symbol": snapshot["symbol"],
        "provider": snapshot.get("venue", "unknown"),
        "interval": settings["interval"], "system": settings["system"],
        "direction": direction, "status": "pending_entry",
        "signal_bar_time": signal_time,
        "fill_time": signal_time + sw.INTERVAL_MS[settings["interval"]],
        "signal_expires_at": signal_time + sw.INTERVAL_MS[settings["interval"]],
        "entry_model": "next_contract_bar_open", "mark_price_risk": True,
        "risk_fraction": effective_risk_fraction, "configured_risk_fraction": settings["risk_fraction"],
        "market_state": market_state, "leverage": settings["leverage"],
        "entry_trigger": plan["entry"], "n": plan["n"],
        "account_equity_snapshot": account_equity,
        "estimated_quantity": estimated_quantity,
        "estimated_margin": round(estimated_margin, 8),
        "estimated_max_loss": round(account_equity * effective_risk_fraction, 8),
        "remaining_risk_capacity": max(0.0, settings["max_total_open_risk"] - _open_risk(state) - effective_risk_fraction),
        "signal_reasons": reasons, "contract_specs": snapshot["contract_specs"],
        "parameter_snapshot": parameters,
        "parameter_sha256": parameter_checksum(parameters),
    }
    state["open_trades"].append(trade)
    return trade


def _notification_event_id(event_type, trade):
    return "perp-notify|{}|{}|{}|{}".format(
        event_type, trade.get("id", ""), trade.get("status", ""),
        trade.get("exit_time") or trade.get("entry_time") or trade.get("fill_time") or
        len(trade.get("units") or []),
    )


def _notification_time(value):
    """Format a millisecond epoch for humans while keeping missing values clear."""
    try:
        return sw.format_time_pair(int(value)) if value not in (None, "", 0) else "--"
    except (TypeError, ValueError, OverflowError, OSError):
        return "--"


def _estimated_risk_fields(trade):
    """Return stable, display-ready risk figures stored on a shadow trade."""
    equity = float(trade.get("account_equity_snapshot") or 0)
    risk_fraction = float(trade.get("risk_fraction") or 0)
    margin = trade.get("estimated_margin")
    max_loss = trade.get("estimated_max_loss")
    remaining = trade.get("remaining_risk_capacity")
    return margin, max_loss if max_loss is not None else equity * risk_fraction, remaining


def _perpetual_notification_content(event_type, trade, snapshot=None):
    direction = "做多" if trade.get("direction") == "long" else "做空"
    symbol = trade.get("symbol", "UNKNOWN")
    interval = trade.get("interval", "")
    provider = trade.get("provider", "unknown")
    lines = [
        f"永续{direction} · {symbol} · {interval}",
        f"事件：{event_type}",
        f"Provider：{provider}",
        f"交易ID：{trade.get('id', '--')}",
    ]
    market_state = trade.get("market_state") or {}
    if market_state:
        lines.append(
            f"市场状态：{market_state.get('state', '--')} · 风险系数：{float(market_state.get('risk_multiplier', 1)) * 100:.0f}%"
            + (f" · 风险标记：{','.join(market_state.get('flags') or [])}" if market_state.get("flags") else "")
        )
    if event_type == "signal_created":
        trigger = trade.get("entry_trigger")
        n = trade.get("n")
        stop = (float(trigger) - 2 * float(n) if trade.get("direction") == "long"
                else float(trigger) + 2 * float(n)) if trigger not in (None, "") and n not in (None, "") else None
        lines.extend([
            f"触发价：{trigger if trigger is not None else '--'}",
            f"预定成交时间：{_notification_time(trade.get('fill_time'))}",
            f"信号有效期至：{_notification_time(trade.get('signal_expires_at') or trade.get('fill_time'))}",
            f"初始止损参考：{round(stop, 8) if stop is not None else '--'}",
            f"杠杆：{trade.get('leverage', '--')} · 风险占比：{float(trade.get('risk_fraction') or 0) * 100:.2f}%",
            "状态：等待下一根合约 K 线开盘影子成交",
        ])
        margin, max_loss, remaining = _estimated_risk_fields(trade)
        lines.append(f"预估保证金：{margin if margin is not None else '--'} · 预估最大亏损：{max_loss:.8f}")
        lines.append(f"风险占用：{float(trade.get('risk_fraction') or 0) * 100:.2f}% · 组合剩余容量：{remaining * 100:.2f}%" if remaining is not None else "组合剩余容量：--")
    elif event_type == "entry_filled":
        entry_slippage = trade.get("entry_slippage") or {}
        lines.extend([
            f"成交时间：{_notification_time(trade.get('entry_time'))}",
            f"成交价：{trade.get('entry', '--')} · 数量：{trade.get('quantity', '--')}",
            f"止损：{trade.get('stop', '--')} · 杠杆：{trade.get('leverage', '--')}",
            f"手续费：{float(trade.get('fees') or 0):.8f}",
            f"滑点：{float(entry_slippage.get('rate') or 0) * 100:.4f}% · 模型：{entry_slippage.get('model', '--')}",
        ])
        margin, max_loss, remaining = _estimated_risk_fields(trade)
        lines.append(f"保证金：{margin if margin is not None else '--'} · 预估最大亏损：{max_loss:.8f}")
    elif event_type == "scale_in":
        lines.extend([
            f"最新成交价：{trade.get('latest_entry', '--')} · 总数量：{trade.get('quantity', '--')}",
            f"单位数：{len(trade.get('units') or [])} · 新止损：{trade.get('stop', '--')}",
        ])
    elif event_type == "closed":
        exit_slippage = trade.get("exit_slippage") or {}
        lines.extend([
            f"出场时间：{_notification_time(trade.get('exit_time'))}",
            f"入场：{trade.get('entry', '--')} · 出场：{trade.get('exit', '--')}",
            f"原因：{trade.get('exit_reason', '--')} · 净盈亏：{float(trade.get('net_pnl') or 0):+.8f}",
            f"资金费：{float(trade.get('funding_cashflow') or 0):+.8f}",
            f"出场滑点：{float(exit_slippage.get('rate') or 0) * 100:.4f}% · 模型：{exit_slippage.get('model', '--')}",
        ])
    elif event_type == "rejected":
        lines.append(f"拒绝原因：{trade.get('rejection_reason', '--')}")
    context = _market_context(snapshot or {}, snapshot.get("contract_klines", [{}])[-1].get("time", 0)) if snapshot and snapshot.get("contract_klines") else {}
    if context:
        lines.append(
            f"标记价：{context.get('mark_close', '--')} · 指数价：{context.get('index_close', '--')} · "
            f"基差：{context.get('basis_pct', '--')}%"
        )
    lines.append("研究影子信号：需人工确认，不会自动下单")
    return "\n".join(lines)


def _collect_notification_events(before_state, state, signal, snapshot):
    events = []
    before = {item.get("id"): item for item in before_state.get("open_trades", [])}
    if signal and signal.get("id") not in before:
        events.append(("signal_created", signal, snapshot))
    for trade in state.get("open_trades", []):
        previous = before.get(trade.get("id"))
        if not previous:
            continue
        if previous.get("status") == "pending_entry" and trade.get("status") == "open":
            events.append(("entry_filled", trade, snapshot))
        elif len(trade.get("units") or []) > len(previous.get("units") or []):
            events.append(("scale_in", trade, snapshot))
    before_closed = {item.get("id") for item in before_state.get("closed_trades", [])}
    events.extend(("closed", trade, snapshot) for trade in state.get("closed_trades", [])
                   if trade.get("id") not in before_closed)
    before_rejected = {item.get("id") for item in before_state.get("rejected_signals", [])}
    events.extend(("rejected", trade, snapshot) for trade in state.get("rejected_signals", [])
                   if trade.get("id") not in before_rejected)
    return events


def dispatch_perpetual_notifications(events, state, config):
    """Send deduplicated lifecycle notifications through existing channels."""
    history = state.setdefault("notification_history", [])
    deliveries = []
    if not sw.has_channel(config):
        return deliveries
    sent_ids = {str(item.get("event_id")) for item in history if item.get("ok")}
    for event_type, trade, snapshot in events:
        event_id = _notification_event_id(event_type, trade)
        if event_id in sent_ids:
            continue
        title = f"CoinPulse 永续 {trade.get('symbol', 'UNKNOWN')} {event_type}"
        content = _perpetual_notification_content(event_type, trade, snapshot)
        results = sw.send_notification(title, content, config)
        ok = any(item.get("ok") for item in results)
        history.append({"event_id": event_id, "event_type": event_type,
                        "trade_id": trade.get("id"), "attempted_at_epoch_ms": int(time.time() * 1000),
                        "ok": ok, "results": results})
        deliveries.append({"event_id": event_id, "event_type": event_type,
                           "trade_id": trade.get("id"), "results": results})
        if ok:
            sent_ids.add(event_id)
    state["notification_history"] = history[-500:]
    return deliveries


def send_perpetual_test_notification(config):
    """Send a deterministic perpetual notification without market data or state writes."""
    now = int(time.time() * 1000)
    trade = {
        "id": "perp-test-notification",
        "market_type": derivatives_data.MARKET_TYPE,
        "research_only": True,
        "symbol": "BTCUSDT", "interval": "4h", "direction": "long",
        "provider": "test", "status": "pending_entry",
        "signal_bar_time": now - sw.INTERVAL_MS["4h"],
        "fill_time": now + sw.INTERVAL_MS["4h"],
        "signal_expires_at": now + sw.INTERVAL_MS["4h"],
        "entry_trigger": 100000.0, "n": 1000.0,
        "leverage": 2.0, "risk_fraction": 0.005,
        "account_equity_snapshot": 10000.0,
        "estimated_margin": 25.0, "estimated_max_loss": 50.0,
        "remaining_risk_capacity": 0.025,
    }
    if not sw.has_channel(config):
        return []
    return sw.send_notification(
        "CoinPulse 永续测试通知",
        _perpetual_notification_content("signal_created", trade),
        config,
    )


def process_snapshot(snapshot, settings, state):
    errors = derivatives_data.validate_perpetual_snapshot(snapshot, settings["interval"])
    if errors:
        raise RuntimeError("invalid perpetual shadow snapshot: " + "; ".join(errors))
    if not snapshot.get("contract_specs"):
        raise RuntimeError("perpetual shadow trading requires contract_specs")
    contract_status = snapshot["contract_specs"].get("status")
    allowed_statuses = (None, "", "live") if snapshot.get("venue") == "okx" else (None, "", "TRADING")
    if contract_status not in allowed_statuses:
        raise RuntimeError(f"perpetual contract is not tradable: status={contract_status}")
    symbol = snapshot["symbol"]
    snapshot_provider = snapshot.get("venue")
    open_for_symbol = [item for item in state["open_trades"] if item["symbol"] == symbol]
    bound_providers = {
        str(item.get("provider")).lower()
        for item in open_for_symbol
        if item.get("provider") not in (None, "", "unknown")
    }
    if bound_providers and snapshot_provider and str(snapshot_provider).lower() not in bound_providers:
        # A direct caller may provide a fallback snapshot. Preserve the
        # existing contract: skip it without advancing time or mutating trades.
        return None
    market_state = classify_market_state(snapshot, settings) if settings.get("market_state_enabled", True) else {
        "state": "disabled", "risk_multiplier": 1.0, "flags": [], "metrics": {},
    }
    state.setdefault("market_state_by_symbol", {})[symbol] = market_state
    if snapshot.get("contract_klines"):
        market_time = int(snapshot["contract_klines"][-1]["time"]) + sw.INTERVAL_MS[settings["interval"]]
        market_times = state.setdefault("market_time_by_symbol", {})
        market_times[snapshot["symbol"]] = max(
            int(market_times.get(snapshot["symbol"]) or 0), market_time
        )
    for trade in open_for_symbol:
        trade_provider = trade.get("provider")
        if (trade_provider not in (None, "", "unknown") and snapshot_provider
                and trade_provider != snapshot_provider):
            # Keep one shadow sample tied to one complete data source. A
            # provider failover must not silently splice another venue into
            # an existing position's price/funding path.
            continue
        result = update_trade(trade, snapshot, settings, state)
        if result in ("closed", "rejected"):
            state["open_trades"].remove(trade)
            if result == "closed":
                state["closed_trades"].append(trade)
    if not any(item["symbol"] == symbol for item in state["open_trades"]):
        return create_signal(snapshot, settings, state)
    return None


def build_stats(state, settings=None):
    closed = state["closed_trades"]
    settings = settings or {}
    minimum_goal = int(settings.get("sample_goal_min_trades", 30))
    preferred_goal = max(minimum_goal, int(settings.get("sample_goal_preferred_trades", 50)))
    progress_target = preferred_goal
    closed_count = len(closed)
    wins = [item for item in closed if float(item.get("net_pnl") or 0) > 0]
    funding = sum(float(item.get("funding_cashflow") or 0) for item in closed + state["open_trades"])
    marked_equity, unrealized = _marked_equity(state)
    curve = state.get("equity_curve") or []
    peak = None
    max_drawdown = 0.0
    for point in curve:
        value = float(point.get("marked_equity") or 0)
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    gross_total = sum(abs(float(item.get("gross_pnl") or 0)) for item in closed)
    absolute_funding = sum(abs(float(item.get("funding_cashflow") or 0)) for item in closed)
    exposure_points = state.get("equity_curve") or []
    max_margin_used = max((float(item.get("open_margin") or 0) for item in exposure_points), default=0.0)
    max_notional = max((float(item.get("open_notional") or 0) for item in exposure_points), default=0.0)
    max_directional_exposure = max((max(float(item.get("long_notional") or 0),
                                         float(item.get("short_notional") or 0))
                                    for item in exposure_points), default=0.0)
    max_open_risk = max((float(item.get("open_risk_fraction") or 0) for item in exposure_points), default=0.0)
    loss_streak = max_loss_streak = 0
    for trade in closed:
        if float(trade.get("net_pnl") or 0) < 0:
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
        else:
            loss_streak = 0
    provider_sample_counts = {}
    for trade in closed:
        provider = str(trade.get("provider") or "unknown")
        provider_sample_counts[provider] = provider_sample_counts.get(provider, 0) + 1
    slippage_samples = []
    for trade in closed + state["open_trades"]:
        for detail in [trade.get("entry_slippage"), trade.get("exit_slippage")]:
            if isinstance(detail, dict) and detail.get("rate") is not None:
                slippage_samples.append(detail)
        for unit in (trade.get("units") or [])[1:]:
            detail = unit.get("slippage") if isinstance(unit, dict) else None
            if isinstance(detail, dict) and detail.get("rate") is not None:
                slippage_samples.append(detail)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_type": derivatives_data.MARKET_TYPE,
        "research_only": True,
        "provider": settings.get("provider", "binance"),
        "provider_cooldown_seconds": float(settings.get("provider_cooldown_seconds", 0) or 0),
        "provider_failure_threshold": int(settings.get("provider_failure_threshold", 1) or 1),
        "research_data_mode": settings.get("research_data_mode", "price_only_research"),
        "equity": round(float(state["equity"]), 8),
        "marked_equity": round(marked_equity, 8),
        "unrealized_pnl": round(unrealized, 8),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "open_count": len(state["open_trades"]),
        "pending_entry_count": sum(item.get("status") == "pending_entry" for item in state["open_trades"]),
        "closed_count": closed_count,
        "closed_count_by_provider": provider_sample_counts,
        "max_margin_used": round(max_margin_used, 8),
        "max_open_notional": round(max_notional, 8),
        "max_direction_exposure": round(max_directional_exposure, 8),
        "max_open_risk_fraction": round(max_open_risk, 8),
        "max_consecutive_losses": max_loss_streak,
        "execution_slippage": {
            "model": settings.get("slippage_model", "fixed"),
            "sample_count": len(slippage_samples),
            "average_rate_pct": round(
                sum(float(item["rate"]) for item in slippage_samples) / len(slippage_samples) * 100, 6
            ) if slippage_samples else 0.0,
            "max_rate_pct": round(max((float(item["rate"]) for item in slippage_samples), default=0.0) * 100, 6),
            "fixed_fallback_count": sum(item.get("model") == "fixed_fallback" for item in slippage_samples),
        },
        "sample_goal_min_trades": minimum_goal,
        "sample_goal_preferred_trades": preferred_goal,
        "sample_progress_pct": round(min(100.0, closed_count / progress_target * 100), 2),
        "sample_next_milestone": (
            "minimum_goal" if closed_count < minimum_goal
            else "preferred_goal" if closed_count < preferred_goal
            else "complete"
        ),
        "sample_reliability": (
            "insufficient_sample" if len(closed) < minimum_goal
            else "observation_sample" if len(closed) < preferred_goal
            else "preferred_sample"
        ),
        "run_count": int(state.get("run_count") or 0),
        "consecutive_unavailable_runs": int(state.get("consecutive_unavailable_runs") or 0),
        "last_success_at_epoch_ms": state.get("last_success_at_epoch_ms"),
        "data_status": state.get("data_status") or "unknown",
        "last_successful_symbols": list(state.get("last_successful_symbols") or []),
        "symbol_health": dict(state.get("symbol_health") or {}),
        "provider_health": dict(state.get("provider_health") or {}),
        "market_time_by_symbol": dict(state.get("market_time_by_symbol") or {}),
        "market_state_by_symbol": dict(state.get("market_state_by_symbol") or {}),
        "healthy_symbols": sorted(
            symbol for symbol, health in (state.get("symbol_health") or {}).items()
            if health.get("status") == "healthy"
        ),
        "stale_symbols": sorted(
            symbol for symbol, health in (state.get("symbol_health") or {}).items()
            if health.get("status") in {"stale", "stale_cache"}
        ),
        "cached_symbols": sorted(
            symbol for symbol, health in (state.get("symbol_health") or {}).items()
            if health.get("status") == "stale_cache"
        ),
        "max_data_lag_minutes": round(max(
            (float(health.get("data_lag_minutes") or 0)
             for health in (state.get("symbol_health") or {}).values()), default=0
        ), 2),
        "rejected_count": len(state["rejected_signals"]),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else 0,
        "net_pnl": round(sum(float(item.get("net_pnl") or 0) for item in closed), 8),
        "funding_cashflow": round(funding, 8),
        "funding_to_gross_pnl_pct": round(absolute_funding / gross_total * 100, 4) if gross_total else 0,
        "avg_mfe_pct": round(sum(float(item.get("mfe_pct") or 0) for item in closed) / len(closed), 4) if closed else 0,
        "avg_mae_pct": round(sum(float(item.get("mae_pct") or 0) for item in closed) / len(closed), 4) if closed else 0,
        "avg_holding_hours": round(sum(float(item.get("holding_hours") or 0) for item in closed) / len(closed), 4) if closed else 0,
        "liquidations": sum(item.get("exit_reason") == "liquidation" for item in closed),
    }


def run(config, state_path=DEFAULT_STATE_PATH, stats_path=DEFAULT_STATS_PATH, fetcher=None):
    settings = shadow_settings(config)
    if not settings["enabled"]:
        return {"enabled": False, "processed_symbols": 0}
    state = load_state(state_path, settings["account_value"])
    state.setdefault("run_count", 0)
    state.setdefault("consecutive_unavailable_runs", 0)
    state.setdefault("last_success_at_epoch_ms", None)
    state.setdefault("last_successful_symbols", [])
    state.setdefault("symbol_health", {})
    state.setdefault("provider_health", {})
    state.setdefault("market_time_by_symbol", {})
    state.setdefault("market_state_by_symbol", {})
    fetchers = {"binance": derivatives_data.fetch_perpetual_snapshot,
                "okx": okx_data.fetch_perpetual_snapshot}
    errors = {}
    successful_market_times = {}
    symbol_health = {}
    notification_events = []
    for symbol in settings["symbols"]:
        provider_errors = {}
        provider_attempts = []
        selected = None
        switch_reason = None
        for provider in (["custom"] if fetcher else settings["providers"]):
          provider_key = f"{symbol}|{provider}"
          provider_health = state["provider_health"].setdefault(provider_key, {
              "consecutive_failures": 0, "last_failure_at_epoch_ms": None,
              "cooldown_until_epoch_ms": 0, "last_success_at_epoch_ms": None,
              "last_latency_ms": None,
          })
          now_ms = int(time.time() * 1000)
          cooldown_until = int(provider_health.get("cooldown_until_epoch_ms") or 0)
          if not fetcher and cooldown_until > now_ms:
            provider_errors[provider] = f"cooldown_until_epoch_ms={cooldown_until}"
            provider_attempts.append({
                "provider": provider, "status": "skipped_cooldown",
                "cooldown_until_epoch_ms": cooldown_until,
            })
            continue
          started = time.perf_counter()
          try:
            fetch_kwargs = {
                "limit": settings["history_limit"], "closed_only": True,
                "include_contract_specs": True,
            }
            fetch = fetcher or fetchers[provider]
            if fetcher is None:
                fetch_kwargs["allow_partial"] = True
                fetch_kwargs["request_timeout"] = settings["request_timeout_seconds"]
                fetch_kwargs["request_attempts"] = settings["request_attempts"]
                fetch_kwargs["request_backoff_seconds"] = settings["request_backoff_seconds"]
            snapshot = fetch(symbol, settings["interval"], **fetch_kwargs)
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            provider_attempts.append({
                "provider": provider, "status": "success", "latency_ms": latency_ms,
            })
            health = snapshot.get("data_health") or {}
            component_errors = dict(health.get("component_errors") or {})
            required = ("contract_klines", "mark_price_klines", "index_price_klines", "contract_specs")
            missing_required = [name for name in required if not snapshot.get(name)]
            bound_providers = {
                str(item.get("provider")).lower()
                for item in state.get("open_trades", [])
                if item.get("symbol") == symbol
                and item.get("provider") not in (None, "", "unknown")
            }
            snapshot_provider = str(snapshot.get("venue") or provider).lower()
            if bound_providers and snapshot_provider not in bound_providers:
                raise RuntimeError(
                    f"provider mismatch for open perpetual trade: bound={sorted(bound_providers)}, "
                    f"snapshot={snapshot_provider}"
                )
            if settings["research_data_mode"] == "full_perpetual_research":
                if not snapshot.get("funding_rates"):
                    missing_required.append("funding_rates")
                elif not _historical_coverage(
                    snapshot, "funding_rates", sw.INTERVAL_MS[settings["interval"]]
                ):
                    missing_required.append("funding_history_coverage")
                elif not _historical_density_ok(
                    snapshot, "funding_rates", sw.INTERVAL_MS[settings["interval"]]
                ):
                    missing_required.append("funding_history_gaps")
                if not snapshot.get("open_interest"):
                    missing_required.append("open_interest")
                elif (snapshot.get("collection") or {}).get("open_interest_coverage") == "latest_only":
                    missing_required.append("historical_open_interest")
                elif not _historical_coverage(
                    snapshot, "open_interest", sw.INTERVAL_MS[settings["interval"]]
                ):
                    missing_required.append("open_interest_coverage")
                elif not _historical_density_ok(
                    snapshot, "open_interest", sw.INTERVAL_MS[settings["interval"]]
                ):
                    missing_required.append("open_interest_gaps")
            if missing_required:
                raise RuntimeError("required perpetual data unavailable: " + ", ".join(missing_required))
            state_before_symbol = copy.deepcopy(state)
            signal = process_snapshot(snapshot, settings, state)
            notification_events.extend(
                _collect_notification_events(state_before_symbol, state, signal, snapshot)
            )
            _save_snapshot_cache(settings["cache_dir"], snapshot)
            provider_health.update({
                "consecutive_failures": 0,
                "last_success_at_epoch_ms": int(time.time() * 1000),
                "last_latency_ms": latency_ms,
                "cooldown_until_epoch_ms": 0,
            })
            if snapshot.get("contract_klines"):
                observed_time = int(snapshot["contract_klines"][-1]["time"]) + sw.INTERVAL_MS[settings["interval"]]
                successful_market_times[symbol] = max(
                    int(state["market_time_by_symbol"].get(symbol) or 0), observed_time
                )
            lag = max((float(value) for value in (health.get("data_lag_minutes") or {}).values()), default=0.0)
            lag_limit = sw.INTERVAL_MS[settings["interval"]] * 3 / 60000
            status = "stale" if lag > lag_limit else ("partial" if component_errors else "healthy")
            symbol_health[symbol] = {
                "status": status, "component_errors": component_errors,
                "data_lag_minutes": round(lag, 2),
                "latest_market_time": successful_market_times.get(symbol),
                "signal_status": "signal_created" if signal else "no_signal",
                "market_state": state.get("market_state_by_symbol", {}).get(symbol),
                "provider": snapshot.get("venue", provider),
                "provider_attempts": provider_attempts,
                "provider_switch_reason": switch_reason,
                "provider_latency_ms": latency_ms,
            }
            if any(item.get("status") == "error" for item in provider_attempts):
                symbol_health[symbol]["provider_switch_reason"] = (
                    f"fallback_after_{sum(item.get('status') == 'error' for item in provider_attempts)}_failure(s)"
                )
            elif any(item.get("status") == "skipped_cooldown" for item in provider_attempts):
                symbol_health[symbol]["provider_switch_reason"] = "fallback_after_provider_cooldown"
            selected = provider
            break
          except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            provider_errors[provider] = str(exc)
            failures = int(provider_health.get("consecutive_failures") or 0) + 1
            provider_health.update({
                "consecutive_failures": failures,
                "last_failure_at_epoch_ms": int(time.time() * 1000),
                "last_latency_ms": latency_ms,
            })
            if failures >= settings["provider_failure_threshold"]:
                provider_health["cooldown_until_epoch_ms"] = int(time.time() * 1000 + settings["provider_cooldown_seconds"] * 1000)
            provider_attempts.append({
                "provider": provider, "status": "error", "latency_ms": latency_ms,
                "error": str(exc), "consecutive_failures": failures,
            })
            if selected is None and len(provider_attempts) > 1:
                switch_reason = f"{provider_attempts[-2].get('provider')} failed; tried {provider}"
        if selected:
            continue
        error_text = " | ".join(f"{name}: {message}" for name, message in provider_errors.items())
        errors[symbol] = error_text
        cached = _load_snapshot_cache(
            settings["cache_dir"], symbol, settings["interval"],
            settings["cache_max_stale_minutes"],
        )
        if cached:
            _, age_minutes = cached
            symbol_health[symbol] = {
                "status": "stale_cache", "component_errors": provider_errors,
                "data_lag_minutes": age_minutes, "latest_market_time": None,
                "signal_status": "not_evaluated", "provider": "cache",
                "provider_attempts": provider_attempts,
                "provider_switch_reason": switch_reason,
                "provider_latency_ms": None,
            }
            continue
        symbol_health[symbol] = {
            "status": "stale" if "stale" in error_text.lower() else "unavailable",
            "component_errors": provider_errors,
            "data_lag_minutes": None, "latest_market_time": None,
            "signal_status": "not_evaluated", "provider": None,
            "provider_attempts": provider_attempts,
            "provider_switch_reason": switch_reason,
            "provider_latency_ms": None,
        }
    state["updated_at_epoch_ms"] = int(time.time() * 1000)
    state["last_errors"] = errors
    state["run_count"] = int(state.get("run_count") or 0) + 1
    state["last_successful_symbols"] = sorted(successful_market_times)
    state["market_time_by_symbol"].update(successful_market_times)
    available_times = [int(state["market_time_by_symbol"].get(symbol) or 0)
                       for symbol in settings["symbols"]]
    available_times = [value for value in available_times if value > 0]
    if available_times:
        state["last_market_time"] = max(
            int(state.get("last_market_time") or 0), min(available_times)
        )
    state["symbol_health"] = symbol_health
    if not errors and not any(item.get("status") == "partial" for item in symbol_health.values()):
        state["data_status"] = "healthy"
        state["consecutive_unavailable_runs"] = 0
        state["last_success_at_epoch_ms"] = state["updated_at_epoch_ms"]
    elif successful_market_times or any(item.get("status") == "stale_cache" for item in symbol_health.values()):
        state["data_status"] = "degraded"
        state["consecutive_unavailable_runs"] = 0
    else:
        state["data_status"] = "unavailable"
        state["consecutive_unavailable_runs"] = int(state.get("consecutive_unavailable_runs") or 0) + 1
    record_equity_snapshot(state)
    sw.atomic_write_json(state_path, state)
    stats = build_stats(state, settings)
    stats["errors"] = errors
    stats["requested_symbols"] = list(settings["symbols"])
    stats["successful_symbols"] = sorted(successful_market_times)
    stats["successful_market_times"] = successful_market_times
    stats["symbol_health"] = symbol_health
    stats["healthy_symbols"] = sorted(symbol for symbol, health in symbol_health.items() if health["status"] == "healthy")
    stats["stale_symbols"] = sorted(symbol for symbol, health in symbol_health.items() if health["status"] in {"stale", "stale_cache"})
    stats["cached_symbols"] = sorted(symbol for symbol, health in symbol_health.items() if health["status"] == "stale_cache")
    stats["max_data_lag_minutes"] = round(max((float(health.get("data_lag_minutes") or 0) for health in symbol_health.values()), default=0.0), 2)
    deliveries = dispatch_perpetual_notifications(notification_events, state, config)
    stats["notification_deliveries"] = deliveries
    stats["notification_history_count"] = len(state.get("notification_history") or [])
    # Persist notification deduplication state after dispatch so a rerun cannot
    # resend an already delivered lifecycle event.
    sw.atomic_write_json(state_path, state)
    sw.atomic_write_json(stats_path, stats)
    return {"enabled": True, "processed_symbols": len(settings["symbols"]) - len(errors), "errors": errors, "stats": stats}


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行 Binance USD-M 永续研究影子交易")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state", default=DEFAULT_STATE_PATH)
    parser.add_argument("--stats", default=DEFAULT_STATS_PATH)
    parser.add_argument(
        "--test-notification", action="store_true",
        help="发送永续专用测试推送，不读取行情且不写入运行状态",
    )
    args = parser.parse_args(argv)
    config = load_json(os.path.abspath(args.config), {})
    if args.test_notification:
        results = send_perpetual_test_notification(config)
        if not results:
            print("未配置推送渠道，永续测试通知未发送。", file=sys.stderr)
            return 1
        print("永续测试通知完成：成功 {}，失败 {}".format(
            sum(1 for item in results if item.get("ok")),
            sum(1 for item in results if not item.get("ok")),
        ))
        return 0 if any(item.get("ok") for item in results) else 1
    try:
        result = run(config, os.path.abspath(args.state), os.path.abspath(args.stats))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not result["enabled"]:
        print("永续影子交易未启用；保持 research_only，不写入状态。")
        return 0
    print(f"永续影子交易完成：成功 {result['processed_symbols']}，失败 {len(result['errors'])}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
