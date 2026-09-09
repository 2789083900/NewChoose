#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a reproducible perpetual backtest against a saved local snapshot."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import derivatives_snapshots
import perp_backtest


def run(symbol, interval="4h", data_dir="derivatives_data", output=None,
        account_value=10000.0, risk_fraction=0.005, leverage=2.0,
        fee_rate=0.0004, slippage_rate=0.0005, system="system2"):
    loaded = derivatives_snapshots.find_latest(data_dir, symbol, interval)
    if not loaded:
        raise RuntimeError(f"没有找到有效永续快照：{symbol} {interval}")
    snapshot = loaded["snapshot"]
    scenarios = perp_backtest.run_cost_stress_tests(
        snapshot, account_value=account_value, risk_fraction=risk_fraction,
        leverage=leverage, fee_rate=fee_rate, slippage_rate=slippage_rate,
        system=system,
    )
    funding_stress = perp_backtest.run_funding_flip_stress(
        snapshot, account_value=account_value, risk_fraction=risk_fraction,
        leverage=leverage, fee_rate=fee_rate, slippage_rate=slippage_rate,
        system=system,
    )
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_type": "linear_perpetual",
        "venue": loaded["metadata"].get("venue"),
        "symbol": loaded["metadata"].get("symbol"),
        "interval": loaded["metadata"].get("interval"),
        "input_snapshot": {
            "file": loaded["metadata"].get("file"),
            "sha256": loaded["metadata"].get("sha256"),
            "saved_at_utc": loaded["metadata"].get("saved_at_utc"),
        },
        "parameters": {
            "account_value": account_value, "risk_fraction": risk_fraction,
            "leverage": leverage, "fee_rate": fee_rate,
            "slippage_rate": slippage_rate, "system": system,
        },
        "contract_specs": snapshot.get("contract_specs"),
        "risk_model": {
            "contract_constraints_bound": bool(snapshot.get("contract_specs")),
            "liquidation_model": "exchange_agnostic_approximation",
            "maintenance_margin_source": "manual_parameter",
        },
        "cost_stress": scenarios,
        "funding_flip_stress": funding_stress,
    }
    if output:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        with open(output, "w", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行可复现的永续海龟回测")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="4h")
    parser.add_argument("--data-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "derivatives_data"))
    parser.add_argument("--output", default="")
    parser.add_argument("--account-value", type=float, default=10000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.005)
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--fee-rate", type=float, default=0.0004)
    parser.add_argument("--slippage-rate", type=float, default=0.0005)
    parser.add_argument("--system", choices=("system1", "system2"), default="system2")
    args = parser.parse_args(argv)
    try:
        report = run(
            args.symbol, args.interval, os.path.abspath(args.data_dir),
            args.output or None, args.account_value, args.risk_fraction,
            args.leverage, args.fee_rate, args.slippage_rate, args.system,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"回测完成：{report['symbol']} {report['interval']}")
    print(f"输入快照 SHA-256：{report['input_snapshot']['sha256']}")
    print(f"基准收益：{report['cost_stress']['baseline']['return_pct']:.4f}%")
    print(f"2倍成本收益：{report['cost_stress']['double_cost']['return_pct']:.4f}%")
    print(f"4倍成本收益：{report['cost_stress']['quadruple_cost']['return_pct']:.4f}%")
    if args.output:
        print(f"报告：{os.path.abspath(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
