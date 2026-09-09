#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Safely quarantine explicit test trades and rebuild derived runtime reports.

The default mode is read-only. Use --apply only after reviewing the dry-run
summary; every changed file is copied to a timestamped backup first.
"""

import argparse
import copy
import json
import os
import shutil
import sys
from datetime import datetime

import signal_watch as sw
import track_signals


FILES = (
    "signal_watch.state.json",
    "trade_stats.json",
    "signal_tracking_stats.json",
    "signal_quality_report.json",
)


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as file:
            value = json.load(file)
        return value
    except (OSError, ValueError):
        return copy.deepcopy(default)


def is_explicit_test_trade(trade):
    """Return true only for unambiguous synthetic/test trades."""
    if not isinstance(trade, dict):
        return False
    trade_id = str(trade.get("id") or "").strip().lower()
    if trade_id in {"test", "pytest", "unittest"} or trade_id.startswith(("test-", "pytest-", "unittest-")):
        return True
    # The validator uses the same lower bound for synthetic millisecond times.
    entry_ts = trade.get("entry_ts")
    try:
        if entry_ts is not None and float(entry_ts) < 1_577_836_800_000:
            return True
    except (TypeError, ValueError):
        pass
    provider = str(trade.get("provider") or "").strip().lower()
    return provider in {"test", "pytest", "unittest"}


def inspect_state(state):
    open_trades = state.get("open_trades") if isinstance(state, dict) else []
    closed_trades = state.get("closed_trades") if isinstance(state, dict) else []
    open_trades = open_trades if isinstance(open_trades, list) else []
    closed_trades = closed_trades if isinstance(closed_trades, list) else []
    return {
        "open_test": [trade for trade in open_trades if is_explicit_test_trade(trade)],
        "closed_test": [trade for trade in closed_trades if is_explicit_test_trade(trade)],
        "open_total": len(open_trades),
        "closed_total": len(closed_trades),
    }


def _clean_state(state):
    cleaned = copy.deepcopy(state)
    cleaned["open_trades"] = [
        trade for trade in (cleaned.get("open_trades") or [])
        if not is_explicit_test_trade(trade)
    ]
    cleaned["closed_trades"] = [
        trade for trade in (cleaned.get("closed_trades") or [])
        if not is_explicit_test_trade(trade)
    ]
    return cleaned


def repair(base_dir, apply=False):
    paths = {name: os.path.join(base_dir, name) for name in FILES}
    state = _load(paths[FILES[0]], {"open_trades": [], "closed_trades": []})
    summary = inspect_state(state)
    print(f"检测到明确测试交易：open={len(summary['open_test'])}, closed={len(summary['closed_test'])}")
    print(f"当前交易总数：open={summary['open_total']}, closed={summary['closed_total']}")
    if not apply:
        print("只读模式：未修改任何文件。确认后使用 --apply 执行备份、隔离和重建。")
        return 0
    if not summary["open_test"] and not summary["closed_test"]:
        print("未发现可安全识别的测试交易，停止写入。")
        return 0

    backup_dir = os.path.join(base_dir, ".runtime-backups", datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(backup_dir, exist_ok=True)
    for path in paths.values():
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))

    cleaned_state = _clean_state(state)
    sw.atomic_write_json(paths[FILES[0]], cleaned_state)
    sw.write_trade_stats(cleaned_state, output_path=paths[FILES[1]])

    records = _load(os.path.join(base_dir, "signal_records.json"), [])
    if not isinstance(records, list):
        records = []
    tracking = track_signals.build_stats(records)
    quality = track_signals.build_quality_report(
        records, _load(paths[FILES[1]], {})
    )
    sw.atomic_write_json(paths[FILES[2]], tracking)
    sw.atomic_write_json(paths[FILES[3]], quality)
    print(f"已完成安全修复，备份目录：{backup_dir}")
    print("已从清理后的 state/records 重建 trade_stats、signal_tracking_stats、signal_quality_report")
    return 0


def main():
    parser = argparse.ArgumentParser(description="CoinPulse 运行态安全修复")
    parser.add_argument("--base-dir", default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--apply", action="store_true", help="确认后执行备份、隔离测试交易并重建报告")
    args = parser.parse_args()
    return repair(os.path.abspath(args.base_dir), apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
