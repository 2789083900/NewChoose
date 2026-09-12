#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate isolated perpetual shadow state before automated persistence."""

import argparse
import hashlib
import json
import math
import os
import sys

import derivatives_data


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_PATH = os.path.join(BASE_DIR, "perp_shadow_state.json")
DEFAULT_STATS_PATH = os.path.join(BASE_DIR, "perp_shadow_stats.json")


def _read(path):
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def _finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _parameter_checksum(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate(state_path=DEFAULT_STATE_PATH, stats_path=DEFAULT_STATS_PATH):
    if not os.path.exists(state_path) and not os.path.exists(stats_path):
        return []
    errors = []
    if not os.path.exists(state_path) or not os.path.exists(stats_path):
        return ["perpetual shadow state and stats must exist together"]
    try:
        state = _read(state_path)
        stats = _read(stats_path)
    except (OSError, ValueError) as exc:
        return [f"cannot read perpetual shadow state: {exc}"]
    for name, value in (("state", state), ("stats", stats)):
        if not isinstance(value, dict):
            errors.append(f"{name} must be an object")
            continue
        if value.get("market_type") != derivatives_data.MARKET_TYPE:
            errors.append(f"{name} market_type must be {derivatives_data.MARKET_TYPE}")
        if value.get("research_only") is not True:
            errors.append(f"{name} must be research_only")
    if errors or not isinstance(state, dict) or not isinstance(stats, dict):
        return errors

    groups = {
        "open_trades": (state.get("open_trades"), {"pending_entry", "open"}),
        "closed_trades": (state.get("closed_trades"), {"closed"}),
        "rejected_signals": (state.get("rejected_signals"), {"rejected"}),
    }
    ids = []
    for group_name, (trades, statuses) in groups.items():
        if not isinstance(trades, list):
            errors.append(f"{group_name} must be a list")
            continue
        for trade in trades:
            if not isinstance(trade, dict):
                errors.append(f"{group_name} contains a non-object")
                continue
            trade_id = str(trade.get("id") or "")
            if not trade_id or trade_id.lower() == "test":
                errors.append(f"{group_name} contains an invalid id")
            else:
                ids.append(trade_id)
            if trade.get("market_type") != derivatives_data.MARKET_TYPE:
                errors.append(f"{trade_id or group_name} has mixed market_type")
            if trade.get("research_only") is not True:
                errors.append(f"{trade_id or group_name} is not research_only")
            if trade.get("status") not in statuses:
                errors.append(f"{trade_id or group_name} has invalid status")
            if trade.get("status") in {"open", "closed"}:
                for field in ("entry", "quantity", "leverage", "fees", "funding_cashflow"):
                    if not _finite(trade.get(field)):
                        errors.append(f"{trade_id} has invalid {field}")
                for field in ("mfe_pct", "mae_pct", "initial_entry", "last_mark_price"):
                    if field in trade and trade.get(field) is not None and not _finite(trade.get(field)):
                        errors.append(f"{trade_id} has invalid {field}")
            if trade.get("status") == "closed" and "holding_hours" in trade:
                if not _finite(trade.get("holding_hours")) or float(trade["holding_hours"]) < 0:
                    errors.append(f"{trade_id} has invalid holding_hours")
            if trade.get("status") in {"pending_entry", "open", "closed"}:
                parameters = trade.get("parameter_snapshot")
                digest = str(trade.get("parameter_sha256") or "")
                if not isinstance(parameters, dict) or len(digest) != 64:
                    errors.append(f"{trade_id} has no frozen parameter snapshot")
                elif digest != _parameter_checksum(parameters):
                    errors.append(f"{trade_id} parameter snapshot checksum mismatch")
    if len(ids) != len(set(ids)):
        errors.append("perpetual shadow trade ids are not unique")

    seen = state.get("seen_signal_ids")
    if not isinstance(seen, list) or len(seen) != len(set(seen)):
        errors.append("seen_signal_ids must be a unique list")
    elif any(trade_id not in seen for trade_id in ids):
        errors.append("seen_signal_ids does not cover all trades")
    if not _finite(state.get("equity")) or float(state.get("equity", 0)) < 0:
        errors.append("state equity must be finite and non-negative")

    expected = {
        "open_count": len(state.get("open_trades") or []),
        "closed_count": len(state.get("closed_trades") or []),
        "rejected_count": len(state.get("rejected_signals") or []),
    }
    for field, value in expected.items():
        if stats.get(field) != value:
            errors.append(f"stats {field} does not match state")
    if not isinstance(stats.get("sample_reliability"), str):
        errors.append("stats sample_reliability is missing")
    if not _finite(stats.get("sample_progress_pct")) or not 0 <= float(stats.get("sample_progress_pct")) <= 100:
        errors.append("stats sample_progress_pct is invalid")
    if stats.get("sample_next_milestone") not in {"minimum_goal", "preferred_goal", "complete"}:
        errors.append("stats sample_next_milestone is invalid")
    if not isinstance(stats.get("sample_goal_min_trades"), int) or stats.get("sample_goal_min_trades", 0) < 1:
        errors.append("stats sample_goal_min_trades is invalid")
    if not isinstance(stats.get("sample_goal_preferred_trades"), int) or stats.get("sample_goal_preferred_trades", 0) < stats.get("sample_goal_min_trades", 1):
        errors.append("stats sample_goal_preferred_trades is invalid")
    if not _finite(stats.get("equity")) or abs(float(stats["equity"]) - float(state["equity"])) > 1e-7:
        errors.append("stats equity does not match state")
    if not isinstance(stats.get("requested_symbols"), list) or not isinstance(stats.get("successful_symbols"), list):
        errors.append("stats symbol progress fields are missing")
    elif not set(stats["successful_symbols"]).issubset(set(stats["requested_symbols"])):
        errors.append("stats successful_symbols is not a subset of requested_symbols")
    if not isinstance(stats.get("successful_market_times"), dict):
        errors.append("stats successful_market_times is missing")
    if not isinstance(stats.get("run_count"), int) or stats.get("run_count", 0) < 0:
        errors.append("stats run_count is invalid")
    if not isinstance(stats.get("consecutive_unavailable_runs"), int) or stats.get("consecutive_unavailable_runs", 0) < 0:
        errors.append("stats consecutive_unavailable_runs is invalid")
    if stats.get("data_status") not in {"unknown", "healthy", "degraded", "unavailable"}:
        errors.append("stats data_status is invalid")
    if not isinstance(stats.get("last_successful_symbols"), list):
        errors.append("stats last_successful_symbols is missing")
    symbol_health = stats.get("symbol_health")
    if not isinstance(symbol_health, dict):
        errors.append("stats symbol_health is missing")
    else:
        allowed = {"healthy", "partial", "stale", "unavailable"}
        for symbol, health in symbol_health.items():
            if not isinstance(health, dict) or health.get("status") not in allowed:
                errors.append(f"stats symbol_health for {symbol} is invalid")
                continue
            lag = health.get("data_lag_minutes")
            if lag is not None and (not _finite(lag) or float(lag) < 0):
                errors.append(f"stats symbol_health for {symbol} has invalid lag")
        healthy = stats.get("healthy_symbols")
        stale = stats.get("stale_symbols")
        if not isinstance(healthy, list) or set(healthy) != {s for s, h in symbol_health.items() if h.get("status") == "healthy"}:
            errors.append("stats healthy_symbols does not match symbol_health")
        if not isinstance(stale, list) or set(stale) != {s for s, h in symbol_health.items() if h.get("status") == "stale"}:
            errors.append("stats stale_symbols does not match symbol_health")
    if not _finite(stats.get("max_data_lag_minutes")) or float(stats.get("max_data_lag_minutes", 0)) < 0:
        errors.append("stats max_data_lag_minutes is invalid")
    if not isinstance(state.get("equity_curve"), list):
        errors.append("equity_curve must be a list")
    else:
        previous = None
        for point in state["equity_curve"]:
            if not isinstance(point, dict) or any(
                not _finite(point.get(field))
                for field in ("realized_equity", "marked_equity", "unrealized_pnl", "open_count")
            ):
                errors.append("equity_curve contains an invalid point")
                continue
            timestamp = int(point.get("time") or 0)
            if timestamp <= 0:
                errors.append("equity_curve contains an invalid timestamp")
            if previous is not None and timestamp <= previous:
                errors.append("equity_curve timestamps must be strictly increasing")
            previous = timestamp
            expected_marked = float(point["realized_equity"]) + float(point["unrealized_pnl"])
            if abs(float(point["marked_equity"]) - expected_marked) > 1e-7:
                errors.append("equity_curve marked equity is inconsistent")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description="校验永续研究影子交易状态")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH)
    parser.add_argument("--stats", default=DEFAULT_STATS_PATH)
    args = parser.parse_args(argv)
    errors = validate(os.path.abspath(args.state), os.path.abspath(args.stats))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("永续影子交易状态校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
