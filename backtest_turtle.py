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


def gap_adjusted_trigger(bar, direction, action, trigger_price):
    """Use the opening price when a gap has already crossed a trigger.

    OHLC data cannot reveal the intrabar path, but the open is known to occur
    before the bar high/low.  Filling a crossed stop or add-on at its trigger
    would otherwise assume liquidity at a price the market had skipped.
    """
    trigger = float(trigger_price)
    opening = float(bar["open"])
    is_buy = (direction == "long" and action == "entry") or (
        direction == "short" and action == "exit"
    )
    return max(trigger, opening) if is_buy else min(trigger, opening)


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
        raw_exit = gap_adjusted_trigger(
            klines[index], position["direction"], "exit", price
        )
        fill_price = execution_price(raw_exit, position["direction"], "exit", slippage_rate)
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
            "exit_before_slippage": raw_exit,
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
                raw_entry = gap_adjusted_trigger(
                    bar, position["direction"], "entry", next_price
                )
                fill_price = execution_price(raw_entry, position["direction"], "entry", slippage_rate)
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
    open_position_at_cutoff = bool(position)
    open_units_at_cutoff = len(position["units"]) if position else 0
    liquidated_equity = last_marked
    if position:
        raw_exit = float(klines[period_end]["close"])
        fill_price = execution_price(raw_exit, position["direction"], "exit", slippage_rate)
        gross = (
            (fill_price - position["avg_entry"]) * position["quantity"]
            if position["direction"] == "long"
            else (position["avg_entry"] - fill_price) * position["quantity"]
        )
        liquidated_equity = equity + gross - fill_price * position["quantity"] * fee_rate
        max_drawdown = min(max_drawdown, (liquidated_equity - peak) / peak)
    return {
        "bars": len(klines),
        "trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / closed * 100, 2) if closed else 0,
        "payoff": round((gross_profit / len(wins)) / (gross_loss / len(losses)), 2) if wins and losses else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (99 if gross_profit else 0),
        "ending_equity": round(liquidated_equity, 2),
        "ending_equity_marked": round(last_marked, 2),
        "return": round((liquidated_equity / capital - 1) * 100, 2),
        "annualized": round((liquidated_equity / capital) ** (365 / days) * 100 - 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "avg_win": round(gross_profit / len(wins) * 100, 2) if wins else 0,
        "avg_loss": round(-gross_loss / len(losses) * 100, 2) if losses else 0,
        "candidates": candidates,
        "filtered": filtered,
        "max_consecutive_losses": max_consecutive_losses,
        "open_position": False,
        "open_units": 0,
        "open_position_at_cutoff": open_position_at_cutoff,
        "open_units_at_cutoff": open_units_at_cutoff,
        "end_of_period_model": "未平仓按最后收盘价、手续费和不利滑点强制平仓；同时保留未平仓市值字段",
    }


def fetch_symbol(symbol, total_bars):
    klines = sw.fetch_binance_history(symbol, INTERVAL, total_bars)
    klines = sw.filter_closed_klines(klines, INTERVAL)
    # Keep enough higher-timeframe warm-up when the requested 4h history grows.
    daily_bars = max(420, int(total_bars / 6) + 220)
    daily = sw.fetch_binance_history(symbol, "1d", daily_bars)
    daily = sw.filter_closed_klines(daily, "1d")
    if len(klines) < 400 or len(daily) < 210:
        raise RuntimeError(f"数据不足：4h={len(klines)}，1d={len(daily)}")
    return klines, daily


def backtest_variants():
    base = dict(sw.TURTLE_FILTER_DEFAULTS)
    base.update({"adx_enabled": False, "volume_confirmation": False, "volatility_filter": False})
    return {
        "base": base,
        "adx_only": {**base, "adx_enabled": True},
        "volume_only": {**base, "volume_confirmation": True},
        "volatility_only": {**base, "volatility_filter": True},
        "adx_volume": {**base, "adx_enabled": True, "volume_confirmation": True},
        "production_default": {**base, "volume_confirmation": True, "volatility_filter": True},
        "enhanced": {**base, "adx_enabled": True, "volume_confirmation": True, "volatility_filter": True},
    }


def run_symbol(symbol, args):
    klines, daily = fetch_symbol(symbol, args.bars)
    variants = backtest_variants()
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
    }, (klines, daily)


def portfolio_capacity(positions, symbol, direction, max_symbol_units, max_total_units, max_direction_units):
    """Return whether a new unit fits the deterministic portfolio risk caps."""
    symbol_units = sum(
        len(position["units"]) for name, position in positions.items() if name == symbol
    )
    total_units = sum(len(position["units"]) for position in positions.values())
    direction_units = sum(
        len(position["units"]) for position in positions.values()
        if position["direction"] == direction
    )
    return (
        symbol_units < max_symbol_units
        and total_units < max_total_units
        and direction_units < max_direction_units
    )


