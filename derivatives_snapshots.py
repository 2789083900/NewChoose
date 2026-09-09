#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Content-addressed storage for reproducible perpetual research snapshots."""

import hashlib
import json
import os
from datetime import datetime, timezone
from glob import glob

import backtest_data
import derivatives_data
import signal_watch as sw


SCHEMA_VERSION = 1
SERIES = ("contract_klines", "mark_price_klines", "index_price_klines")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def snapshot_checksum(snapshot):
    payload = {
        "market_type": snapshot.get("market_type"),
        "venue": snapshot.get("venue"),
        "symbol": snapshot.get("symbol"),
        "interval": snapshot.get("interval"),
        "contract_specs": snapshot.get("contract_specs") or {},
        "collection": snapshot.get("collection") or {},
        **{name: snapshot.get(name) or [] for name in SERIES},
        "funding_rates": snapshot.get("funding_rates") or [],
        "open_interest": snapshot.get("open_interest") or [],
    }
    return _checksum(payload)


def _atomic_write(path, value):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, separators=(",", ":"))
        file.write("\n")
    os.replace(temporary, path)


def save_snapshot(cache_dir, snapshot, source="binance-public"):
    symbol = derivatives_data.normalize_symbol(snapshot.get("symbol"))
    interval = derivatives_data.validate_interval(snapshot.get("interval"))
    errors = derivatives_data.validate_perpetual_snapshot(
        snapshot, interval, max_staleness_intervals=10 ** 9
    )
    if errors:
        raise ValueError("invalid perpetual snapshot: " + "; ".join(errors))
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "venue": snapshot.get("venue") or "binance",
        "market_type": derivatives_data.MARKET_TYPE,
        "symbol": symbol,
        "interval": interval,
        "fetched_at_epoch_ms": snapshot.get("fetched_at_epoch_ms"),
        "contract_specs": snapshot.get("contract_specs"),
        "collection": snapshot.get("collection") or {},
        **{name: snapshot.get(name) or [] for name in SERIES},
        "funding_rates": snapshot.get("funding_rates") or [],
        "open_interest": snapshot.get("open_interest") or [],
    }
    digest = snapshot_checksum(normalized)
    quality = {
        name: backtest_data.validate_klines(normalized[name], sw.INTERVAL_MS[interval])
        for name in SERIES
    }
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "interval": interval,
        "venue": normalized["venue"],
        "market_type": normalized["market_type"],
        "source": source,
        "saved_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": digest,
        "series_sha256": {name: _checksum(normalized[name]) for name in SERIES},
        "funding_sha256": _checksum(normalized["funding_rates"]),
        "open_interest_sha256": _checksum(normalized["open_interest"]),
        "quality": quality,
    }
    filename = f"{symbol}-{interval}-{digest[:12]}.json"
    path = os.path.join(cache_dir, filename)
    metadata["file"] = filename
    _atomic_write(path, {"metadata": metadata, "snapshot": normalized})
    return {"path": path, "metadata": metadata, "snapshot": normalized}


def load_snapshot(path):
    with open(path, encoding="utf-8") as file:
        document = json.load(file)
    metadata = document.get("metadata") or {}
    snapshot = document.get("snapshot") or {}
    if metadata.get("schema_version") != SCHEMA_VERSION:
        return None
    if metadata.get("market_type") != derivatives_data.MARKET_TYPE:
        return None
    if metadata.get("symbol") != snapshot.get("symbol") or metadata.get("interval") != snapshot.get("interval"):
        return None
    try:
        errors = derivatives_data.validate_perpetual_snapshot(
            snapshot, snapshot.get("interval"), max_staleness_intervals=10 ** 9
        )
    except (TypeError, ValueError, KeyError):
        return None
    if errors:
        return None
    if metadata.get("sha256") != snapshot_checksum(snapshot):
        return None
    for name in SERIES:
        if metadata.get("series_sha256", {}).get(name) != _checksum(snapshot.get(name) or []):
            return None
    if metadata.get("funding_sha256") != _checksum(snapshot.get("funding_rates") or []):
        return None
    if metadata.get("open_interest_sha256") != _checksum(snapshot.get("open_interest") or []):
        return None
    return {"path": path, "metadata": {**metadata, "file": os.path.basename(path)}, "snapshot": snapshot}


def find_latest(cache_dir, symbol, interval):
    symbol = derivatives_data.normalize_symbol(symbol)
    interval = derivatives_data.validate_interval(interval)
    valid = []
    for path in glob(os.path.join(cache_dir, f"{symbol}-{interval}-*.json")):
        try:
            loaded = load_snapshot(path)
        except (OSError, ValueError, TypeError):
            loaded = None
        if loaded:
            valid.append(loaded)
    return max(valid, key=lambda item: item["metadata"].get("saved_at_utc", "")) if valid else None


def save_manifest(cache_dir, snapshots):
    entries = [
        {key: item["metadata"].get(key) for key in (
            "file", "symbol", "interval", "venue", "market_type", "source",
            "saved_at_utc", "sha256", "quality")}
        for item in sorted(snapshots, key=lambda row: (row["metadata"].get("symbol", ""), row["metadata"].get("interval", "")))
    ]
    _atomic_write(os.path.join(cache_dir, "manifest.json"), {
        "schema_version": SCHEMA_VERSION,
        "market_type": derivatives_data.MARKET_TYPE,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshots": entries,
    })
