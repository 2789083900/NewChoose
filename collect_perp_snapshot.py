#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect and version a public Binance USD-M perpetual research snapshot."""

import argparse
import os
import sys

import derivatives_data
import derivatives_snapshots


DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "derivatives_data")


def collect(symbol, interval="4h", limit=3600, output_dir=DEFAULT_OUTPUT_DIR,
            allow_stale=False, fetcher=None):
    if fetcher is None:
        def fetch(symbol_value, interval_value, limit=3600, closed_only=True):
            return derivatives_data.fetch_perpetual_snapshot(
                symbol_value, interval_value, limit=limit, closed_only=closed_only,
                include_contract_specs=True,
            )
    else:
        fetch = fetcher
    snapshot = fetch(symbol, interval, limit=limit, closed_only=True)
    errors = derivatives_data.validate_perpetual_snapshot(
        snapshot,
        interval,
        max_staleness_intervals=(10 ** 9 if allow_stale else 3),
    )
    if errors:
        raise RuntimeError("永续快照校验失败：" + "; ".join(errors))
    saved = derivatives_snapshots.save_snapshot(
        output_dir, snapshot, source="binance-usdm-public"
    )
    snapshots = []
    for name in os.listdir(output_dir):
        if not name.endswith(".json") or name == "manifest.json":
            continue
        path = os.path.join(output_dir, name)
        try:
            loaded = derivatives_snapshots.load_snapshot(path)
        except (OSError, ValueError, TypeError):
            loaded = None
        if loaded:
            snapshots.append(loaded)
    derivatives_snapshots.save_manifest(output_dir, snapshots)
    return saved


def main(argv=None):
    parser = argparse.ArgumentParser(description="保存 Binance USD-M 永续研究快照")
    parser.add_argument("--symbol", required=True, help="例如 BTCUSDT")
    parser.add_argument("--interval", default="4h", choices=sorted(derivatives_data.SUPPORTED_INTERVALS))
    parser.add_argument("--limit", type=int, default=3600, help="K线与资金费率目标条数，支持自动分页")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--allow-stale", action="store_true", help="允许保存过旧行情，仅用于历史归档")
    args = parser.parse_args(argv)
    try:
        result = collect(
            args.symbol, args.interval, args.limit,
            os.path.abspath(args.output_dir), args.allow_stale,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"快照已保存：{result['path']}")
    print(f"SHA-256：{result['metadata']['sha256']}")
    print(f"清单：{os.path.join(os.path.abspath(args.output_dir), 'manifest.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
