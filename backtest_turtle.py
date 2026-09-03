#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare the original Turtle entry rules with the optional confirmations.

This intentionally uses the same signal functions as signal_watch.py so that
the report does not drift away from the live watcher.
"""

import json
import os
import sys
import argparse
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed

import signal_watch as sw


CAPITAL = 10000.0
FEE_RATE = 0.001
SLIPPAGE_RATE = 0.0005
INTERVAL = "4h"
TOTAL_BARS = 2500
SYSTEM = "system2"
SYMBOLS = sw.DEFAULT_SYMBOLS


def execution_price(trigger_price, direction, action, slippage_rate):
    """Apply adverse market slippage to an entry or exit fill."""
    price = float(trigger_price)
    slippage = max(0.0, float(slippage_rate))
    is_buy = (direction == "long" and action == "entry") or (
        direction == "short" and action == "exit"
    )
    return price * (1 + slippage) if is_buy else price * (1 - slippage)


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


def simulate(
    klines, daily, filters, capital=CAPITAL,
    fee_rate=FEE_RATE, slippage_rate=SLIPPAGE_RATE,
    start_index=None, end_index=None
):
    params = sw.turtle_params(SYSTEM, INTERVAL)
    n_series = sw.calc_n_series(klines, params["n_period"])
    higher_at = higher_trend_lookup(daily)
    start = max(params["entry_bars"], params["n_period"])
    equity = float(capital)
    peak = float(capital)
    max_drawdown = 0.0
    position = None
    pending_entry = None
    last_marked = equity
    trades = []
    candidates = 0
    filtered = 0

    def close_position(index, price, reason):
        nonlocal equity, position
        fill_price = execution_price(price, position["direction"], "exit", slippage_rate)
        gross = (
            (fill_price - position["avg_entry"]) * position["quantity"]
            if position["direction"] == "long"
            else (position["avg_entry"] - fill_price) * position["quantity"]
        )
        equity += gross - fill_price * position["quantity"] * fee_rate
        net = (equity - position["equity_at_entry"]) / position["equity_at_entry"]
        trades.append({
            "direction": position["direction"],
            "entry_time": position["entry_time"],
            "exit_time": klines[index]["time"],
            "entry": position["avg_entry"],
            "exit_trigger": price,
            "exit": fill_price,
            "return": net,
            "reason": reason,
            "units": len(position["units"]),
        })
        position = None

    first_index = start if start_index is None else max(start, int(start_index))
    last_index = len(klines) if end_index is None else min(len(klines), int(end_index))
    for index in range(first_index, last_index):
        bar = klines[index]
        n = n_series[index]
        levels = sw.turtle_levels(klines, index, SYSTEM, INTERVAL)
        if not levels or n is None or n <= 0:
            continue

        # 信号在上一根已收盘K线确认，下一根K线开盘执行，避免使用确认K线收盘价成交。
        entered_this_bar = False
        if pending_entry is not None and pending_entry["index"] == index and position is None:
            signal = pending_entry
            direction = signal["direction"]
            entry_trigger = signal["entry_trigger"]
            entry = execution_price(bar["open"], direction, "entry", slippage_rate)
            quantity = equity * 0.01 / signal["n"]
            entry_fee = entry * quantity * fee_rate
            equity -= entry_fee
            position = {
                "direction": direction,
                "entry_time": bar["time"],
                "signal_time": signal["signal_time"],
                "equity_at_entry": equity + entry_fee,
                "quantity": quantity,
                "avg_entry": entry,
                "last_price": entry_trigger,
                "last_n": signal["n"],
                "units": [{"price": entry, "trigger": entry_trigger, "n": signal["n"]}],
            }
            pending_entry = None
            entered_this_bar = True

        exited = False
        if position and not entered_this_bar:
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

        if not position and not exited and pending_entry is None and index + 1 < last_index:
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
                pending_entry = {
                    "index": index + 1,
                    "direction": direction,
                    "entry_trigger": plan["entry"],
                    "signal_time": bar["time"],
                    "n": n,
                }

        if position and not entered_this_bar:
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
                fill_price = execution_price(next_price, position["direction"], "entry", slippage_rate)
                equity -= fill_price * quantity * fee_rate
                total_cost = position["avg_entry"] * position["quantity"] + fill_price * quantity
                position["quantity"] += quantity
                position["avg_entry"] = total_cost / position["quantity"]
                position["last_price"] = next_price
                position["last_n"] = n
                position["units"].append({"price": fill_price, "trigger": next_price, "n": n})

        marked = equity if not position else equity + (
            (bar["close"] - position["avg_entry"]) * position["quantity"]
            if position["direction"] == "long"
            else (position["avg_entry"] - bar["close"]) * position["quantity"]
        )
        last_marked = marked
        peak = max(peak, marked)
        max_drawdown = min(max_drawdown, (marked - peak) / peak)

    closed = len(trades)
    wins = [trade for trade in trades if trade["return"] > 0]
    losses = [trade for trade in trades if trade["return"] <= 0]
    max_consecutive_losses = 0
    consecutive_losses = 0
    for trade in trades:
        if trade["return"] <= 0:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0
    gross_profit = sum(trade["return"] for trade in wins)
    gross_loss = abs(sum(trade["return"] for trade in losses))
    period_start = max(0, first_index)
    period_end = max(period_start, min(len(klines) - 1, last_index - 1))
    days = max(1.0, (klines[period_end]["time"] - klines[period_start]["time"]) / 86400000)
    return {
        "bars": len(klines),
        "trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / closed * 100, 2) if closed else 0,
        "payoff": round((gross_profit / len(wins)) / (gross_loss / len(losses)), 2) if wins and losses else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (99 if gross_profit else 0),
        "ending_equity": round(last_marked, 2),
        "return": round((last_marked / capital - 1) * 100, 2),
        "annualized": round((last_marked / capital) ** (365 / days) * 100 - 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "avg_win": round(gross_profit / len(wins) * 100, 2) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses) * 100, 2) if losses else 0,
        "candidates": candidates,
        "filtered": filtered,
        "max_consecutive_losses": max_consecutive_losses,
        "open_position": bool(position),
        "open_units": len(position["units"]) if position else 0,
    }


def fetch_symbol(symbol, total_bars):
    klines = sw.fetch_binance_history(symbol, INTERVAL, total_bars)
    klines = sw.filter_closed_klines(klines, INTERVAL)
    daily = sw.fetch_binance_history(symbol, "1d", 420)
    daily = sw.filter_closed_klines(daily, "1d")
    if len(klines) < 400 or len(daily) < 210:
        raise RuntimeError(f"数据不足：4h={len(klines)}，1d={len(daily)}")
    return klines, daily


def run_symbol(symbol, args):
    klines, daily = fetch_symbol(symbol, args.bars)
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
    full_results = {
        name: simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate, slippage_rate=args.slippage
        ) for name, filters in variants.items()
    }
    split = max(0.0, min(0.9, args.test_ratio))
    split_index = max(1, int(len(klines) * (1 - split)))
    in_sample = {
        name: simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate, slippage_rate=args.slippage,
            end_index=split_index
        ) for name, filters in variants.items()
    }
    out_of_sample = {
        name: simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate, slippage_rate=args.slippage,
            start_index=split_index
        ) for name, filters in variants.items()
    }
    return symbol, {
        "full": full_results,
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "split_index": split_index,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="CoinPulse 海龟策略回测")
    parser.add_argument("--capital", type=float, default=CAPITAL)
    parser.add_argument("--fee-rate", type=float, default=FEE_RATE)
    parser.add_argument("--slippage", type=float, default=SLIPPAGE_RATE)
    parser.add_argument("--bars", type=int, default=TOTAL_BARS)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--output", default="turtle_backtest_compare.json")
    parser.add_argument(
        "--test-ratio", type=float, default=0.3,
        help="最后多少比例的数据作为样本外测试，默认30%"
    )
    return parser.parse_args()


def aggregate(results_by_symbol):
    """Summarize symbol means; this is not a portfolio simulation."""
    summary = {}
    for period in ("full", "in_sample", "out_of_sample"):
        variants = {}
        for symbol_result in results_by_symbol.values():
            for variant, metrics in symbol_result.get(period, {}).items():
                variants.setdefault(variant, []).append(metrics)
        summary[period] = {}
        for variant, metrics_list in variants.items():
            trade_count = sum(item.get("trades", 0) for item in metrics_list)
            wins = sum(item.get("wins", 0) for item in metrics_list)
            returns = [float(item.get("return", 0)) for item in metrics_list]
            drawdowns = [float(item.get("max_drawdown", 0)) for item in metrics_list]
            summary[period][variant] = {
                "symbols": len(metrics_list),
                "trades": trade_count,
                "wins": wins,
                "losses": sum(item.get("losses", 0) for item in metrics_list),
                "trade_win_rate": round(wins / trade_count * 100, 2) if trade_count else 0,
                "mean_symbol_return": round(sum(returns) / len(returns), 2) if returns else 0,
                "worst_symbol_drawdown": round(min(drawdowns), 2) if drawdowns else 0,
                "mean_max_consecutive_losses": round(
                    sum(item.get("max_consecutive_losses", 0) for item in metrics_list) / len(metrics_list), 2
                ) if metrics_list else 0
            }
    return summary


def main():
    args = parse_args()
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    results = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_symbol, symbol, args): symbol for symbol in symbols}
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
        "bars_requested": args.bars,
        "capital": args.capital,
        "fee_rate": args.fee_rate,
        "slippage_rate": args.slippage,
        "test_ratio": max(0.0, min(0.9, args.test_ratio)),
        "execution_model": "信号收盘确认，下一根K线开盘成交；按成交价施加不利滑点；手续费按成交额单边计算",
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
        "summary": aggregate(results),
        "errors": errors,
    }
    output = os.path.join(sw.BASE_DIR, args.output)
    with open(output, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告已写入: {output}")
    return 1 if not results else 0


if __name__ == "__main__":
    raise SystemExit(main())
