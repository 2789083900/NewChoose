"""Secret-free monitoring semantics shared by scanner, health check and reports."""
import math

REASON_LABELS = {
    "signals_expired_before_dispatch": "信号发现过晚，已按有效期保护跳过发送（不是渠道发送失败）",
    "notification_delivery_failed": "通知投递未成功，需检查渠道或重试队列",
    "notification_events_expired": "通知发件箱事件过期未送达",
    "coverage_insufficient": "行情覆盖率不足",
    "market_data_stale": "部分市场缺少应有的已收盘K线",
    "market_freshness_unknown": "部分市场K线时效无法核实",
    "scan_failed": "扫描执行失败",
    "scan_degraded": "扫描降级（旧格式未记录具体原因）",
}


def notification_round_summary(deliveries):
    rows = list(deliveries or [])
    suppressed = [r for r in rows if r.get("suppressed") == "signal_expired_before_dispatch"
                  or (r.get("expired") and not r.get("event_id") and not r.get("results"))]
    expired = [r for r in rows if r.get("expired") and r not in suppressed]
    failures = [r for r in rows if not r.get("delivered", True) and r not in suppressed and r not in expired]
    results = [x for r in rows if not r.get("deduplicated") for x in (r.get("results") or [])]
    return {
        "round_failed": len(failures), "expired_signals": len(suppressed),
        "expired_notifications": len(expired),
        "channel_attempts": len(results),
        "channel_failures": sum(not r.get("ok", False) for r in results),
    }


def market_freshness(markets, now_ms):
    """UTC epoch-aligned fixed intervals; never treat a bar's whole duration as lag.

    Age since close can legitimately approach one full interval. Overdue measures
    how late the *next* closed bar is, with the existing 30m monitoring tolerance.
    This reports freshness; it does not alter signal validity or entry rules.
    """
    rows = []
    for market in markets or []:
        row = {k: market.get(k) for k in ("symbol", "interval", "bar_open_epoch_ms", "interval_ms", "scope")}
        try:
            opened, interval = float(market["bar_open_epoch_ms"]), float(market["interval_ms"])
            if not all(math.isfinite(x) for x in (opened, interval, now_ms)) or opened <= 0 or interval <= 0:
                raise ValueError("invalid timestamp")
            close = opened + interval
            if close > now_ms or not market.get("closed_only", True):
                raise ValueError("not a confirmed closed bar")
            expected_close = (now_ms // interval) * interval
            overdue = max(0, now_ms - (close + interval)) / 60000
            row.update(bar_close_epoch_ms=int(close),
                       age_since_close_minutes=round((now_ms - close) / 60000, 1),
                       missing_closed_bars=max(0, int((expected_close - close) // interval)),
                       closed_bar_overdue_minutes=round(overdue, 1),
                       status="stale" if overdue > 30 else "fresh")
        except (KeyError, TypeError, ValueError, OverflowError):
            row.update(status="unknown", bar_close_epoch_ms=None,
                       age_since_close_minutes=None, closed_bar_overdue_minutes=None,
                       missing_closed_bars=None)
        rows.append(row)
    valid = [r for r in rows if r["status"] != "unknown"]
    unknown = not rows or len(valid) != len(rows)
    worst = max(valid, key=lambda r: r["age_since_close_minutes"], default=None)
    return {
        "freshness_schema_version": 2, "market_freshness": rows,
        "data_freshness_status": "stale" if any(r["status"] == "stale" for r in rows) else "unknown" if unknown else "fresh",
        "data_lag_basis": "max_age_since_confirmed_bar_close; not transport_delay",
        "data_lag_minutes": None if unknown else (worst["age_since_close_minutes"] if worst else None),
        "max_closed_bar_overdue_minutes": None if unknown else max((r["closed_bar_overdue_minutes"] for r in valid), default=None),
        "data_reference_close_epoch_ms": worst["bar_close_epoch_ms"] if worst and not unknown else None,
    }


def runtime_health_reasons(scan, deliveries):
    reasons = []
    scan = scan or {}
    try:
        if float(scan["coverage_pct"]) < float(scan["minimum_coverage_pct"]) or int(scan.get("failed_markets") or 0):
            reasons.append("coverage_insufficient")
    except (KeyError, TypeError, ValueError):
        pass
    if scan.get("data_freshness_status") == "stale":
        reasons.append("market_data_stale")
    elif scan.get("data_freshness_status") == "unknown":
        reasons.append("market_freshness_unknown")
    counts = notification_round_summary(deliveries)
    if counts["expired_signals"]:
        reasons.append("signals_expired_before_dispatch")
    if counts["round_failed"]:
        reasons.append("notification_delivery_failed")
    if counts["expired_notifications"]:
        reasons.append("notification_events_expired")
    return reasons
