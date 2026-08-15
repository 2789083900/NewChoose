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


def http_get_json(url, timeout=9):
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
    ("binance-data", fetch_binance_data),
    ("binance-api", fetch_binance_api),
    ("bybit", fetch_bybit),
    ("okx", fetch_okx)
]


def fetch_klines_with_fallback(symbol, interval):
    errors = []
    for name, fetcher in PROVIDERS:
        try:
            klines = fetcher(symbol, interval)
            if isinstance(klines, list) and len(klines) > 50:
                return klines, name
            raise RuntimeError("数据不足")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(" / ".join(errors))


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
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


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


def scan_once(config, state):
    events = []
    threshold = float(config.get("threshold", 2))
    symbols = config.get("symbols") or DEFAULT_SYMBOLS
    intervals = config.get("intervals") or ["1h"]

    for symbol in symbols:
        for interval in intervals:
            key = f"{symbol}|{interval}"
            try:
                klines, provider = fetch_klines_with_fallback(symbol, interval)
                indicators = compute_indicators(klines)
                analysis = analyze_indicators(klines, indicators)
                prev_label = state.get(key)
                if prev_label is not None and prev_label != analysis["label"] and abs(analysis["score"]) >= threshold:
                    events.append({
                        "symbol": symbol,
                        "interval": interval,
                        "label": analysis["label"],
                        "direction": analysis["signalClass"],
                        "score": analysis["score"],
                        "reason": analysis["reason"],
                        "strategy": build_strategy_text(klines, indicators, analysis, interval),
                        "price": format_price(klines[-1]["close"]),
                        "change": get_change(klines, interval),
                        "provider": provider,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                state[key] = analysis["label"]
            except Exception as exc:
                logging.warning("%s %s 获取失败: %s", symbol, interval, exc)
    return events


def process_events(events, config):
    for event in events:
        title = f"CoinPulse {event['symbol']} {event['label']}"
        content = build_message(event, config)
        send_notification(title, content, config)
        logging.info("发现信号: %s", content.replace("\n", " / "))


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
            events = scan_once(config, state)
            save_state(state)
            process_events(events, config)
        except Exception as exc:
            logging.exception("扫描失败: %s", exc)
        time.sleep(max(10, float(config.get("refresh_seconds", 30))))


if __name__ == "__main__":
    main()