def simulate_portfolio(market_data, filters, args, start_index=None, end_index=None):
    """Simulate all symbols against one cash pool and shared unit limits.

    Signals confirmed at the same close are queued for the next open and
    accepted alphabetically.  This is intentionally explicit: without a
    deterministic priority, a portfolio result can depend on dict ordering.
    """
    if not market_data:
        return {"error": "没有可用于组合回测的币种数据"}
    symbols = sorted(market_data)
    time_sets = [{bar["time"] for bar in market_data[symbol][0]} for symbol in symbols]
    common_times = sorted(set.intersection(*time_sets))
    params = sw.turtle_params(SYSTEM, INTERVAL)
    minimum = max(params["entry_bars"], params["n_period"])
    first = minimum if start_index is None else max(minimum, int(start_index))
    last = len(common_times) if end_index is None else min(len(common_times), int(end_index))
    if last - first < 2:
        return {"error": "共同历史K线不足"}

    prepared = {}
    for symbol, (klines, daily) in market_data.items():
        index_by_time = {bar["time"]: index for index, bar in enumerate(klines)}
        prepared[symbol] = {
            "klines": klines,
            "index_by_time": index_by_time,
            "n_series": sw.calc_n_series(klines, params["n_period"]),
            "higher_at": higher_trend_lookup(daily),
        }

    cash = float(args.capital)
    positions = {}
    pending = {}
    trades = []
    rejected_signals = 0
    rejected_adds = 0
    peak = cash
    max_drawdown = 0.0
    equity_curve = []

    def marked_equity(price_field):
        value = cash
        for symbol, position in positions.items():
            index = prepared[symbol]["index_by_time"][current_time]
            price = prepared[symbol]["klines"][index][price_field]
            gross = (price - position["avg_entry"]) * position["quantity"]
            if position["direction"] == "short":
                gross = -gross
            value += gross
        return value

    def close_position(symbol, index, trigger, reason):
        nonlocal cash
        position = positions[symbol]
        bar = prepared[symbol]["klines"][index]
        raw_exit = gap_adjusted_trigger(bar, position["direction"], "exit", trigger)
        fill = execution_price(raw_exit, position["direction"], "exit", args.slippage)
        gross = (fill - position["avg_entry"]) * position["quantity"]
        if position["direction"] == "short":
            gross = -gross
        exit_fee = fill * position["quantity"] * args.fee_rate
        cash += gross - exit_fee
        trades.append({
            "symbol": symbol, "direction": position["direction"],
            "entry_time": position["entry_time"], "exit_time": bar["time"],
            "entry": position["avg_entry"], "exit": fill, "reason": reason,
            "units": len(position["units"]),
            "return": (
                gross - position["entry_fees"] - exit_fee
            ) / position["equity_at_entry"],
        })
        del positions[symbol]

    for time_index in range(first, last):
        current_time = common_times[time_index]
        # Entries created at the preceding close use this bar's known opening price.
        entered_symbols = set()
        for symbol in sorted(list(pending)):
            signal = pending.pop(symbol)
            index = prepared[symbol]["index_by_time"][current_time]
            if not portfolio_capacity(
                positions, symbol, signal["direction"], args.portfolio_max_symbol_units,
                args.portfolio_max_total_units, args.portfolio_max_direction_units
            ):
                rejected_signals += 1
                continue
            account_value = max(0.0, marked_equity("open"))
            entry = execution_price(
                prepared[symbol]["klines"][index]["open"], signal["direction"], "entry", args.slippage
            )
            quantity = account_value * 0.01 / signal["n"]
            cash -= entry * quantity * args.fee_rate
            positions[symbol] = {
                "direction": signal["direction"], "quantity": quantity, "avg_entry": entry,
                "last_price": signal["entry_trigger"], "last_n": signal["n"],
                "units": [{"price": entry, "n": signal["n"]}],
                "entry_time": prepared[symbol]["klines"][index]["time"],
                "equity_at_entry": account_value,
                "entry_fees": entry * quantity * args.fee_rate,
            }
            entered_symbols.add(symbol)

        # Pessimistic intrabar precedence: stop, then channel exit, then add-on.
        for symbol in sorted(list(positions)):
            if symbol in entered_symbols:
                continue
            position = positions.get(symbol)
            index = prepared[symbol]["index_by_time"][current_time]
            bar = prepared[symbol]["klines"][index]
            stop = position["last_price"] - 2 * position["last_n"] if position["direction"] == "long" else position["last_price"] + 2 * position["last_n"]
            levels = sw.turtle_levels(prepared[symbol]["klines"], index, SYSTEM, INTERVAL)
            exit_level = levels["exit_low"] if position["direction"] == "long" else levels["exit_high"]
            stop_hit = bar["low"] <= stop if position["direction"] == "long" else bar["high"] >= stop
            exit_hit = bar["low"] <= exit_level if position["direction"] == "long" else bar["high"] >= exit_level
            if stop_hit:
                close_position(symbol, index, stop, "2N止损")
            elif exit_hit:
                close_position(symbol, index, exit_level, "通道退出")

        # Close-confirmed signals are queued only if a following common bar exists.
        if time_index + 1 < last:
            for symbol in symbols:
                if symbol in positions or symbol in pending:
                    continue
                index = prepared[symbol]["index_by_time"][current_time]
                n = prepared[symbol]["n_series"][index]
                if not n or n <= 0:
                    continue
                direction, _, plan = sw.build_turtle_signal(
                    prepared[symbol]["klines"][:index + 1], SYSTEM, max(0.0, marked_equity("close")),
                    0.01, INTERVAL, filter_options=filters,
                    higher_trend=prepared[symbol]["higher_at"](current_time)
                )
                if direction:
                    pending[symbol] = {
                        "direction": direction, "entry_trigger": plan["entry"], "n": n
                    }

        for symbol in sorted(list(positions)):
            if symbol in entered_symbols:
                continue
            position = positions.get(symbol)
            index = prepared[symbol]["index_by_time"][current_time]
            bar = prepared[symbol]["klines"][index]
            n = prepared[symbol]["n_series"][index] or position["last_n"]
            while portfolio_capacity(
                positions, symbol, position["direction"], args.portfolio_max_symbol_units,
                args.portfolio_max_total_units, args.portfolio_max_direction_units
            ):
                next_price = position["last_price"] + 0.5 * position["last_n"] if position["direction"] == "long" else position["last_price"] - 0.5 * position["last_n"]
                reached = bar["high"] >= next_price if position["direction"] == "long" else bar["low"] <= next_price
                if not reached:
                    break
                raw_entry = gap_adjusted_trigger(bar, position["direction"], "entry", next_price)
                fill = execution_price(raw_entry, position["direction"], "entry", args.slippage)
                quantity = max(0.0, marked_equity("close")) * 0.01 / n
                cash -= fill * quantity * args.fee_rate
                position["entry_fees"] += fill * quantity * args.fee_rate
                total_cost = position["avg_entry"] * position["quantity"] + fill * quantity
                position["quantity"] += quantity
                position["avg_entry"] = total_cost / position["quantity"]
                position["last_price"] = next_price
                position["last_n"] = n
                position["units"].append({"price": fill, "n": n})
            if len(position["units"]) < args.portfolio_max_symbol_units and not portfolio_capacity(
                positions, symbol, position["direction"], args.portfolio_max_symbol_units,
                args.portfolio_max_total_units, args.portfolio_max_direction_units
            ):
                rejected_adds += 1

        equity = marked_equity("close")
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, (equity - peak) / peak) if peak else max_drawdown
        equity_curve.append({"time": current_time, "equity": round(equity, 2)})

    for symbol in sorted(list(positions)):
        index = prepared[symbol]["index_by_time"][common_times[last - 1]]
        close_position(symbol, index, prepared[symbol]["klines"][index]["close"], "期末强制平仓")
    max_drawdown = min(max_drawdown, (cash - peak) / peak) if peak else max_drawdown
    wins = sum(1 for trade in trades if trade["return"] > 0)
    return {
        "symbols": symbols, "trades": len(trades), "wins": wins,
        "losses": len(trades) - wins,
        "trade_win_rate": round(wins / len(trades) * 100, 2) if trades else 0,
        "ending_equity": round(cash, 2),
        "return": round((cash / args.capital - 1) * 100, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "rejected_signals": rejected_signals, "rejected_adds": rejected_adds,
        "risk_limits": {
            "max_symbol_units": args.portfolio_max_symbol_units,
            "max_total_units": args.portfolio_max_total_units,
            "max_direction_units": args.portfolio_max_direction_units,
        },
        "execution_priority": "同一时点：字母顺序入场；止损、通道退出、加仓",
        "equity_curve": equity_curve,
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
        help="最后多少比例的数据作为样本外测试，默认30%%"
    )
    parser.add_argument("--portfolio-max-symbol-units", type=int, default=4)
    parser.add_argument("--portfolio-max-total-units", type=int, default=12)
    parser.add_argument("--portfolio-max-direction-units", type=int, default=12)
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
    market_data = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_symbol, symbol, args): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                name, result, data = future.result()
                results[name] = result
                market_data[name] = data
                print(f"{name}: 完成")
            except Exception as exc:
                errors[symbol] = str(exc)
                print(f"{symbol}: 失败 - {exc}", file=sys.stderr)

    portfolio = {}
    if market_data:
        filters = backtest_variants()["production_default"]
        aligned_bars = min(len(data[0]) for data in market_data.values())
        split_index = max(
            1, int(aligned_bars * (1 - max(0.0, min(0.9, args.test_ratio))))
        )
        portfolio = {
            "variant": "production_default",
            "full": simulate_portfolio(market_data, filters, args),
            "in_sample": simulate_portfolio(
                market_data, filters, args, end_index=split_index
            ),
            "out_of_sample": simulate_portfolio(
                market_data, filters, args, start_index=split_index
            ),
        }

    report = {
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "interval": INTERVAL,
        "system": SYSTEM,
        "bars_requested": args.bars,
        "capital": args.capital,
        "fee_rate": args.fee_rate,
        "slippage_rate": args.slippage,
        "test_ratio": max(0.0, min(0.9, args.test_ratio)),
        "execution_model": "信号收盘确认，下一根K线开盘成交；跳空时按更差的开盘价成交，再施加不利滑点；手续费按成交额单边计算；期末强制平仓",
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
        "portfolio": portfolio,
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
