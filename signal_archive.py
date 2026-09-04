#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable, idempotent archival for completed signal-record batches."""

import json
import os
import tempfile
from datetime import datetime, timezone


DEFAULT_LIMIT = 500


def _archive_month(record):
    """Use the signal bar's UTC month so reruns select the same file."""
    try:
        timestamp = int(record.get("signal_bar_time") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp > 0:
        return datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime("%Y-%m")
    return "unknown"


def _load_json_list(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_json_atomically(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".signal-archive-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def archive_and_trim(records, archive_dir, limit=DEFAULT_LIMIT):
    """Archive records that would be trimmed, then return the retained tail.

    Archive files are monthly JSON arrays rather than append-only text so a
    rerun can de-duplicate by signal id after an interrupted workflow run.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    records = list(records or [])
    evicted = records[:-limit]
    retained = records[-limit:]
    groups = {}
    for record in evicted:
        groups.setdefault(_archive_month(record), []).append(record)

    archived = 0
    for month, batch in groups.items():
        path = os.path.join(archive_dir, "signals-%s.json" % month)
        existing = _load_json_list(path)
        ids = {item.get("id") for item in existing if item.get("id")}
        additions = [item for item in batch if not item.get("id") or item.get("id") not in ids]
        if additions:
            _write_json_atomically(path, existing + additions)
            archived += len(additions)
    return retained, archived
