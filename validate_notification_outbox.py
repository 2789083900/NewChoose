#!/usr/bin/env python3
"""Validate that the durable notification outbox has no delivery backlog."""

import argparse
import json
import os
import time


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
    if not health_path or not os.path.exists(health_path):
        pass
    else:
        with open(health_path, encoding="utf-8") as file:
            health = json.load(file)
        health_status = health.get("status") if isinstance(health, dict) else "invalid"
        result["monitor_health_status"] = health_status
        if health_status != "ok":
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
            result["status"] = "failed"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--health")
    parser.add_argument("--config")
    args = parser.parse_args()
    result = validate(args.path, health_path=args.health, config_path=args.config)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(1 if result["status"] != "ok" else 0)


if __name__ == "__main__":
    main()
