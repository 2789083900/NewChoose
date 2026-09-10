#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate generated artifacts before a scheduled backtest is published."""

import argparse
import json
import os
import sys


def validate_backtest_report(path):
    errors = []
    try:
        with open(path, encoding="utf-8") as file:
            report = json.load(file)
    except (OSError, ValueError) as exc:
        return [f"无法读取回测报告：{exc}"]
    for field in ("generated_at", "interval", "system", "data_snapshot", "results", "portfolio"):
        if field not in report:
            errors.append(f"缺少字段：{field}")
    if report.get("report_schema_version", 0) < 2:
        errors.append("报告缺少 schema v2 顶层版本字段")
    threshold = report.get("sample_reliability_threshold_trades")
    if not isinstance(threshold, int) or threshold < 1:
        errors.append("缺少有效的低样本交易门槛")
    if report.get("errors"):
        errors.append(f"存在失败币种：{', '.join(sorted(report['errors']))}")
    if not report.get("results"):
        errors.append("没有成功生成任何币种结果")
    disabled = {str(item).upper() for item in (report.get("symbols_disabled") or [])}
    present = {str(item).upper() for item in (report.get("results") or {})}
    leaked = sorted(disabled & present)
    if leaked:
        errors.append(f"停用币种仍出现在回测结果：{', '.join(leaked)}")
    for symbol, periods in (report.get("results") or {}).items():
        for period, variants in (periods or {}).items():
            if period not in ("full", "in_sample", "out_of_sample"):
                continue
            metrics = (variants or {}).get("production_default")
            if not metrics:
                continue
            if "sample_reliability" not in metrics:
                errors.append(f"{symbol}/{period} 缺少样本可靠性字段")
            elif metrics.get("sample_reliability") == "insufficient_sample" and int(metrics.get("trades", 0)) >= threshold:
                errors.append(f"{symbol}/{period} 样本状态与交易数矛盾")
    snapshot = report.get("data_snapshot") or {}
    datasets = snapshot.get("datasets") or {}
    if not datasets:
        errors.append("没有数据快照元数据")
    for symbol, intervals in datasets.items():
        for interval, metadata in (intervals or {}).items():
            if not metadata.get("sha256") or len(str(metadata["sha256"])) != 64:
                errors.append(f"{symbol}/{interval} 缺少有效SHA-256")
            filename = metadata.get("file")
            if filename and not os.path.exists(os.path.join(os.path.dirname(path), snapshot.get("directory", ""), filename)):
                # The report may use an absolute data directory outside the repo.
                data_dir = snapshot.get("directory") or ""
                if not os.path.exists(os.path.join(data_dir, filename)):
                    errors.append(f"{symbol}/{interval} 快照文件不存在：{filename}")
    portfolio = report.get("portfolio") or {}
    rolling = portfolio.get("rolling_validation") or {}
    if not rolling.get("enabled"):
        errors.append("组合滚动验证未启用")
    elif not rolling.get("windows"):
        errors.append("组合滚动验证没有完整窗口")
    return errors


def main():
    parser = argparse.ArgumentParser(description="CoinPulse 回测报告质量检查")
    parser.add_argument("--backtest", default="turtle_backtest_compare.json")
    args = parser.parse_args()
    errors = validate_backtest_report(args.backtest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"报告校验通过：{args.backtest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
