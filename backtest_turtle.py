#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare the original Turtle entry rules with the optional confirmations.

This intentionally uses the same signal functions as signal_watch.py so that
the report does not drift away from the live watcher.
"""

import json
import os
import platform
import sys
import argparse
import copy
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed

import signal_watch as sw
import backtest_data as data_store


CAPITAL = 10000.0
FEE_RATE = 0.001
SLIPPAGE_RATE = 0.0005
INTERVAL = "4h"
TOTAL_BARS = 3600
SYSTEM = "system2"
SYMBOLS = sw.DEFAULT_SYMBOLS
DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_data")


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
    start_index=None, end_index=None, risk_fraction=sw.TURTLE_RISK_FRACTION
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
            quantity = equity * risk_fraction / signal["n"]
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
                klines[:index + 1], SYSTEM, equity, risk_fraction, INTERVAL,
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
                quantity = equity * risk_fraction / n
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


def load_or_download_dataset(symbol, interval, required_bars, args):
    """Use a validated snapshot unless an explicit refresh is requested."""
    cached = None if args.refresh_data else data_store.load_dataset(
        args.data_dir, symbol, interval, required_bars
    )
    if cached:
        return cached["klines"], cached["metadata"]
    klines = sw.fetch_binance_history(symbol, interval, required_bars)
    klines = sw.filter_closed_klines(klines, interval)
    if len(klines) < required_bars:
        raise RuntimeError(f"{symbol} {interval} 数据不足：需要{required_bars}，实际{len(klines)}")
    saved = data_store.save_dataset(
        args.data_dir, symbol, interval, "binance-data", "spot",
        klines[-required_bars:], sw.INTERVAL_MS[interval]
    )
    return saved["klines"], saved["metadata"]


def fetch_symbol(symbol, args):
    klines, intraday_metadata = load_or_download_dataset(
        symbol, INTERVAL, args.bars, args
    )
    # Keep enough higher-timeframe warm-up when the requested 4h history grows.
    daily_bars = max(420, int(args.bars / 6) + 220)
    daily, daily_metadata = load_or_download_dataset(
        symbol, "1d", daily_bars, args
    )
    if len(klines) < 400 or len(daily) < 210:
        raise RuntimeError(f"数据不足：4h={len(klines)}，1d={len(daily)}")
    return klines, daily, {INTERVAL: intraday_metadata, "1d": daily_metadata}


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
    klines, daily, datasets = fetch_symbol(symbol, args)
    variants = backtest_variants()
    full_results = {
        name: simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate, slippage_rate=args.slippage
            , risk_fraction=args.risk_fraction
        ) for name, filters in variants.items()
    }
    split = max(0.0, min(0.9, args.test_ratio))
    split_index = max(1, int(len(klines) * (1 - split)))
    in_sample = {
        name: simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate, slippage_rate=args.slippage,
            end_index=split_index, risk_fraction=args.risk_fraction
        ) for name, filters in variants.items()
    }
    out_of_sample = {
        name: simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate, slippage_rate=args.slippage,
            start_index=split_index, risk_fraction=args.risk_fraction
        ) for name, filters in variants.items()
    }
    return symbol, {
        "full": full_results,
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "split_index": split_index,
    }, (klines, daily), datasets


def rolling_validation(klines, daily, filters, args):
    """Evaluate fixed production settings on successive unseen windows.

    The development slice is reported for context only.  It is never used to
    select a variant or alter a parameter, avoiding hidden optimisation.
    """
    params = sw.turtle_params(SYSTEM, INTERVAL)
    train_bars = max(0, int(args.rolling_train_bars))
    test_bars = max(1, int(args.rolling_test_bars))
    step_bars = max(1, int(args.rolling_step_bars))
    minimum_train = max(params["entry_bars"], params["n_period"]) + 1
    if train_bars < minimum_train:
        return {
            "enabled": False,
            "reason": f"训练窗口至少需要{minimum_train}根{INTERVAL} K线",
            "train_bars": train_bars,
            "test_bars": test_bars,
            "step_bars": step_bars,
            "windows": [],
        }

    windows = []
    train_end = train_bars
    while train_end + test_bars <= len(klines):
        test_end = train_end + test_bars
        baseline = simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate, slippage_rate=args.slippage,
            start_index=train_end, end_index=test_end,
            risk_fraction=args.risk_fraction,
        )
        doubled_cost = simulate(
            klines, daily, filters, capital=args.capital,
            fee_rate=args.fee_rate * 2, slippage_rate=args.slippage * 2,
            start_index=train_end, end_index=test_end,
            risk_fraction=args.risk_fraction,
        )
        windows.append({
            "development": {
                "start_index": 0, "end_index": train_end,
                "start_time": klines[0]["time"],
                "end_time": klines[train_end - 1]["time"],
                "metrics": simulate(
                    klines, daily, filters, capital=args.capital,
                    fee_rate=args.fee_rate, slippage_rate=args.slippage,
                    end_index=train_end, risk_fraction=args.risk_fraction,
                ),
            },
            "validation": {
                "start_index": train_end, "end_index": test_end,
                "start_time": klines[train_end]["time"],
                "end_time": klines[test_end - 1]["time"],
                "baseline_cost": baseline,
                "double_cost": doubled_cost,
            },
        })
        train_end += step_bars

    return {
        "enabled": True,
        "method": "固定production_default参数；开发窗口仅作展示，不自动调参或筛选",
        "train_bars": train_bars,
        "test_bars": test_bars,
        "step_bars": step_bars,
        "windows": windows,
    }


def portfolio_capacity(positions, symbol, direction, max_symbol_units, max_total_units, max_direction_units):
    """Return whether a new unit fits the basic deterministic portfolio caps."""
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


def portfolio_capacity_reason(positions, symbol, direction, args, allow_existing_symbol=False):
    """Mirror the live watcher's unit caps and explain a rejected unit."""
    symbol_units = sum(
        len(position["units"]) for name, position in positions.items() if name == symbol
    )
    if symbol_units and not allow_existing_symbol:
        return False, "symbol"
    if symbol_units >= args.portfolio_max_symbol_units:
        return False, "symbol"
    total_units = sum(len(position["units"]) for position in positions.values())
    if total_units >= args.portfolio_max_total_units:
        return False, "total"
    direction_units = sum(
        len(position["units"]) for position in positions.values()
        if position["direction"] == direction
    )
    if direction_units >= args.portfolio_max_direction_units:
        return False, "direction"

    strong_groups = getattr(args, "portfolio_strong_groups", [sw.DEFAULT_SYMBOLS])
    weak_groups = getattr(args, "portfolio_weak_groups", [])

    def containing_group(groups):
        return next((set(group) for group in groups if symbol in group), set())

    strong_group = containing_group(strong_groups)
    weak_group = containing_group(weak_groups) if not strong_group else set()
    group = strong_group or weak_group
    if group:
        group_units = sum(
            len(position["units"]) for name, position in positions.items()
            if name in group and position["direction"] == direction
        )
        limit = (
            getattr(args, "portfolio_max_strong_group_units", 6)
            if strong_group else getattr(args, "portfolio_max_weak_group_units", 10)
        )
        if group_units >= limit:
            return False, "strong_group" if strong_group else "weak_group"
    return True, None


