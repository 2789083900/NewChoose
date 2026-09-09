#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate CoinPulse runtime state before publishing scheduled artifacts."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


MIN_REALISTIC_TS_MS = 1_577_836_800_000  # 2020-01-01; rejects synthetic epoch values.


def _load(path, label, errors):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError) as exc:
        errors.append(f"{label} 无法读取：{exc}")
        return None


def _contains_test_marker(value):
    if isinstance(value, dict):
        return any(_contains_test_marker(key) or _contains_test_marker(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_test_marker(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized in {"test", "pytest", "unittest"} or normalized.startswith(("test-", "pytest-", "unittest-"))


def _check_timestamp(value, field, errors):
    if value in (None, ""):
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} 不是有效时间戳：{value!r}")
        return
    # Milliseconds are used by signal state; seconds are accepted for metadata.
    timestamp_ms = numeric if numeric > 10_000_000_000 else numeric * 1000
    if timestamp_ms < MIN_REALISTIC_TS_MS:
        errors.append(f"{field} 疑似测试时间戳：{value!r}")


def _check_trade(trade, location, ids, errors):
    if not isinstance(trade, dict):
        errors.append(f"{location} 不是对象")
        return
    trade_id = trade.get("id")
    if not trade_id:
        errors.append(f"{location} 缺少 id")
    elif trade_id in ids:
        errors.append(f"交易 id 重复：{trade_id}")
    else:
        ids.add(trade_id)
    for field in ("symbol", "interval", "direction"):
        if not trade.get(field):
            errors.append(f"{location} 缺少字段：{field}")
    _check_timestamp(trade.get("entry_ts"), f"{location}.entry_ts", errors)
    _check_timestamp(trade.get("exit_ts"), f"{location}.exit_ts", errors)
    if _contains_test_marker(trade):
        errors.append(f"{location} 含测试标记")


def validate_runtime_state(state_path="signal_watch.state.json",
                           trade_stats_path="trade_stats.json",
                           tracking_path="signal_tracking_stats.json",
                           quality_path="signal_quality_report.json",
                           records_path="signal_records.json"):
    errors = []
    state = _load(state_path, "state", errors)
    trade_stats = _load(trade_stats_path, "trade_stats", errors)
    tracking = _load(tracking_path, "signal_tracking_stats", errors)
    quality = _load(quality_path, "signal_quality_report", errors)
    records = _load(records_path, "signal_records", errors)
    if not isinstance(state, dict):
        state = {}
    if not isinstance(trade_stats, dict):
        trade_stats = {}
    if not isinstance(tracking, dict):
        tracking = {}
    if not isinstance(quality, dict):
        quality = {}
    if not isinstance(records, list):
        records = []

    open_trades = state.get("open_trades", [])
    closed_trades = state.get("closed_trades", [])
    if not isinstance(open_trades, list):
        errors.append("state.open_trades 必须是数组")
        open_trades = []
    if not isinstance(closed_trades, list):
        errors.append("state.closed_trades 必须是数组")
        closed_trades = []
    ids = set()
    for index, trade in enumerate(open_trades):
        _check_trade(trade, f"state.open_trades[{index}]", ids, errors)
    for index, trade in enumerate(closed_trades):
        _check_trade(trade, f"state.closed_trades[{index}]", ids, errors)
        if isinstance(trade, dict):
            if trade.get("status") != "closed":
                errors.append(f"state.closed_trades[{index}] 状态不是 closed")
            for field in ("result", "exit", "closed_at", "pnl_pct"):
                if trade.get(field) in (None, ""):
                    errors.append(f"state.closed_trades[{index}] 缺少结算字段：{field}")
    if _contains_test_marker(state):
        errors.append("state 含测试标记")

    expected_total = len(closed_trades)
    if trade_stats.get("total") != expected_total:
        errors.append(f"trade_stats.total={trade_stats.get('total')!r} 与 closed_trades={expected_total} 不一致")
    if trade_stats.get("open_count") != len(open_trades):
        errors.append(f"trade_stats.open_count={trade_stats.get('open_count')!r} 与 open_trades={len(open_trades)} 不一致")
    expected_wins = sum(1 for trade in closed_trades if (trade.get("pnl_pct") or 0) > 0)
    expected_losses = expected_total - expected_wins
    if trade_stats.get("wins") != expected_wins or trade_stats.get("losses") != expected_losses:
        errors.append("trade_stats 的 wins/losses 与结算交易不一致")
    stats_ids = {trade.get("id") for trade in trade_stats.get("trades", []) if isinstance(trade, dict)}
    if not stats_ids.issubset(ids):
        errors.append("trade_stats.trades 存在 state 中不存在的交易 id")
    if _contains_test_marker(trade_stats):
        errors.append("trade_stats 含测试标记")

    if tracking.get("total_signals") != len(records):
        errors.append(f"tracking.total_signals={tracking.get('total_signals')!r} 与 signal_records={len(records)} 不一致")
    for horizon in ("24h", "48h"):
        observed = ((tracking.get("horizons") or {}).get(horizon) or {}).get("observed", 0)
        if not isinstance(observed, (int, float)) or observed < 0 or observed > len(records):
            errors.append(f"tracking.horizons.{horizon}.observed 超出记录范围")
    if _contains_test_marker(tracking) or _contains_test_marker(quality) or _contains_test_marker(records):
        errors.append("信号跟踪或质量报告含测试标记")

    sample = quality.get("sample") or {}
    if sample.get("closed_trades") != expected_total:
        errors.append(f"quality.sample.closed_trades={sample.get('closed_trades')!r} 与 closed_trades={expected_total} 不一致")
    if sample.get("pending_signals") != tracking.get("pending_signals"):
        errors.append("quality.sample.pending_signals 与 tracking.pending_signals 不一致")

    for path in (state_path, trade_stats_path, tracking_path, quality_path, records_path):
        if not os.path.isfile(path):
            errors.append(f"缺少运行态文件：{path}")
    return errors


def main():
    parser = argparse.ArgumentParser(description="CoinPulse 运行态数据完整性检查")
    parser.add_argument("--state", default="signal_watch.state.json")
    parser.add_argument("--trade-stats", default="trade_stats.json")
    parser.add_argument("--tracking", default="signal_tracking_stats.json")
    parser.add_argument("--quality", default="signal_quality_report.json")
    parser.add_argument("--records", default="signal_records.json")
    args = parser.parse_args()
    errors = validate_runtime_state(args.state, args.trade_stats, args.tracking, args.quality, args.records)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("运行态数据校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
