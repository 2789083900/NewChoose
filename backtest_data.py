#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versioned historical OHLCV datasets for reproducible backtests."""

import hashlib
import json
import os
import tempfile
from glob import glob
from datetime import datetime, timezone


SCHEMA_VERSION = 1


def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".coinpulse-data-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def canonical_bars(klines):
    """Normalize bar values so checksums are stable across Python versions."""
    return [
        {
            "time": int(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume") or 0),
        }
        for row in klines
    ]


def bars_checksum(klines):
    payload = json.dumps(
        canonical_bars(klines), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_klines(klines, interval_ms):
    """Return an explicit quality report; callers reject invalid datasets."""
    rows = canonical_bars(klines)
    duplicate_count = 0
    out_of_order_count = 0
    missing_bars = 0
    invalid_ohlc = 0
    for index, row in enumerate(rows):
        if min(row["open"], row["high"], row["low"], row["close"]) <= 0:
            invalid_ohlc += 1
        if row["high"] < max(row["open"], row["close"]) or row["low"] > min(row["open"], row["close"]):
            invalid_ohlc += 1
        if index:
            delta = row["time"] - rows[index - 1]["time"]
            if delta == 0:
                duplicate_count += 1
            elif delta < 0:
                out_of_order_count += 1
            elif delta > interval_ms:
                missing_bars += max(0, delta // interval_ms - 1)
    return {
        "bars": len(rows),
        "start_time": rows[0]["time"] if rows else None,
        "end_time": rows[-1]["time"] if rows else None,
        "duplicate_bars": duplicate_count,
        "out_of_order_bars": out_of_order_count,
        "missing_bars": missing_bars,
        "invalid_ohlc": invalid_ohlc,
        "continuous": not any((duplicate_count, out_of_order_count, missing_bars, invalid_ohlc)),
    }


def dataset_path(cache_dir, symbol, interval, checksum=None):
    """Return a content-addressed snapshot path when a checksum is known.

    Legacy non-versioned paths remain discoverable during the transition.
    """
    suffix = f"-{checksum[:12]}" if checksum else ""
    return os.path.join(cache_dir, f"{symbol}-{interval}{suffix}.json")


def load_dataset(cache_dir, symbol, interval, required_bars):
    candidates = glob(os.path.join(cache_dir, f"{symbol}-{interval}*.json"))
    valid = []
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as file:
                dataset = json.load(file)
            metadata = dataset.get("metadata") or {}
            klines = canonical_bars(dataset.get("klines") or [])
            if (
                metadata.get("schema_version") != SCHEMA_VERSION
                or metadata.get("symbol") != symbol
                or metadata.get("interval") != interval
                or len(klines) < required_bars
                or metadata.get("sha256") != bars_checksum(klines)
            ):
                continue
            metadata = {**metadata, "file": os.path.basename(path)}
            valid.append({"path": path, "metadata": metadata, "klines": klines[-required_bars:]})
        except (OSError, ValueError, TypeError):
            continue
    return max(valid, key=lambda item: item["metadata"].get("downloaded_at_utc", "")) if valid else None


def save_dataset(cache_dir, symbol, interval, source, market_type, klines, interval_ms):
    rows = canonical_bars(klines)
    quality = validate_klines(rows, interval_ms)
    if not quality["continuous"]:
        raise RuntimeError(f"{symbol} {interval} 历史数据质量不合格：{quality}")
    checksum = bars_checksum(rows)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "interval": interval,
        "source": source,
        "market_type": market_type,
        "downloaded_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": checksum,
        "quality": quality,
    }
    path = dataset_path(cache_dir, symbol, interval, checksum)
    metadata["file"] = os.path.basename(path)
    _atomic_write_json(path, {"metadata": metadata, "klines": rows})
    return {"path": path, "metadata": metadata, "klines": rows}


def save_manifest(cache_dir, datasets):
    entries = [
        {
            "file": os.path.basename(item["path"]),
            "symbol": item["metadata"]["symbol"],
            "interval": item["metadata"]["interval"],
            "source": item["metadata"]["source"],
            "market_type": item["metadata"]["market_type"],
            "downloaded_at_utc": item["metadata"].get("downloaded_at_utc"),
            "sha256": item["metadata"]["sha256"],
            "quality": item["metadata"]["quality"],
        }
        for item in sorted(datasets, key=lambda row: (row["metadata"]["symbol"], row["metadata"]["interval"]))
    ]
    _atomic_write_json(os.path.join(cache_dir, "manifest.json"), {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datasets": entries,
    })
