#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Evaluate recorded signals after fixed horizons without using future data."""

import json
import logging
import os
import time
import math
from datetime import datetime

import signal_watch as sw
from signal_archive import archive_and_trim


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_RECORDS_PATH = os.path.join(BASE_DIR, "signal_records.json")
HORIZONS = {"24h": 24 * 60 * 60 * 1000, "48h": 48 * 60 * 60 * 1000}


def load_records():
    if not os.path.exists(SIGNAL_RECORDS_PATH):
        return []
    try:
        with open(SIGNAL_RECORDS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_records(records):
    records, archived = archive_and_trim(
        records, os.path.join(BASE_DIR, "signal_archive")
    )
    sw.atomic_write_json(SIGNAL_RECORDS_PATH, records)
    if archived:
        logging.info("已归档 %s 条较早的影子信号记录", archived)


def evaluate_horizon(record, klines, label, now_ms=None):
    """Return a horizon result once the complete observation window is closed."""
    signal_bar_time = int(record.get("signal_bar_time") or 0)
    interval = record.get("interval") or "4h"
    interval_ms = sw.INTERVAL_MS.get(interval)
    horizon_ms = HORIZONS[label]
    if not signal_bar_time or not interval_ms:
        return None
    signal_close_time = signal_bar_time + interval_ms
    target_time = signal_close_time + horizon_ms
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if current_ms < target_time:
        return None

    future = [
        row for row in klines
        if signal_close_time <= int(row["time"]) < target_time
    ]
    if not future:
        return None
    first_bar = future[0]
    try:
        entry = (
            float(first_bar["open"])
            if record.get("entry_model", "next_bar_open") == "next_bar_open"
            else float(record["entry_price"])
        )
    except (TypeError, ValueError):
        return None
    direction = record.get("direction")
    if entry <= 0 or direction not in ("long", "short"):
        return None

    if direction == "long":
        favorable = max(row["high"] for row in future)
        adverse = min(row["low"] for row in future)
        final_price = future[-1]["close"]
        mfe = (favorable - entry) / entry * 100
        mae = (adverse - entry) / entry * 100
        final_return = (final_price - entry) / entry * 100
    else:
        favorable = min(row["low"] for row in future)
        adverse = max(row["high"] for row in future)
        final_price = future[-1]["close"]
        mfe = (entry - favorable) / entry * 100
        mae = (entry - adverse) / entry * 100
        final_return = (entry - final_price) / entry * 100

    outcome = "WIN" if final_return > 0 else "LOSS" if final_return < 0 else "FLAT"
    return {
        "entry_price": entry,
        "entry_model": record.get("entry_model", "next_bar_open"),
        "mfe_pct": round(mfe, 3),
        "mae_pct": round(mae, 3),
        "final_return_pct": round(final_return, 3),
        "final_price": final_price,
        "outcome": outcome,
        "observed_until": target_time,
        "observed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }


def track_records(records):
    symbols = sorted({item.get("symbol") for item in records if item.get("symbol")})
    cache = {}
    errors = {}
    for symbol in symbols:
        intervals = sorted({
            item.get("interval") for item in records
            if item.get("symbol") == symbol and item.get("interval") in sw.INTERVAL_MS
        })
        for interval in intervals:
            try:
                klines, provider = sw.fetch_klines_with_fallback(symbol, interval, closed_only=True)
                cache[(symbol, interval)] = (klines, provider)
            except Exception as exc:
                errors[f"{symbol}|{interval}"] = str(exc)

    updated = 0
    for record in records:
        key = (record.get("symbol"), record.get("interval"))
        if key not in cache:
            continue
        klines, provider = cache[key]
        horizons = record.setdefault("horizons", {"24h": None, "48h": None})
        for label in HORIZONS:
            if horizons.get(label) is not None:
                continue
            result = evaluate_horizon(record, klines, label)
            if result is not None:
                result["provider"] = provider
                horizons[label] = result
                updated += 1
        if all(horizons.get(label) is not None for label in HORIZONS):
            record["status"] = "complete"
    return updated, errors


def build_stats(records):
    """Create a compact aggregate report for the repository and dashboard."""
    stats = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_signals": len(records),
        "pending_signals": sum(1 for item in records if item.get("status") != "complete"),
        "horizons": {},
        "by_symbol": {}
    }
    for label in HORIZONS:
        results = [
            item.get("horizons", {}).get(label)
            for item in records
            if item.get("horizons", {}).get(label)
        ]
        wins = sum(1 for item in results if item.get("outcome") == "WIN")
        losses = sum(1 for item in results if item.get("outcome") == "LOSS")
        returns = [float(item.get("final_return_pct") or 0) for item in results]
        stats["horizons"][label] = {
            "observed": len(results),
            "wins": wins,
            "losses": losses,
            "flats": len(results) - wins - losses,
            "win_rate": round(wins / len(results) * 100, 2) if results else 0,
            "avg_return_pct": round(sum(returns) / len(returns), 3) if returns else 0,
            "avg_mfe_pct": round(sum(float(item.get("mfe_pct") or 0) for item in results) / len(results), 3) if results else 0,
            "avg_mae_pct": round(sum(float(item.get("mae_pct") or 0) for item in results) / len(results), 3) if results else 0
        }
    for record in records:
        symbol = record.get("symbol")
        if not symbol:
            continue
        group = stats["by_symbol"].setdefault(symbol, {"signals": 0, "24h": {}, "48h": {}})
        group["signals"] += 1
        for label in HORIZONS:
            result = record.get("horizons", {}).get(label)
            if result:
                target = group[label]
                target["observed"] = target.get("observed", 0) + 1
                target["wins"] = target.get("wins", 0) + (result.get("outcome") == "WIN")
                target["losses"] = target.get("losses", 0) + (result.get("outcome") == "LOSS")
                target["return_sum"] = round(target.get("return_sum", 0) + float(result.get("final_return_pct") or 0), 3)
    for group in stats["by_symbol"].values():
        for label in HORIZONS:
            target = group[label]
            observed = target.get("observed", 0)
            if observed:
                target["win_rate"] = round(target["wins"] / observed * 100, 2)
                target["avg_return_pct"] = round(target["return_sum"] / observed, 3)
            target.pop("return_sum", None)
    return stats


def _wilson_interval(wins, observed, z=1.96):
    """Return a conservative 95% Wilson interval for a small signal sample."""
    if not observed:
        return {"low": 0.0, "high": 0.0}
    p = wins / observed
    denominator = 1 + z * z / observed
    centre = (p + z * z / (2 * observed)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * observed)) / observed) / denominator
    return {"low": round(max(0, centre - margin) * 100, 2), "high": round(min(1, centre + margin) * 100, 2)}


