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

import console_output
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
MARKET_STATE_DEFAULTS = {
    "extreme_basis_pct": 1.0,
    "extreme_funding_rate": 0.001,
    "extreme_oi_change_pct": 0.20,
    "expansion_atr_ratio": 1.8,
    "trend_efficiency_min": 0.45,
    "trend_move_min_pct": 2.0,
    "range_risk_multiplier": 0.5,
    "expansion_risk_multiplier": 0.5,
}


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
        "signal_funnel": {},
        "last_signal_diagnostics_by_symbol": {},
        "provider_redundancy_status": "unknown",
        "provider_redundancy_events": [],
        "risk_tier_metadata_by_symbol": {},
        "risk_tier_health_by_symbol": {},
        "strategy_cohorts": {},
        "active_cohort": {},
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
    if not isinstance(state.get("signal_funnel"), dict):
        state["signal_funnel"] = {}
    if not isinstance(state.get("last_signal_diagnostics_by_symbol"), dict):
        state["last_signal_diagnostics_by_symbol"] = {}
    if not isinstance(state.get("provider_redundancy_events"), list):
        state["provider_redundancy_events"] = []
    if not isinstance(state.get("risk_tier_metadata_by_symbol"), dict):
        state["risk_tier_metadata_by_symbol"] = {}
    if not isinstance(state.get("risk_tier_health_by_symbol"), dict):
        state["risk_tier_health_by_symbol"] = {}
    state.setdefault("provider_redundancy_status", "unknown")
    if not isinstance(state.get("strategy_cohorts"), dict):
        state["strategy_cohorts"] = {}
    if not isinstance(state.get("active_cohort"), dict):
        state["active_cohort"] = {}
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


def _boolean(config, key, default):
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"derivatives.{key} must be boolean")
    return value


def _version_identifier(config, key, default):
    value = str(config.get(key) or default).strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")
    if not value or len(value) > 80 or any(char not in allowed for char in value):
        raise ValueError(f"derivatives.{key} must be a stable identifier")
    return value


