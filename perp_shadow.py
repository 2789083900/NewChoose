#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Research-only shadow trading for Binance USD-M perpetual contracts.

The module uses public market data only. It never accepts API credentials and
contains no account or order endpoints. State is intentionally isolated from
the spot watcher.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

import derivatives_data
import derivatives_risk
import signal_watch as sw


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "signal_watch.config.json")
DEFAULT_STATE_PATH = os.path.join(BASE_DIR, "perp_shadow_state.json")
DEFAULT_STATS_PATH = os.path.join(BASE_DIR, "perp_shadow_stats.json")
SCHEMA_VERSION = 1


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
    }


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as file:
            value = json.load(file)
        return value
    except (OSError, ValueError):
        return default


def load_state(path, account_value=10000.0):
    state = load_json(path, empty_state(account_value))
    if not isinstance(state, dict) or state.get("market_type") not in (None, derivatives_data.MARKET_TYPE):
        raise ValueError("invalid or mixed-market perpetual shadow state")
    state.setdefault("schema_version", SCHEMA_VERSION)
    state["market_type"] = derivatives_data.MARKET_TYPE
    state["research_only"] = True
    state.setdefault("equity", float(account_value))
    for key in ("open_trades", "closed_trades", "rejected_signals", "seen_signal_ids", "equity_curve"):
        state.setdefault(key, [])
    for trade in state["open_trades"] + state["closed_trades"]:
        if trade.get("market_type") != derivatives_data.MARKET_TYPE or trade.get("research_only") is not True:
            raise ValueError("perpetual shadow state contains an invalid trade")
        if trade.get("status") == "open" and not trade.get("initial_entry"):
            baseline = trade.get("entry", trade.get("avg_entry"))
            if baseline not in (None, ""):
                trade["initial_entry"] = float(baseline)
    return state


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
    return {
        "enabled": bool(raw.get("enabled", False)),
        "research_only": True,
        "symbols": list(dict.fromkeys(symbols)),
        "interval": interval,
        "history_limit": int(_number(raw, "history_limit", 1000, 100)),
        "sample_goal_min_trades": int(_number(raw, "sample_goal_min_trades", 30, 1)),
        "sample_goal_preferred_trades": int(_number(raw, "sample_goal_preferred_trades", 50, 1)),
        "system": raw.get("system", "system2"),
        "account_value": _number(raw, "account_value", 10000.0, 1.0),
        "risk_fraction": _number(raw, "risk_fraction", 0.005),
        "leverage": _number(raw, "max_leverage", 2.0, 0.01),
        "maintenance_margin_rate": _number(raw, "maintenance_margin_rate", 0.005),
        "liquidation_fee_rate": _number(raw, "liquidation_fee_rate", 0.0),
        "fee_rate": _number(raw, "fee_rate", 0.0004),
        "slippage_rate": _number(raw, "slippage_rate", 0.0005),
        "max_total_open_risk": _number(raw, "max_total_open_risk", 0.03),
        "filters": raw.get("filters") or {
            "higher_timeframe": False,
            "volume_confirmation": False,
            "volatility_filter": False,
            "anomaly_filter": True,
        },
    }


def _signal_id(symbol, interval, bar_time, direction, system):
    return f"perp|{symbol}|{interval}|{int(bar_time)}|{direction}|{system}"


def parameter_snapshot(settings):
    return {
        key: settings[key] for key in (
            "account_value", "risk_fraction", "leverage",
            "maintenance_margin_rate", "liquidation_fee_rate",
            "fee_rate", "slippage_rate", "max_total_open_risk",
            "interval", "system",
        )
    } | {
        "max_units": sw.TURTLE_MAX_UNITS,
        "add_n": 0.5,
        "stop_n": 2.0,
        "filters": settings["filters"],
        "execution_model": "signal_close_next_contract_bar_open",
        "risk_price": "mark_price",
        "margin_mode": "isolated_approximation",
        "sample_collection_phase": "phase_1_btc_eth_shadow_only",
    }


def parameter_checksum(parameters):
    canonical = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        "open_interest_value": float(oi["open_interest_value"]) if oi else None,
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


def record_equity_snapshot(state):
    marked, unrealized = _marked_equity(state)
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
    curve = state.setdefault("equity_curve", [])
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


def _close_trade(trade, raw_exit, reason, exit_time, mark_open, settings, specs, state,
                 market_context=None):
    fill = _adverse_fill(
        raw_exit, trade["direction"], "exit", settings["slippage_rate"], specs,
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
    })
    trade["return_pct"] = trade["net_pnl"] / settings["account_value"] * 100
    trade["funding_to_gross_pnl_pct"] = (
        round(abs(trade["funding_cashflow"]) / abs(gross) * 100, 4)
        if gross else 0.0
    )


