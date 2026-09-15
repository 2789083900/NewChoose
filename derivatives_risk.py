#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Risk and cost primitives for linear USDT-margined perpetual research.

This module deliberately contains no exchange client and never submits orders.
It provides deterministic calculations for backtests and shadow simulations.
"""

from dataclasses import dataclass
import math


SUPPORTED_MARKET_TYPES = {"spot", "linear_perpetual", "delivery"}
LIQUIDATION_MODEL_VERSION_FLAT = "isolated_linear_v1"
LIQUIDATION_MODEL_VERSION_TIERED = "isolated_linear_tiered_v1"


def liquidation_model_version(tiers=None):
    return LIQUIDATION_MODEL_VERSION_TIERED if tiers else LIQUIDATION_MODEL_VERSION_FLAT


def validate_market_type(market_type):
    value = str(market_type or "").strip().lower()
    if value not in SUPPORTED_MARKET_TYPES:
        raise ValueError(f"unsupported market_type: {market_type!r}")
    return value


def position_notional(entry_price, quantity):
    price = float(entry_price)
    qty = float(quantity)
    if price <= 0 or qty < 0:
        raise ValueError("entry_price must be > 0 and quantity must be >= 0")
    return price * qty


def margin_required(entry_price, quantity, leverage):
    lev = float(leverage)
    if lev <= 0:
        raise ValueError("leverage must be > 0")
    return position_notional(entry_price, quantity) / lev


def execution_slippage(base_rate, quantity, bar_volume=None, model="fixed",
                       impact_coefficient=0.001, max_rate=0.01):
    """Estimate one-way adverse slippage with an auditable volume-impact model.

    Kline volume is only a coarse liquidity proxy, not an order book. The
    square-root impact term is therefore capped, and missing/invalid volume
    falls back to the configured fixed rate.
    """
    base = float(base_rate)
    cap_value = float(max_rate)
    qty = float(quantity)
    coefficient = float(impact_coefficient)
    if not all(math.isfinite(value) for value in (base, cap_value, qty, coefficient)):
        raise ValueError("slippage inputs must be finite")
    if min(base, cap_value, qty, coefficient) < 0:
        raise ValueError("slippage inputs must be non-negative")
    cap = max(base, cap_value)
    name = str(model or "fixed").strip().lower()
    if name not in {"fixed", "volume_impact"}:
        raise ValueError("slippage model must be fixed or volume_impact")
    try:
        volume = float(bar_volume)
    except (TypeError, ValueError):
        volume = 0.0
    if not math.isfinite(volume) or name == "fixed" or qty <= 0 or volume <= 0:
        return {
            "model": "fixed" if name == "fixed" else "fixed_fallback",
            "rate": min(base, cap), "base_rate": base,
            "impact_rate": 0.0, "participation_rate": None,
        }
    participation = qty / volume
    impact = coefficient * math.sqrt(participation)
    effective = min(cap, base + impact)
    return {
        "model": "volume_impact", "rate": effective, "base_rate": base,
        "impact_rate": max(0.0, effective - base),
        "participation_rate": participation,
    }


def normalize_maintenance_margin_tiers(tiers):
    """Validate and canonicalize notional-based maintenance margin tiers."""
    if tiers in (None, []):
        return []
    if not isinstance(tiers, list):
        raise ValueError("maintenance margin tiers must be a list")
    normalized = []
    for tier in tiers:
        if not isinstance(tier, dict):
            raise ValueError("maintenance margin tiers must be objects")
        cap = tier.get("max_notional", tier.get("notional_cap"))
        rate = tier.get("maintenance_margin_rate", tier.get("rate"))
        try:
            cap_value, rate_value = float(cap), float(rate)
        except (TypeError, ValueError) as exc:
            raise ValueError("maintenance margin tiers require numeric max_notional and rate") from exc
        if (not math.isfinite(cap_value) or not math.isfinite(rate_value)
                or cap_value <= 0 or not 0 <= rate_value < 1):
            raise ValueError("maintenance margin tier values are outside supported bounds")
        normalized.append((cap_value, rate_value))
    normalized.sort()
    caps = [cap for cap, _rate in normalized]
    if len(caps) != len(set(caps)):
        raise ValueError("maintenance margin tier max_notional values must be unique")
    rates = [rate for _cap, rate in normalized]
    if any(rates[index] < rates[index - 1] for index in range(1, len(rates))):
        raise ValueError("maintenance margin tier rates must not decrease")
    return [
        {"max_notional": cap, "maintenance_margin_rate": rate}
        for cap, rate in normalized
    ]


def _tier_maintenance_rate(notional, tiers, default_rate):
    """Select the first maintenance rate whose cap contains ``notional``."""
    normalized = normalize_maintenance_margin_tiers(tiers)
    if not normalized:
        return float(default_rate), "flat"
    value = float(notional)
    for item in normalized:
        cap, rate = item["max_notional"], item["maintenance_margin_rate"]
        if value <= cap:
            return rate, f"tier_{cap:g}"
    last = normalized[-1]
    return last["maintenance_margin_rate"], f"tier_{last['max_notional']:g}_plus"


def liquidation_price(entry_price, direction, leverage, maintenance_margin_rate=0.005,
                      liquidation_fee_rate=0.0, quantity=None,
                      maintenance_margin_tiers=None):
    """Approximate isolated-margin liquidation price for a linear contract.

    The formula is intentionally conservative and exchange-agnostic. Exact
    liquidation prices vary by venue, risk tier, wallet balance and fees; the
    caller must provide the exchange's tier parameters for production-like
    research. ``liquidation_fee_rate`` widens the loss allowance slightly.
    """
    entry = float(entry_price)
    lev = float(leverage)
    mmr = float(maintenance_margin_rate)
    liq_fee = max(0.0, float(liquidation_fee_rate))
    if entry <= 0 or lev <= 0:
        raise ValueError("entry_price and leverage must be > 0")
    if not 0 <= mmr < 1 or liq_fee >= 1:
        raise ValueError("maintenance and liquidation fee rates must be in [0, 1)")
    side = str(direction).strip().lower()
    if side not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    tier_label = "flat"
    if maintenance_margin_tiers:
        if quantity is None:
            raise ValueError("quantity is required when maintenance margin tiers are configured")
        notional = position_notional(entry, quantity)
        mmr, tier_label = _tier_maintenance_rate(notional, maintenance_margin_tiers, mmr)
    # Isolated linear approximation: equity is exhausted at 1/leverage loss
    # after reserving maintenance margin and liquidation fee allowance.
    loss_allowance = (1.0 / lev) - mmr - liq_fee
    if side == "long":
        value = entry * (1.0 - loss_allowance)
    else:
        value = entry * (1.0 + loss_allowance)
    return value


def liquidation_model_metadata(entry_price, quantity, direction, leverage,
                                maintenance_margin_rate=0.005,
                                liquidation_fee_rate=0.0,
                                maintenance_margin_tiers=None):
    """Return auditable parameters used by the research liquidation model."""
    normalized = normalize_maintenance_margin_tiers(maintenance_margin_tiers)
    notional = position_notional(entry_price, quantity)
    mmr, tier = _tier_maintenance_rate(
        notional, normalized,
        maintenance_margin_rate,
    )
    price = liquidation_price(
        entry_price, direction, leverage, maintenance_margin_rate,
        liquidation_fee_rate, quantity=quantity,
        maintenance_margin_tiers=normalized,
    )
    return {
        "model_version": liquidation_model_version(normalized),
        "direction": str(direction).strip().lower(),
        "leverage": float(leverage),
        "liquidation_price": price,
        "maintenance_margin_rate": mmr,
        "liquidation_fee_rate": float(liquidation_fee_rate),
        "notional": notional,
        "maintenance_tier": tier,
        "tier_count": len(normalized),
    }


def funding_payment(notional, funding_rate, direction):
    """Return signed funding cashflow from the trader's perspective.

    Positive funding means longs pay shorts. A positive result is income; a
    negative result is a cost. The calculation is per funding settlement.
    """
    value = float(notional)
    rate = float(funding_rate)
    if value < 0:
        raise ValueError("notional must be >= 0")
    side = str(direction).strip().lower()
    if side not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    signed = -value * rate if side == "long" else value * rate
    return signed


def funding_mark_at_settlement(event, mark_rows, interval_ms, fallback_mark=None):
    """Resolve a funding mark without using a candle that closes after settlement."""
    raw = event.get("mark_price")
    if raw not in (None, ""):
        try:
            value = float(raw)
            if math.isfinite(value) and value > 0:
                return value, False, "settlement_mark"
        except (TypeError, ValueError):
            pass
    event_time = int(event.get("time") or 0)
    duration = int(interval_ms or 0)
    if duration <= 0:
        raise ValueError("interval_ms must be > 0 when estimating a funding mark")
    eligible = [
        row for row in (mark_rows or [])
        if int(row.get("time", 0)) + duration <= event_time
    ]
    if eligible:
        observed = max(eligible, key=lambda row: int(row["time"]))
        try:
            value = float(observed["close"])
            if math.isfinite(value) and value > 0:
                return value, True, "prior_closed_mark_candle"
        except (KeyError, TypeError, ValueError):
            pass
    if fallback_mark not in (None, ""):
        try:
            value = float(fallback_mark)
            if math.isfinite(value) and value > 0:
                return value, True, "current_bar_fallback"
        except (TypeError, ValueError):
            pass
    return None, True, "unavailable"


def adverse_price(entry_price, direction, price_change_pct):
    """Apply an adverse mark-price move to an entry for stress testing."""
    entry = float(entry_price)
    change = abs(float(price_change_pct)) / 100.0
    if entry <= 0:
        raise ValueError("entry_price must be > 0")
    side = str(direction).strip().lower()
    if side == "long":
        return entry * (1.0 - change)
    if side == "short":
        return entry * (1.0 + change)
    raise ValueError("direction must be 'long' or 'short'")


@dataclass(frozen=True)
class PerpetualRiskSnapshot:
    market_type: str
    direction: str
    entry_price: float
    quantity: float
    leverage: float
    notional: float
    initial_margin: float
    liquidation_price: float
    funding_cashflow: float


def build_risk_snapshot(entry_price, quantity, direction, leverage,
                        funding_rate=0.0, maintenance_margin_rate=0.005,
                        liquidation_fee_rate=0.0, maintenance_margin_tiers=None):
    market_type = validate_market_type("linear_perpetual")
    side = str(direction).strip().lower()
    notional = position_notional(entry_price, quantity)
    return PerpetualRiskSnapshot(
        market_type=market_type,
        direction=side,
        entry_price=float(entry_price),
        quantity=float(quantity),
        leverage=float(leverage),
        notional=notional,
        initial_margin=margin_required(entry_price, quantity, leverage),
        liquidation_price=liquidation_price(
            entry_price, side, leverage, maintenance_margin_rate, liquidation_fee_rate,
            quantity=quantity, maintenance_margin_tiers=maintenance_margin_tiers,
        ),
        funding_cashflow=funding_payment(notional, funding_rate, side),
    )