def _tier_checksum(tiers):
    canonical = json.dumps(tiers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_symbol_tier_bindings(value):
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("derivatives.maintenance_margin_tiers_by_provider_symbol must be an object")
    normalized = {}
    for provider, symbols in value.items():
        venue = str(provider).strip().lower()
        if venue not in {"binance", "okx"} or not isinstance(symbols, dict):
            raise ValueError("symbol maintenance tiers require binance/okx provider objects")
        normalized[venue] = {}
        for symbol, raw_binding in symbols.items():
            symbol_value = derivatives_data.normalize_symbol(symbol)
            if not isinstance(raw_binding, dict):
                raise ValueError("symbol maintenance tier binding must be an object")
            tiers = derivatives_risk.normalize_maintenance_margin_tiers(
                raw_binding.get("tiers") or []
            )
            source = str(raw_binding.get("source") or "").strip()
            effective_at = str(raw_binding.get("effective_at") or "").strip()
            tier_version = str(raw_binding.get("tier_version") or "").strip()
            market_type = str(raw_binding.get("market_type") or derivatives_data.MARKET_TYPE)
            if market_type != derivatives_data.MARKET_TYPE:
                raise ValueError("symbol maintenance tiers must target linear_perpetual")
            if tiers and not (source and effective_at and tier_version):
                raise ValueError(
                    "non-empty symbol maintenance tiers require source, effective_at and tier_version"
                )
            if effective_at:
                try:
                    parsed = datetime.fromisoformat(effective_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("symbol maintenance tier effective_at must be ISO-8601") from exc
                if parsed.tzinfo is None:
                    raise ValueError("symbol maintenance tier effective_at must include a timezone")
            checksum = _tier_checksum(tiers)
            supplied = str(raw_binding.get("tier_checksum") or "").strip().lower()
            if supplied and supplied != checksum:
                raise ValueError("symbol maintenance tier checksum mismatch")
            normalized[venue][symbol_value] = {
                "provider": venue,
                "symbol": symbol_value,
                "market_type": derivatives_data.MARKET_TYPE,
                "tiers": tiers,
                "source": source,
                "effective_at": effective_at or None,
                "tier_version": tier_version or None,
                "tier_checksum": checksum,
            }
    return normalized


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
    if data_mode == "full_perpetual_research" and interval not in derivatives_data.OPEN_INTEREST_PERIODS:
        supported = ", ".join(sorted(derivatives_data.OPEN_INTEREST_PERIODS))
        raise ValueError(
            f"full_perpetual_research interval {interval!r} has no Binance OI support; "
            f"use one of: {supported}"
        )
    leverage = _number(raw, "max_leverage", 2.0, 0.01)
    maintenance = _number(raw, "maintenance_margin_rate", 0.005)
    margin_tiers = derivatives_risk.normalize_maintenance_margin_tiers(
        raw.get("maintenance_margin_tiers") or []
    )
    raw_provider_tiers = raw.get("maintenance_margin_tiers_by_provider") or {}
    if not isinstance(raw_provider_tiers, dict):
        raise ValueError("derivatives.maintenance_margin_tiers_by_provider must be an object")
    provider_tiers = {}
    for name, tiers in raw_provider_tiers.items():
        provider_name = str(name).strip().lower()
        if provider_name not in {"binance", "okx"}:
            raise ValueError("maintenance margin tier provider must be binance or okx")
        provider_tiers[provider_name] = derivatives_risk.normalize_maintenance_margin_tiers(tiers)
    provider_symbol_tiers = _normalize_symbol_tier_bindings(
        raw.get("maintenance_margin_tiers_by_provider_symbol") or {}
    )
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
    strategy_version = _version_identifier(
        raw, "strategy_version", "perp_turtle_system2_v1"
    )
    cohort_id = _version_identifier(
        raw, "cohort_id", "btc_eth_4h_baseline_v1"
    )
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
        "risk_tier_cache_ttl_minutes": _number(
            raw, "risk_tier_cache_ttl_minutes", 360.0, 0.0
        ),
        "risk_tier_cache_max_stale_minutes": max(
            _number(raw, "risk_tier_cache_ttl_minutes", 360.0, 0.0),
            _number(raw, "risk_tier_cache_max_stale_minutes", 1440.0, 0.0),
        ),
        "block_new_entries_on_risk_tier_unavailable": _boolean(
            raw, "block_new_entries_on_risk_tier_unavailable", True
        ),
        "allow_new_entries_on_stale_tier_cache": _boolean(
            raw, "allow_new_entries_on_stale_tier_cache", True
        ),
        "minimum_risk_tier_remaining_minutes_for_entry": _number(
            raw, "minimum_risk_tier_remaining_minutes_for_entry", 120.0, 0.0
        ),
        "market_data_max_lag_intervals": _number(
            raw, "market_data_max_lag_intervals", 1.05, 1.0
        ),
        "cache_dir": str(raw.get("cache_dir") or ""),
        "sample_goal_min_trades": int(_number(raw, "sample_goal_min_trades", 30, 1)),
        "sample_goal_preferred_trades": int(_number(raw, "sample_goal_preferred_trades", 50, 1)),
        "sample_group_goal_min_trades": int(_number(raw, "sample_group_goal_min_trades", 10, 1)),
        "system": raw.get("system", "system2"),
        "strategy_version": strategy_version,
        "cohort_id": cohort_id,
        "account_value": _number(raw, "account_value", 10000.0, 1.0),
        "risk_fraction": risk_fraction,
        "leverage": leverage,
        "maintenance_margin_rate": maintenance,
        "maintenance_margin_tiers": margin_tiers,
        "maintenance_margin_tiers_by_provider": provider_tiers,
        "maintenance_margin_tiers_by_provider_symbol": provider_symbol_tiers,
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
    settings = {**MARKET_STATE_DEFAULTS, **(settings or {})}
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
    true_ranges = []
    previous_close = None
    for row in bars:
        try:
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        except (KeyError, TypeError, ValueError):
            previous_close = None
            continue
        if close <= 0 or high < low:
            previous_close = close
            continue
        tr = max(high - low,
                 abs(high - previous_close) if previous_close is not None else high - low,
                 abs(low - previous_close) if previous_close is not None else high - low)
        true_ranges.append(tr / close)
        previous_close = close
    # Wilder ATR: seed with the first period mean, then recursively smooth.
    period = 14
    atr_series = []
    if len(true_ranges) >= period:
        atr = sum(true_ranges[:period]) / period
        atr_series.append(atr)
        for value in true_ranges[period:]:
            atr = ((period - 1) * atr + value) / period
            atr_series.append(atr)
    current_atr = sum(atr_series[-5:]) / min(5, len(atr_series)) if atr_series else 0.0
    prior = atr_series[-25:-5] if len(atr_series) >= 10 else atr_series[:-5]
    prior_atr = sorted(prior)[len(prior) // 2] if prior else current_atr
    atr_ratio = current_atr / prior_atr if prior_atr > 0 else 1.0
    flags = []
    mark = _latest_observation(snapshot.get("mark_price_klines"), bars[-1].get("time", 0))
    index = _latest_observation(snapshot.get("index_price_klines"), bars[-1].get("time", 0))
    basis_pct = ((float(mark["close"]) - float(index["close"])) / float(index["close"]) * 100
                 if mark and index and float(index.get("close") or 0) else None)
    if basis_pct is not None and abs(basis_pct) >= float(settings["extreme_basis_pct"]):
        flags.append("basis_extreme")
    funding = sorted(snapshot.get("funding_rates") or [], key=lambda row: int(row.get("time", 0)))
    funding_rate = float(funding[-1]["funding_rate"]) if funding and funding[-1].get("funding_rate") not in (None, "") else None
    if funding_rate is not None and abs(funding_rate) >= float(settings["extreme_funding_rate"]):
        flags.append("funding_extreme")
    oi = sorted(snapshot.get("open_interest") or [], key=lambda row: int(row.get("time", 0)))
    oi_change_pct = None
    if len(oi) >= 2 and float(oi[-2].get("open_interest") or 0) > 0:
        oi_change_pct = (float(oi[-1].get("open_interest")) / float(oi[-2].get("open_interest")) - 1) * 100
        if abs(oi_change_pct) >= float(settings["extreme_oi_change_pct"]) * 100:
            flags.append("oi_shock")
    if flags:
        state = "extreme_risk"
        multiplier = 0.0
    elif atr_ratio >= float(settings["expansion_atr_ratio"]):
        state = "volatility_expansion"
        multiplier = float(settings["expansion_risk_multiplier"])
    elif efficiency >= float(settings["trend_efficiency_min"]) and move_pct >= float(settings["trend_move_min_pct"]):
        state = "trend"
        multiplier = 1.0
    else:
        state = "range"
        multiplier = float(settings["range_risk_multiplier"])
    return {
        "state": state,
        "risk_multiplier": round(max(0.0, min(1.0, multiplier)), 4),
        "flags": flags,
        "metrics": {
            "move_pct": round(move_pct, 4), "trend_efficiency": round(efficiency, 4),
            "atr_method": "wilder_true_range",
            "atr_current": round(current_atr, 8), "atr_prior": round(prior_atr, 8),
            "atr_ratio": round(atr_ratio, 4), "basis_pct": round(basis_pct, 6) if basis_pct is not None else None,
            "funding_rate": funding_rate, "oi_change_pct": round(oi_change_pct, 4) if oi_change_pct is not None else None,
        },
    }


def _signal_id(symbol, interval, bar_time, direction, system, cohort_id="legacy"):
    return f"perp|{cohort_id}|{symbol}|{interval}|{int(bar_time)}|{direction}|{system}"


def parameter_snapshot(settings):
    return {
        key: settings[key] for key in (
            "account_value", "risk_fraction", "leverage",
            "maintenance_margin_rate", "liquidation_fee_rate",
            "maintenance_margin_tiers",
            "fee_rate", "slippage_rate", "slippage_model", "slippage_impact_coefficient",
            "max_slippage_rate", "max_total_open_risk",
            "interval", "system", "strategy_version", "cohort_id",
            "market_state_enabled", "extreme_basis_pct",
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
        "risk_parameter_provider": settings.get("risk_parameter_provider", "unknown"),
        "risk_parameter_symbol": settings.get("risk_parameter_symbol", "unknown"),
        "cohort_parameter_sha256": settings.get("cohort_parameter_sha256"),
        "maintenance_margin_tier_metadata": copy.deepcopy(
            settings.get("maintenance_margin_tier_metadata") or {
                "scope": "global_flat_or_legacy",
                "tier_checksum": _tier_checksum(settings.get("maintenance_margin_tiers") or []),
            }
        ),
        "sample_collection_phase": "phase_1_btc_eth_shadow_only",
    }


def cohort_parameter_snapshot(settings):
    keys = (
        "symbols", "research_data_mode", "interval", "system", "strategy_version",
        "account_value", "risk_fraction", "leverage", "maintenance_margin_rate",
        "maintenance_margin_tiers", "maintenance_margin_tiers_by_provider",
        "maintenance_margin_tiers_by_provider_symbol", "liquidation_fee_rate",
        "block_new_entries_on_risk_tier_unavailable",
        "allow_new_entries_on_stale_tier_cache",
        "minimum_risk_tier_remaining_minutes_for_entry",
        "fee_rate", "slippage_rate", "slippage_model", "slippage_impact_coefficient",
        "max_slippage_rate", "max_total_open_risk", "market_state_enabled",
        "extreme_basis_pct", "extreme_funding_rate", "extreme_oi_change_pct",
        "expansion_atr_ratio", "trend_efficiency_min", "trend_move_min_pct",
        "range_risk_multiplier", "expansion_risk_multiplier", "filters",
    )
    return {key: copy.deepcopy(settings.get(key)) for key in keys}


def _bind_strategy_cohort(state, settings):
    cohort_id = settings["cohort_id"]
    parameters = cohort_parameter_snapshot(settings)
    checksum = parameter_checksum(parameters)
    registry = state.setdefault("strategy_cohorts", {})
    existing = registry.get(cohort_id)
    now_ms = int(time.time() * 1000)
    if not isinstance(existing, dict):
        existing = {
            "cohort_id": cohort_id,
            "strategy_version": settings["strategy_version"],
            "parameter_sha256": checksum,
            "parameter_snapshot": parameters,
            "registered_at_epoch_ms": now_ms,
        }
        registry[cohort_id] = existing
    matches = (
        existing.get("strategy_version") == settings["strategy_version"]
        and existing.get("parameter_sha256") == checksum
    )
    active = {
        "cohort_id": cohort_id,
        "strategy_version": settings["strategy_version"],
        "parameter_sha256": checksum,
        "registered_parameter_sha256": existing.get("parameter_sha256"),
        "status": "active" if matches else "parameter_mismatch",
        "new_entries_enabled": bool(matches),
    }
    state["active_cohort"] = active
    bound = dict(settings)
    bound["cohort_parameter_sha256"] = checksum
    bound["new_entries_enabled"] = bool(matches)
    bound["cohort_status"] = active["status"]
    return bound


def _provider_settings(settings, provider, symbol=None, snapshot=None):
    """Bind risk tiers to the venue and instrument that own the trade path."""
    bound = dict(settings)
    venue = str(provider or "unknown").strip().lower()
    symbol_value = derivatives_data.normalize_symbol(symbol) if symbol else "unknown"
    provider_tiers = settings.get("maintenance_margin_tiers_by_provider") or {}
    metadata = {
        "scope": "global",
        "tier_checksum": _tier_checksum(settings.get("maintenance_margin_tiers") or []),
    }
    if venue in provider_tiers:
        bound["maintenance_margin_tiers"] = copy.deepcopy(provider_tiers[venue])
        metadata = {
            "scope": "provider",
            "provider": venue,
            "tier_checksum": _tier_checksum(bound["maintenance_margin_tiers"]),
        }
    binding = (
        (settings.get("maintenance_margin_tiers_by_provider_symbol") or {})
        .get(venue, {}).get(symbol_value)
    )
    if binding:
        bound["maintenance_margin_tiers"] = copy.deepcopy(binding["tiers"])
        metadata = {key: copy.deepcopy(value) for key, value in binding.items() if key != "tiers"}
        metadata["scope"] = "provider_symbol"
    official_tiers = (snapshot or {}).get("maintenance_margin_tiers") or []
    if not bound.get("maintenance_margin_tiers") and venue == "okx" and official_tiers:
        bound["maintenance_margin_tiers"] = (
            derivatives_risk.normalize_maintenance_margin_tiers(official_tiers)
        )
        metadata = copy.deepcopy(
            (snapshot or {}).get("maintenance_margin_tier_metadata") or {}
        )
        metadata.update({
            "scope": "provider_symbol_official_snapshot",
            "provider": venue,
            "symbol": symbol_value,
            "tier_checksum": _tier_checksum(bound["maintenance_margin_tiers"]),
        })
    bound["risk_parameter_provider"] = venue
    bound["risk_parameter_symbol"] = symbol_value
    bound["maintenance_margin_tier_metadata"] = metadata
    return bound


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


def _funding_until(trade, funding, timestamp, state, fallback_mark=None,
                   mark_rows=None, interval_ms=None):
    last_time = int(trade.get("last_funding_time") or 0)
    for event in funding:
        event_time = int(event["time"])
        if event_time <= last_time or event_time <= int(trade["entry_time"]) or event_time > timestamp:
            continue
        mark, estimated, source = derivatives_risk.funding_mark_at_settlement(
            event, mark_rows, interval_ms, fallback_mark
        )
        if mark is None:
            trade["funding_mark_unavailable_count"] = int(trade.get("funding_mark_unavailable_count") or 0) + 1
            trade["last_funding_time"] = event_time
            continue
        cashflow = derivatives_risk.funding_payment(
            derivatives_risk.position_notional(float(mark), trade["quantity"]),
            event["funding_rate"], trade["direction"],
        )
        trade["funding_cashflow"] += cashflow
        trade["funding_settlement_count"] = int(trade.get("funding_settlement_count") or 0) + 1
        if estimated:
            trade["funding_mark_estimated_count"] = int(trade.get("funding_mark_estimated_count") or 0) + 1
        sources = trade.setdefault("funding_mark_sources", {})
        sources[source] = int(sources.get(source, 0)) + 1
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
        _record_contract_constraint(state, trade)
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
        "funding_settlement_count": 0, "funding_mark_estimated_count": 0,
        "funding_mark_unavailable_count": 0, "funding_mark_sources": {},
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
        _funding_until(
            trade, funding, bar_time, state,
            fallback_mark=mark["close"], mark_rows=snapshot.get("mark_price_klines"),
            interval_ms=sw.INTERVAL_MS[trade["interval"]],
        )
        direction = trade["direction"]
        liq = derivatives_risk.liquidation_price(
            trade["avg_entry"], direction, settings["leverage"],
            settings["maintenance_margin_rate"], settings["liquidation_fee_rate"],
            quantity=trade["quantity"],
            maintenance_margin_tiers=settings.get("maintenance_margin_tiers"),
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


SIGNAL_FUNNEL_COUNTERS = (
    "evaluated_bars", "insufficient_history", "no_breakout",
    "raw_breakout_candidates", "filter_rejections",
    "market_state_rejections", "risk_budget_rejections",
    "duplicate_signals", "cohort_rejections", "risk_tier_rejections", "final_entries",
    "contract_constraint_rejections",
)


def _signal_funnel_state(state):
    funnel = state.setdefault("signal_funnel", {})
    for key in SIGNAL_FUNNEL_COUNTERS:
        funnel[key] = int(funnel.get(key) or 0)
    if not isinstance(funnel.get("rejection_reasons"), dict):
        funnel["rejection_reasons"] = {}
    if not isinstance(funnel.get("last_evaluated_bar_by_symbol"), dict):
        funnel["last_evaluated_bar_by_symbol"] = {}
    return funnel


def _breakout_diagnostic(snapshot, market_state, plan, reasons, outcome):
    bars = snapshot.get("contract_klines") or []
    signal_time = int(bars[-1]["time"]) if bars else None
    price = float((plan or {}).get("price", bars[-1].get("close") if bars else 0) or 0)
    long_threshold = (plan or {}).get("long_breakout_threshold")
    short_threshold = (plan or {}).get("short_breakout_threshold")

    def distance_pct(threshold, direction):
        if threshold in (None, "") or price <= 0:
            return None
        gap = float(threshold) - price if direction == "long" else price - float(threshold)
        return round(gap / price * 100, 6)

    return {
        "symbol": snapshot.get("symbol"),
        "provider": snapshot.get("venue", "unknown"),
        "signal_bar_time": signal_time,
        "outcome": outcome,
        "candidate_direction": (plan or {}).get("candidate_direction"),
        "filter_stage": (plan or {}).get("filter_stage"),
        "reasons": list(reasons or []),
        "price": price or None,
        "n": (plan or {}).get("n"),
        "long_breakout_threshold": long_threshold,
        "short_breakout_threshold": short_threshold,
        "distance_to_long_breakout_pct": distance_pct(long_threshold, "long"),
        "distance_to_short_breakout_pct": distance_pct(short_threshold, "short"),
        "filter_metrics": dict((plan or {}).get("filter_metrics") or {}),
        "market_state": market_state,
    }


def _record_signal_evaluation(state, diagnostic, counter, rejection_reason=None):
    symbol = diagnostic.get("symbol")
    signal_time = diagnostic.get("signal_bar_time")
    state.setdefault("last_signal_diagnostics_by_symbol", {})[symbol] = diagnostic
    funnel = _signal_funnel_state(state)
    last_by_symbol = funnel["last_evaluated_bar_by_symbol"]
    if signal_time is None or int(last_by_symbol.get(symbol) or -1) == int(signal_time):
        return False
    last_by_symbol[symbol] = int(signal_time)
    funnel["evaluated_bars"] += 1
    funnel[counter] += 1
    if counter not in {"insufficient_history", "no_breakout"}:
        funnel["raw_breakout_candidates"] += 1
    if rejection_reason:
        reasons = funnel["rejection_reasons"]
        reasons[rejection_reason] = int(reasons.get(rejection_reason) or 0) + 1
    return True


def _record_contract_constraint(state, trade):
    if trade.get("funnel_contract_constraint_recorded"):
        return
    funnel = _signal_funnel_state(state)
    funnel["contract_constraint_rejections"] += 1
    reasons = funnel["rejection_reasons"]
    reason = "contract_or_margin_constraint"
    reasons[reason] = int(reasons.get(reason) or 0) + 1
    trade["funnel_contract_constraint_recorded"] = True


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
        outcome = "insufficient_history" if plan is None else (
            "filtered" if plan.get("filtered") or plan.get("blocked") else "no_breakout"
        )
        counter = {
            "insufficient_history": "insufficient_history",
            "filtered": "filter_rejections",
            "no_breakout": "no_breakout",
        }[outcome]
        diagnostic = _breakout_diagnostic(snapshot, market_state, plan, reasons, outcome)
        rejection_reason = (plan or {}).get("filter_reason") or (reasons[0] if reasons else None)
        _record_signal_evaluation(state, diagnostic, counter, rejection_reason)
        return None
    plan = {
        **plan,
        "candidate_direction": direction,
        "price": float(bars[-1]["close"]),
    }
    levels = sw.turtle_levels(bars, len(bars) - 1, settings["system"], settings["interval"])
    if levels:
        buffer_value = float(settings["filters"].get("breakout_buffer_n", 0.0)) * float(plan["n"])
        plan.update({
            "long_breakout_threshold": float(levels["entry_high"]) + buffer_value,
            "short_breakout_threshold": float(levels["entry_low"]) - buffer_value,
        })
    if not settings.get("new_entries_enabled", True):
        rejection_reason = str(
            settings.get("new_entry_block_reason") or "cohort_parameter_mismatch"
        )
        counter = (
            "risk_tier_rejections"
            if rejection_reason.startswith("risk_tier_")
            else "cohort_rejections"
        )
        diagnostic = _breakout_diagnostic(
            snapshot, market_state, plan, reasons, rejection_reason
        )
        _record_signal_evaluation(
            state, diagnostic, counter, rejection_reason
        )
        return None
    signal_time = int(bars[-1]["time"])
    signal_id = _signal_id(
        snapshot["symbol"], settings["interval"], signal_time, direction,
        settings["system"], settings["cohort_id"],
    )
    if signal_id in state["seen_signal_ids"]:
        diagnostic = _breakout_diagnostic(snapshot, market_state, plan, reasons, "duplicate_signal")
        _record_signal_evaluation(state, diagnostic, "duplicate_signals", "duplicate_signal")
        return None
    state["seen_signal_ids"].append(signal_id)
    if market_state["state"] == "extreme_risk":
        state["rejected_signals"].append({
            "id": signal_id, "status": "rejected", "rejection_reason": "market_state_extreme_risk",
            "market_state": market_state, "market_type": derivatives_data.MARKET_TYPE,
            "symbol": snapshot["symbol"], "provider": snapshot.get("venue", "unknown"),
            "research_only": True, "signal_bar_time": signal_time,
        })
        diagnostic = _breakout_diagnostic(snapshot, market_state, plan, reasons, "market_state_rejected")
        _record_signal_evaluation(
            state, diagnostic, "market_state_rejections", "market_state_extreme_risk"
        )
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
        diagnostic = _breakout_diagnostic(snapshot, market_state, plan, reasons, "risk_budget_rejected")
        _record_signal_evaluation(
            state, diagnostic, "risk_budget_rejections", "portfolio_risk_limit"
        )
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
        "strategy_version": settings["strategy_version"],
        "cohort_id": settings["cohort_id"],
        "cohort_parameter_sha256": settings.get("cohort_parameter_sha256"),
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
        "risk_tier_health_at_signal": copy.deepcopy(
            (state.get("risk_tier_health_by_symbol") or {}).get(snapshot["symbol"]) or {}
        ),
    }
    state["open_trades"].append(trade)
    diagnostic = _breakout_diagnostic(snapshot, market_state, plan, reasons, "signal_created")
    _record_signal_evaluation(state, diagnostic, "final_entries")
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
        f"策略版本：{trade.get('strategy_version', 'legacy_unknown')} · Cohort：{trade.get('cohort_id', 'legacy_unknown')}",
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


def process_snapshot(snapshot, settings, state, component_errors=None, now_ms=None):
    settings = _provider_settings(
        settings, snapshot.get("venue"), snapshot.get("symbol"), snapshot=snapshot
    )
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
    state.setdefault("risk_tier_metadata_by_symbol", {})[symbol] = copy.deepcopy(
        settings.get("maintenance_margin_tier_metadata") or {}
    )
    effective_component_errors = dict(
        component_errors
        if component_errors is not None
        else ((snapshot.get("data_health") or {}).get("component_errors") or {})
    )
    risk_tier_health = _update_risk_tier_health(
        state, symbol, snapshot, settings, effective_component_errors,
        int(time.time() * 1000) if now_ms is None else int(now_ms),
    )
    settings = _apply_risk_tier_entry_policy(settings, risk_tier_health)
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


def _funding_quality(trade):
    settled = int(trade.get("funding_settlement_count") or 0)
    estimated = int(trade.get("funding_mark_estimated_count") or 0)
    unavailable = int(trade.get("funding_mark_unavailable_count") or 0)
    if unavailable:
        return "has_unavailable_mark"
    if settled <= 0:
        return "no_settlement"
    if estimated <= 0:
        return "exact_settlement_mark"
    if estimated >= settled:
        return "estimated_mark_only"
    return "mixed_exact_and_estimated"


def _market_state_label(trade):
    value = trade.get("market_state")
    if isinstance(value, dict):
        return str(value.get("state") or "unknown")
    return str(value or "unknown")


def _sample_breakdown(closed, group_goal):
    dimensions = {
        "provider": lambda trade: str(trade.get("provider") or "unknown"),
        "symbol": lambda trade: str(trade.get("symbol") or "unknown"),
        "strategy_version": lambda trade: str(trade.get("strategy_version") or "legacy_unknown"),
        "cohort_id": lambda trade: str(trade.get("cohort_id") or "legacy_unknown"),
        "market_state": _market_state_label,
        "funding_mark_quality": _funding_quality,
    }
    result = {}
    for dimension, key_fn in dimensions.items():
        buckets = {}
        for trade in closed:
            buckets.setdefault(key_fn(trade), []).append(trade)
        result[dimension] = {}
        for name, trades in sorted(buckets.items()):
            wins = sum(float(trade.get("net_pnl") or 0) > 0 for trade in trades)
            count = len(trades)
            result[dimension][name] = {
                "count": count,
                "wins": wins,
                "losses": count - wins,
                "win_rate": round(wins / count * 100, 2),
                "net_pnl": round(sum(float(trade.get("net_pnl") or 0) for trade in trades), 8),
                "average_return_pct": round(
                    sum(float(trade.get("return_pct") or 0) for trade in trades) / count, 4
                ),
                "minimum_goal": int(group_goal),
                "remaining_to_minimum": max(0, int(group_goal) - count),
                "reliability": "observation_sample" if count >= int(group_goal) else "insufficient_sample",
            }
    return result


def _configured_sample_coverage(closed, settings, group_goal):
    coverage = {}
    for symbol in settings.get("symbols") or []:
        count = sum(str(trade.get("symbol")) == str(symbol) for trade in closed)
        coverage[str(symbol)] = {
            "count": count,
            "minimum_goal": int(group_goal),
            "remaining_to_minimum": max(0, int(group_goal) - count),
        }
    return coverage


def _update_risk_tier_health(state, symbol, snapshot, settings, component_errors, now_ms):
    """Persist secret-free official-tier freshness and fallback diagnostics."""
    provider = str(snapshot.get("venue") or "unknown").lower()
    metadata = copy.deepcopy(
        (state.get("risk_tier_metadata_by_symbol") or {}).get(symbol) or {}
    )
    previous = copy.deepcopy((state.get("risk_tier_health_by_symbol") or {}).get(symbol) or {})
    ttl = float(settings.get("risk_tier_cache_ttl_minutes", 360.0))
    max_stale = float(settings.get("risk_tier_cache_max_stale_minutes", 1440.0))
    scope = metadata.get("scope") or (
        "provider_symbol_official_snapshot" if metadata.get("source_parameters") else None
    )
    symbol_bindings = settings.get("maintenance_margin_tiers_by_provider_symbol") or {}
    configured_override = bool(
        (symbol_bindings.get(provider, {}).get(symbol) or {}).get("tiers")
        or (settings.get("maintenance_margin_tiers_by_provider") or {}).get(provider)
        or settings.get("maintenance_margin_tiers")
    )
    cache_status = metadata.get("cache_status")
    age = metadata.get("cache_age_minutes")
    try:
        age = max(0.0, float(age)) if age is not None else None
    except (TypeError, ValueError):
        age = None
    remaining = max(0.0, max_stale - age) if age is not None else None
    warning_window = max(60.0, max_stale * 0.1)
    refresh_failed = bool(
        cache_status == "stale_fallback"
        or "maintenance_margin_tiers" in component_errors
        or "maintenance_margin_tiers_refresh" in component_errors
    )
    if scope in {"provider_symbol", "provider", "global"} and configured_override:
        status = "configured_override"
        consecutive = 0
        expiry_risk = "not_applicable"
    elif scope == "provider_symbol_official_snapshot" and cache_status in {
            "live_refresh", "fresh_cache", "stale_fallback"}:
        status = cache_status
        consecutive = int(previous.get("consecutive_refresh_failures") or 0) + 1 if refresh_failed else 0
        expiry_risk = "warning" if remaining is not None and remaining <= warning_window else "normal"
    elif provider == "okx" and refresh_failed:
        status = "official_unavailable"
        consecutive = int(previous.get("consecutive_refresh_failures") or 0) + 1
        expiry_risk = "expired_or_missing"
    else:
        status = "fixed_fallback" if provider == "okx" else "not_applicable"
        consecutive = int(previous.get("consecutive_refresh_failures") or 0) if provider == "okx" else 0
        expiry_risk = "expired_or_missing" if provider == "okx" else "not_applicable"
    tracked_refresh_failure = refresh_failed and status in {
        "stale_fallback", "official_unavailable",
    }
    refresh_error_category = metadata.get("refresh_error_category")
    if tracked_refresh_failure and not refresh_error_category:
        refresh_error_category = "official_tier_fetch_failed"
    if not tracked_refresh_failure and status not in {"stale_fallback", "official_unavailable"}:
        refresh_error_category = None
    health = {
        "provider": provider,
        "scope": scope or "flat",
        "status": status,
        "cache_status": cache_status,
        "cache_age_minutes": round(age, 2) if age is not None else None,
        "cache_ttl_minutes": ttl,
        "cache_max_stale_minutes": max_stale,
        "remaining_stale_minutes": round(remaining, 2) if remaining is not None else None,
        "expiry_risk": expiry_risk,
        "consecutive_refresh_failures": consecutive,
        "last_checked_at_epoch_ms": int(now_ms),
        "last_refresh_failure_at_epoch_ms": (
            int(now_ms) if tracked_refresh_failure
            else previous.get("last_refresh_failure_at_epoch_ms")
        ),
        "retrieved_at_epoch_ms": metadata.get("retrieved_at_epoch_ms"),
        "tier_version": metadata.get("tier_version"),
        "tier_checksum": metadata.get("tier_checksum"),
        "refresh_error_category": refresh_error_category,
    }
    state.setdefault("risk_tier_health_by_symbol", {})[symbol] = health
    return health


def _apply_risk_tier_entry_policy(settings, health):
    """Block only new entries when the bound OKX risk model is unsafe."""
    bound = dict(settings)
    status = str((health or {}).get("status") or "not_applicable")
    remaining = (health or {}).get("remaining_stale_minutes")
    block_reason = None
    if status in {"official_unavailable", "fixed_fallback"}:
        if settings.get("block_new_entries_on_risk_tier_unavailable", True):
            block_reason = f"risk_tier_{status}"
    elif status == "stale_fallback":
        if not settings.get("allow_new_entries_on_stale_tier_cache", True):
            block_reason = "risk_tier_stale_fallback_disabled"
        else:
            minimum = float(
                settings.get("minimum_risk_tier_remaining_minutes_for_entry", 120.0)
            )
            if remaining is None:
                block_reason = "risk_tier_stale_remaining_unknown"
            elif float(remaining) < minimum:
                block_reason = "risk_tier_stale_near_expiry"
    tier_entries_allowed = block_reason is None
    health["new_entries_allowed"] = tier_entries_allowed
    health["new_entry_block_reason"] = block_reason
    health["minimum_remaining_minutes_for_entry"] = float(
        settings.get("minimum_risk_tier_remaining_minutes_for_entry", 120.0)
    )
    if block_reason:
        bound["new_entries_enabled"] = False
        bound["new_entry_block_reason"] = block_reason
    elif not bound.get("new_entries_enabled", True):
        bound.setdefault("new_entry_block_reason", "cohort_parameter_mismatch")
    else:
        bound.pop("new_entry_block_reason", None)
    return bound


def _risk_tier_health_summary(state, now_ms=None):
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    by_symbol = copy.deepcopy(state.get("risk_tier_health_by_symbol") or {})
    for health in by_symbol.values():
        if not isinstance(health, dict) or not health.get("cache_status"):
            continue
        try:
            retrieved = int(health.get("retrieved_at_epoch_ms") or 0)
            max_stale = float(health.get("cache_max_stale_minutes") or 0)
        except (TypeError, ValueError):
            continue
        if retrieved <= 0 or current_ms < retrieved or max_stale <= 0:
            continue
        age = round((current_ms - retrieved) / 60000, 2)
        remaining = round(max(0.0, max_stale - age), 2)
        health["cache_age_minutes"] = age
        health["remaining_stale_minutes"] = remaining
        warning_window = max(60.0, max_stale * 0.1)
        health["expiry_risk"] = "warning" if remaining <= warning_window else "normal"
    official = {symbol: health for symbol, health in by_symbol.items() if health.get("provider") == "okx"}
    degraded = sorted(
        symbol for symbol, health in official.items()
        if health.get("status") in {"stale_fallback", "official_unavailable", "fixed_fallback"}
        or health.get("expiry_risk") in {"warning", "expired_or_missing"}
    )
    unavailable = sorted(
        symbol for symbol, health in official.items()
        if health.get("status") in {"official_unavailable", "fixed_fallback"}
    )
    entry_blocked = sorted(
        symbol for symbol, health in by_symbol.items()
        if health.get("new_entries_allowed") is False
    )
    in_use = [health for health in official.values() if health.get("cache_status")]
    overall = (
        "not_in_use" if not official
        else "unavailable" if unavailable and len(unavailable) == len(official)
        else "degraded" if degraded
        else "healthy"
    )
    remaining_values = [
        float(health["remaining_stale_minutes"]) for health in in_use
        if health.get("remaining_stale_minutes") is not None
    ]
    return {
        "status": overall,
        "by_symbol": by_symbol,
        "official_symbols": sorted(official),
        "degraded_symbols": degraded,
        "unavailable_symbols": unavailable,
        "entry_blocked_symbols": entry_blocked,
        "max_consecutive_refresh_failures": max((
            int(health.get("consecutive_refresh_failures") or 0) for health in official.values()
        ), default=0),
        "minimum_remaining_stale_minutes": round(min(remaining_values), 2) if remaining_values else None,
    }


def _provider_redundancy_summary(settings, symbol_health):
    configured = list(settings.get("providers") or [])
    if len(configured) < 2:
        return {
            "status": "not_configured", "configured_providers": configured,
            "degraded_symbols": [], "unavailable_symbols": [],
        }
    degraded = []
    unavailable = []
    for symbol, health in symbol_health.items():
        attempts = health.get("provider_attempts") or []
        if not health.get("provider") or health.get("provider") == "cache":
            unavailable.append(symbol)
        elif any(item.get("status") in {"error", "skipped_cooldown"} for item in attempts):
            degraded.append(symbol)
    status = "unavailable" if unavailable and len(unavailable) == len(symbol_health) else (
        "degraded_redundancy" if degraded or unavailable else "healthy"
    )
    return {
        "status": status,
        "configured_providers": configured,
        "degraded_symbols": sorted(degraded),
        "unavailable_symbols": sorted(unavailable),
    }


def _market_data_freshness(snapshot, settings):
    """Assess only price series required by every research mode."""
    required = ("contract_klines", "mark_price_klines", "index_price_klines")
    health = snapshot.get("data_health") or {}
    reported = health.get("data_lag_minutes") or {}
    by_component = {}
    for name in required:
        if name in reported:
            try:
                value = float(reported[name])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                by_component[name] = round(value, 2)
    interval_minutes = sw.INTERVAL_MS[settings["interval"]] / 60000
    limit = interval_minutes * float(settings["market_data_max_lag_intervals"])
    lag = max(by_component.values()) if len(by_component) == len(required) else None
    return {
        "status": "unknown" if lag is None else ("stale" if lag > limit else "fresh"),
        "lag_minutes": round(lag, 2) if lag is not None else None,
        "max_lag_minutes": round(limit, 2),
        "max_lag_intervals": float(settings["market_data_max_lag_intervals"]),
        "component_lag_minutes": by_component,
    }


def _reconciliation_detail(state, symbol, snapshot, settings):
    interval_ms = sw.INTERVAL_MS[settings["interval"]]
    previous_close = int((state.get("market_time_by_symbol") or {}).get(symbol) or 0)
    bars = snapshot.get("contract_klines") or []
    latest_close = (int(bars[-1]["time"]) + interval_ms) if bars else 0
    if previous_close <= 0 or latest_close <= 0:
        return {
            "reconciliation_uncertain": True,
            "uncertainty_reason": "missing_market_time_checkpoint",
            "replayed_bar_count": None,
            "previous_market_time": previous_close or None,
            "recovered_market_time": latest_close or None,
            "conservative_rule": "liquidation_then_stop_then_channel_exit_before_scale_in",
        }
    replayed = max(0, (latest_close - previous_close) // interval_ms)
    return {
        "reconciliation_uncertain": replayed > 0,
        "uncertainty_reason": (
            "ohlc_path_unknown_during_replayed_bars" if replayed > 0 else None
        ),
        "replayed_bar_count": int(replayed),
        "previous_market_time": previous_close,
        "recovered_market_time": latest_close,
        "conservative_rule": "liquidation_then_stop_then_channel_exit_before_scale_in",
    }


def build_stats(state, settings=None):
    closed = state["closed_trades"]
    settings = settings or {}
    minimum_goal = int(settings.get("sample_goal_min_trades", 30))
    preferred_goal = max(minimum_goal, int(settings.get("sample_goal_preferred_trades", 50)))
    group_goal = int(settings.get("sample_group_goal_min_trades", 10))
    progress_target = preferred_goal
    closed_count = len(closed)
    active_cohort = state.get("active_cohort") or {}
    active_cohort_id = active_cohort.get("cohort_id") or settings.get("cohort_id")
    active_cohort_closed = [
        trade for trade in closed if trade.get("cohort_id") == active_cohort_id
    ] if active_cohort_id else []
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
    funding_settlements = sum(int(item.get("funding_settlement_count") or 0)
                              for item in closed + state["open_trades"])
    funding_estimated = sum(int(item.get("funding_mark_estimated_count") or 0)
                            for item in closed + state["open_trades"])
    funding_unavailable = sum(int(item.get("funding_mark_unavailable_count") or 0)
                              for item in closed + state["open_trades"])
    funding_mark_sources = {}
    for trade in closed + state["open_trades"]:
        for source, count in (trade.get("funding_mark_sources") or {}).items():
            funding_mark_sources[source] = funding_mark_sources.get(source, 0) + int(count)
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
    symbol_tier_bindings = settings.get("maintenance_margin_tiers_by_provider_symbol") or {}
    symbol_tier_binding_count = sum(len(symbols) for symbols in symbol_tier_bindings.values())
    active_tier_metadata = state.get("risk_tier_metadata_by_symbol") or {}
    has_tiered_margins = bool(settings.get("maintenance_margin_tiers")) or any(
        bool(tiers) for tiers in (settings.get("maintenance_margin_tiers_by_provider") or {}).values()
    ) or any(
        bool(binding.get("tiers"))
        for symbols in symbol_tier_bindings.values() for binding in symbols.values()
    ) or any(
        isinstance(metadata, dict) and metadata.get("scope") == "provider_symbol_official_snapshot"
        for metadata in active_tier_metadata.values()
    )
    tier_collections = [list(settings.get("maintenance_margin_tiers") or [])]
    tier_collections.extend(
        list(tiers or [])
        for tiers in (settings.get("maintenance_margin_tiers_by_provider") or {}).values()
    )
    tier_collections.extend(
        list(binding.get("tiers") or [])
        for symbols in symbol_tier_bindings.values() for binding in symbols.values()
    )
    tier_collections.extend(
        list((trade.get("parameter_snapshot") or {}).get("maintenance_margin_tiers") or [])
        for trade in closed + state["open_trades"]
    )
    deduction_tiers = next((
        tiers for tiers in tier_collections
        if any(float(item.get("maintenance_amount") or 0) > 0 for item in tiers)
    ), None)
    liquidation_version_input = (
        deduction_tiers if deduction_tiers is not None
        else [{}] if has_tiered_margins else []
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_type": derivatives_data.MARKET_TYPE,
        "research_only": True,
        "provider": settings.get("provider", "binance"),
        "provider_cooldown_seconds": float(settings.get("provider_cooldown_seconds", 0) or 0),
        "provider_failure_threshold": int(settings.get("provider_failure_threshold", 1) or 1),
        "market_data_max_lag_intervals": float(
            settings.get("market_data_max_lag_intervals", 1.05) or 1.05
        ),
        "research_data_mode": settings.get("research_data_mode", "price_only_research"),
        "equity": round(float(state["equity"]), 8),
        "marked_equity": round(marked_equity, 8),
        "unrealized_pnl": round(unrealized, 8),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "open_count": len(state["open_trades"]),
        "pending_entry_count": sum(item.get("status") == "pending_entry" for item in state["open_trades"]),
        "closed_count": closed_count,
        "strategy_version": settings.get("strategy_version", "legacy_unknown"),
        "cohort_id": active_cohort_id,
        "cohort_status": active_cohort.get("status", "unknown"),
        "new_entries_enabled": bool(active_cohort.get("new_entries_enabled", True)),
        "active_cohort_parameter_sha256": active_cohort.get("parameter_sha256"),
        "active_cohort_closed_count": len(active_cohort_closed),
        "active_cohort_remaining_to_minimum": max(0, minimum_goal - len(active_cohort_closed)),
        "active_cohort_remaining_to_preferred": max(0, preferred_goal - len(active_cohort_closed)),
        "active_cohort_reliability": (
            "insufficient_sample" if len(active_cohort_closed) < minimum_goal
            else "observation_sample" if len(active_cohort_closed) < preferred_goal
            else "preferred_sample"
        ),
        "strategy_cohorts": {
            name: {
                "strategy_version": item.get("strategy_version"),
                "parameter_sha256": item.get("parameter_sha256"),
                "registered_at_epoch_ms": item.get("registered_at_epoch_ms"),
            }
            for name, item in (state.get("strategy_cohorts") or {}).items()
            if isinstance(item, dict)
        },
        "closed_count_by_provider": provider_sample_counts,
        "sample_group_goal_min_trades": group_goal,
        "sample_breakdown": _sample_breakdown(closed, group_goal),
        "configured_symbol_sample_coverage": _configured_sample_coverage(
            closed, settings, group_goal
        ),
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
        "liquidation_model": {
            "model_version": derivatives_risk.liquidation_model_version(
                liquidation_version_input
            ),
            "maintenance_amount_supported": True,
            "maintenance_margin_rate": float(settings.get("maintenance_margin_rate", 0.005)),
            "maintenance_margin_tiers": list(settings.get("maintenance_margin_tiers") or []),
            "maintenance_margin_tiers_by_provider": copy.deepcopy(
                settings.get("maintenance_margin_tiers_by_provider") or {}
            ),
            "maintenance_margin_tiers_by_provider_symbol": copy.deepcopy(
                symbol_tier_bindings
            ),
            "provider_symbol_binding_count": symbol_tier_binding_count,
            "active_tier_metadata_by_symbol": copy.deepcopy(active_tier_metadata),
            "official_tier_cache_policy": {
                "ttl_minutes": float(settings.get("risk_tier_cache_ttl_minutes", 360.0)),
                "max_stale_minutes": float(
                    settings.get("risk_tier_cache_max_stale_minutes", 1440.0)
                ),
            },
            "liquidation_fee_rate": float(settings.get("liquidation_fee_rate", 0.0)),
            "limitations": [
                "isolated_margin_approximation",
                "no_wallet_balance_or_partial_liquidation_model",
                "manual_or_official_venue_tiers",
            ],
        },
        "sample_goal_min_trades": minimum_goal,
        "sample_goal_preferred_trades": preferred_goal,
        "sample_progress_pct": round(min(100.0, closed_count / progress_target * 100), 2),
        "sample_remaining_to_minimum": max(0, minimum_goal - closed_count),
        "sample_remaining_to_preferred": max(0, preferred_goal - closed_count),
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
        "risk_tier_health": _risk_tier_health_summary(state),
        "provider_redundancy": copy.deepcopy(state.get("provider_redundancy") or {
            "status": state.get("provider_redundancy_status", "unknown"),
            "configured_providers": list(settings.get("providers") or []),
            "degraded_symbols": [], "unavailable_symbols": [],
        }),
        "provider_redundancy_events": list(state.get("provider_redundancy_events") or [])[-100:],
        "signal_funnel": copy.deepcopy(_signal_funnel_state(state)),
        "last_signal_diagnostics_by_symbol": copy.deepcopy(
            state.get("last_signal_diagnostics_by_symbol") or {}
        ),
        "data_recovery_events": list(state.get("data_recovery_events") or [])[-100:],
        "reconciliation_uncertain_count": sum(
            bool(event.get("reconciliation_uncertain"))
            for event in (state.get("data_recovery_events") or [])
        ),
        "data_unavailable_hold_symbols": sorted(
            symbol for symbol, health in (state.get("symbol_health") or {}).items()
            if health.get("position_status") == "data_unavailable_hold"
        ),
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
        "max_market_data_lag_minutes": round(max(
            (float(health.get("market_data_lag_minutes") or 0)
             for health in (state.get("symbol_health") or {}).values()), default=0
        ), 2),
        "rejected_count": len(state["rejected_signals"]),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 2) if closed else 0,
        "net_pnl": round(sum(float(item.get("net_pnl") or 0) for item in closed), 8),
        "funding_cashflow": round(funding, 8),
        "funding_settlement_count": funding_settlements,
        "funding_mark_estimated_count": funding_estimated,
        "funding_mark_unavailable_count": funding_unavailable,
        "funding_mark_sources": funding_mark_sources,
        "funding_mark_estimated_pct": round(
            funding_estimated / funding_settlements * 100, 4
        ) if funding_settlements else 0.0,
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
    state.setdefault("data_recovery_events", [])
    settings = _bind_strategy_cohort(state, settings)
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
          attempt_component_errors = {}
          attempt_market_freshness = None
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
                if provider == "okx":
                    fetch_kwargs["open_interest_limit"] = (
                        settings["history_limit"]
                        if settings["research_data_mode"] == "full_perpetual_research"
                        else 2
                    )
                    fetch_kwargs["risk_tier_cache_dir"] = settings["cache_dir"]
                    fetch_kwargs["risk_tier_cache_ttl_minutes"] = (
                        settings["risk_tier_cache_ttl_minutes"]
                    )
                    fetch_kwargs["risk_tier_cache_max_stale_minutes"] = (
                        settings["risk_tier_cache_max_stale_minutes"]
                    )
            snapshot = fetch(symbol, settings["interval"], **fetch_kwargs)
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            health = snapshot.get("data_health") or {}
            component_errors = dict(health.get("component_errors") or {})
            attempt_component_errors = component_errors
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
                detail = "; ".join(
                    f"{name}={message}" for name, message in sorted(component_errors.items())
                )
                message = "required perpetual data unavailable: " + ", ".join(missing_required)
                raise RuntimeError(message + (f"; component_errors: {detail}" if detail else ""))
            market_freshness = _market_data_freshness(snapshot, settings)
            attempt_market_freshness = market_freshness
            if market_freshness["status"] == "stale":
                raise RuntimeError(
                    "stale perpetual market data: "
                    f"lag_minutes={market_freshness['lag_minutes']}; "
                    f"max_lag_minutes={market_freshness['max_lag_minutes']}"
                )
            state_before_symbol = copy.deepcopy(state)
            had_data_hold = (
                (state.get("symbol_health", {}).get(symbol) or {}).get("status") in
                {"stale_cache", "stale", "unavailable"}
                and any(item.get("symbol") == symbol and item.get("status") == "open"
                        for item in state.get("open_trades", []))
            )
            reconciliation = (
                _reconciliation_detail(state, symbol, snapshot, settings)
                if had_data_hold else None
            )
            candidate_state = copy.deepcopy(state)
            processing_now_ms = int(time.time() * 1000)
            signal = process_snapshot(
                snapshot, settings, candidate_state,
                component_errors=component_errors, now_ms=processing_now_ms,
            )
            risk_tier_health = copy.deepcopy(
                (candidate_state.get("risk_tier_health_by_symbol") or {}).get(symbol) or {}
            )
            _save_snapshot_cache(settings["cache_dir"], snapshot)
            pending_notifications = _collect_notification_events(
                state_before_symbol, candidate_state, signal, snapshot
            )
            if had_data_hold:
                candidate_state.setdefault("data_recovery_events", []).append({
                    "symbol": symbol, "at_epoch_ms": int(time.time() * 1000),
                    "provider": snapshot.get("venue", provider),
                    "status": "data_recovered_reconcile",
                    **reconciliation,
                })
                candidate_state["data_recovery_events"] = candidate_state["data_recovery_events"][-100:]
            candidate_provider_health = candidate_state["provider_health"].setdefault(provider_key, {})
            candidate_provider_health.update({
                "consecutive_failures": 0,
                "last_success_at_epoch_ms": int(time.time() * 1000),
                "last_latency_ms": latency_ms,
                "cooldown_until_epoch_ms": 0,
            })
            latest_market_time = None
            if snapshot.get("contract_klines"):
                observed_time = int(snapshot["contract_klines"][-1]["time"]) + sw.INTERVAL_MS[settings["interval"]]
                latest_market_time = max(
                    int(candidate_state["market_time_by_symbol"].get(symbol) or 0), observed_time
                )
            lag = max((float(value) for value in (health.get("data_lag_minutes") or {}).values()), default=0.0)
            status = "partial" if component_errors else "healthy"
            signal_diagnostic = copy.deepcopy(
                (candidate_state.get("last_signal_diagnostics_by_symbol") or {}).get(symbol)
            )
            current_signal_time = int(snapshot["contract_klines"][-1]["time"])
            if ((signal_diagnostic or {}).get("signal_bar_time") != current_signal_time):
                signal_diagnostic = None
            has_open_position = any(
                item.get("symbol") == symbol and item.get("status") in {"pending_entry", "open"}
                for item in candidate_state.get("open_trades", [])
            )
            next_symbol_health = {
                "status": status, "component_errors": component_errors,
                "data_lag_minutes": round(lag, 2),
                "market_data_lag_minutes": market_freshness["lag_minutes"],
                "market_data_max_lag_minutes": market_freshness["max_lag_minutes"],
                "market_data_freshness": market_freshness["status"],
                "market_data_component_lag_minutes": market_freshness["component_lag_minutes"],
                "latest_market_time": latest_market_time,
                "signal_status": "signal_created" if signal else (
                    "position_open" if has_open_position
                    else (signal_diagnostic or {}).get("outcome") or "not_evaluated"
                ),
                "signal_diagnostic": signal_diagnostic,
                "market_state": candidate_state.get("market_state_by_symbol", {}).get(symbol),
                "provider": snapshot.get("venue", provider),
                "provider_attempts": provider_attempts,
                "provider_switch_reason": switch_reason,
                "provider_latency_ms": latency_ms,
                "position_status": "data_recovered_reconcile" if had_data_hold else "normal",
                "reconciliation_uncertain": bool(
                    reconciliation and reconciliation["reconciliation_uncertain"]
                ),
                "risk_tier_health": copy.deepcopy(risk_tier_health),
                "new_entries_enabled": bool(
                    risk_tier_health.get("new_entries_allowed", True)
                    and settings.get("new_entries_enabled", True)
                ),
                "new_entry_block_reason": (
                    risk_tier_health.get("new_entry_block_reason")
                    or settings.get("new_entry_block_reason")
                ),
            }
            if any(item.get("status") == "error" for item in provider_attempts):
                next_symbol_health["provider_switch_reason"] = (
                    f"fallback_after_{sum(item.get('status') == 'error' for item in provider_attempts)}_failure(s)"
                )
            elif any(item.get("status") == "skipped_cooldown" for item in provider_attempts):
                next_symbol_health["provider_switch_reason"] = "fallback_after_provider_cooldown"
            provider_attempts.append({
                "provider": provider, "status": "success", "latency_ms": latency_ms,
                "market_data_freshness": market_freshness,
            })
            # Commit this symbol atomically only after all processing and
            # derived health calculations succeed.
            state.clear()
            state.update(candidate_state)
            notification_events.extend(pending_notifications)
            if latest_market_time is not None:
                successful_market_times[symbol] = latest_market_time
            symbol_health[symbol] = next_symbol_health
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
                "component_errors": attempt_component_errors,
                "market_data_freshness": attempt_market_freshness,
            })
            if selected is None and len(provider_attempts) > 1:
                switch_reason = f"{provider_attempts[-2].get('provider')} failed; tried {provider}"
        if selected:
            continue
        error_text = " | ".join(f"{name}: {message}" for name, message in provider_errors.items())
        errors[symbol] = error_text
        known_freshness = [
            item.get("market_data_freshness") for item in provider_attempts
            if isinstance(item.get("market_data_freshness"), dict)
            and item["market_data_freshness"].get("lag_minutes") is not None
        ]
        best_freshness = min(
            known_freshness, key=lambda item: float(item["lag_minutes"]), default=None
        )
        fallback_freshness = best_freshness or {
            "status": "unknown", "lag_minutes": None,
            "max_lag_minutes": round(
                sw.INTERVAL_MS[settings["interval"]] / 60000
                * settings["market_data_max_lag_intervals"], 2
            ),
            "component_lag_minutes": {},
        }
        cached = _load_snapshot_cache(
            settings["cache_dir"], symbol, settings["interval"],
            settings["cache_max_stale_minutes"],
        )
        if cached:
            _, age_minutes = cached
            symbol_health[symbol] = {
                "status": "stale_cache", "component_errors": provider_errors,
                "data_lag_minutes": age_minutes, "latest_market_time": None,
                "market_data_lag_minutes": fallback_freshness["lag_minutes"],
                "market_data_max_lag_minutes": fallback_freshness["max_lag_minutes"],
                "market_data_freshness": "stale_cache",
                "market_data_component_lag_minutes": fallback_freshness["component_lag_minutes"],
                "signal_status": "not_evaluated", "provider": "cache",
                "provider_attempts": provider_attempts,
                "provider_switch_reason": switch_reason,
                "provider_latency_ms": None,
                "position_status": (
                    "data_unavailable_hold"
                    if any(item.get("symbol") == symbol and item.get("status") == "open"
                           for item in state.get("open_trades", []))
                    else "no_open_position"
                ),
            }
            continue
        symbol_health[symbol] = {
            "status": "stale" if "stale" in error_text.lower() else "unavailable",
            "component_errors": provider_errors,
            "data_lag_minutes": None, "latest_market_time": None,
            "market_data_lag_minutes": fallback_freshness["lag_minutes"],
            "market_data_max_lag_minutes": fallback_freshness["max_lag_minutes"],
            "market_data_freshness": fallback_freshness["status"],
            "market_data_component_lag_minutes": fallback_freshness["component_lag_minutes"],
            "signal_status": "not_evaluated", "provider": None,
            "provider_attempts": provider_attempts,
            "provider_switch_reason": switch_reason,
            "provider_latency_ms": None,
            "position_status": (
                "data_unavailable_hold"
                if any(item.get("symbol") == symbol and item.get("status") == "open"
                       for item in state.get("open_trades", []))
                else "no_open_position"
            ),
        }
    redundancy = _provider_redundancy_summary(settings, symbol_health)
    previous_redundancy = str(state.get("provider_redundancy_status") or "unknown")
    current_redundancy = redundancy["status"]
    if current_redundancy != previous_redundancy:
        state.setdefault("provider_redundancy_events", []).append({
            "at_epoch_ms": int(time.time() * 1000),
            "from": previous_redundancy,
            "to": current_redundancy,
            "degraded_symbols": list(redundancy["degraded_symbols"]),
            "unavailable_symbols": list(redundancy["unavailable_symbols"]),
        })
        state["provider_redundancy_events"] = state["provider_redundancy_events"][-100:]
    state["provider_redundancy_status"] = current_redundancy
    state["provider_redundancy"] = redundancy
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
    redundancy_degraded = current_redundancy == "degraded_redundancy"
    if (not errors and not redundancy_degraded
            and not any(item.get("status") == "partial" for item in symbol_health.values())):
        state["data_status"] = "healthy"
        state["consecutive_unavailable_runs"] = 0
        state["last_success_at_epoch_ms"] = state["updated_at_epoch_ms"]
    elif successful_market_times or any(item.get("status") == "stale_cache" for item in symbol_health.values()):
        state["data_status"] = "degraded"
        state["consecutive_unavailable_runs"] = 0
        if successful_market_times:
            state["last_success_at_epoch_ms"] = state["updated_at_epoch_ms"]
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
    stats["max_market_data_lag_minutes"] = round(max(
        (float(health.get("market_data_lag_minutes") or 0)
         for health in symbol_health.values()), default=0.0
    ), 2)
    deliveries = dispatch_perpetual_notifications(notification_events, state, config)
    stats["notification_deliveries"] = deliveries
    stats["notification_history_count"] = len(state.get("notification_history") or [])
    # Persist notification deduplication state after dispatch so a rerun cannot
    # resend an already delivered lifecycle event.
    sw.atomic_write_json(state_path, state)
    sw.atomic_write_json(stats_path, stats)
    return {"enabled": True, "processed_symbols": len(settings["symbols"]) - len(errors), "errors": errors, "stats": stats}


def main(argv=None):
    console_output.configure_utf8_output()
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