def _fill_pending(trade, bar, settings, specs, state, snapshot=None):
    entry = _adverse_fill(
        bar["open"], trade["direction"], "entry", settings["slippage_rate"], specs
    )
    quantity = sw.turtle_unit_quantity(
        state["equity"], trade["n"], settings["risk_fraction"], 2.0
    )
    quantity = derivatives_data.quantize_quantity(quantity, specs)
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
        "units": [{"price": entry, "quantity": quantity, "n": trade["n"]}],
        "latest_entry": entry, "stop": stop, "fees": fee,
        "funding_cashflow": 0.0, "last_funding_time": 0,
        "mfe_pct": 0.0, "mae_pct": 0.0,
        "last_mark_price": None,
        "entry_market_context": _market_context(snapshot or {}, bar["time"]),
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
        if not _fill_pending(trade, fill_bar, settings, specs, state, snapshot=snapshot):
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
            fill = _adverse_fill(
                next_add, direction, "entry", settings["slippage_rate"], specs,
                opening=mark["open"],
            )
            quantity = derivatives_data.quantize_quantity(
                sw.turtle_unit_quantity(state["equity"], trade["n"], settings["risk_fraction"], 2.0), specs
            )
            added_risk = _open_risk(state, exclude_id=trade["id"]) + settings["risk_fraction"] * (len(trade["units"]) + 1)
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
            trade["units"].append({"price": fill, "quantity": quantity, "n": trade["n"]})
        trade["last_mark_price"] = float(mark["close"])
        trade["last_processed_bar"] = bar_time
    return "open"


def create_signal(snapshot, settings, state):
    bars = snapshot["contract_klines"]
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
    if _open_risk(state) + settings["risk_fraction"] > settings["max_total_open_risk"]:
        state["rejected_signals"].append({
            "id": signal_id, "status": "rejected", "rejection_reason": "portfolio_risk_limit",
            "market_type": derivatives_data.MARKET_TYPE, "symbol": snapshot["symbol"],
            "research_only": True, "signal_bar_time": signal_time,
        })
        return None
    parameters = parameter_snapshot(settings)
    trade = {
        "id": signal_id, "market_type": derivatives_data.MARKET_TYPE,
        "research_only": True, "symbol": snapshot["symbol"],
        "interval": settings["interval"], "system": settings["system"],
        "direction": direction, "status": "pending_entry",
        "signal_bar_time": signal_time,
        "fill_time": signal_time + sw.INTERVAL_MS[settings["interval"]],
        "entry_model": "next_contract_bar_open", "mark_price_risk": True,
        "risk_fraction": settings["risk_fraction"], "leverage": settings["leverage"],
        "entry_trigger": plan["entry"], "n": plan["n"],
        "signal_reasons": reasons, "contract_specs": snapshot["contract_specs"],
        "parameter_snapshot": parameters,
        "parameter_sha256": parameter_checksum(parameters),
    }
    state["open_trades"].append(trade)
    return trade


def process_snapshot(snapshot, settings, state):
    errors = derivatives_data.validate_perpetual_snapshot(snapshot, settings["interval"])
    if errors:
        raise RuntimeError("invalid perpetual shadow snapshot: " + "; ".join(errors))
    if not snapshot.get("contract_specs"):
        raise RuntimeError("perpetual shadow trading requires contract_specs")
    if snapshot.get("contract_klines"):
        state["last_market_time"] = max(
            int(state.get("last_market_time") or 0),
            int(snapshot["contract_klines"][-1]["time"]),
        )
    symbol = snapshot["symbol"]
    open_for_symbol = [item for item in state["open_trades"] if item["symbol"] == symbol]
    for trade in open_for_symbol:
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
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_type": derivatives_data.MARKET_TYPE,
        "research_only": True,
        "equity": round(float(state["equity"]), 8),
        "marked_equity": round(marked_equity, 8),
        "unrealized_pnl": round(unrealized, 8),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "open_count": len(state["open_trades"]),
        "pending_entry_count": sum(item.get("status") == "pending_entry" for item in state["open_trades"]),
        "closed_count": closed_count,
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
    fetch = fetcher or derivatives_data.fetch_perpetual_snapshot
    errors = {}
    successful_market_times = {}
    for symbol in settings["symbols"]:
        try:
            snapshot = fetch(
                symbol, settings["interval"], limit=settings["history_limit"], closed_only=True,
                include_contract_specs=True,
            )
            process_snapshot(snapshot, settings, state)
            if snapshot.get("contract_klines"):
                successful_market_times[symbol] = int(snapshot["contract_klines"][-1]["time"])
        except Exception as exc:
            errors[symbol] = str(exc)
    state["updated_at_epoch_ms"] = int(time.time() * 1000)
    state["last_errors"] = errors
    state["run_count"] = int(state.get("run_count") or 0) + 1
    state["last_successful_symbols"] = sorted(successful_market_times)
    if not errors:
        state["data_status"] = "healthy"
        state["consecutive_unavailable_runs"] = 0
        state["last_success_at_epoch_ms"] = state["updated_at_epoch_ms"]
    elif successful_market_times:
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
    sw.atomic_write_json(stats_path, stats)
    return {"enabled": True, "processed_symbols": len(settings["symbols"]) - len(errors), "errors": errors, "stats": stats}


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行 Binance USD-M 永续研究影子交易")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state", default=DEFAULT_STATE_PATH)
    parser.add_argument("--stats", default=DEFAULT_STATS_PATH)
    args = parser.parse_args(argv)
    config = load_json(os.path.abspath(args.config), {})
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