def simulate_portfolio(market_data, filters, args, start_index=None, end_index=None):
    """Simulate all symbols against one cash pool and shared unit limits.

    Signals confirmed at the same close are queued for the next open and
    accepted alphabetically.  This is intentionally explicit: without a
    deterministic priority, a portfolio result can depend on dict ordering.
    """
    risk_fraction = float(getattr(args, "risk_fraction", sw.TURTLE_RISK_FRACTION))
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
    rejected_by_limit = {}
    max_total_units = 0
    max_direction_units = 0
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
            allowed, reason = portfolio_capacity_reason(
                positions, symbol, signal["direction"], args
            )
            if not allowed:
                rejected_signals += 1
                rejected_by_limit[reason] = rejected_by_limit.get(reason, 0) + 1
                continue
            account_value = max(0.0, marked_equity("open"))
            entry = execution_price(
                prepared[symbol]["klines"][index]["open"], signal["direction"], "entry", args.slippage
            )
            quantity = account_value * risk_fraction / signal["n"]
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
                    risk_fraction, INTERVAL, filter_options=filters,
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
            while portfolio_capacity_reason(
                positions, symbol, position["direction"], args, allow_existing_symbol=True
            )[0]:
                next_price = position["last_price"] + 0.5 * position["last_n"] if position["direction"] == "long" else position["last_price"] - 0.5 * position["last_n"]
                reached = bar["high"] >= next_price if position["direction"] == "long" else bar["low"] <= next_price
                if not reached:
                    break
                raw_entry = gap_adjusted_trigger(bar, position["direction"], "entry", next_price)
                fill = execution_price(raw_entry, position["direction"], "entry", args.slippage)
                quantity = max(0.0, marked_equity("close")) * risk_fraction / n
                cash -= fill * quantity * args.fee_rate
                position["entry_fees"] += fill * quantity * args.fee_rate
                total_cost = position["avg_entry"] * position["quantity"] + fill * quantity
                position["quantity"] += quantity
                position["avg_entry"] = total_cost / position["quantity"]
                position["last_price"] = next_price
                position["last_n"] = n
                position["units"].append({"price": fill, "n": n})
            if len(position["units"]) < args.portfolio_max_symbol_units and not portfolio_capacity_reason(
                positions, symbol, position["direction"], args, allow_existing_symbol=True
            )[0]:
                rejected_adds += 1

        equity = marked_equity("close")
        total_units = sum(len(position["units"]) for position in positions.values())
        max_total_units = max(max_total_units, total_units)
        max_direction_units = max(
            max_direction_units,
            max((sum(len(position["units"]) for position in positions.values() if position["direction"] == side) for side in ("long", "short")), default=0)
        )
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
        "rejected_by_limit": rejected_by_limit,
        "max_total_units_observed": max_total_units,
        "max_direction_units_observed": max_direction_units,
        "risk_limits": {
            "max_symbol_units": args.portfolio_max_symbol_units,
            "max_total_units": args.portfolio_max_total_units,
            "max_direction_units": args.portfolio_max_direction_units,
            "max_strong_group_units": getattr(args, "portfolio_max_strong_group_units", 6),
            "max_weak_group_units": getattr(args, "portfolio_max_weak_group_units", 10),
            "strong_groups": getattr(args, "portfolio_strong_groups", [sw.DEFAULT_SYMBOLS]),
            "weak_groups": getattr(args, "portfolio_weak_groups", []),
        },
        "execution_priority": "同一时点：字母顺序入场；止损、通道退出、加仓",
        "equity_curve": equity_curve,
    }


