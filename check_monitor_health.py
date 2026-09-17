#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Detect a missing/stale CoinPulse monitor heartbeat and notify once per transition."""

import argparse
import json
import os
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH_PATH = os.path.join(BASE_DIR, "monitor_health.json")
ALERT_STATE_PATH = os.path.join(BASE_DIR, "monitor_alert_state.json")
PERP_STATS_PATH = os.path.join(BASE_DIR, "perp_shadow_stats.json")


def load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, type(fallback)) else fallback
    except (OSError, ValueError):
        return fallback


def atomic_write(path, data):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, path)


def health_age_seconds(health, now=None):
    now = int(time.time()) if now is None else int(now)
    try:
        updated = int(health.get("updated_at_epoch"))
    except (TypeError, ValueError):
        updated = 0
    return max(0, now - updated) if updated else None


def perpetual_age_seconds(stats, now=None):
    now = int(time.time()) if now is None else int(now)
    try:
        generated = datetime.fromisoformat(
            str(stats.get("generated_at_utc") or "").replace("Z", "+00:00")
        )
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age = now - int(generated.timestamp())
        return age if age >= 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def send_serverchan(sendkey, title, content):
    if not sendkey:
        return False
    url = f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey, safe='')}.send"
    payload = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return 200 <= response.status < 300


def health_alert_detail(health, age_seconds):
    """Build a diagnostic alert without exposing channel credentials."""
    scan = health.get("scan") or {}
    parts = ["monitor_health.json 未在预期周期内更新，或最近扫描失败。"]
    if age_seconds is not None:
        parts.append(f"最近更新距今约 {round(age_seconds / 60, 1)} 分钟。")
    if health.get("updated_at"):
        parts.append(f"最后健康时间：{health['updated_at']}。")
    if scan.get("run_id"):
        parts.append(f"运行 ID：{scan['run_id']}。")
    if scan.get("coverage_pct") is not None:
        parts.append(
            f"行情覆盖率：{scan.get('coverage_pct')}%（成功 {scan.get('successful_markets', 0)}/"
            f"{scan.get('expected_markets', 0)}，失败 {scan.get('failed_markets', 0)}）。"
        )
    failures = [str(item) for item in (scan.get("failures") or []) if item]
    if failures:
        parts.append("最近失败：" + "；".join(failures[:2]))
    return "".join(parts)


def combined_alert_detail(health, health_age, perp_stats, perp_age, failed_components):
    parts = []
    if "ordinary_monitor" in failed_components:
        parts.append(health_alert_detail(health, health_age))
    if "perpetual_shadow" in failed_components:
        parts.append("perp_shadow_stats.json 未在预期周期内更新。")
        if perp_age is not None:
            parts.append(f"永续统计距今约 {round(perp_age / 60, 1)} 分钟。")
        if perp_stats.get("generated_at_utc"):
            parts.append(f"最后生成时间：{perp_stats['generated_at_utc']}。")
        redundancy = (perp_stats.get("provider_redundancy") or {}).get("status")
        if redundancy:
            parts.append(f"最近 provider 冗余状态：{redundancy}。")
    return "".join(parts)


def _failure_severity_bucket(count):
    value = max(0, int(count or 0))
    if value >= 12:
        return "12_plus"
    if value >= 6:
        return "6_to_11"
    if value >= 3:
        return "3_to_5"
    if value >= 1:
        return "1_to_2"
    return "zero"


def _expiry_severity_bucket(minutes):
    if minutes is None:
        return "unknown"
    value = max(0.0, float(minutes))
    if value <= 0:
        return "expired"
    if value <= 60:
        return "within_60m"
    if value <= 120:
        return "within_120m"
    return "warning_window"


def risk_tier_alert_state(perp_stats):
    """Return a stable alert signature without notifying on every failed refresh."""
    summary = perp_stats.get("risk_tier_health")
    if not isinstance(summary, dict):
        return {
            "active": False, "status": "not_monitored", "signature": "not_monitored",
            "degraded_symbols": [], "unavailable_symbols": [],
        }
    status = str(summary.get("status") or "unknown")
    degraded = sorted(str(item) for item in (summary.get("degraded_symbols") or []))
    unavailable = sorted(str(item) for item in (summary.get("unavailable_symbols") or []))
    try:
        failures = max(0, int(summary.get("max_consecutive_refresh_failures") or 0))
    except (TypeError, ValueError):
        failures = 0
    try:
        remaining = summary.get("minimum_remaining_stale_minutes")
        remaining = max(0.0, float(remaining)) if remaining is not None else None
    except (TypeError, ValueError):
        remaining = None
    active = status in {"degraded", "unavailable"} or bool(degraded or unavailable)
    signature_parts = [status, ",".join(degraded), ",".join(unavailable)]
    if active:
        signature_parts.extend([
            _failure_severity_bucket(failures),
            _expiry_severity_bucket(remaining),
        ])
    return {
        "active": active,
        "status": status,
        "signature": "|".join(signature_parts),
        "degraded_symbols": degraded,
        "unavailable_symbols": unavailable,
        "max_consecutive_refresh_failures": failures,
        "minimum_remaining_stale_minutes": remaining,
        "by_symbol": summary.get("by_symbol") if isinstance(summary.get("by_symbol"), dict) else {},
    }


