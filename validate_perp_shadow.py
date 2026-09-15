#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate isolated perpetual shadow state before automated persistence."""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime

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


def validate(state_path=DEFAULT_STATE_PATH, stats_path=DEFAULT_STATS_PATH,
             max_age_minutes=None, now_ms=None):
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
    if max_age_minutes is not None:
        try:
            max_age = float(max_age_minutes)
            generated = datetime.fromisoformat(
                str(stats.get("generated_at_utc") or "").replace("Z", "+00:00")
            ).timestamp() * 1000
            current = float(now_ms if now_ms is not None else time.time() * 1000)
            if max_age <= 0 or generated > current + 60000 or current - generated > max_age * 60000:
                errors.append("stats generated_at_utc is stale or invalid")
        except (TypeError, ValueError, OverflowError):
            errors.append("stats generated_at_utc is stale or invalid")

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
                if _finite(trade.get("quantity")) and float(trade.get("quantity")) < 0:
                    errors.append(f"{trade_id} has negative quantity")
                if _finite(trade.get("leverage")) and not 0 < float(trade.get("leverage")) <= 20:
                    errors.append(f"{trade_id} has unsupported leverage")
                for field in ("mfe_pct", "mae_pct", "initial_entry", "last_mark_price"):
                    if field in trade and trade.get(field) is not None and not _finite(trade.get(field)):
                        errors.append(f"{trade_id} has invalid {field}")
                funding_counts = {
                    field: trade.get(field)
                    for field in ("funding_settlement_count", "funding_mark_estimated_count",
                                  "funding_mark_unavailable_count")
                    if field in trade
                }
                if any(not isinstance(value, int) or value < 0
                       for value in funding_counts.values()):
                    errors.append(f"{trade_id} has invalid funding mark counts")
                settled = funding_counts.get("funding_settlement_count", 0)
                estimated = funding_counts.get("funding_mark_estimated_count", 0)
                if estimated > settled:
                    errors.append(f"{trade_id} estimated funding marks exceed settlements")
                sources = trade.get("funding_mark_sources")
                if sources is not None:
                    if not isinstance(sources, dict) or any(
                            not isinstance(count, int) or count < 0 for count in sources.values()):
                        errors.append(f"{trade_id} has invalid funding mark sources")
                    elif "funding_settlement_count" in trade and sum(sources.values()) != settled:
                        errors.append(f"{trade_id} funding mark sources do not match settlements")
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
    closed_count = len(state.get("closed_trades") or [])
    for field, goal_field in (
            ("sample_remaining_to_minimum", "sample_goal_min_trades"),
            ("sample_remaining_to_preferred", "sample_goal_preferred_trades")):
        if field in stats:
            expected_remaining = max(0, int(stats.get(goal_field) or 0) - closed_count)
            if stats.get(field) != expected_remaining:
                errors.append(f"stats {field} does not match closed_count")
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
        allowed = {"healthy", "partial", "stale", "stale_cache", "unavailable"}
        for symbol, health in symbol_health.items():
            if not isinstance(health, dict) or health.get("status") not in allowed:
                errors.append(f"stats symbol_health for {symbol} is invalid")
                continue
            lag = health.get("data_lag_minutes")
            if lag is not None and (not _finite(lag) or float(lag) < 0):
                errors.append(f"stats symbol_health for {symbol} has invalid lag")
            attempts = health.get("provider_attempts")
            if attempts is not None:
                if not isinstance(attempts, list):
                    errors.append(f"stats symbol_health for {symbol} has invalid provider_attempts")
                else:
                    for attempt in attempts:
                        if not isinstance(attempt, dict) or attempt.get("status") not in {"success", "error", "skipped_cooldown"}:
                            errors.append(f"stats symbol_health for {symbol} has invalid provider attempt")
                            continue
                        latency = attempt.get("latency_ms")
                        if latency is not None and (not _finite(latency) or float(latency) < 0):
                            errors.append(f"stats symbol_health for {symbol} has invalid provider latency")
        healthy = stats.get("healthy_symbols")
        stale = stats.get("stale_symbols")
        if not isinstance(healthy, list) or set(healthy) != {s for s, h in symbol_health.items() if h.get("status") == "healthy"}:
            errors.append("stats healthy_symbols does not match symbol_health")
        if not isinstance(stale, list) or set(stale) != {s for s, h in symbol_health.items() if h.get("status") in {"stale", "stale_cache"}}:
            errors.append("stats stale_symbols does not match symbol_health")
        cached = stats.get("cached_symbols")
        if not isinstance(cached, list) or set(cached) != {s for s, h in symbol_health.items() if h.get("status") == "stale_cache"}:
            errors.append("stats cached_symbols does not match symbol_health")
    if not _finite(stats.get("max_data_lag_minutes")) or float(stats.get("max_data_lag_minutes", 0)) < 0:
        errors.append("stats max_data_lag_minutes is invalid")
    provider_health = stats.get("provider_health")
    if provider_health is not None:
        if not isinstance(provider_health, dict):
            errors.append("stats provider_health must be an object")
        else:
            for key, value in provider_health.items():
                if not isinstance(value, dict):
                    errors.append(f"stats provider_health for {key} is invalid")
                    continue
                failures = value.get("consecutive_failures")
                if not isinstance(failures, int) or failures < 0:
                    errors.append(f"stats provider_health for {key} has invalid failure count")
                cooldown = value.get("cooldown_until_epoch_ms")
                if cooldown is not None and (not isinstance(cooldown, (int, float)) or cooldown < 0):
                    errors.append(f"stats provider_health for {key} has invalid cooldown")
                latency = value.get("last_latency_ms")
                if latency is not None and (not _finite(latency) or float(latency) < 0):
                    errors.append(f"stats provider_health for {key} has invalid latency")
    closed_by_provider = stats.get("closed_count_by_provider")
    if closed_by_provider is not None:
        expected_by_provider = {}
        for trade in state.get("closed_trades") or []:
            provider = str(trade.get("provider") or "unknown")
            expected_by_provider[provider] = expected_by_provider.get(provider, 0) + 1
        if closed_by_provider != expected_by_provider:
            errors.append("stats closed_count_by_provider does not match state")
    funding_fields = (
        ("funding_settlement_count", "funding_settlement_count"),
        ("funding_mark_estimated_count", "funding_mark_estimated_count"),
        ("funding_mark_unavailable_count", "funding_mark_unavailable_count"),
    )
    all_trades = (state.get("open_trades") or []) + (state.get("closed_trades") or [])
    for stats_field, trade_field in funding_fields:
        if stats_field in stats:
            expected_count = sum(int(trade.get(trade_field) or 0) for trade in all_trades)
            if stats.get(stats_field) != expected_count:
                errors.append(f"stats {stats_field} does not match state")
    if "funding_mark_sources" in stats:
        expected_sources = {}
        for trade in all_trades:
            for source, count in (trade.get("funding_mark_sources") or {}).items():
                expected_sources[source] = expected_sources.get(source, 0) + int(count)
        if stats.get("funding_mark_sources") != expected_sources:
            errors.append("stats funding_mark_sources does not match state")
    breakdown = stats.get("sample_breakdown")
    if breakdown is not None:
        if not isinstance(breakdown, dict):
            errors.append("stats sample_breakdown must be an object")
        else:
            for dimension in ("provider", "symbol", "market_state", "funding_mark_quality"):
                buckets = breakdown.get(dimension)
                if not isinstance(buckets, dict):
                    errors.append(f"stats sample_breakdown {dimension} is missing")
                    continue
                counts = []
                for name, bucket in buckets.items():
                    if not isinstance(bucket, dict) or not isinstance(bucket.get("count"), int) or bucket["count"] < 0:
                        errors.append(f"stats sample_breakdown {dimension}/{name} is invalid")
                        continue
                    counts.append(bucket["count"])
                    wins = bucket.get("wins")
                    losses = bucket.get("losses")
                    if not isinstance(wins, int) or not isinstance(losses, int) or wins + losses != bucket["count"]:
                        errors.append(f"stats sample_breakdown {dimension}/{name} win/loss count is invalid")
                if sum(counts) != closed_count:
                    errors.append(f"stats sample_breakdown {dimension} does not cover closed trades")
    market_times = stats.get("market_time_by_symbol")
    if market_times is not None:
        if not isinstance(market_times, dict) or any(
                not isinstance(value, int) or value <= 0 for value in market_times.values()):
            errors.append("stats market_time_by_symbol is invalid")
    if not isinstance(state.get("equity_curve"), list):
        errors.append("equity_curve must be a list")
    else:
        previous = None
        for point in state["equity_curve"]:
            required_curve_fields = ("realized_equity", "marked_equity", "unrealized_pnl", "open_count")
            exposure_curve_fields = ("open_margin", "open_notional", "long_notional",
                                     "short_notional", "open_risk_fraction")
            if not isinstance(point, dict) or any(
                not _finite(point.get(field))
                for field in required_curve_fields
            ) or any(
                field in point and (not _finite(point.get(field)) or float(point[field]) < 0)
                for field in exposure_curve_fields
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
    parser.add_argument("--max-age-minutes", type=float, default=None)
    args = parser.parse_args(argv)
    errors = validate(
        os.path.abspath(args.state), os.path.abspath(args.stats),
        max_age_minutes=args.max_age_minutes,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("永续影子交易状态校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