def portfolio_rolling_validation(market_data, filters, args):
    """Run fixed, out-of-sample portfolio windows with live-like risk caps."""
    if not market_data:
        return {"enabled": False, "reason": "没有可用于组合回测的币种数据", "windows": []}
    symbols = sorted(market_data)
    common_times = sorted(set.intersection(*(
        {bar["time"] for bar in market_data[symbol][0]} for symbol in symbols
    )))
    params = sw.turtle_params(SYSTEM, INTERVAL)
    train_bars = max(0, int(args.rolling_train_bars))
    test_bars = max(1, int(args.rolling_test_bars))
    step_bars = max(1, int(args.rolling_step_bars))
    minimum_train = max(params["entry_bars"], params["n_period"]) + 1
    if train_bars < minimum_train:
        return {
            "enabled": False,
            "reason": f"训练窗口至少需要{minimum_train}根{INTERVAL}共同K线",
            "train_bars": train_bars, "test_bars": test_bars, "step_bars": step_bars,
            "common_bars": len(common_times), "windows": [],
        }

    windows = []
    train_end = train_bars
    while train_end + test_bars <= len(common_times):
        test_end = train_end + test_bars
        baseline = simulate_portfolio(
            market_data, filters, args, start_index=train_end, end_index=test_end
        )
        stress_args = copy.copy(args)
        stress_args.fee_rate = args.fee_rate * 2
        stress_args.slippage = args.slippage * 2
        doubled_cost = simulate_portfolio(
            market_data, filters, stress_args, start_index=train_end, end_index=test_end
        )
        windows.append({
            "development": {
                "start_index": 0, "end_index": train_end,
                "start_time": common_times[0], "end_time": common_times[train_end - 1],
                "metrics": simulate_portfolio(market_data, filters, args, end_index=train_end),
            },
            "validation": {
                "start_index": train_end, "end_index": test_end,
                "start_time": common_times[train_end], "end_time": common_times[test_end - 1],
                "baseline_cost": baseline, "double_cost": doubled_cost,
            },
        })
        train_end += step_bars

    return {
        "enabled": True,
        "method": "固定production_default参数；统一资金池并执行币种、总单位、方向与相关组限仓；开发窗口不用于自动调参或筛选",
        "symbols": symbols,
        "common_bars": len(common_times),
        "train_bars": train_bars, "test_bars": test_bars, "step_bars": step_bars,
        "windows": windows,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="CoinPulse 海龟策略回测")
    parser.add_argument("--capital", type=float, default=CAPITAL)
    parser.add_argument("--fee-rate", type=float, default=FEE_RATE)
    parser.add_argument("--slippage", type=float, default=SLIPPAGE_RATE)
    parser.add_argument("--risk-fraction", type=float, default=sw.TURTLE_RISK_FRACTION)
    parser.add_argument("--bars", type=int, default=TOTAL_BARS)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--refresh-data", action="store_true", help="重新下载并验证历史数据快照")
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
    parser.add_argument("--portfolio-max-strong-group-units", type=int, default=6)
    parser.add_argument("--portfolio-max-weak-group-units", type=int, default=10)
    parser.add_argument("--portfolio-strong-groups", default=json.dumps([sw.DEFAULT_SYMBOLS]))
    parser.add_argument("--portfolio-weak-groups", default="[]")
    parser.add_argument("--rolling-train-bars", type=int, default=2160)
    parser.add_argument("--rolling-test-bars", type=int, default=540)
    parser.add_argument("--rolling-step-bars", type=int, default=540)
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
    try:
        args.portfolio_strong_groups = json.loads(args.portfolio_strong_groups)
        args.portfolio_weak_groups = json.loads(args.portfolio_weak_groups)
        if not all(isinstance(group, list) for group in args.portfolio_strong_groups + args.portfolio_weak_groups):
            raise ValueError("相关组必须是数组的数组")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"组合相关组参数无效：{exc}")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    results = {}
    market_data = {}
    dataset_metadata = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_symbol, symbol, args): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                name, result, data, datasets = future.result()
                results[name] = result
                market_data[name] = data
                dataset_metadata[name] = datasets
                result["rolling_validation"] = rolling_validation(
                    data[0], data[1], backtest_variants()["production_default"], args
                )
                print(f"{name}: 完成")
            except Exception as exc:
                errors[symbol] = str(exc)
                print(f"{symbol}: 失败 - {exc}", file=sys.stderr)

    portfolio = {}
    if market_data:
        filters = backtest_variants()["production_default"]
        common_times = sorted(set.intersection(*(
            {bar["time"] for bar in data[0]} for data in market_data.values()
        )))
        aligned_bars = len(common_times)
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
            "rolling_validation": portfolio_rolling_validation(
                market_data, filters, args
            ),
        }

    report = {
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reproducibility": {
            "report_schema_version": 1,
            "code_revision": os.environ.get("GITHUB_SHA", "local-uncommitted"),
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "interval": INTERVAL,
        "system": SYSTEM,
        "bars_requested": args.bars,
        "data_snapshot": {
            "directory": os.path.relpath(args.data_dir, sw.BASE_DIR),
            "refresh_requested": bool(args.refresh_data),
            "datasets": dataset_metadata,
        },
        "capital": args.capital,
        "fee_rate": args.fee_rate,
        "slippage_rate": args.slippage,
        "risk_fraction": args.risk_fraction,
        "test_ratio": max(0.0, min(0.9, args.test_ratio)),
        "rolling_validation": {
            "variant": "production_default",
            "note": "固定策略参数的逐段样本外验证；同时包含逐币种与统一资金池组合级结果，开发窗口不会用于自动选择参数。",
        },
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
    data_store.save_manifest(
        args.data_dir,
        [
            {"path": os.path.join(args.data_dir, metadata["file"]), "metadata": metadata}
            for symbol, intervals in dataset_metadata.items()
            for interval, metadata in intervals.items()
        ],
    )
    with open(output, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告已写入: {output}")
    return 1 if not results else 0


if __name__ == "__main__":
    raise SystemExit(main())