def risk_tier_alert_detail(tier_state):
    parts = [f"OKX 官方风险档位状态：{tier_state.get('status') or 'unknown'}。"]
    degraded = tier_state.get("degraded_symbols") or []
    unavailable = tier_state.get("unavailable_symbols") or []
    if degraded:
        parts.append("降级币种：" + "、".join(degraded) + "。")
    if unavailable:
        parts.append("不可用币种：" + "、".join(unavailable) + "。")
    failures = int(tier_state.get("max_consecutive_refresh_failures") or 0)
    if failures:
        parts.append(f"最大连续刷新失败：{failures} 次。")
    remaining = tier_state.get("minimum_remaining_stale_minutes")
    if remaining is not None:
        parts.append(f"最短剩余缓存有效时间约 {round(float(remaining), 1)} 分钟。")
    details = []
    for symbol in degraded[:4]:
        health = (tier_state.get("by_symbol") or {}).get(symbol) or {}
        details.append(
            f"{symbol}={health.get('status', 'unknown')}"
            f"/age={health.get('cache_age_minutes', '--')}m"
            f"/remaining={health.get('remaining_stale_minutes', '--')}m"
            f"/error={health.get('refresh_error_category') or '--'}"
        )
    if details:
        parts.append("档位诊断：" + "；".join(details) + "。")
    parts.append("该告警仅表示研究强平档位降级，不代表行情扫描或影子仓位处理已经失败。")
    return "".join(parts)


def check(max_age_minutes=20, max_perp_age_minutes=30, now=None, sendkey=""):
    health = load_json(HEALTH_PATH, {})
    perp_stats = load_json(PERP_STATS_PATH, {})
    age = health_age_seconds(health, now=now)
    perp_age = perpetual_age_seconds(perp_stats, now=now)
    status = health.get("status")
    failed_components = []
    if age is None or age > max_age_minutes * 60 or status in {"failed", "stale"}:
        failed_components.append("ordinary_monitor")
    if perp_age is None or perp_age > max_perp_age_minutes * 60:
        failed_components.append("perpetual_shadow")
    tier_state = risk_tier_alert_state(perp_stats)
    advisory_components = ["risk_tiers"] if tier_state["active"] else []
    current = "stale" if failed_components else (
        "degraded" if advisory_components else "healthy"
    )
    current_signature = {
        "status": current,
        "failed_components": sorted(failed_components),
        "advisory_components": sorted(advisory_components),
        "risk_tier_signature": tier_state["signature"],
    }
    previous = load_json(ALERT_STATE_PATH, {})
    previous_signature = {
        "status": previous.get("status"),
        "failed_components": sorted(previous.get("failed_components") or []),
        "advisory_components": sorted(previous.get("advisory_components") or []),
        "risk_tier_signature": previous.get("risk_tier_signature", "not_monitored"),
    }
    transition = current_signature != previous_signature
    notify = transition and bool(sendkey)
    notification_error = None
    if notify:
        try:
            if current == "stale":
                detail = combined_alert_detail(
                    health, age, perp_stats, perp_age, failed_components
                )
                if tier_state["active"]:
                    detail += risk_tier_alert_detail(tier_state)
                send_serverchan(sendkey, "CoinPulse 监控失联告警", detail)
            elif previous.get("status") == "stale":
                send_serverchan(
                    sendkey,
                    "CoinPulse 监控恢复（风险档位仍降级）"
                    if tier_state["active"] else "CoinPulse 监控恢复",
                    "监控健康记录已恢复更新，扫描任务重新可用。"
                    + (risk_tier_alert_detail(tier_state) if tier_state["active"] else ""),
                )
            elif current == "degraded":
                send_serverchan(
                    sendkey, "CoinPulse 风险档位降级告警",
                    risk_tier_alert_detail(tier_state),
                )
            elif previous.get("status") == "degraded":
                send_serverchan(
                    sendkey, "CoinPulse 风险档位恢复",
                    "OKX 官方风险档位缓存已恢复健康，研究强平模型不再处于降级状态。",
                )
            else:
                send_serverchan(
                    sendkey, "CoinPulse 监控恢复",
                    "监控健康记录已初始化为正常状态。",
                )
        except (OSError, ValueError, urllib.error.URLError) as exc:
            notification_error = str(exc)
    next_state = {
        "status": current,
        "checked_at_epoch": int(time.time() if now is None else now),
        "health_age_seconds": age,
        "perpetual_age_seconds": perp_age,
        "failed_components": failed_components,
        "advisory_components": advisory_components,
        "risk_tier_status": tier_state["status"],
        "risk_tier_signature": tier_state["signature"],
        "risk_tier_degraded_symbols": tier_state["degraded_symbols"],
        "risk_tier_unavailable_symbols": tier_state["unavailable_symbols"],
        "last_health_status": status,
    }
    if transition or previous.get("last_health_status") != status or not previous:
        atomic_write(ALERT_STATE_PATH, next_state)
    return {
        "status": current, "age_seconds": age,
        "perpetual_age_seconds": perp_age,
        "failed_components": failed_components,
        "advisory_components": advisory_components,
        "risk_tier_status": tier_state["status"],
        "risk_tier_signature": tier_state["signature"],
        "notified": notify, "notification_error": notification_error,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-minutes", type=float, default=20)
    parser.add_argument("--max-perp-age-minutes", type=float, default=30)
    args = parser.parse_args()
    result = check(args.max_age_minutes, args.max_perp_age_minutes,
                   sendkey=os.environ.get("SERVERCHAN_SENDKEY", ""))
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "stale" else 0


if __name__ == "__main__":
    raise SystemExit(main())
