#!/usr/bin/env python3
"""Validate that the durable notification outbox has no delivery backlog."""

import argparse
import json
import os
import time
from monitor_reporting import REASON_LABELS


DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notification_outbox.json")


def validate(path=DEFAULT_PATH, now_ms=None, health_path=None, config_path=None):
    if not os.path.exists(path):
        result = {"status": "ok", "pending": 0, "expired": 0, "exhausted": 0}
        return _apply_runtime_status(result, health_path, config_path)
    with open(path, encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("notification outbox must contain an events list")
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    pending = []
    expired = 0
    exhausted = 0
    for event in data["events"]:
        if not isinstance(event, dict) or not event.get("event_id"):
            raise ValueError("notification outbox contains an invalid event")
        status = event.get("status")
        if status == "pending" and now_ms <= int(event.get("expires_epoch_ms") or now_ms):
            pending.append(event["event_id"])
        elif status == "expired" or (status == "pending" and now_ms > int(event.get("expires_epoch_ms") or 0)):
            expired += 1
        elif status == "exhausted":
            exhausted += 1
    result = {
        "status": "failed" if pending or exhausted else "ok",
        "pending": len(pending),
        "expired": expired,
        "exhausted": exhausted,
        "pending_event_ids": pending[:20],
    }
    return _apply_runtime_status(result, health_path, config_path)


def _apply_runtime_status(result, health_path, config_path):
    result["notification_queue_status"] = result["status"]
    result["failure_reasons"] = []
    if result.get("pending"):
        result["failure_reasons"].append("notification_backlog")
    if result.get("exhausted"):
        result["failure_reasons"].append("notification_retries_exhausted")
    if health_path and not os.path.exists(health_path):
        result["monitor_health_status"] = "missing"
        result["failure_reasons"].append("monitor_health_missing")
        result["status"] = "failed"
    elif health_path:
        with open(health_path, encoding="utf-8") as file:
            health = json.load(file)
        health_status = health.get("status") if isinstance(health, dict) else "invalid"
        result["monitor_health_status"] = health_status
        result["monitor_health_reasons"] = [r for r in (health.get("health_reasons") or []) if r in REASON_LABELS] if isinstance(health, dict) else []
        if health_status != "ok":
            result["failure_reasons"].append("monitor_health_not_ok")
            result["status"] = "failed"
    if config_path:
        with open(config_path, encoding="utf-8") as file:
            config = json.load(file)
        delivery = config.get("delivery") or {}
        channels = config.get("channels") or {}
        fallbacks = delivery.get("fallback") or []
        if isinstance(fallbacks, str):
            fallbacks = [fallbacks]
        requested = [delivery.get("primary")] + list(fallbacks)
        credential_fields = {
            "serverchan": "sendkey", "pushplus": "token", "dingtalk": "webhook",
            "wecom": "webhook", "bark": "key", "generic": "webhook",
        }
        missing = [name for name in requested if name and not
                   (channels.get(name) or {}).get(credential_fields.get(name, ""))]
        result["missing_delivery_channels"] = missing
        if delivery.get("mode") != "primary_fallback" or missing or not requested[0]:
            result["failure_reasons"].append("notification_configuration_invalid")
            result["status"] = "failed"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--health")
    parser.add_argument("--config")
    args = parser.parse_args()
    try:
        result = validate(args.path, health_path=args.health, config_path=args.config)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        # Never print credentials or malformed JSON contents from config.
        result = {"status": "failed", "failure_reasons": ["validation_input_unreadable_or_invalid"],
                  "error_category": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as file:
            file.write("## CoinPulse 通知队列与运行健康校验\n\n")
            file.write(f"队列：{result.get('notification_queue_status', 'unknown')}；运行健康：{result.get('monitor_health_status', 'unknown')}；最终：{result['status']}\n\n")
            file.write("运行原因：" + "、".join(REASON_LABELS.get(r, r) for r in result.get('monitor_health_reasons', [])) + "\n\n")
            file.write("队列正常不代表扫描及时；信号过期未发送不是渠道失败。运行健康非ok仍保留失败门禁。\n")
    raise SystemExit(1 if result["status"] != "ok" else 0)


if __name__ == "__main__":
    main()
