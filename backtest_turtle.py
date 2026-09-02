#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare the original Turtle entry rules with the optional confirmations.

This intentionally uses the same signal functions as signal_watch.py so that
the report does not drift away from the live watcher.
"""

import json
import os
import sys
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed

import signal_watch as sw


CAPITAL = 10000.0
FEE_RATE = 0.001
INTERVAL = "4h"
TOTAL_BARS = 2500
SYSTEM = "system2"
SYMBOLS = sw.DEFAULT_SYMBOLS


def higher_trend_lookup(daily, period=200):
    closes = [row["close"] for row in daily]
    ema_values = sw.ema(closes, period)
    ends = [row["time"] + sw.INTERVAL_MS["1d"] for row in daily]
    trends = []
    for index, close in enumerate(closes):
        ema_value = ema_values[index]
        trends.append(
            "long" if ema_value is not None and close > ema_value
            else "short" if ema_value is not None and close < ema_value
            else None
        )

    def at(timestamp):
        index = bisect_right(ends, timestamp) - 1
        return trends[index] if index >= 0 else None

    return at


def simulate(klines, daily, filters):
    params = sw.turtle_params(SYSTEM, INTERVAL)
    n_series = sw.calc_n_series(klines, params["n_period"])
    higher_at = higher_trend_lookup(daily)
    start = max(params["entry_bars"], params["n_period"])
    equity = CAPITAL
    peak = CAPITAL
    max_drawdown = 0.0
    position = None
    trades = []
    candidates = 0
    filtered = 0

    def close_position(index, price, reason):
        nonlocal equity, position
        gross = (
            (price - position["avg_entry"]) * position["quantity"]
            if position["direction"] == "long"
            else (position["avg_entry"] - price) * position["quantity"]
        )
        equity += gross - price * position["quantity"] * FEE_RATE
        net = (equity - position["equity_at_entry"]) / position["equity_at_entry"]
        trades.append({
            "direction": position["direction"],
            "entry_time": position["entry_time"],
            "exit_time": klines[index]["time"],
            "entry": position["avg_entry"],
            "exit": price,
            "return": net,
            "reason": reason,
            "units": len(position["units"]),
        })
        position = None

    for index in range(start, len(klines)):
        bar = klines[index]
        n = n_series[index]
        levels = sw.turtle_levels(klines, index, SYSTEM, INTERVAL)
        if not levels or n is None or n <= 0:
            continue

        exited = False
        if position:
            stop = (
                position["last_price"] - 2 * position["last_n"]
                if position["direction"] == "long"
                else position["last_price"] + 2 * position["last_n"]
            )
            stop_hit = bar["low"] <= stop if position["direction"] == "long" else bar["high"] >= stop
            exit_level = levels["exit_low"] if position["direction"] == "long" else levels["exit_high"]
            exit_hit = bar["low"] <= exit_level if position["direction"] == "long" else bar["high"] >= exit_level
            if stop_hit:
                close_position(index, stop, "2N止损")
                exited = True
            elif exit_hit:
                close_position(index, exit_level, "通道退出")
                exited = True

        if not position and not exited:
            higher = higher_at(bar["time"])
            breakout = sw.build_turtle_signal(
                klines[:index + 1], SYSTEM, equity, 0.01, INTERVAL,
                filter_options=filters, higher_trend=higher
            )
            plan = breakout[2]
            if plan and plan.get("filtered"):
                filtered += 1
            if breakout[0]:
                candidates += 1
                direction = breakout[0]
                entry = plan["entry"]
                quantity = equity * 0.01 / n
                entry_fee = entry * quantity * FEE_RATE
                equity -= entry_fee
                position = {
                    "direction": direction,
                    "entry_time": bar["time"],
                    "equity_at_entry": equity + entry_fee,
                    "quantity": quantity,
                    "avg_entry": entry,
                    "last_price": entry,
                    "last_n": n,
                    "units": [{"price": entry, "n": n}],
                }

        if position:
            while len(position["units"]) < sw.TURTLE_MAX_UNITS:
                next_price = (
                    position["last_price"] + 0.5 * position["last_n"]
                    if position["direction"] == "long"
                    else position["last_price"] - 0.5 * position["last_n"]
                )
                reached = bar["high"] >= next_price if position["direction"] == "long" else bar["low"] <= next_price
                if not reached:
                    break
                quantity = equity * 0.01 / n
                equity -= next_price * quantity * FEE_RATE
                total_cost = position["avg_entry"] * position["quantity"] + next_price * quantity
                position["quantity"] += quantity
                position["avg_entry"] = total_cost / position["quantity"]
                position["last_price"] = next_price
                position["last_n"] = n
                position["units"].append({"price": next_price, "n": n})

        marked = equity if not position else equity + (
            (bar["close"] - position["avg_entry"]) * position["quantity"]
            if position["direction"] == "long"
            else (position["avg_entry"] - bar["close"]) * position["quantity"]
        )
        peak = max(peak, marked)
        max_drawdown = min(max_drawdown, (marked - peak) / peak)

    closed = len(trades)
    wins = [trade for trade in trades if trade["return"] > 0]
    losses = [trade for trade in trades if trade["return"] <= 0]
    gross_profit = sum(trade["return"] for trade in wins)
    gross_loss = abs(sum(trade["return"] for trade in losses))
    days = max(1.0, (klines[-1]["time"] - klines[start]["time"]) / 86400000)
    return {
        "bars": len(klines),
        "trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / closed * 100, 2) if closed else 0,
        "payoff": round((gross_profit / len(wins)) / (gross_loss / len(losses)), 2) if wins and losses else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (99 if gross_profit else 0),
        "return": round((equity / CAPITAL - 1) * 100, 2),
        "annualized": round((equity / CAPITAL) ** (365 / days) * 100 - 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "avg_win": round(gross_profit / len(wins) * 100, 2) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses) * 100, 2) if losses else 0,
        "candidates": candidates,
        "filtered": filtered,
    }


def fetch_symbol(symbol):
    klines = sw.fetch_binance_history(symbol, INTERVAL, TOTAL_BARS)
    klines = sw.filter_closed_klines(klines, INTERVAL)
    daily = sw.fetch_binance_history(symbol, "1d", 420)
    daily = sw.filter_closed_klines(daily, "1d")
    if len(klines) < 400 or len(daily) < 210:
        raise RuntimeError(f"数据不足：4h={len(klines)}，1d={len(daily)}")
    return klines, daily


def run_symbol(symbol):
    klines, daily = fetch_symbol(symbol)
    base = dict(sw.TURTLE_FILTER_DEFAULTS)
    base.update({"adx_enabled": False, "volume_confirmation": False, "volatility_filter": False})
    variants = {
        "base": base,
        "adx_only": {**base, "adx_enabled": True},
        "volume_only": {**base, "volume_confirmation": True},
        "volatility_only": {**base, "volatility_filter": True},
        "adx_volume": {**base, "adx_enabled": True, "volume_confirmation": True},
        # The production default keeps ADX optional; this variant explicitly
        # enables every confirmation for comparison.
        "production_default": {**base, "volume_confirmation": True, "volatility_filter": True},
        "enhanced": {**base, "adx_enabled": True, "volume_confirmation": True, "volatility_filter": True},
    }
    return symbol, {
        name: simulate(klines, daily, filters) for name, filters in variants.items()
    }


def main():
    results = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(run_symbol, symbol): symbol for symbol in SYMBOLS}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                name, result = future.result()
                results[name] = result
                print(f"{name}: 完成")
            except Exception as exc:
                errors[symbol] = str(exc)
                print(f"{symbol}: 失败 - {exc}", file=sys.stderr)

    report = {
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "interval": INTERVAL,
        "system": SYSTEM,
        "bars_requested": TOTAL_BARS,
        "fee_rate": FEE_RATE,
        "base": "原有规则：收盘突破 + 高周期EMA200 + 0.1N缓冲",
        "variants": {
            "base": "原有规则：收盘突破 + 高周期EMA200 + 0.1N缓冲",
            "adx_only": "原有规则 + ADX≥20且方向一致",
            "volume_only": "原有规则 + 突破量≥前20根均量",
            "volatility_only": "原有规则 + ATR/收盘价0.2%～12%",
            "adx_volume": "原有规则 + ADX + 成交量",
            "production_default": "原有规则 + 成交量 + ATR波动率（当前默认）",
            "enhanced": "原有规则 + ADX + 成交量 + ATR波动率",
        },
        "results": dict(sorted(results.items())),
        "errors": errors,
    }
    output = os.path.join(sw.BASE_DIR, "turtle_backtest_compare.json")
    with open(output, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告已写入: {output}")
    return 1 if not results else 0


if __name__ == "__main__":
    raise SystemExit(main())
