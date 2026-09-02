#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CoinPulse background signal watcher with phone push support."""

import json
import logging
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "signal_watch.config.json")
STATE_PATH = os.path.join(BASE_DIR, "signal_watch.state.json")
LOG_PATH = os.path.join(BASE_DIR, "signal_watch.log")

DEFAULT_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "TONUSDT",
    "SUIUSDT",
    "PEPEUSDT"
]

INTERVAL_BARS = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}
INTERVAL_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000, "1d": 24 * 60 * 60 * 1000}
TURTLE_N_PERIOD = 20
TURTLE_RISK_FRACTION = 0.01
TURTLE_MAX_UNITS = 4
TURTLE_FILTER_DEFAULTS = {
    "closed_candles_only": True,
    "higher_timeframe": True,
    "higher_ema_period": 200,
    "breakout_buffer_n": 0.1,
    "require_higher_timeframe": True,
    # 这些确认层默认开启，但每项都可以单独关闭，便于回测和实盘对比。
    "adx_enabled": False,
    "adx_period": 14,
    "adx_min": 20.0,
    "volume_confirmation": True,
    "volume_period": 20,
    "volume_min_ratio": 1.0,
    "volatility_filter": True,
    "min_atr_pct": 0.002,
    "max_atr_pct": 0.12,
}


def filter_closed_klines(klines, interval, now_ms=None):
    """只保留已经结束的K线，避免使用当前仍在形成的K线。"""
    interval_ms = INTERVAL_MS.get(interval)
    if not interval_ms:
        return klines
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return [k for k in klines if int(k["time"]) + interval_ms <= current_ms]


def higher_timeframe(interval):
    return {"15m": "1h", "1h": "4h", "4h": "1d"}.get(interval)


def turtle_filter_options(strategy_config=None):
    raw = (strategy_config or {}).get("filters") or {}
    options = dict(TURTLE_FILTER_DEFAULTS)
    options["closed_candles_only"] = bool(raw.get("closed_candles_only", options["closed_candles_only"]))
    options["higher_timeframe"] = bool(raw.get("higher_timeframe", options["higher_timeframe"]))
    options["require_higher_timeframe"] = bool(raw.get("require_higher_timeframe", options["require_higher_timeframe"]))
    try:
        options["higher_ema_period"] = max(20, int(raw.get("higher_ema_period", options["higher_ema_period"])))
    except (TypeError, ValueError):
        options["higher_ema_period"] = TURTLE_FILTER_DEFAULTS["higher_ema_period"]
    try:
        options["breakout_buffer_n"] = max(0.0, float(raw.get("breakout_buffer_n", options["breakout_buffer_n"])))
    except (TypeError, ValueError):
        options["breakout_buffer_n"] = TURTLE_FILTER_DEFAULTS["breakout_buffer_n"]
    options["adx_enabled"] = bool(raw.get("adx_enabled", options["adx_enabled"]))
    try:
        options["adx_period"] = max(5, int(raw.get("adx_period", options["adx_period"])))
    except (TypeError, ValueError):
        options["adx_period"] = TURTLE_FILTER_DEFAULTS["adx_period"]
    try:
        options["adx_min"] = max(0.0, float(raw.get("adx_min", options["adx_min"])))
    except (TypeError, ValueError):
        options["adx_min"] = TURTLE_FILTER_DEFAULTS["adx_min"]
    options["volume_confirmation"] = bool(raw.get("volume_confirmation", options["volume_confirmation"]))
    try:
        options["volume_period"] = max(5, int(raw.get("volume_period", options["volume_period"])))
    except (TypeError, ValueError):
        options["volume_period"] = TURTLE_FILTER_DEFAULTS["volume_period"]
    try:
        options["volume_min_ratio"] = max(0.0, float(raw.get("volume_min_ratio", options["volume_min_ratio"])))
    except (TypeError, ValueError):
        options["volume_min_ratio"] = TURTLE_FILTER_DEFAULTS["volume_min_ratio"]
    options["volatility_filter"] = bool(raw.get("volatility_filter", options["volatility_filter"]))
    try:
        options["min_atr_pct"] = max(0.0, float(raw.get("min_atr_pct", options["min_atr_pct"])))
    except (TypeError, ValueError):
        options["min_atr_pct"] = TURTLE_FILTER_DEFAULTS["min_atr_pct"]
    try:
        options["max_atr_pct"] = max(options["min_atr_pct"], float(raw.get("max_atr_pct", options["max_atr_pct"])))
    except (TypeError, ValueError):
        options["max_atr_pct"] = TURTLE_FILTER_DEFAULTS["max_atr_pct"]
    return options


