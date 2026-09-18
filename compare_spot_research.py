#!/usr/bin/env python3
"""Compare the live S2 baseline with the isolated S1 4h research cohort."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import backtest_turtle as bt
import signal_watch as sw


def _profile(name, system, strategy, research_only):
    return {
        "name": name,
        "system": system,
        "strategy_version": strategy.get("strategy_version"),
        "cohort_id": strategy.get("cohort_id"),
        "research_only": research_only,
        "filters": sw.turtle_filter_options(strategy),
    }


def profiles(config):
    formal = dict(config.get("strategy") or {})
    formal.update({
        "strategy_version": "turtle_s2_4h_formal_v1",
        "cohort_id": "spot_s2_4h_formal_v1",
    })
    research_cfg = config.get("research_queue") or {}
    research = dict(formal)
    research["filters"] = {**(formal.get("filters") or {}), **(research_cfg.get("filters") or {})}
    research.update({
        "strategy_version": research_cfg.get("strategy_version", "turtle_s1_4h_research_v1"),
        "cohort_id": research_cfg.get("cohort_id", "spot_s1_4h_research_v1"),
    })
    return [
        _profile("formal_s2_4h", formal.get("turtle_system", "system2"), formal, False),
        _profile("fast_s1_4h", research_cfg.get("turtle_system", "system1"), research, True),
    ]


def _simulate_profile(klines, daily, profile, args, start_index=None, cost_multiplier=1):
    return bt.simulate(
        klines, daily, profile["filters"], capital=args.capital,
        fee_rate=args.fee_rate * cost_multiplier,
        slippage_rate=args.slippage * cost_multiplier,
        start_index=start_index, risk_fraction=args.risk_fraction,
        system=profile["system"],
    )


def _run_symbol(symbol, profile_list, args):
    klines, daily, datasets = bt.fetch_symbol(symbol, args)
    split_index = max(1, int(len(klines) * (1 - args.test_ratio)))
    result = {}
    for profile in profile_list:
        full = _simulate_profile(klines, daily, profile, args)
        out = _simulate_profile(klines, daily, profile, args, start_index=split_index)
        double = _simulate_profile(klines, daily, profile, args, start_index=split_index, cost_multiplier=2)
        quadruple = _simulate_profile(klines, daily, profile, args, start_index=split_index, cost_multiplier=4)
        result[profile["name"]] = {
            "identity": {key: profile[key] for key in ("system", "strategy_version", "cohort_id", "research_only")},
            "full": full,
            "out_of_sample": out,
            "out_of_sample_cost_stress": {
                "baseline_return": out["return"],
                "double_cost_return": double["return"],
                "quadruple_cost_return": quadruple["return"],
                "quadruple_cost_turns_negative": out["return"] >= 0 and quadruple["return"] < 0,
            },
        }
    return symbol, result, datasets


def aggregate(results, profile_name, period):
    metrics = [value[profile_name][period] for value in results.values()]
    trades = sum(item["trades"] for item in metrics)
    candidates = sum(item["candidates"] for item in metrics)
    wins = sum(item["wins"] for item in metrics)
    bars = sum(item.get("evaluated_bars", item["bars"]) for item in metrics)
    symbol_years = bars * 4 / 24 / 365 if bars else 0
    return {
        "symbols": len(metrics),
        "candidates": candidates,
        "closed_trades": trades,
        "candidates_per_symbol_year": round(candidates / symbol_years, 2) if symbol_years else 0,
        "closed_trades_per_symbol_year": round(trades / symbol_years, 2) if symbol_years else 0,
        "win_rate": round(wins / trades * 100, 2) if trades else 0,
        "mean_symbol_return": round(sum(item["return"] for item in metrics) / len(metrics), 2) if metrics else 0,
        "worst_symbol_drawdown": min((item["max_drawdown"] for item in metrics), default=0),
        "worst_consecutive_losses": max((item["max_consecutive_losses"] for item in metrics), default=0),
        "sample_reliability": "actionable_sample" if trades >= bt.MIN_RELIABLE_TRADES else "insufficient_sample",
    }


def aggregate_cost_stress(results, profile_name):
    stress = [value[profile_name]["out_of_sample_cost_stress"] for value in results.values()]
    return {
        "mean_baseline_return": round(sum(item["baseline_return"] for item in stress) / len(stress), 2) if stress else 0,
        "mean_double_cost_return": round(sum(item["double_cost_return"] for item in stress) / len(stress), 2) if stress else 0,
        "mean_quadruple_cost_return": round(sum(item["quadruple_cost_return"] for item in stress) / len(stress), 2) if stress else 0,
        "symbols_turning_negative_at_quadruple_cost": sum(1 for item in stress if item["quadruple_cost_turns_negative"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="S1/S2 现货历史研究对照")
    parser.add_argument("--config", default="signal_watch.config.template.json")
    parser.add_argument("--bars", type=int, default=3600)
    parser.add_argument("--symbols", default=",".join(sw.DEFAULT_SYMBOLS))
    parser.add_argument("--capital", type=float, default=bt.CAPITAL)
    parser.add_argument("--fee-rate", type=float, default=bt.FEE_RATE)
    parser.add_argument("--slippage", type=float, default=bt.SLIPPAGE_RATE)
    parser.add_argument("--risk-fraction", type=float, default=sw.TURTLE_RISK_FRACTION)
    parser.add_argument("--test-ratio", type=float, default=0.3)
    parser.add_argument("--data-dir", default=bt.DEFAULT_DATA_DIR)
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", default="spot_fast_research_compare.json")
    args = parser.parse_args(argv)
    args.test_ratio = max(0.05, min(0.9, args.test_ratio))
    with open(args.config, encoding="utf-8") as file:
        config = json.load(file)
    disabled = bt.load_disabled_symbols(args.config)
    requested = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    symbols = [item for item in requested if item not in disabled]
    profile_list = profiles(config)
    results, datasets, errors = {}, {}, {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        pending = {pool.submit(_run_symbol, symbol, profile_list, args): symbol for symbol in symbols}
        for future in as_completed(pending):
            symbol = pending[future]
            try:
                name, result, metadata = future.result()
                results[name], datasets[name] = result, metadata
            except Exception as exc:
                errors[symbol] = type(exc).__name__
    summary = {
        profile["name"]: {
            period: aggregate(results, profile["name"], period)
            for period in ("full", "out_of_sample")
        } for profile in profile_list
    }
    for profile in profile_list:
        summary[profile["name"]]["out_of_sample_cost_stress"] = aggregate_cost_stress(
            results, profile["name"]
        )
    s1 = summary["fast_s1_4h"]["out_of_sample"]
    s2 = summary["formal_s2_4h"]["out_of_sample"]
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "report_schema_version": 1,
        "purpose": "比较快速研究队列与正式基线；S1包含盈利后跳过下一次突破；报告不会自动修改线上策略",
        "interval": "4h", "bars_requested": args.bars,
        "symbols_requested": requested, "symbols_disabled": sorted(disabled),
        "test_ratio": args.test_ratio,
        "cost_model": {"fee_rate": args.fee_rate, "slippage_rate": args.slippage,
                       "stress_multipliers": [1, 2, 4]},
        "profiles": {item["name"]: {key: item[key] for key in ("system", "strategy_version", "cohort_id", "research_only")} for item in profile_list},
        "summary": summary,
        "comparison": {
            "out_of_sample_candidate_density_ratio_s1_vs_s2": round(
                s1["candidates_per_symbol_year"] / s2["candidates_per_symbol_year"], 2
            ) if s2["candidates_per_symbol_year"] else None,
            "out_of_sample_trade_difference": s1["closed_trades"] - s2["closed_trades"],
            "decision": "keep_research_only",
            "decision_reason": "快速队列须同时满足样本、回撤和成本压力要求后才能升级，当前报告不自动选优。",
        },
        "data_snapshot": {"directory": os.path.relpath(args.data_dir, sw.BASE_DIR), "datasets": datasets},
        "results": dict(sorted(results.items())), "errors": errors,
    }
    with open(os.path.join(sw.BASE_DIR, args.output), "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps({"summary": summary, "comparison": report["comparison"], "errors": errors}, ensure_ascii=False, indent=2))
    return 1 if errors or not results else 0


if __name__ == "__main__":
    raise SystemExit(main())