def build_quality_report(records, trade_stats=None):
    """Combine directional horizons and strategy exits into one decision report."""
    trade_stats = trade_stats or {}
    horizons = {}
    for label in HORIZONS:
        results = [item.get("horizons", {}).get(label) for item in records]
        results = [item for item in results if item]
        wins = sum(1 for item in results if item.get("outcome") == "WIN")
        observed = len(results)
        returns = [float(item.get("final_return_pct") or 0) for item in results]
        horizons[label] = {
            "observed": observed,
            "wins": wins,
            "losses": sum(1 for item in results if item.get("outcome") == "LOSS"),
            "win_rate": round(wins / observed * 100, 2) if observed else 0,
            "win_rate_95_ci": _wilson_interval(wins, observed),
            "avg_return_pct": round(sum(returns) / observed, 3) if observed else 0,
        }
    closed = int(trade_stats.get("total") or 0)
    closed_wins = int(trade_stats.get("wins") or 0)
    if closed < 30:
        reliability = "insufficient_sample"
        reliability_text = "样本不足：至少完成30笔，建议50笔后再判断"
    elif closed < 50:
        reliability = "provisional"
        reliability_text = "初步样本：可以观察，不宜据此扩大仓位"
    else:
        reliability = "actionable_sample"
        reliability_text = "样本达到最低判断门槛，仍需结合回撤和成本"
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "definition": {
            "trade_win_rate": "按海龟实际影子交易的止损/通道退出结算",
            "horizon_win_rate": "按下一根K线开盘后固定24h/48h方向收益结算，不等同于策略最终胜率",
        },
        "sample": {
            "closed_trades": closed,
            "pending_signals": sum(1 for item in records if item.get("status") != "complete"),
            "minimum_for_judgement": 30,
            "recommended_for_judgement": 50,
            "reliability": reliability,
            "reliability_text": reliability_text,
        },
        "trade": {
            "observed": closed,
            "wins": closed_wins,
            "losses": int(trade_stats.get("losses") or 0),
            "win_rate": trade_stats.get("win_rate", 0),
            "avg_win": trade_stats.get("avg_win", 0),
            "avg_loss": trade_stats.get("avg_loss", 0),
            "payoff": trade_stats.get("payoff", 0),
            "total_pnl_pct": trade_stats.get("total_pnl_pct", 0),
        },
        "horizons": horizons,
        "action_policy": "在完成至少30笔海龟影子交易前，仅把推送作为观察信号；不要仅根据24h/48h方向胜率扩大仓位。",
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    records = load_records()
    if not records:
        logging.info("没有待追踪的信号记录")
        stats = build_stats([])
        sw.atomic_write_json(os.path.join(BASE_DIR, "signal_tracking_stats.json"), stats)
        sw.atomic_write_json(
            os.path.join(BASE_DIR, "signal_quality_report.json"),
            build_quality_report([], {}),
        )
        return 0
    updated, errors = track_records(records)
    save_records(records)
    stats_path = os.path.join(BASE_DIR, "signal_tracking_stats.json")
    stats = build_stats(records)
    sw.atomic_write_json(stats_path, stats)
    trade_stats = {}
    trade_stats_path = os.path.join(BASE_DIR, "trade_stats.json")
    try:
        with open(trade_stats_path, encoding="utf-8") as file:
            trade_stats = json.load(file)
    except (OSError, ValueError):
        pass
    sw.atomic_write_json(
        os.path.join(BASE_DIR, "signal_quality_report.json"),
        build_quality_report(records, trade_stats),
    )
    logging.info("影子信号追踪完成：更新 %s 个观察窗口，记录数 %s", updated, len(records))
    for key, message in errors.items():
        logging.warning("%s 获取失败：%s", key, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
