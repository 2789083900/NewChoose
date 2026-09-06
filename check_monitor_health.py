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


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH_PATH = os.path.join(BASE_DIR, "monitor_health.json")
ALERT_STATE_PATH = os.path.join(BASE_DIR, "monitor_alert_state.json")


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


def send_serverchan(sendkey, title, content):
    if not sendkey:
        return False
    url = f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey, safe='')}.send"
    payload = urllib.parse.urlencode({"title": title, "desp": content}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return 200 <= response.status < 300


def check(max_age_minutes=20, now=None, sendkey=""):
    health = load_json(HEALTH_PATH, {})
    age = health_age_seconds(health, now=now)
    status = health.get("status")
    stale = age is None or age > max_age_minutes * 60 or status in {"failed", "stale"}
    current = "stale" if stale else "healthy"
    previous = load_json(ALERT_STATE_PATH, {})
    previous_status = previous.get("status")
    notify = current != previous_status and bool(sendkey)
    notification_error = None
    if notify:
        try:
            if current == "stale":
                detail = "monitor_health.json 未在预期周期内更新，或最近扫描失败。"
                if age is not None:
                    detail += f" 最近更新距今约 {round(age / 60, 1)} 分钟。"
                send_serverchan(sendkey, "CoinPulse 监控失联告警", detail)
            else:
                send_serverchan(sendkey, "CoinPulse 监控恢复", "监控健康记录已恢复更新，扫描任务重新可用。")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            notification_error = str(exc)
    next_state = {
        "status": current,
        "checked_at_epoch": int(time.time() if now is None else now),
        "health_age_seconds": age,
        "last_health_status": status,
    }
    # Persist only transitions (or the initial state) so the health workflow
    # does not create a commit every 15 minutes while nothing changed.
    if previous.get("status") != current or previous.get("last_health_status") != status or not previous:
        atomic_write(ALERT_STATE_PATH, next_state)
    return {"status": current, "age_seconds": age, "notified": notify, "notification_error": notification_error}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-minutes", type=float, default=20)
    args = parser.parse_args()
    result = check(args.max_age_minutes, sendkey=os.environ.get("SERVERCHAN_SENDKEY", ""))
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "stale" else 0


if __name__ == "__main__":
    raise SystemExit(main())