def http_get_json(url, timeout=5):
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "CoinPulse/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_binance_data(symbol, interval):
    url = (
        "https://data-api.binance.vision/api/v3/klines?"
        f"symbol={urllib.parse.quote(symbol)}&interval={interval}&limit=320"
    )
    rows = http_get_json(url)
    return [
        {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]


def fetch_binance_api(symbol, interval):
    url = (
        "https://api.binance.com/api/v3/klines?"
        f"symbol={urllib.parse.quote(symbol)}&interval={interval}&limit=320"
    )
    rows = http_get_json(url)
    return [
        {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]


def fetch_bybit(symbol, interval):
    interval_map = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}
    url = (
        "https://api.bybit.com/v5/market/kline?"
        f"category=spot&symbol={urllib.parse.quote(symbol)}"
        f"&interval={interval_map[interval]}&limit=320"
    )
    data = http_get_json(url)
    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg") or "Bybit error")
    rows = data.get("result", {}).get("list") or []
    result = [
        {
            "time": int(row[0]) * 1000,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]
    result.reverse()
    return result


def fetch_okx(symbol, interval):
    interval_map = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}
    inst_id = symbol.replace("USDT", "-USDT")
    url = (
        "https://www.okx.com/api/v5/market/candles?"
        f"instId={urllib.parse.quote(inst_id)}"
        f"&bar={interval_map[interval]}&limit=320"
    )
    data = http_get_json(url)
    if data.get("code") != "0":
        raise RuntimeError(data.get("msg") or "OKX error")
    rows = data.get("data") or []
    result = [
        {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]
    result.reverse()
    return result


PROVIDERS = [
    ("okx", fetch_okx),
    ("binance-data", fetch_binance_data),
    ("binance-api", fetch_binance_api),
    ("bybit", fetch_bybit)
]


def fetch_klines_with_fallback(symbol, interval, closed_only=True):
    errors = []
    for name, fetcher in PROVIDERS:
        try:
            klines = fetcher(symbol, interval)
            if closed_only:
                klines = filter_closed_klines(klines, interval)
            if isinstance(klines, list) and len(klines) > 50:
                return klines, name
            raise RuntimeError("数据不足")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(" / ".join(errors))


def fetch_binance_history(symbol, interval, total):
    """分页拉取海龟所需的历史长度（4h S2 需要超过默认320根）。"""
    rows = []
    cursor = int(time.time() * 1000)
    page_size = min(1000, total)
    while len(rows) < total and cursor > 0:
        url = (
            "https://data-api.binance.vision/api/v3/klines?"
            f"symbol={urllib.parse.quote(symbol)}&interval={interval}"
            f"&limit={page_size}&endTime={cursor}"
        )
        page = http_get_json(url)
        if not isinstance(page, list) or not page:
            break
        rows = page + rows
        if len(page) < page_size:
            break
        cursor = int(page[0][0]) - 1
    parsed = [
        {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]
    deduped = []
    seen = set()
    for kline in sorted(parsed, key=lambda item: item["time"]):
        if kline["time"] not in seen:
            seen.add(kline["time"])
            deduped.append(kline)
    return deduped[-total:]


def turtle_required_bars(interval, system="system2"):
    params = turtle_params(system, interval)
    return max(params["entry_bars"], params["n_period"]) + 5


def fetch_klines_for_strategy(symbol, interval, system="system2", closed_only=True):
    required = turtle_required_bars(interval, system)
    if required <= 320:
        return fetch_klines_with_fallback(symbol, interval, closed_only)
    try:
        klines = filter_closed_klines(
            fetch_binance_history(symbol, interval, required + 1), interval
        )
        if len(klines) >= required:
            return klines[-required:], "binance-history"
    except Exception as exc:
        logging.warning("%s %s 海龟历史数据分页失败: %s", symbol, interval, exc)
    klines, provider = fetch_klines_with_fallback(symbol, interval, closed_only)
    if len(klines) < required:
        raise RuntimeError(f"海龟{system}需要至少{required}根K线，当前仅{len(klines)}根")
    return klines, provider


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    total = sum(values[:period])
    prev = total / period
    result[period - 1] = prev
    multiplier = 2 / (period + 1)
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        result[i] = prev
    return result


def calc_rsi(closes, period=14):
    rsi = [None] * len(closes)
    if len(closes) <= period:
        return rsi
    gain_sum = 0.0
    loss_sum = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gain_sum += max(change, 0)
        loss_sum += max(-change, 0)
    avg_gain = gain_sum / period
    avg_loss = loss_sum / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi


def calc_kdj(klines, period=9):
    length = len(klines)
    k_values = [None] * length
    d_values = [None] * length
    j_values = [None] * length
    prev_k = 50.0
    prev_d = 50.0
    for i in range(length):
        frm = max(0, i - period + 1)
        high = max(kline["high"] for kline in klines[frm:i + 1])
        low = min(kline["low"] for kline in klines[frm:i + 1])
        rsv = 50.0 if high == low else ((klines[i]["close"] - low) / (high - low)) * 100
        prev_k = (2 / 3) * prev_k + (1 / 3) * rsv
        prev_d = (2 / 3) * prev_d + (1 / 3) * prev_k
        k_values[i] = prev_k
        d_values[i] = prev_d
        j_values[i] = 3 * prev_k - 2 * prev_d
    return {"K": k_values, "D": d_values, "J": j_values}


def calc_atr(klines, period=14):
    if len(klines) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(klines)):
        prev_close = klines[i - 1]["close"]
        true_ranges.append(max(
            klines[i]["high"] - klines[i]["low"],
            abs(klines[i]["high"] - prev_close),
            abs(klines[i]["low"] - prev_close)
        ))
    tail = true_ranges[-period:]
    return sum(tail) / len(tail)


def calc_atr_slice(klines, idx, period=14):
    """计算到 idx 为止的 ATR（回测用，避免未来数据）"""
    if idx < period + 1:
        return None
    true_ranges = []
    for i in range(idx - period + 1, idx + 1):
        prev_close = klines[i - 1]["close"]
        true_ranges.append(max(
            klines[i]["high"] - klines[i]["low"],
            abs(klines[i]["high"] - prev_close),
            abs(klines[i]["low"] - prev_close)
        ))
    return sum(true_ranges) / len(true_ranges)


def calc_n_series(klines, period=TURTLE_N_PERIOD):
    """海龟 N：20周期真实波幅的 Wilder 平滑值。"""
    values = [None] * len(klines)
    if len(klines) <= period:
        return values
    ranges = []
    for i in range(1, len(klines)):
        prev_close = klines[i - 1]["close"]
        ranges.append(max(
            klines[i]["high"] - klines[i]["low"],
            abs(klines[i]["high"] - prev_close),
            abs(klines[i]["low"] - prev_close)
        ))
    current = sum(ranges[:period]) / period
    values[period] = current
    for i in range(period + 1, len(klines)):
        current = (current * (period - 1) + ranges[i - 1]) / period
        values[i] = current
    return values


def turtle_params(system, interval="1d"):
    bars_per_day = INTERVAL_BARS.get(interval, 1)
    if system == "system1":
        return {
            "key": "system1", "label": "海龟S1 20/10", "entry_days": 20, "exit_days": 10,
            "entry_bars": 20 * bars_per_day, "exit_bars": 10 * bars_per_day,
            "n_period": 20 * bars_per_day, "skip_winning_breakout": True
        }
    return {
        "key": "system2", "label": "海龟S2 55/20", "entry_days": 55, "exit_days": 20,
        "entry_bars": 55 * bars_per_day, "exit_bars": 20 * bars_per_day,
        "n_period": 20 * bars_per_day, "skip_winning_breakout": False
    }


def turtle_levels(klines, idx, system="system2", interval="1d"):
    params = turtle_params(system, interval)
    if idx < params["entry_bars"] or idx < params["exit_bars"]:
        return None
    entry_window = klines[idx - params["entry_bars"]:idx]
    exit_window = klines[idx - params["exit_bars"]:idx]
    return {
        **params,
        "entry_high": max(k["high"] for k in entry_window),
        "entry_low": min(k["low"] for k in entry_window),
        "exit_high": max(k["high"] for k in exit_window),
        "exit_low": min(k["low"] for k in exit_window),
    }


def higher_timeframe_trend(klines, period=200):
    """返回高周期方向：long/short/None。"""
    closes = [k["close"] for k in klines]
    series = ema(closes, period)
    if not series or series[-1] is None:
        return None
    if closes[-1] > series[-1]:
        return "long"
    if closes[-1] < series[-1]:
        return "short"
    return None


def turtle_confirmation_filters(klines, idx, direction, n, filters):
    """突破后的低成本确认层；只使用 idx 及之前的已完成K线。"""
    reasons = []
    metrics = {}

    if filters.get("adx_enabled", True):
        period = int(filters.get("adx_period", 14))
        adx_data = calc_adx(klines[:idx + 1], period)
        adx = adx_data["adx"][-1]
        plus_di = adx_data["plusDi"][-1]
        minus_di = adx_data["minusDi"][-1]
        metrics.update({"adx": adx, "plusDi": plus_di, "minusDi": minus_di})
        if adx is None or plus_di is None or minus_di is None:
            reasons.append("ADX数据不足")
        elif adx < float(filters.get("adx_min", 20.0)):
            reasons.append(f"ADX{adx:.1f}低于{float(filters.get('adx_min', 20.0)):.0f}")
        elif (direction == "long" and plus_di <= minus_di) or (direction == "short" and minus_di <= plus_di):
            reasons.append("ADX方向未与突破一致")

    if filters.get("volume_confirmation", True):
        period = int(filters.get("volume_period", 20))
        if idx < period:
            reasons.append("成交量历史不足")
        else:
            current_volume = float(klines[idx].get("volume") or 0)
            previous_volumes = [float(k.get("volume") or 0) for k in klines[idx - period:idx]]
            average_volume = sum(previous_volumes) / period if previous_volumes else 0
            ratio = current_volume / average_volume if average_volume > 0 else 0
            metrics["volumeRatio"] = ratio
            minimum = float(filters.get("volume_min_ratio", 1.0))
            if ratio < minimum:
                reasons.append(f"成交量仅为均量{ratio:.2f}倍")

    if filters.get("volatility_filter", True):
        close = float(klines[idx].get("close") or 0)
        atr_pct = n / close if close > 0 else 0
        metrics["atrPct"] = atr_pct
        minimum = float(filters.get("min_atr_pct", 0.002))
        maximum = float(filters.get("max_atr_pct", 0.12))
        if atr_pct < minimum:
            reasons.append(f"波动率过低（ATR仅{atr_pct * 100:.2f}%）")
        elif atr_pct > maximum:
            reasons.append(f"波动率过高（ATR达{atr_pct * 100:.2f}%）")

    return reasons, metrics


def build_turtle_signal(
    klines, system="system2", account_value=10000,
    risk_fraction=TURTLE_RISK_FRACTION, interval="1d", system1_blocked=False,
    filter_options=None, higher_trend=None
):
    idx = len(klines) - 1
    params = turtle_params(system, interval)
    filters = dict(TURTLE_FILTER_DEFAULTS)
    filters.update(filter_options or {})
    levels = turtle_levels(klines, idx, system, interval)
    n = calc_n_series(klines, params["n_period"])[idx]
    if levels is None or n is None or n <= 0:
        return None, [], None
    price = klines[idx]["close"]
    breakout_buffer = float(filters.get("breakout_buffer_n", 0.0)) * n
    if price > levels["entry_high"] + breakout_buffer:
        direction = "long"
        entry = levels["entry_high"]
        stop = entry - 2 * n
        next_add = entry + 0.5 * n
        exit_level = levels["exit_low"]
    elif price < levels["entry_low"] - breakout_buffer:
        direction = "short"
        entry = levels["entry_low"]
        stop = entry + 2 * n
        next_add = entry - 0.5 * n
        exit_level = levels["exit_high"]
    else:
        return None, [], {
            "levels": levels, "n": n, "price": price,
            "wait": f"上破 {format_price(levels['entry_high'])} 做多 / 下破 {format_price(levels['entry_low'])} 做空"
        }
    if system == "system1" and system1_blocked:
        return None, ["S1跳过上一次盈利突破"], {
            "levels": levels, "n": n, "price": price, "blocked": True
        }
    if filters.get("higher_timeframe", True) and higher_timeframe(interval):
        if higher_trend is None and filters.get("require_higher_timeframe", True):
            return None, ["高周期EMA趋势不可用，暂不入场"], {
                "levels": levels, "n": n, "price": price, "filtered": True,
                "filter_reason": "高周期EMA趋势不可用，暂不入场"
            }
        if higher_trend in ("long", "short") and higher_trend != direction:
            trend_label = "多头" if higher_trend == "long" else "空头"
            reason = f"高周期EMA{int(filters.get('higher_ema_period', 200))}为{trend_label}，过滤反向突破"
            return None, [reason], {
                "levels": levels, "n": n, "price": price, "filtered": True,
                "filter_reason": reason
            }
    confirmation_reasons, confirmation_metrics = turtle_confirmation_filters(
        klines, idx, direction, n, filters
    )
    if confirmation_reasons:
        reason = "；".join(confirmation_reasons)
        return None, confirmation_reasons, {
            "levels": levels, "n": n, "price": price, "filtered": True,
            "filter_reason": reason, "filter_metrics": confirmation_metrics
        }
    unit_quantity = account_value * risk_fraction / n
    plan = {
        "system": system,
        "entry": entry,
        "stop": stop,
        "next_add": next_add,
        "exit_level": exit_level,
        "n": n,
        "unit_quantity": unit_quantity,
        "max_quantity": unit_quantity * TURTLE_MAX_UNITS,
        "exit_days": levels["exit_days"],
    }
    reason = [f"{levels['label']}{'向上' if direction == 'long' else '向下'}突破", f"N={format_price(n)}"]
    return direction, reason, plan


def build_turtle_strategy_text(direction, plan):
    side = "做多" if direction == "long" else "做空"
    max_units = int(plan.get("max_units") or TURTLE_MAX_UNITS)
    return (
        f"{plan['system']} {side}，入场点位 ≈ {format_price(plan['entry'])}\n"
        f"止损 {format_price(plan['stop'])}（2N）\n"
        f"每 0.5N 至 {format_price(plan['next_add'])} 加 1 单位，"
        f"首单位 {format_price(plan['unit_quantity'])}，最多{max_units}单位；"
        f"{plan['exit_days']}周期反向突破 {format_price(plan['exit_level'])} 全平"
    )


def turtle_unit_capacity(symbol, direction, state, config):
    """按单市场、相关组和单方向上限计算本次至少可开的单位数。"""
    strategy = config.get("strategy") or {}
    limits = strategy.get("limits") or {}
    max_symbol = int(limits.get("max_symbol_units", TURTLE_MAX_UNITS))
    max_strong = int(limits.get("max_strong_group_units", 6))
    max_weak = int(limits.get("max_weak_group_units", 10))
    max_direction = int(limits.get("max_direction_units", 12))
    open_trades = [
        t for t in state.get("open_trades", [])
        if t.get("strategy_type") == "turtle" and t.get("direction") == direction
    ]
    symbol_units = sum(int(t.get("units") or 1) for t in open_trades if t.get("symbol") == symbol)
    direction_units = sum(int(t.get("units") or 1) for t in open_trades)
    correlation = strategy.get("correlation") or {}
    strong_groups = correlation.get("strong_groups") or [DEFAULT_SYMBOLS]
    weak_groups = correlation.get("weak_groups") or []

    def find_group(groups):
        for group in groups:
            if symbol in group:
                return set(group)
        return set()

    strong_group = find_group(strong_groups)
    weak_group = find_group(weak_groups) if not strong_group else set()
    group_units = sum(
        int(t.get("units") or 1)
        for t in open_trades
        if t.get("symbol") in (strong_group or weak_group)
    )
    caps = [max_symbol - symbol_units, max_direction - direction_units]
    if strong_group:
        caps.append(max_strong - group_units)
    elif weak_group:
        caps.append(max_weak - group_units)
    return max(0, min(caps))


def compute_bollinger(klines, period=20, mult=2.0):
    """布林带（percentB），与看板一致"""
    closes = [k["close"] for k in klines]
    length = len(closes)
    upper = [None] * length
    middle = [None] * length
    lower = [None] * length
    percentB = [None] * length
    for i in range(period - 1, length):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        middle[i] = mean
        upper[i] = mean + mult * std
        lower[i] = mean - mult * std
        if upper[i] - lower[i] > 0:
            percentB[i] = (closes[i] - lower[i]) / (upper[i] - lower[i])
        else:
            percentB[i] = 0.5
    return {"upper": upper, "middle": middle, "lower": lower, "percentB": percentB}


def calc_adx(klines, period=14):
    length = len(klines)
    adx = [None] * length
    plus_di = [None] * length
    minus_di = [None] * length
    if length <= period:
        return {"adx": adx, "plusDi": plus_di, "minusDi": minus_di}

    trs = [0.0] * length
    plus_dms = [0.0] * length
    minus_dms = [0.0] * length
    for i in range(1, length):
        high = klines[i]["high"]
        low = klines[i]["low"]
        prev_high = klines[i - 1]["high"]
        prev_low = klines[i - 1]["low"]
        prev_close = klines[i - 1]["close"]
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dms[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dms[i] = down_move if down_move > up_move and down_move > 0 else 0.0
        trs[i] = max(high - low, abs(high - prev_close), abs(low - prev_close))

    tr_sum = sum(trs[1:period + 1])
    plus_sum = sum(plus_dms[1:period + 1])
    minus_sum = sum(minus_dms[1:period + 1])
    prev_tr = tr_sum / period
    prev_plus = plus_sum / period
    prev_minus = minus_sum / period
    plus_di[period] = (prev_plus / prev_tr) * 100 if prev_tr > 0 else 0.0
    minus_di[period] = (prev_minus / prev_tr) * 100 if prev_tr > 0 else 0.0
    first_sum = prev_plus + prev_minus
    prev_dx = (abs(prev_plus - prev_minus) / first_sum) * 100 if first_sum > 0 else 0.0
    adx[period] = prev_dx

    for i in range(period + 1, length):
        prev_tr = (prev_tr * (period - 1) + trs[i]) / period
        prev_plus = (prev_plus * (period - 1) + plus_dms[i]) / period
        prev_minus = (prev_minus * (period - 1) + minus_dms[i]) / period
        plus_di[i] = (prev_plus / prev_tr) * 100 if prev_tr > 0 else 0.0
        minus_di[i] = (prev_minus / prev_tr) * 100 if prev_tr > 0 else 0.0
        total_di = prev_plus + prev_minus
        dx = (abs(prev_plus - prev_minus) / total_di) * 100 if total_di > 0 else 0.0
        prev_dx = (prev_dx * (period - 1) + dx) / period
        adx[i] = prev_dx

    return {"adx": adx, "plusDi": plus_di, "minusDi": minus_di}


def calc_stoch_rsi(closes, rsi_period=14, stoch_period=14):
    rsi = calc_rsi(closes, rsi_period)
    length = len(closes)
    k_values = [None] * length
    d_values = [None] * length
    first = rsi_period + stoch_period - 1
    for i in range(first, length):
        window = [
            rsi[q] for q in range(i - stoch_period + 1, i + 1)
            if rsi[q] is not None
        ]
        if window and max(window) > min(window):
            k_values[i] = (rsi[i] - min(window)) / (max(window) - min(window)) * 100
        else:
            k_values[i] = 50.0
    for i in range(length):
        if k_values[i] is None:
            continue
        window = [
            k_values[q] for q in range(max(0, i - 2), i + 1)
            if k_values[q] is not None
        ]
        if len(window) >= 3:
            d_values[i] = sum(window) / len(window)
    return {"K": k_values, "D": d_values}


def compute_indicators(klines):
    closes = [kline["close"] for kline in klines]
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    dif = [None] * len(closes)
    dea = [None] * len(closes)
    hist = [None] * len(closes)
    for i in range(25, len(closes)):
        dif[i] = ema12[i] - ema26[i]
    dea_slice = ema(dif[25:], 9)
    for i, value in enumerate(dea_slice):
        index = 25 + i
        if value is not None:
            dea[index] = value
            hist[index] = (dif[index] - value) * 2
    return {
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "ema100": ema(closes, 100),
        "ema200": ema(closes, 200),
        "macd": {"dif": dif, "dea": dea, "hist": hist},
        "rsi": calc_rsi(closes, 14),
        "kdj": calc_kdj(klines, 9),
        "adx": calc_adx(klines, 14),
        "stochRsi": calc_stoch_rsi(closes, 14, 14)
    }


def round_half_up(value):
    return math.floor(value * 10 + 0.5) / 10


def analyze_indicators(klines, indicators):
    macd = indicators["macd"]
    kdj = indicators["kdj"]
    rsi = indicators["rsi"]
    last = len(klines) - 1
    prev = max(0, last - 1)
    dif = macd["dif"][last]
    dea = macd["dea"][last]
    hist = macd["hist"][last]
    k = kdj["K"][last]
    d = kdj["D"][last]
    j = kdj["J"][last]
    rsi_cur = rsi[last] if rsi[last] is not None else 50
    rsi_prev = rsi[prev] if prev > 0 and rsi[prev] is not None else rsi_cur
    has_prev = prev > 0 and macd["dif"][prev] is not None and macd["dea"][prev] is not None

    score = 0.0
    reasons = []

    if has_prev:
        if macd["dif"][prev] <= macd["dea"][prev] and dif > dea:
            score += 2.5
            reasons.append("MACD金叉")
        elif macd["dif"][prev] >= macd["dea"][prev] and dif < dea:
            score -= 2.5
            reasons.append("MACD死叉")

        if kdj["K"][prev] <= kdj["D"][prev] and k > d:
            score += 1.5
            reasons.append("KDJ金叉")
        elif kdj["K"][prev] >= kdj["D"][prev] and k < d:
            score -= 1.5
            reasons.append("KDJ死叉")

        score += 0.5 if hist > macd["hist"][prev] else -0.5

        if rsi_prev < 50 and rsi_cur >= 50:
            score += 1
            reasons.append("RSI上穿50")
        elif rsi_prev > 50 and rsi_cur <= 50:
            score -= 1
            reasons.append("RSI下穿50")

    score += 1 if dif > dea else -1
    score += 0.5 if k > d else -0.5
    score += 0.5 if rsi_cur > 50 else -0.5

    if j < 20:
        score += 1
        reasons.append("KDJ超卖")
    elif j > 80:
        score -= 1
        reasons.append("KDJ超买")

    if rsi_cur < 30:
        score += 1
        reasons.append("RSI超卖")
    elif rsi_cur > 70:
        score -= 1
        reasons.append("RSI超买")

    stoch = indicators.get("stochRsi")
    st_k = stoch["K"][last] if stoch else None
    st_d = stoch["D"][last] if stoch else None
    st_k_prev = stoch["K"][prev] if stoch and prev > 0 else None
    st_d_prev = stoch["D"][prev] if stoch and prev > 0 else None
    if st_k is not None and st_k_prev is not None and st_d_prev is not None:
        if st_k_prev <= st_d_prev and st_k > st_d:
            score += 1
            reasons.append("StochRSI金叉")
        elif st_k_prev >= st_d_prev and st_k < st_d:
            score -= 1
            reasons.append("StochRSI死叉")
    if st_k is not None:
        if st_k < 20:
            score += 1
            reasons.append("StochRSI超卖")
        elif st_k > 80:
            score -= 1
            reasons.append("StochRSI超买")

    adx_data = indicators.get("adx")
    adx_cur = adx_data["adx"][last] if adx_data else None
    plus_di = adx_data["plusDi"][last] if adx_data else None
    minus_di = adx_data["minusDi"][last] if adx_data else None
    if adx_cur is not None and plus_di is not None and minus_di is not None and adx_cur >= 25:
        if plus_di > minus_di:
            score += 1
            reasons.append("ADX多头趋势")
        elif minus_di > plus_di:
            score -= 1
            reasons.append("ADX空头趋势")

    rounded = round_half_up(score)
    if rounded >= 4:
        label = "强烈看多"
    elif rounded >= 2:
        label = "偏多"
    elif rounded <= -4:
        label = "强烈看空"
    elif rounded <= -2:
        label = "偏空"
    else:
        label = "震荡观望"
    signal_class = "bull" if rounded >= 2 else "bear" if rounded <= -2 else "flat"
    unique_reasons = list(dict.fromkeys(reasons))[:4]
    reason = " · ".join(unique_reasons) if unique_reasons else "指标信号暂未共振"

    return {
        "score": rounded,
        "label": label,
        "signalClass": signal_class,
        "reason": reason
    }


def get_change(klines, interval):
    bars = INTERVAL_BARS.get(interval, 24)
    count = min(bars, len(klines) - 1)
    prev_close = klines[len(klines) - 1 - count]["close"] if count else klines[0]["close"]
    last_close = klines[-1]["close"]
    return (last_close / prev_close - 1) * 100


def format_price(value):
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.6f}"


def build_strategy_text(klines, indicators, analysis, interval):
    if analysis["signalClass"] == "flat":
        return "等待 MACD/KDJ 金叉或死叉共振、RSI 明确穿越 50 后再入场"

    last = klines[-1]
    look = klines[-INTERVAL_BARS.get(interval, 24):] or [last]
    recent_high = max(kline["high"] for kline in look)
    recent_low = min(kline["low"] for kline in look)
    atr = calc_atr(klines, 14) or last["close"] * 0.008
    ema20 = indicators["ema20"][-1]
    ema50 = indicators["ema50"][-1]
    ema100 = indicators["ema100"][-1]
    ema200 = indicators["ema200"][-1]
    is_bull = analysis["signalClass"] == "bull"
    has_all_ema = all(value is not None for value in (ema20, ema50, ema100, ema200))
    bull_align = has_all_ema and ema20 > ema50 > ema100 > ema200
    bear_align = has_all_ema and ema20 < ema50 < ema100 < ema200
    entry_ref = ema20 if ema20 is not None else last["close"]
    direction_risk = (
        max(0.0, last["close"] - recent_low)
        if is_bull
        else max(0.0, recent_high - last["close"])
    )
    risk = max(atr * 1.5, direction_risk * 0.6)
    stop = last["close"] - risk if is_bull else last["close"] + risk
    target = last["close"] + risk * 2 if is_bull else last["close"] - risk * 2
    action = "回踩" if is_bull else "反弹"
    stop_verb = "跌破" if is_bull else "突破"
    if is_bull and bull_align:
        return (
            f"均线多头排列（EMA20>50>100>200），{action} EMA20 {format_price(entry_ref)} 附近分批，"
            f"{stop_verb}止损 {format_price(stop)} 离场，目标 {format_price(target)}"
        )
    if is_bull and bear_align:
        return "指标偏多但均线空头排列，逆势信号，建议放弃或轻仓，等 EMA20 上穿 EMA50 后再做多"
    if not is_bull and bear_align:
        return (
            f"均线空头排列（EMA20<50<100<200），{action} EMA20 {format_price(entry_ref)} 附近分批，"
            f"{stop_verb}止损 {format_price(stop)} 离场，目标 {format_price(target)}"
        )
    if not is_bull and bull_align:
        return "指标偏空但均线多头排列，逆势信号，建议放弃或轻仓，等 EMA20 下穿 EMA50 后再做空"
    return "均线方向尚未统一，建议轻仓试探，等 EMA20 与 EMA50 方向一致后加仓"


def load_config():
    default = {
        "symbols": DEFAULT_SYMBOLS,
        "intervals": ["1h"],
        "threshold": 2,
        "refresh_seconds": 30,
        "dashboard_url": "http://192.168.10.13:5173",
        "strategy": {
            "mode": "turtle",
            "turtle_system": "system2",
            "account_value": 10000,
            "risk_fraction": 0.01,
            "filters": dict(TURTLE_FILTER_DEFAULTS),
            "limits": {
                "max_symbol_units": 4,
                "max_strong_group_units": 6,
                "max_weak_group_units": 10,
                "max_direction_units": 12
            },
            "correlation": {"strong_groups": [], "weak_groups": []}
        },
        "channels": {
            "dingtalk": {"webhook": ""},
            "wecom": {"webhook": ""},
            "serverchan": {"sendkey": ""},
            "pushplus": {"token": ""},
            "bark": {"server": "https://api.day.app", "key": ""},
            "generic": {"webhook": ""}
        }
    }
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as file:
            json.dump(default, file, ensure_ascii=False, indent=2)
        logging.info("已生成配置文件: %s", CONFIG_PATH)
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"open_trades": [], "closed_trades": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return {"open_trades": [], "closed_trades": []}
        data.setdefault("open_trades", [])
        data.setdefault("closed_trades", [])
        return data
    except (OSError, ValueError):
        return {"open_trades": [], "closed_trades": []}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def post_json(url, payload, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "CoinPulse/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def post_form(url, payload, timeout=10):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "CoinPulse/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "CoinPulse/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def try_channel(name, callback):
    try:
        callback()
        logging.info("%s 推送成功", name)
    except Exception as exc:
        logging.error("%s 推送失败: %s", name, exc)


def has_channel(config):
    channels = config.get("channels", {})
    dingtalk = (channels.get("dingtalk") or {}).get("webhook", "")
    wecom = (channels.get("wecom") or {}).get("webhook", "")
    serverchan = (channels.get("serverchan") or {}).get("sendkey", "")
    pushplus = (channels.get("pushplus") or {}).get("token", "")
    bark = (channels.get("bark") or {}).get("key", "")
    generic = (channels.get("generic") or {}).get("webhook", "")
    return bool(dingtalk or wecom or serverchan or pushplus or bark or generic)


def send_notification(title, content, config):
    channels = config.get("channels", {})

    dingtalk = channels.get("dingtalk") or {}
    if dingtalk.get("webhook"):
        try_channel(
            "钉钉",
            lambda: post_json(dingtalk["webhook"], {"msgtype": "text", "text": {"content": content}})
        )

    wecom = channels.get("wecom") or {}
    if wecom.get("webhook"):
        try_channel(
            "企业微信",
            lambda: post_json(wecom["webhook"], {"msgtype": "text", "text": {"content": content}})
        )

    serverchan = channels.get("serverchan") or {}
    if serverchan.get("sendkey"):
        url = f"https://sctapi.ftqq.com/{serverchan['sendkey']}.send"
        try_channel(
            "Server酱",
            lambda: post_form(url, {"title": title, "desp": content, "channel": "9"})
        )

    pushplus = channels.get("pushplus") or {}
    if pushplus.get("token"):
        try_channel(
            "PushPlus",
            lambda: post_json(
                "https://www.pushplus.plus/send",
                {"token": pushplus["token"], "title": title, "content": content, "template": "txt"}
            )
        )

    bark = channels.get("bark") or {}
    if bark.get("key"):
        server = (bark.get("server") or "https://api.day.app").rstrip("/")
        url = (
            f"{server}/{urllib.parse.quote(bark['key'])}/"
            f"{urllib.parse.quote(title)}/{urllib.parse.quote(content)}"
        )
        try_channel("Bark", lambda: http_get(url))

    generic = channels.get("generic") or {}
    if generic.get("webhook"):
        try_channel(
            "通用Webhook",
            lambda: post_json(generic["webhook"], {"title": title, "content": content})
        )


def build_message(event, config):
    direction = "做多" if event["direction"] == "bull" else "做空"
    lines = [
        f"{direction}信号 {event['symbol']} {event['interval']} {event['label']}（{event['score']:+.1f}）",
        f"现价 {event['price']}（{event['change']:+.2f}%）",
        f"依据：{event['reason']}",
        f"策略：{event['strategy']}",
        f"时间：{event['time']}"
    ]
    dashboard_url = config.get("dashboard_url")
    if dashboard_url:
        lines.append(f"看板：{dashboard_url}")
    return "\n".join(lines)


def detect_reversal(klines, indicators):
    """高胜率反转信号检测：极值 + 反转确认 + 多条件共振（与看板/回测一致）
    返回 (direction, points, reasons)。direction: 'long'/'short'/None
    """
    return detect_reversal_slice(klines, indicators, len(klines) - 1)


def detect_reversal_slice(klines, indicators, idx):
    """detect_reversal 的切片版：计算到 idx 为止的信号（回测用，避免未来数据）"""
    closes = [k["close"] for k in klines]
    last = idx
    prev = max(0, last - 1)
    kline = klines[last]
    prev_k = klines[prev]

    rsi = indicators["rsi"][last]
    rsi_prev = indicators["rsi"][prev] if prev > 0 else None
    kdj = indicators["kdj"]
    j = kdj["J"][last]
    j_prev = kdj["J"][prev]
    k = kdj["K"][last]
    d = kdj["D"][last]
    k_prev = kdj["K"][prev]
    d_prev = kdj["D"][prev]
    stoch = indicators["stochRsi"]
    st_k = stoch["K"][last]
    st_d = stoch["D"][last]
    st_k_prev = stoch["K"][prev]
    st_d_prev = stoch["D"][prev]

    ema20 = indicators["ema20"][last]
    price = closes[last]
    bias = (price - ema20) / ema20 * 100 if ema20 else 0.0

    long_points = 0
    short_points = 0
    long_reasons = []
    short_reasons = []

    # --- 极值区（超卖 → 做多候选） ---
    if rsi is not None and rsi < 30:
        long_points += 1
        long_reasons.append(f"RSI超卖({rsi:.0f})")
    if rsi is not None and rsi < 20:
        long_points += 1
        long_reasons.append(f"RSI深度超卖({rsi:.0f})")
    if j is not None and j < 20:
        long_points += 1
        long_reasons.append(f"KDJ-J超卖({j:.0f})")
    if j is not None and j < 5:
        long_points += 1
        long_reasons.append(f"KDJ-J深度超卖({j:.0f})")
    if st_k is not None and st_k < 20:
        long_points += 1
        long_reasons.append(f"StochRSI超卖({st_k:.0f})")
    if bias < -5:
        long_points += 1
        long_reasons.append(f"乖离率{bias:+.1f}%（偏离均线过大）")

    # --- 极值区（超买 → 做空候选） ---
    if rsi is not None and rsi > 70:
        short_points += 1
        short_reasons.append(f"RSI超买({rsi:.0f})")
    if rsi is not None and rsi > 80:
        short_points += 1
        short_reasons.append(f"RSI深度超买({rsi:.0f})")
    if j is not None and j > 80:
        short_points += 1
        short_reasons.append(f"KDJ-J超买({j:.0f})")
    if j is not None and j > 95:
        short_points += 1
        short_reasons.append(f"KDJ-J深度超买({j:.0f})")
    if st_k is not None and st_k > 80:
        short_points += 1
        short_reasons.append(f"StochRSI超买({st_k:.0f})")
    if bias > 5:
        short_points += 1
        short_reasons.append(f"乖离率{bias:+.1f}%（偏离均线过大）")

    # --- 反转确认信号 ---
    if k_prev is not None and d_prev is not None and k is not None and d is not None:
        if k_prev <= d_prev and k > d:
            long_points += 1
            long_reasons.append("KDJ金叉")
            if j_prev is not None and j_prev < 20:
                long_points += 1
                long_reasons.append("超卖区金叉")
        if k_prev >= d_prev and k < d:
            short_points += 1
            short_reasons.append("KDJ死叉")
            if j_prev is not None and j_prev > 80:
                short_points += 1
                short_reasons.append("超买区死叉")

    if st_k_prev is not None and st_d_prev is not None and st_k is not None and st_d is not None:
        if st_k_prev <= st_d_prev and st_k > st_d:
            long_points += 1
            long_reasons.append("StochRSI金叉")
        if st_k_prev >= st_d_prev and st_k < st_d:
            short_points += 1
            short_reasons.append("StochRSI死叉")

    if rsi_prev is not None and rsi is not None:
        if rsi_prev < 30 <= rsi:
            long_points += 1
            long_reasons.append("RSI上穿30")
        if rsi_prev > 70 >= rsi:
            short_points += 1
            short_reasons.append("RSI下穿70")

    # 吞没 K 线
    body = abs(kline["close"] - kline["open"])
    prev_body = abs(prev_k["close"] - prev_k["open"])
    if body > prev_body * 1.2 and body > 0:
        if kline["close"] > kline["open"] and prev_k["close"] < prev_k["open"]:
            long_points += 1
            long_reasons.append("阳线吞没")
        if kline["close"] < kline["open"] and prev_k["close"] > prev_k["open"]:
            short_points += 1
            short_reasons.append("阴线吞没")

    # 长下影（多头反转）
    low_range = kline["high"] - kline["low"]
    if low_range > 0:
        lower_wick = min(kline["open"], kline["close"]) - kline["low"]
        if lower_wick / low_range > 0.5 and lower_wick > body * 0.8:
            long_points += 1
            long_reasons.append("长下影线")

    # 放量
    vols = [k["volume"] for k in klines[-6:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if avg_vol > 0 and kline["volume"] > avg_vol * 1.5:
        if long_points > 0:
            long_points += 1
            long_reasons.append("放量")
        if short_points > 0:
            short_points += 1
            short_reasons.append("放量")

    if long_points >= 3 and long_points >= short_points:
        return "long", long_points, long_reasons
    if short_points >= 3 and short_points > long_points:
        return "short", short_points, short_reasons
    return None, max(long_points, short_points), (
        long_reasons if long_points >= short_points else short_reasons
    )


def detect_rsi_divergence_slice(klines, indicators, idx):
    """RSI 背离检测（回测验证：止损1.2×ATR/止盈4.0×ATR，全币年化+23%，优选币+54%）
    底背离：价格创新低但 RSI 抬高 → 做多
    顶背离：价格创新高但 RSI 降低 → 做空
    返回 (direction, reasons)
    """
    if idx < 30:
        return None, []
    price = klines[idx]["close"]
    rsi = indicators["rsi"][idx]
    rsi_prev = indicators["rsi"][idx - 5]
    if rsi is None or rsi_prev is None:
        return None, []
    low_now = min(k["low"] for k in klines[idx - 3:idx + 1])
    low_prev = min(k["low"] for k in klines[idx - 8:idx - 4])
    high_now = max(k["high"] for k in klines[idx - 3:idx + 1])
    high_prev = max(k["high"] for k in klines[idx - 8:idx - 4])
    # 底背离：价格创新低，RSI 抬高
    if low_now < low_prev and rsi > rsi_prev + 3 and rsi < 45:
        return "long", [f"底背离(RSI {rsi_prev:.0f}→{rsi:.0f} 价格新低)"]
    # 顶背离：价格创新高，RSI 降低
    if high_now > high_prev and rsi < rsi_prev - 3 and rsi > 55:
        return "short", [f"顶背离(RSI {rsi_prev:.0f}→{rsi:.0f} 价格新高)"]
    return None, []


def build_rsi_divergence_text(klines, indicators, direction):
    """RSI背离策略：入场/止损/目标（1.2×ATR止损 / 4.0×ATR止盈，回测最优）"""
    last = klines[-1]
    price = last["close"]
    atr = calc_atr(klines, 14) or price * 0.01
    if direction == "long":
        stop = price - atr * 1.2
        target = price + atr * 4.0
        entry = price  # 背离确认处即入场
        return (f"入场点位 ≈ {format_price(entry)}（背离确认处现价进场）\n"
                f"止损 {format_price(stop)}（1.2×ATR，约{atr*1.2/price*100:.1f}%）\n"
                f"目标 {format_price(target)}（4×ATR，盈亏比约3.3:1）")
    else:
        stop = price + atr * 1.2
        target = price - atr * 4.0
        entry = price
        return (f"入场点位 ≈ {format_price(entry)}（背离确认处现价进场）\n"
                f"止损 {format_price(stop)}（1.2×ATR，约{atr*1.2/price*100:.1f}%）\n"
                f"目标 {format_price(target)}（4×ATR，盈亏比约3.3:1）")


def build_reversal_message(event, config):
    direction = "抄底做多" if event["direction"] == "long" else "逃顶做空"
    lines = [
        f"🔄 高胜率反转信号 {event['symbol']} {event['interval']}",
        f"{direction} · {event['points']} 项共振（{event['grade']}）",
        f"现价 {event['price']}（{event['change']:+.2f}%）",
        f"依据：{event['reason']}",
        f"策略：{event['strategy']}",
        f"时间：{event['time']}"
    ]
    dashboard_url = config.get("dashboard_url")
    if dashboard_url:
        lines.append(f"看板：{dashboard_url}")
    return "\n".join(lines)


def _scan_one(symbol, interval, state, config):
    """单个币/周期的扫描（供并发调用）"""
    try:
        strategy_config = config.get("strategy") or {}
        mode = strategy_config.get("mode", "turtle")
        turtle_system = strategy_config.get("turtle_system", "system2")
        filter_options = turtle_filter_options(strategy_config)
        klines, provider = (
            fetch_klines_for_strategy(
                symbol, interval, turtle_system, filter_options["closed_candles_only"]
            )
            if mode == "turtle" else fetch_klines_with_fallback(
                symbol, interval, filter_options["closed_candles_only"]
            )
        )
        indicators = compute_indicators(klines)
        if mode == "turtle":
            system = turtle_system
            account_value = float(strategy_config.get("account_value", 10000))
            risk_fraction = float(strategy_config.get("risk_fraction", TURTLE_RISK_FRACTION))
            higher_trend = None
            higher_interval = higher_timeframe(interval)
            if filter_options["higher_timeframe"] and higher_interval:
                try:
                    higher_klines, _higher_provider = fetch_klines_with_fallback(
                        symbol, higher_interval, filter_options["closed_candles_only"]
                    )
                    higher_trend = higher_timeframe_trend(
                        higher_klines, filter_options["higher_ema_period"]
                    )
                except Exception as exc:
                    logging.warning("%s %s 高周期趋势获取失败: %s", symbol, interval, exc)
            st_key = f"{symbol}|{interval}|turtle|{system}"
            direction, reasons, trade_plan = build_turtle_signal(
                klines, system, account_value, risk_fraction, interval,
                bool(state.get(f"{st_key}|blocked")), filter_options, higher_trend
            )
            if trade_plan and trade_plan.get("blocked"):
                # S1 only skips the next same-system breakout after a win.
                state[f"{st_key}|blocked"] = False
            if direction and trade_plan:
                capacity = turtle_unit_capacity(symbol, direction, state, config)
                if capacity < 1:
                    direction = None
                    reasons = ["组合风险上限已满"]
                    trade_plan["blocked"] = True
                else:
                    trade_plan["max_units"] = min(TURTLE_MAX_UNITS, capacity)
            strategy_text = build_turtle_strategy_text(direction, trade_plan) if direction else ""
            label = "海龟突破做多" if direction == "long" else "海龟突破做空"
            grade = turtle_params(system, interval)["label"]
        # ===== 保留原有策略，可通过 strategy.mode=legacy 恢复 =====
        elif interval in ("1h", "15m"):
            direction, reasons, strategy_text = detect_bias_regression(klines, indicators)
            st_key = f"{symbol}|{interval}|bias"
            label = "乖离做多" if direction == "long" else "乖离做空"
            trade_plan = None
            grade = "回测年化+12%~+23%"
        else:
            direction, reasons = detect_rsi_divergence_slice(klines, indicators, len(klines) - 1)
            strategy_text = build_rsi_divergence_text(klines, indicators, direction) if direction else ""
            st_key = f"{symbol}|{interval}|div"
            label = "背离做多" if direction == "long" else "背离做空"
            trade_plan = None
            grade = "回测年化+12%~+23%"
        prev_sig = state.get(st_key)
        event = None
        if direction is not None and prev_sig != direction:
            event = {
                "symbol": symbol,
                "interval": interval,
                "label": label,
                "direction": direction,
                "score": 0.0,
                "reason": " · ".join(reasons),
                "strategy": strategy_text,
                "price": format_price(klines[-1]["close"]),
                "change": get_change(klines, interval),
                "provider": provider,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "bar_time": klines[-1]["time"],
                "points": 4,
                "grade": grade,
                "divergence": mode != "turtle",
                "turtle": mode == "turtle",
                "trade_plan": trade_plan
            }
        state[st_key] = direction if direction is not None else "none"
        return event
    except Exception as exc:
        logging.warning("%s %s 获取失败: %s", symbol, interval, exc)
        return None


def scan_once(config, state):
    events = []
    symbols = config.get("symbols") or DEFAULT_SYMBOLS
    intervals = config.get("intervals") or ["1h"]

    try:
        from concurrent.futures import ThreadPoolExecutor
    except ImportError:
        ThreadPoolExecutor = None

    tasks = [(s, iv) for s in symbols for iv in intervals]
    if ThreadPoolExecutor:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(_scan_one, s, iv, state, config) for s, iv in tasks]
            for f in futures:
                try:
                    ev = f.result(timeout=30)
                    if ev:
                        events.append(ev)
                except Exception as exc:
                    logging.warning("并发扫描任务异常: %s", exc)
    else:
        for s, iv in tasks:
            ev = _scan_one(s, iv, state, config)
            if ev:
                events.append(ev)
    return events


def detect_bias_regression(klines, indicators, dev_th=2.2):
    """乖离回归（短线策略，回测1h年化+12.1%）：偏离EMA20超过±2.2×ATR时反向"""
    last = klines[-1]
    price = last["close"]
    ema20 = indicators["ema20"][-1]
    atr = calc_atr(klines, 14) or price * 0.01
    if ema20 is None:
        return None, [], ""
    dev = (price - ema20) / atr
    bias_pct = (price - ema20) / ema20 * 100
    if dev <= -dev_th:
        reasons = [f"乖离{bias_pct:.1f}%（{dev:.1f}×ATR偏离EMA20）"]
        entry = price  # 乖离极值时现价入场
        stop = price - atr * 1.2
        target = price + atr * 4.0
        text = (f"入场点位 ≈ {format_price(entry)}（超跌乖离极值处进场）\n"
                f"止损 {format_price(stop)}（1.2×ATR，约{atr*1.2/price*100:.1f}%）\n"
                f"目标 {format_price(target)}（4×ATR，盈亏比约3.3:1）")
        return "long", reasons, text
    if dev >= dev_th:
        reasons = [f"乖离+{bias_pct:.1f}%（{dev:.1f}×ATR偏离EMA20）"]
        entry = price
        stop = price + atr * 1.2
        target = price - atr * 4.0
        text = (f"入场点位 ≈ {format_price(entry)}（超涨乖离极值处进场）\n"
                f"止损 {format_price(stop)}（1.2×ATR，约{atr*1.2/price*100:.1f}%）\n"
                f"目标 {format_price(target)}（4×ATR，盈亏比约3.3:1）")
        return "short", reasons, text
    return None, [], ""


def build_reversal_strategy_text(klines, indicators, direction):
    """反转策略：入场/止损/目标"""
    last = klines[-1]
    price = last["close"]
    atr = calc_atr(klines, 14) or price * 0.008
    ema20 = indicators["ema20"][-1]
    ema50 = indicators["ema50"][-1]
    if direction == "long":
        recent_low = min(k["low"] for k in klines[-10:])
        stop = min(ema20 if ema20 else price, recent_low) - atr * 0.5
        target = ema50 if ema50 and ema50 > price else (ema20 + atr * 2) if ema20 else price * 1.03
        return (f"超卖反弹，现价附近分批入场；跌破止损 {format_price(stop)} 离场；"
                f"目标 {format_price(target)}（2.5倍盈亏比）")
    else:
        recent_high = max(k["high"] for k in klines[-10:])
        stop = max(ema20 if ema20 else price, recent_high) + atr * 0.5
        target = ema50 if ema50 and ema50 < price else (ema20 - atr * 2) if ema20 else price * 0.97
        return (f"超买回落，现价附近分批入场；突破止损 {format_price(stop)} 离场；"
                f"目标 {format_price(target)}（2.5倍盈亏比）")


def build_divergence_message(event, config):
    direction = "抄底做多" if event["direction"] == "long" else "逃顶做空"
    lines = [
        f"📉📈 RSI背离反转信号 {event['symbol']} {event['interval']}",
        f"{direction} · {event['grade']}",
        f"现价 {event['price']}（{event['change']:+.2f}%）",
        f"依据：{event['reason']}",
        f"{event['strategy']}",
        f"时间：{event['time']}"
    ]
    dashboard_url = config.get("dashboard_url")
    if dashboard_url:
        lines.append(f"看板：{dashboard_url}")
    return "\n".join(lines)


def build_turtle_message(event, config):
    direction = "突破做多" if event["direction"] == "long" else "突破做空"
    plan = event.get("trade_plan") or {}
    lines = [
        f"海龟突破信号 {event['symbol']} {event['interval']}",
        f"{plan.get('system', event['grade'])} · {direction}",
        f"现价 {event['price']}（{event['change']:+.2f}%）",
        f"依据：{event['reason']}",
        f"{event['strategy']}",
        f"规则：首单位风险=账户1% · 每0.5N加1单位 · 最多{int(plan.get('max_units') or TURTLE_MAX_UNITS)}单位 · 2N止损",
        f"时间：{event['time']}"
    ]
    dashboard_url = config.get("dashboard_url")
    if dashboard_url:
        lines.append(f"看板：{dashboard_url}")
    return "\n".join(lines)


def process_events(events, config):
    for event in events:
        if event.get("turtle"):
            title = f"CoinPulse {event['symbol']} 海龟突破{event['label']}"
            content = build_turtle_message(event, config)
        elif event.get("divergence"):
            title = f"CoinPulse {event['symbol']} RSI背离{event['label']}"
            content = build_divergence_message(event, config)
        elif event.get("direction") in ("long", "short") and event.get("points"):
            title = f"CoinPulse {event['symbol']} 反转{event['label']}"
            content = build_reversal_message(event, config)
        else:
            title = f"CoinPulse {event['symbol']} {event['label']}"
            content = build_message(event, config)
        send_notification(title, content, config)
        logging.info("发现信号: %s", content.replace("\n", " / "))
        register_trade(event, config)


# ---------------------------------------------------------------------------
# 交易跟踪：信号建档 → 触达止损/目标自动结算 → 统计
# ---------------------------------------------------------------------------

TRADE_STATS_PATH = os.path.join(BASE_DIR, "trade_stats.json")


def register_trade(event, config):
    """信号推送时登记一笔未平仓交易，优先使用结构化策略字段。"""
    state = load_state()
    trades = state.setdefault("open_trades", [])
    # 提取价格：策略文本里已有，解析出来
    try:
        price = float(str(event["price"]).replace(",", ""))
    except (ValueError, TypeError):
        price = None
    strategy = event.get("strategy", "")
    entry = stop = target = None
    trade_plan = event.get("trade_plan") or {}
    if event.get("turtle") and trade_plan:
        entry = trade_plan.get("entry")
        stop = trade_plan.get("stop")
    # 从策略文本解析：入场点位 ≈ X（...）
    import re as _re
    m_entry = _re.search(r"入场点位[≈≈]?\s*([\d.]+)", strategy)
    m_stop = _re.search(r"止损\s*([\d.]+)", strategy)
    m_target = _re.search(r"目标\s*([\d.]+)", strategy)
    if m_entry:
        entry = float(m_entry.group(1))
    if m_stop:
        stop = float(m_stop.group(1))
    if m_target:
        target = float(m_target.group(1))
    if entry is None:
        entry = price

    trade = {
        "id": f"{event['symbol']}|{event['interval']}|{event['time']}",
        "symbol": event["symbol"],
        "interval": event["interval"],
        "direction": event.get("direction", ""),
        "entry": entry,
        "stop": stop,
        "target": target,
        "opened_at": event["time"],
        "entry_ts": int(time.time() * 1000),  # 入场时的时间戳（毫秒），结算时只检查之后的K线
        "status": "open",
        "result": None,
        "pnl_pct": None,
        "closed_at": None,
    }
    if event.get("turtle") and trade_plan:
        trade.update({
            "strategy_type": "turtle",
            "system": trade_plan.get("system"),
            "n": trade_plan.get("n"),
            "stop_n": 2.0,
            "add_n": 0.5,
            "max_units": int(trade_plan.get("max_units") or TURTLE_MAX_UNITS),
            "units": 1,
            "unit_quantity": trade_plan.get("unit_quantity"),
            "unit_entries": [{"price": entry, "n": trade_plan.get("n")}],
            "latest_entry": entry,
            "next_add": trade_plan.get("next_add"),
            "exit_period": trade_plan.get("exit_days"),
            "bar_time": event.get("bar_time")
        })
    # 避免重复登记同一信号
    if not any(t["id"] == trade["id"] for t in trades):
        trades.append(trade)
        save_state(state)


def manage_turtle_trade(trade, klines):
    """按 0.5N 加仓，并按最新单位的 2N 止损/反向通道退出。"""
    interval = trade.get("interval", "1d")
    system = trade.get("system", "system2")
    params = turtle_params(system, interval)
    n_series = calc_n_series(klines, params["n_period"])
    entry_bar = trade.get("bar_time") or trade.get("entry_ts") or 0
    last_managed_bar = trade.get("last_managed_bar") or entry_bar
    entries = list(trade.get("unit_entries") or [{"price": trade["entry"], "n": trade.get("n")}])
    n = float(trade.get("n") or 0)
    if n <= 0:
        return None
    direction = trade.get("direction")
    max_units = int(trade.get("max_units") or TURTLE_MAX_UNITS)
    add_n = float(trade.get("add_n") or 0.5)
    stop_n = float(trade.get("stop_n") or 2.0)
    unit_quantity = float(trade.get("unit_quantity") or 0)
    exit_price = None
    result = None
    exit_time = None

    for index, kline in enumerate(klines):
        if kline["time"] <= last_managed_bar:
            continue
        current_n = n_series[index] or n
        latest_entry = float(entries[-1]["price"])
        latest_n = float(entries[-1].get("n") or current_n)
        stop = latest_entry - stop_n * latest_n if direction == "long" else latest_entry + stop_n * latest_n
        stop_hit = kline["low"] <= stop if direction == "long" else kline["high"] >= stop
        if stop_hit:
            exit_price, result, exit_time = stop, "止损", kline["time"]
            break

        prior = klines[max(0, index - params["exit_bars"]):index]
        if len(prior) >= params["exit_bars"]:
            exit_level = min(k["low"] for k in prior) if direction == "long" else max(k["high"] for k in prior)
            exit_hit = kline["low"] <= exit_level if direction == "long" else kline["high"] >= exit_level
            if exit_hit:
                exit_price, result, exit_time = exit_level, "通道退出", kline["time"]
                break

        # 先处理止损/退出，再处理同一根K线的加仓，避免对盘中顺序作乐观假设。
        while len(entries) < max_units:
            latest_entry = float(entries[-1]["price"])
            next_add = latest_entry + add_n * current_n if direction == "long" else latest_entry - add_n * current_n
            reached = kline["high"] >= next_add if direction == "long" else kline["low"] <= next_add
            if not reached:
                break
            entries.append({"price": next_add, "n": current_n})

    trade["unit_entries"] = entries
    if klines:
        trade["last_managed_bar"] = klines[-1]["time"]
    trade["units"] = len(entries)
    trade["latest_entry"] = entries[-1]["price"]
    latest_n = float(entries[-1].get("n") or n)
    trade["next_add"] = (
        entries[-1]["price"] + add_n * latest_n if len(entries) < max_units
        else None
    )
    trade["stop"] = (
        entries[-1]["price"] - stop_n * latest_n if direction == "long"
        else entries[-1]["price"] + stop_n * latest_n
    )
    if exit_price is None:
        return None

    total_quantity = unit_quantity * len(entries)
    weighted_entry = sum(item["price"] for item in entries) / len(entries)
    pnl_pct = (
        (exit_price - weighted_entry) / weighted_entry * 100
        if direction == "long" else (weighted_entry - exit_price) / weighted_entry * 100
    )
    trade["entry"] = weighted_entry
    trade["exit"] = exit_price
    trade["result"] = result
    trade["pnl_pct"] = round(pnl_pct, 2)
    trade["closed_at"] = datetime.fromtimestamp(exit_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
    trade["exit_ts"] = exit_time
    trade["quantity"] = total_quantity
    return trade


def settle_trades(state, config):
    """用最新行情结算未平仓交易：触达止损=亏，触达目标=赚"""
    open_trades = state.get("open_trades", [])
    if not open_trades:
        return []
    settled = []
    remaining = []
    state_changed = False
    for trade in open_trades:
        try:
            if trade.get("strategy_type") == "turtle":
                klines, _prov = fetch_klines_for_strategy(
                    trade["symbol"], trade["interval"], trade.get("system", "system2"),
                    turtle_filter_options(config.get("strategy") or {})["closed_candles_only"]
                )
            else:
                klines, _prov = fetch_klines_with_fallback(trade["symbol"], trade["interval"])
        except Exception:
            remaining.append(trade)  # 拉不到数据，保留下轮再查
            continue
        if trade.get("strategy_type") == "turtle":
            before_state = (
                trade.get("units"), trade.get("last_managed_bar"),
                trade.get("stop"), trade.get("next_add")
            )
            settled_trade = manage_turtle_trade(trade, klines)
            after_state = (
                trade.get("units"), trade.get("last_managed_bar"),
                trade.get("stop"), trade.get("next_add")
            )
            state_changed = state_changed or before_state != after_state
            if settled_trade is None:
                remaining.append(trade)
                continue
            trade["status"] = "closed"
            state.setdefault("closed_trades", []).append(trade)
            if trade.get("system") == "system1":
                state[
                    f"{trade['symbol']}|{trade['interval']}|turtle|system1|blocked"
                ] = (trade.get("pnl_pct") or 0) > 0
            settled.append(trade)
            logging.info(
                "海龟交易结算: %s %s %s 盈亏%+.2f%%",
                trade["symbol"], trade["interval"], trade["result"], trade["pnl_pct"]
            )
            continue

        closes = [k["close"] for k in klines]
        entry = trade["entry"] or closes[-1]
        stop = trade["stop"]
        target = trade["target"]
        entry_ts = trade.get("entry_ts") or 0

        # 只检查入场时间之后的K线（避免用入场前的历史价格误判）
        future = [k for k in klines if k["time"] > entry_ts]
        if not future:
            remaining.append(trade)
            continue

        # 按时间顺序检查：先触达哪个算哪个（真实持仓逻辑）
        exit_price = None
        result = None
        for k in future:
            high = k["high"]
            low = k["low"]
            if trade["direction"] == "long":
                if stop is not None and low <= stop:
                    exit_price, result = stop, "止损"
                    break
                if target is not None and high >= target:
                    exit_price, result = target, "止盈"
                    break
            else:
                if stop is not None and high >= stop:
                    exit_price, result = stop, "止损"
                    break
                if target is not None and low <= target:
                    exit_price, result = target, "止盈"
                    break

        if exit_price is None:
            remaining.append(trade)
            continue

        if trade["direction"] == "long":
            pnl_pct = (exit_price - entry) / entry * 100
        else:
            pnl_pct = (entry - exit_price) / entry * 100

        trade["status"] = "closed"
        trade["result"] = result
        trade["pnl_pct"] = round(pnl_pct, 2)
        trade["exit"] = exit_price
        trade["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.setdefault("closed_trades", []).append(trade)
        settled.append(trade)
        logging.info("交易结算: %s %s %s 盈亏%+.2f%%", trade["symbol"], trade["interval"], result, pnl_pct)

    state["open_trades"] = remaining
    if settled or state_changed:
        save_state(state)
    if settled:
        write_trade_stats(state)
    return settled


def write_trade_stats(state):
    """把已结算交易汇总写入 trade_stats.json（看板读取）"""
    closed = state.get("closed_trades", [])
    stats = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(closed),
        "wins": sum(1 for t in closed if (t.get("pnl_pct") or 0) > 0),
        "losses": sum(1 for t in closed if (t.get("pnl_pct") or 0) <= 0),
        "open_count": len(state.get("open_trades", [])),
    }
    stats["win_rate"] = round(stats["wins"] / stats["total"] * 100, 1) if stats["total"] else 0
    total_pnl = sum(t.get("pnl_pct") or 0 for t in closed)
    stats["total_pnl_pct"] = round(total_pnl, 2)
    avg_win = sum(t.get("pnl_pct") or 0 for t in closed if (t.get("pnl_pct") or 0) > 0)
    avg_loss = sum(t.get("pnl_pct") or 0 for t in closed if (t.get("pnl_pct") or 0) <= 0)
    stats["avg_win"] = round(avg_win / stats["wins"], 2) if stats["wins"] else 0
    stats["avg_loss"] = round(avg_loss / stats["losses"], 2) if stats["losses"] else 0
    stats["payoff"] = round(abs(stats["avg_win"] / stats["avg_loss"]), 2) if stats["avg_loss"] else 0
    # 按周期/币种分组
    by_symbol = {}
    for t in closed:
        by_symbol.setdefault(t["symbol"], {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        g = by_symbol[t["symbol"]]
        g["total"] += 1
        g["wins"] += 1 if (t.get("pnl_pct") or 0) > 0 else 0
        g["losses"] += 1 if (t.get("pnl_pct") or 0) <= 0 else 0
        g["pnl"] = round(g["pnl"] + (t.get("pnl_pct") or 0), 2)
    stats["by_symbol"] = [
        {"symbol": s, **v} for s, v in sorted(by_symbol.items(), key=lambda x: -x[1]["pnl"])
    ]
    stats["trades"] = closed[-50:]  # 最近50笔
    with open(TRADE_STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def send_startup_message(config):
    if not has_channel(config):
        logging.warning(
            "尚未配置推送渠道，请编辑 %s 填入钉钉、企业微信、Server酱、PushPlus 或 Bark，配置后自动生效",
            CONFIG_PATH
        )
        return
    symbols = config.get("symbols") or DEFAULT_SYMBOLS
    intervals = config.get("intervals") or ["1h"]
    refresh = config.get("refresh_seconds", 30)
    content = (
        "CoinPulse 信号监控已启动\n"
        f"监控 {len(symbols)} 个币种 · {'/'.join(intervals)} · 每 {refresh} 秒检测"
    )
    send_notification("CoinPulse 监控已启动", content, config)


def send_test_message(config):
    if not has_channel(config):
        logging.warning(
            "尚未配置推送渠道，请编辑 %s 填入钉钉、企业微信、Server酱、PushPlus 或 Bark，配置后自动生效",
            CONFIG_PATH
        )
        return
    send_notification(
        "CoinPulse 测试通知",
        "如果你收到这条消息，说明 CoinPulse 信号推送已经配置成功。",
        config
    )


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    config = load_config()
    state = load_state()
    logging.info("CoinPulse signal watcher started")

    if "--once" in sys.argv:
        events = scan_once(config, state)
        save_state(state)
        process_events(events, config)
        return

    if "--test" in sys.argv:
        send_test_message(config)
        return

    send_startup_message(config)
    while True:
        try:
            config = load_config()
            state = load_state()
            events = scan_once(config, state)
            save_state(state)
            process_events(events, config)
            # 结算未平仓交易（用最新行情）
            state = load_state()
            settle_trades(state, config)
        except Exception as exc:
            logging.exception("扫描失败: %s", exc)
        time.sleep(max(10, float(config.get("refresh_seconds", 30))))


if __name__ == "__main__":
    main()
