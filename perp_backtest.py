#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Research-only backtest for a linear USDT perpetual Turtle strategy.

Input is a validated local snapshot from derivatives_data. No network or
account/order APIs are used. The model is intentionally conservative: entries
fill on the next bar open, exits use mark-price bars, and funding is settled at
its recorded timestamp.
"""

import math

import derivatives_data
import derivatives_risk
import signal_watch as sw


def _bar_map(rows):
    return {int(row["time"]): row for row in rows}


def backtest_perpetual(snapshot, account_value=10000.0, risk_fraction=0.005,
                       leverage=2.0, fee_rate=0.0004, slippage_rate=0.0005,
                       maintenance_margin_rate=0.005,
                       liquidation_fee_rate=0.0, system="system2",
                       filters=None):
    errors = derivatives_data.validate_perpetual_snapshot(
        snapshot, snapshot.get("interval", "4h"),
        max_staleness_intervals=10 ** 9,
    )
    if errors:
        raise ValueError("invalid perpetual snapshot: " + "; ".join(errors))
    if leverage <= 0 or leverage > 20:
        raise ValueError("leverage must be between 0 and 20")
    contract = snapshot["contract_klines"]
    marks = snapshot["mark_price_klines"]
    interval = snapshot["interval"]
    specs = snapshot.get("contract_specs")
    params = sw.turtle_params(system, interval)
    if len(contract) < params["entry_bars"] + 2:
        raise ValueError("insufficient perpetual bars for Turtle strategy")
    mark_map = _bar_map(marks)
    funding = sorted(snapshot.get("funding_rates") or [], key=lambda row: row["time"])
    equity = float(account_value)
    peak = equity
    max_drawdown = 0.0
    position = None
    pending = None
    trades = []
    funding_total = 0.0
    liquidation_count = 0
    constraint_rejections = 0

    def mark_equity(mark_price):
        if not position:
            return equity
        direction = position["direction"]
        pnl = ((mark_price - position["entry"]) if direction == "long"
               else (position["entry"] - mark_price)) * position["quantity"]
        return equity + pnl

    for index in range(params["entry_bars"], len(contract) - 1):
        bar = contract[index]
        next_bar = contract[index + 1]
        bar_time = int(bar["time"])
        entered_this_bar = False
        exit_price = None
        reason = None
        # Pending entries are generated at the prior close and filled next open.
        if pending and position is None and int(bar["time"]) == pending["fill_time"]:
            # The signal is confirmed on the prior bar close.  This bar is
            # therefore the next-bar-open execution window.
            raw_entry = float(bar["open"])
            entry = raw_entry * (1 + slippage_rate if pending["direction"] == "long" else 1 - slippage_rate)
            if specs:
                entry = derivatives_data.quantize_price(
                    entry, specs, "buy" if pending["direction"] == "long" else "sell"
                )
            n = pending["n"]
            quantity = sw.turtle_unit_quantity(equity, n, risk_fraction, 2.0)
            if specs:
                quantity = derivatives_data.quantize_quantity(quantity, specs)
            notional = derivatives_risk.position_notional(entry, quantity)
            margin = derivatives_risk.margin_required(entry, quantity, leverage)
            executable = not specs or derivatives_data.quantity_is_executable(quantity, entry, specs)
            if margin <= equity and executable and quantity > 0:
                entry_fee = notional * fee_rate
                equity -= entry_fee
                position = {"direction": pending["direction"], "entry": entry,
                            "quantity": quantity, "entry_time": int(bar["time"]),
                            "stop": pending["stop"], "exit_level": pending["exit_level"],
                            "funding_index": 0, "fees": entry_fee,
                            "funding": 0.0, "units": 1}
                entered_this_bar = True
            elif specs:
                constraint_rejections += 1
            pending = None

        if position:
            direction = position["direction"]
            mark = mark_map.get(bar_time, bar)
            mark_price = float(mark["close"])
            # Settle every funding event that occurred since the last bar.
            while position["funding_index"] < len(funding) and int(funding[position["funding_index"]]["time"]) <= bar_time:
                event = funding[position["funding_index"]]
                if int(event["time"]) >= position["entry_time"]:
                    funding_mark = event.get("mark_price")
                    try:
                        funding_mark = float(funding_mark) if funding_mark not in (None, "") else mark_price
                    except (TypeError, ValueError):
                        funding_mark = mark_price
                    cashflow = derivatives_risk.funding_payment(
                        derivatives_risk.position_notional(funding_mark, position["quantity"]),
                        event["funding_rate"], direction,
                    )
                    equity += cashflow
                    funding_total += cashflow
                    position["funding"] += cashflow
                position["funding_index"] += 1
            if not entered_this_bar:
                liq = derivatives_risk.liquidation_price(
                    position["entry"], direction, leverage,
                    maintenance_margin_rate, liquidation_fee_rate,
                )
                levels = sw.turtle_levels(contract, index, system, interval)
                exit_level = (
                    levels["exit_low"] if direction == "long" else levels["exit_high"]
                ) if levels else position["exit_level"]
                high, low = float(mark["high"]), float(mark["low"])
                exit_price = None
                reason = None
                if direction == "long":
                    if low <= liq:
                        exit_price, reason = liq, "liquidation"
                    elif low <= position["stop"]:
                        exit_price, reason = position["stop"], "stop"
                    elif exit_level is not None and low <= exit_level:
                        exit_price, reason = exit_level, "channel_exit"
                else:
                    if high >= liq:
                        exit_price, reason = liq, "liquidation"
                    elif high >= position["stop"]:
                        exit_price, reason = position["stop"], "stop"
                    elif exit_level is not None and high >= exit_level:
                        exit_price, reason = exit_level, "channel_exit"
            if exit_price is not None:
                raw_exit = float(exit_price)
                fill = raw_exit * (1 - slippage_rate if direction == "long" else 1 + slippage_rate)
                if specs:
                    fill = derivatives_data.quantize_price(
                        fill, specs, "sell" if direction == "long" else "buy"
                    )
                gross = ((fill - position["entry"]) if direction == "long" else (position["entry"] - fill)) * position["quantity"]
                exit_fee = abs(fill * position["quantity"]) * fee_rate
                equity += gross - exit_fee
                position["fees"] += exit_fee
                if reason == "liquidation":
                    liquidation_count += 1
                trades.append({"direction": direction, "entry_time": position["entry_time"],
                               "exit_time": bar_time, "entry": position["entry"],
                               "exit": fill, "reason": reason,
                               "gross_pnl": gross, "fees": position["fees"],
                               "funding": position["funding"],
                               "net_pnl": gross - position["fees"] + position["funding"],
                               "return_pct": (gross - position["fees"] + position["funding"]) / account_value * 100})
                position = None

        if position is None and pending is None:
            direction, _reasons, plan = sw.build_turtle_signal(
                contract[: index + 1], system, account_value, risk_fraction,
                interval, filter_options=filters or {
                    "higher_timeframe": False, "volume_confirmation": False,
                    "volatility_filter": False, "anomaly_filter": False,
                },
            )
            if direction and plan:
                pending = {"direction": direction, "n": plan["n"],
                           "stop": plan["stop"], "exit_level": plan["exit_level"],
                           "fill_time": int(next_bar["time"])}
        marked = mark_equity(float(mark_map.get(bar_time, bar)["close"]))
        peak = max(peak, marked)
        max_drawdown = max(max_drawdown, (peak - marked) / peak if peak else 0)

    if position:
        last = marks[-1]
        fill = float(last["close"]) * (1 - slippage_rate if position["direction"] == "long" else 1 + slippage_rate)
        if specs:
            fill = derivatives_data.quantize_price(
                fill, specs, "sell" if position["direction"] == "long" else "buy"
            )
        gross = ((fill - position["entry"]) if position["direction"] == "long" else (position["entry"] - fill)) * position["quantity"]
        exit_fee = abs(fill * position["quantity"]) * fee_rate
        equity += gross - exit_fee
        trades.append({"direction": position["direction"], "entry_time": position["entry_time"],
                       "exit_time": int(marks[-1]["time"]), "entry": position["entry"],
                       "exit": fill, "reason": "end_of_test", "gross_pnl": gross,
                       "fees": position["fees"] + exit_fee, "funding": position["funding"],
                       "net_pnl": gross - position["fees"] - exit_fee + position["funding"],
                       "return_pct": (gross - position["fees"] - exit_fee + position["funding"]) / account_value * 100})
    return {
        "market_type": "linear_perpetual", "strategy": "perp_trend_turtle",
        "initial_equity": float(account_value), "ending_equity": round(equity, 8),
        "return_pct": round((equity / account_value - 1) * 100, 4),
        "max_drawdown_pct": round(max_drawdown * 100, 4),
        "trades": trades, "trade_count": len(trades),
        "funding_cashflow": round(funding_total, 8),
        "liquidation_count": liquidation_count,
        "constraint_rejections": constraint_rejections,
        "contract_constraints_bound": bool(specs),
        "fee_rate": fee_rate, "slippage_rate": slippage_rate, "leverage": leverage,
    }


def run_cost_stress_tests(snapshot, account_value=10000.0, risk_fraction=0.005,
                          leverage=2.0, fee_rate=0.0004, slippage_rate=0.0005,
                          maintenance_margin_rate=0.005,
                          liquidation_fee_rate=0.0, system="system2",
                          filters=None):
    """Run baseline, 2x and 4x execution-cost scenarios.

    Funding observations are kept unchanged; only explicit trading costs are
    scaled. This makes sensitivity to fees/slippage visible without silently
    altering the market data or strategy rules.
    """
    scenarios = {}
    for label, multiplier in (("baseline", 1), ("double_cost", 2), ("quadruple_cost", 4)):
        result = backtest_perpetual(
            snapshot, account_value=account_value, risk_fraction=risk_fraction,
            leverage=leverage, fee_rate=fee_rate * multiplier,
            slippage_rate=slippage_rate * multiplier,
            maintenance_margin_rate=maintenance_margin_rate,
            liquidation_fee_rate=liquidation_fee_rate, system=system,
            filters=filters,
        )
        scenarios[label] = result
    baseline = scenarios["baseline"]
    scenarios["summary"] = {
        "market_type": "linear_perpetual",
        "strategy": "perp_trend_turtle",
        "baseline_return_pct": baseline["return_pct"],
        "double_cost_return_pct": scenarios["double_cost"]["return_pct"],
        "quadruple_cost_return_pct": scenarios["quadruple_cost"]["return_pct"],
        "return_decay_double_pct": round(
            scenarios["double_cost"]["return_pct"] - baseline["return_pct"], 4
        ),
        "return_decay_quadruple_pct": round(
            scenarios["quadruple_cost"]["return_pct"] - baseline["return_pct"], 4
        ),
    }
    return scenarios


def funding_flip_snapshot(snapshot):
    """Return a copy with funding signs flipped for adverse funding stress."""
    flipped = dict(snapshot)
    flipped["funding_rates"] = [
        {**event, "funding_rate": -float(event.get("funding_rate") or 0)}
        for event in (snapshot.get("funding_rates") or [])
    ]
    return flipped


def run_funding_flip_stress(snapshot, **kwargs):
    """Compare recorded funding with a sign-flipped adverse funding scenario."""
    normal = backtest_perpetual(snapshot, **kwargs)
    flipped = backtest_perpetual(funding_flip_snapshot(snapshot), **kwargs)
    return {
        "normal": normal,
        "funding_flipped": flipped,
        "return_delta_pct": round(flipped["return_pct"] - normal["return_pct"], 4),
        "funding_delta": round(flipped["funding_cashflow"] - normal["funding_cashflow"], 8),
    }
