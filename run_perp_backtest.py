#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run a reproducible perpetual backtest against a saved local snapshot."""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import console_output
import derivatives_snapshots
import derivatives_risk
import perp_backtest
import signal_watch as sw

OKX_POSITION_TIERS_SOURCE = "https://www.okx.com/api/v5/public/position-tiers"
CONFIGURED_TIER_SCOPES = {"provider_symbol", "provider", "global"}


def _snapshot_tier_binding(snapshot):
    tiers = derivatives_risk.normalize_maintenance_margin_tiers(
        snapshot.get("maintenance_margin_tiers") or []
    )
    if not tiers:
        return [], "snapshot_flat_rate", {}, None
    raw_metadata = snapshot.get("maintenance_margin_tier_metadata") or {}
    if not isinstance(raw_metadata, dict):
        raise RuntimeError("历史快照风险档位元数据格式无效")
    metadata = dict(raw_metadata)
    canonical = json.dumps(
        tiers, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    recorded_checksum = metadata.get("tier_checksum")
    if not recorded_checksum or recorded_checksum != checksum:
        raise RuntimeError("历史快照风险档位校验和不匹配")

    venue = str(snapshot.get("venue") or "").lower()
    symbol = str(snapshot.get("symbol") or "").upper()
    source_parameters = metadata.get("source_parameters") or {}
    expected_family = f"{symbol[:-4]}-USDT" if symbol.endswith("USDT") else ""
    official_okx = (
        venue == "okx"
        and metadata.get("source") == OKX_POSITION_TIERS_SOURCE
        and metadata.get("provider") in (None, "", "okx")
        and metadata.get("scope") in (None, "", "provider_symbol_official_snapshot")
        and metadata.get("symbol") in (None, "", symbol)
        and source_parameters.get("instType") == "SWAP"
        and source_parameters.get("tdMode") == "isolated"
        and source_parameters.get("instFamily") == expected_family
    )
    if official_okx:
        try:
            retrieved = int(metadata.get("retrieved_at_epoch_ms") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("历史快照官方风险档位获取时间无效") from exc
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if retrieved <= 0 or retrieved > now_ms:
            raise RuntimeError("历史快照官方风险档位获取时间无效")
        if metadata.get("tier_version") != checksum[:16]:
            raise RuntimeError("历史快照风险档位版本不匹配")
        metadata.update({
            "provider": "okx",
            "scope": "provider_symbol_official_snapshot",
            "symbol": symbol,
            "tier_checksum": checksum,
        })
        return tiers, "snapshot_official_tiers", metadata, checksum

    scope = metadata.get("scope")
    if scope in CONFIGURED_TIER_SCOPES:
        configured_provider = str(metadata.get("provider") or "").lower()
        configured_symbol = str(metadata.get("symbol") or "").upper()
        if configured_provider and configured_provider != venue:
            raise RuntimeError("历史快照配置风险档位 provider 绑定不匹配")
        if scope == "provider_symbol" and configured_symbol != symbol:
            raise RuntimeError("历史快照配置风险档位 symbol 绑定不匹配")
        if configured_symbol and configured_symbol != symbol:
            raise RuntimeError("历史快照配置风险档位 symbol 绑定不匹配")
        metadata["tier_checksum"] = checksum
        return tiers, "snapshot_configured_tiers", metadata, checksum
    raise RuntimeError("历史快照风险档位来源未经验证")


def run(symbol, interval="4h", data_dir="derivatives_data", output=None,
        account_value=10000.0, risk_fraction=0.005, leverage=2.0,
        fee_rate=0.0004, slippage_rate=0.0005, system="system2",
        slippage_model="volume_impact", slippage_impact_coefficient=0.001,
        max_slippage_rate=0.01, maintenance_margin_rate=0.005,
        liquidation_fee_rate=0.0, maintenance_margin_tiers=None,
        max_total_open_risk=0.04, liquidation_fee_stress_rate=0.005,
        liquidation_extreme_slippage_multiplier=2.0):
    manual_tiers_provided = maintenance_margin_tiers is not None
    manual_tiers = derivatives_risk.normalize_maintenance_margin_tiers(
        maintenance_margin_tiers
    )
    loaded = derivatives_snapshots.find_latest(data_dir, symbol, interval)
    if not loaded:
        raise RuntimeError(f"没有找到有效永续快照：{symbol} {interval}")
    snapshot = loaded["snapshot"]
    snapshot_tier_checksum = None
    if manual_tiers_provided:
        maintenance_margin_tiers = manual_tiers
        margin_source = "manual_tiers_override" if manual_tiers else "manual_flat_rate_override"
        effective_tier_metadata = {}
    else:
        (maintenance_margin_tiers, margin_source, effective_tier_metadata,
         snapshot_tier_checksum) = _snapshot_tier_binding(snapshot)
    scenarios = perp_backtest.run_cost_stress_tests(
        snapshot, account_value=account_value, risk_fraction=risk_fraction,
        leverage=leverage, fee_rate=fee_rate, slippage_rate=slippage_rate,
        system=system, slippage_model=slippage_model,
        slippage_impact_coefficient=slippage_impact_coefficient,
        max_slippage_rate=max_slippage_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
        maintenance_margin_tiers=maintenance_margin_tiers,
        max_total_open_risk=max_total_open_risk,
    )
    funding_stress = perp_backtest.run_funding_flip_stress(
        snapshot, account_value=account_value, risk_fraction=risk_fraction,
        leverage=leverage, fee_rate=fee_rate, slippage_rate=slippage_rate,
        system=system, slippage_model=slippage_model,
        slippage_impact_coefficient=slippage_impact_coefficient,
        max_slippage_rate=max_slippage_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
        maintenance_margin_tiers=maintenance_margin_tiers,
        baseline_result=scenarios["baseline"],
        max_total_open_risk=max_total_open_risk,
    )
    liquidity_stress = perp_backtest.run_liquidity_stress_tests(
        snapshot, account_value=account_value, risk_fraction=risk_fraction,
        leverage=leverage, fee_rate=fee_rate, slippage_rate=slippage_rate,
        system=system, slippage_model=slippage_model,
        slippage_impact_coefficient=slippage_impact_coefficient,
        max_slippage_rate=max_slippage_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
        maintenance_margin_tiers=maintenance_margin_tiers,
        baseline_result=scenarios["baseline"],
        max_total_open_risk=max_total_open_risk,
    )
    maintenance_margin_stress = perp_backtest.run_maintenance_margin_stress(
        snapshot, account_value=account_value, risk_fraction=risk_fraction,
        leverage=leverage, fee_rate=fee_rate, slippage_rate=slippage_rate,
        system=system, slippage_model=slippage_model,
        slippage_impact_coefficient=slippage_impact_coefficient,
        max_slippage_rate=max_slippage_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
        maintenance_margin_tiers=maintenance_margin_tiers,
        tiered_result=scenarios["baseline"],
        max_total_open_risk=max_total_open_risk,
    )
    liquidation_fee_stress = perp_backtest.run_liquidation_fee_stress(
        snapshot, account_value=account_value, risk_fraction=risk_fraction,
        leverage=leverage, fee_rate=fee_rate, slippage_rate=slippage_rate,
        system=system, slippage_model=slippage_model,
        slippage_impact_coefficient=slippage_impact_coefficient,
        max_slippage_rate=max_slippage_rate,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
        stress_liquidation_fee_rate=liquidation_fee_stress_rate,
        extreme_slippage_multiplier=liquidation_extreme_slippage_multiplier,
        maintenance_margin_tiers=maintenance_margin_tiers,
        baseline_result=scenarios["baseline"],
        max_total_open_risk=max_total_open_risk,
    )
    generated_at = datetime.now(timezone.utc)
    contract_rows = snapshot.get("contract_klines") or []
    last_close_ms = (
        int(contract_rows[-1]["time"]) + sw.INTERVAL_MS[snapshot["interval"]]
        if contract_rows else None
    )
    data_age_hours = (
        (generated_at.timestamp() * 1000 - last_close_ms) / (60 * 60 * 1000)
        if last_close_ms is not None else None
    )
    report = {
        "schema_version": 1,
        "generated_at_utc": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_type": "linear_perpetual",
        "venue": loaded["metadata"].get("venue"),
        "symbol": loaded["metadata"].get("symbol"),
        "interval": loaded["metadata"].get("interval"),
        "input_snapshot": {
            "file": loaded["metadata"].get("file"),
            "sha256": loaded["metadata"].get("sha256"),
            "saved_at_utc": loaded["metadata"].get("saved_at_utc"),
            "collection": snapshot.get("collection") or {},
            "data_health": snapshot.get("data_health") or {},
            "research_mode": "historical_snapshot_backtest",
            "last_contract_close_epoch_ms": last_close_ms,
            "last_contract_close_utc": (
                datetime.fromtimestamp(last_close_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if last_close_ms is not None else None
            ),
            "data_age_hours": round(data_age_hours, 4) if data_age_hours is not None else None,
        },
        "parameters": {
            "account_value": account_value, "risk_fraction": risk_fraction,
            "leverage": leverage, "fee_rate": fee_rate,
            "slippage_rate": slippage_rate, "system": system,
            "slippage_model": slippage_model,
            "slippage_impact_coefficient": slippage_impact_coefficient,
            "max_slippage_rate": max_slippage_rate,
            "maintenance_margin_rate": maintenance_margin_rate,
            "liquidation_fee_rate": liquidation_fee_rate,
            "maintenance_margin_tiers": maintenance_margin_tiers or [],
            "max_total_open_risk": max_total_open_risk,
        },
        "contract_specs": snapshot.get("contract_specs"),
        "risk_model": {
            "contract_constraints_bound": bool(snapshot.get("contract_specs")),
            "liquidation_model": scenarios["baseline"]["liquidation_model"]["model_version"],
            "maintenance_margin_source": margin_source,
            "maintenance_margin_tier_metadata": effective_tier_metadata,
            "maintenance_margin_tier_checksum": (
                snapshot_tier_checksum
                if margin_source in {"snapshot_official_tiers", "snapshot_configured_tiers"}
                else None
            ),
            "slippage_model": slippage_model,
            "liquidity_proxy": "contract_kline_base_volume",
        },
        "cost_stress": scenarios,
        "funding_flip_stress": funding_stress,
        "liquidity_stress": liquidity_stress,
        "maintenance_margin_stress": maintenance_margin_stress,
        "liquidation_fee_stress": liquidation_fee_stress,
    }
    if output:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        with open(output, "w", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.write("\n")
    return report


def main(argv=None):
    console_output.configure_utf8_output()
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
    parser.add_argument("--slippage-model", choices=("fixed", "volume_impact"), default="volume_impact")
    parser.add_argument("--slippage-impact-coefficient", type=float, default=0.001)
    parser.add_argument("--max-slippage-rate", type=float, default=0.01)
    parser.add_argument("--maintenance-margin-rate", type=float, default=0.005)
    parser.add_argument("--liquidation-fee-rate", type=float, default=0.0)
    parser.add_argument(
        "--liquidation-fee-stress-rate", type=float, default=0.005,
        help="保守强平费用压力基准，默认 0.5%%",
    )
    parser.add_argument(
        "--liquidation-extreme-slippage-multiplier", type=float, default=2.0,
        help="极端强平场景的滑点与冲击倍数",
    )
    parser.add_argument("--max-total-open-risk", type=float, default=0.04)
    parser.add_argument(
        "--maintenance-margin-tiers-json", default="",
        help='JSON array such as [{"max_notional":50000,"rate":0.005}]',
    )
    parser.add_argument("--system", choices=("system1", "system2"), default="system2")
    args = parser.parse_args(argv)
    try:
        maintenance_tiers = json.loads(args.maintenance_margin_tiers_json) if args.maintenance_margin_tiers_json else None
        if maintenance_tiers is not None and not isinstance(maintenance_tiers, list):
            raise ValueError("--maintenance-margin-tiers-json must be a JSON array")
        report = run(
            args.symbol, args.interval, os.path.abspath(args.data_dir),
            args.output or None, args.account_value, args.risk_fraction,
            args.leverage, args.fee_rate, args.slippage_rate, args.system,
            args.slippage_model, args.slippage_impact_coefficient, args.max_slippage_rate,
            args.maintenance_margin_rate, args.liquidation_fee_rate, maintenance_tiers,
            args.max_total_open_risk, args.liquidation_fee_stress_rate,
            args.liquidation_extreme_slippage_multiplier,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"回测完成：{report['symbol']} {report['interval']}")
    print(f"输入快照 SHA-256：{report['input_snapshot']['sha256']}")
    print(f"最后收盘时间：{report['input_snapshot']['last_contract_close_utc']}")
    print(f"数据年龄：{report['input_snapshot']['data_age_hours']:.2f} 小时（历史快照回测）")
    print(f"基准收益：{report['cost_stress']['baseline']['return_pct']:.4f}%")
    print(f"2倍成本收益：{report['cost_stress']['double_cost']['return_pct']:.4f}%")
    print(f"4倍成本收益：{report['cost_stress']['quadruple_cost']['return_pct']:.4f}%")
    margin_stress = report["maintenance_margin_stress"]
    if margin_stress["enabled"]:
        print(
            "维持保证金对照："
            f"固定 {margin_stress['flat']['return_pct']:.4f}% / "
            f"分层 {margin_stress['tiered']['return_pct']:.4f}%，"
            f"强平 {margin_stress['flat']['liquidation_count']} / "
            f"{margin_stress['tiered']['liquidation_count']}"
        )
    else:
        print("维持保证金对照：未配置已核验分层档位，未启用")
    liquidation_stress = report["liquidation_fee_stress"]
    print(
        "强平费用压力："
        f"配置 {liquidation_stress['summary']['configured']['return_pct']:.4f}% / "
        f"正常费用 {liquidation_stress['summary']['normal_fee']['return_pct']:.4f}% / "
        f"双倍费用 {liquidation_stress['summary']['double_fee']['return_pct']:.4f}% / "
        f"极端费用与滑点 {liquidation_stress['summary']['extreme_fee_slippage']['return_pct']:.4f}%"
    )
    if args.output:
        print(f"报告：{os.path.abspath(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
