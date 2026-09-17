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
LIQUIDATION_MODEL_VERSION_TIERED_DEDUCTION = "isolated_linear_tiered_deduction_v2"


def liquidation_model_version(tiers=None):
    # build_stats historically passes [{}] as an explicit tiered-model sentinel.
    if tiers == [{}]:
        return LIQUIDATION_MODEL_VERSION_TIERED
    normalized = normalize_maintenance_margin_tiers(tiers) if tiers else []
    if any(float(item.get("maintenance_amount") or 0) > 0 for item in normalized):
        return LIQUIDATION_MODEL_VERSION_TIERED_DEDUCTION
    return LIQUIDATION_MODEL_VERSION_TIERED if normalized else LIQUIDATION_MODEL_VERSION_FLAT


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
    """Validate notional- or quantity-based maintenance margin tiers."""
    if tiers in (None, []):
        return []
    if not isinstance(tiers, list):
        raise ValueError("maintenance margin tiers must be a list")
    normalized = []
    dimension = None
    for tier in tiers:
        if not isinstance(tier, dict):
            raise ValueError("maintenance margin tiers must be objects")
        notional_cap = tier.get("max_notional", tier.get("notional_cap"))
        quantity_cap = tier.get("max_quantity", tier.get("quantity_cap"))
        if (notional_cap is None) == (quantity_cap is None):
            raise ValueError(
                "maintenance margin tiers require exactly one cap dimension"
            )
        tier_dimension = "notional" if notional_cap is not None else "quantity"
        if dimension is not None and tier_dimension != dimension:
            raise ValueError("maintenance margin tiers must use one cap dimension")
        dimension = tier_dimension
        cap = notional_cap if tier_dimension == "notional" else quantity_cap
        rate = tier.get("maintenance_margin_rate", tier.get("rate"))
        raw_amount = tier.get(
            "maintenance_amount",
            tier.get("maintenance_deduction", tier.get("deduction", tier.get("cum"))),
        )
        try:
            cap_value, rate_value = float(cap), float(rate)
            amount_value = 0.0 if raw_amount in (None, "") else float(raw_amount)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "maintenance margin tiers require numeric cap, rate and maintenance amount"
            ) from exc
        if (not math.isfinite(cap_value) or not math.isfinite(rate_value)
                or not math.isfinite(amount_value) or cap_value <= 0
                or not 0 <= rate_value < 1 or amount_value < 0):
            raise ValueError("maintenance margin tier values are outside supported bounds")
        normalized.append((cap_value, rate_value, amount_value, raw_amount is not None))
    normalized.sort()
    caps = [cap for cap, _rate, _amount, _has_amount in normalized]
    if len(caps) != len(set(caps)):
        raise ValueError("maintenance margin tier max_notional values must be unique")
    rates = [rate for _cap, rate, _amount, _has_amount in normalized]
    if any(rates[index] < rates[index - 1] for index in range(1, len(rates))):
        raise ValueError("maintenance margin tier rates must not decrease")
    if dimension == "notional":
        for cap, rate, amount, _has_amount in normalized:
            if amount > cap * rate:
                raise ValueError(
                    "maintenance amount exceeds maintenance margin at tier cap"
                )
        for previous, current in zip(normalized, normalized[1:]):
            previous_cap, previous_rate, previous_amount, _ = previous
            _current_cap, current_rate, current_amount, _ = current
            previous_requirement = previous_cap * previous_rate - previous_amount
            next_requirement = previous_cap * current_rate - current_amount
            if next_requirement + 1e-12 < previous_requirement:
                raise ValueError(
                    "maintenance margin requirement must not decrease across tiers"
                )
    result = []
    for cap, rate, amount, has_amount in normalized:
        item = {f"max_{dimension}": cap, "maintenance_margin_rate": rate}
        if has_amount or amount > 0:
            item["maintenance_amount"] = amount
        result.append(item)
    return result


def _tier_maintenance_terms(notional, tiers, default_rate, quantity=None):
    """Select rate and quote-currency maintenance deduction for a position."""
    normalized = normalize_maintenance_margin_tiers(tiers)
    if not normalized:
        return float(default_rate), 0.0, "flat"
    dimension = "quantity" if "max_quantity" in normalized[0] else "notional"
    if dimension == "quantity" and quantity is None:
        raise ValueError("quantity is required for quantity-based maintenance tiers")
    value = float(quantity if dimension == "quantity" else notional)
    cap_key = f"max_{dimension}"
    for item in normalized:
        cap = item[cap_key]
        if value <= cap:
            return (
                item["maintenance_margin_rate"],
                float(item.get("maintenance_amount") or 0),
                f"{dimension}_tier_{cap:g}",
            )
    last = normalized[-1]
    return (
        last["maintenance_margin_rate"],
        float(last.get("maintenance_amount") or 0),
        f"{dimension}_tier_{last[cap_key]:g}_plus",
    )


def _tier_maintenance_rate(notional, tiers, default_rate, quantity=None):
    rate, _amount, label = _tier_maintenance_terms(
        notional, tiers, default_rate, quantity=quantity
    )
    return rate, label


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
    maintenance_amount = 0.0
    position_quantity = None
    if maintenance_margin_tiers:
        if quantity is None:
            raise ValueError("quantity is required when maintenance margin tiers are configured")
        position_quantity = float(quantity)
        notional = position_notional(entry, position_quantity)
        mmr, maintenance_amount, tier_label = _tier_maintenance_terms(
            notional, maintenance_margin_tiers, mmr, quantity=position_quantity
        )
    if maintenance_amount > 0:
        # Isolated linear research approximation with a quote-currency
        # maintenance deduction: equity equals mark-notional maintenance plus
        # liquidation fee allowance at the estimated liquidation price.
        deduction_per_unit = maintenance_amount / position_quantity
        if side == "long":
            denominator = 1.0 - mmr - liq_fee
            numerator = entry * (1.0 - 1.0 / lev) - deduction_per_unit
        else:
            denominator = 1.0 + mmr + liq_fee
            numerator = entry * (1.0 + 1.0 / lev) + deduction_per_unit
        if denominator <= 0 or numerator <= 0:
            raise ValueError("maintenance amount produces an invalid liquidation estimate")
        return numerator / denominator
    # Preserve the established v1 approximation when no deduction is supplied.
    loss_allowance = (1.0 / lev) - mmr - liq_fee
    if side == "long":
        return entry * (1.0 - loss_allowance)
    return entry * (1.0 + loss_allowance)


def liquidation_model_metadata(entry_price, quantity, direction, leverage,
                                maintenance_margin_rate=0.005,
                                liquidation_fee_rate=0.0,
                                maintenance_margin_tiers=None):
    """Return auditable parameters used by the research liquidation model."""
    normalized = normalize_maintenance_margin_tiers(maintenance_margin_tiers)
    notional = position_notional(entry_price, quantity)
    mmr, maintenance_amount, tier = _tier_maintenance_terms(
        notional, normalized,
        maintenance_margin_rate, quantity=quantity,
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
        "maintenance_amount": maintenance_amount,
        "maintenance_margin_requirement_at_entry": max(
            0.0, notional * mmr - maintenance_amount
        ),
        "liquidation_fee_rate": float(liquidation_fee_rate),
        "notional": notional,
        "maintenance_tier": tier,
        "maintenance_tier_dimension": (
            "quantity" if normalized and "max_quantity" in normalized[0]
            else "notional" if normalized else "flat"
        ),
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
