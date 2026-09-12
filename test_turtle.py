import unittest
import json
import os
import tempfile
import urllib.parse
from unittest import mock

import signal_watch as sw
import track_signals
import backtest_turtle
import backtest_data
import validate_reports
import validate_runtime_state
import repair_runtime_state
import derivatives_risk
import derivatives_data
import perp_backtest
import derivatives_snapshots
import collect_perp_snapshot
import run_perp_backtest
import perp_shadow
import validate_perp_shadow
import check_monitor_health
from track_signals import build_quality_report
from signal_archive import archive_and_trim


def make_bars(count, close=100.0, high=101.0, low=99.0, start=1):
    return [
        {
            "time": (start + i) * 86400000,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1.0,
        }
        for i in range(count)
    ]


class TurtleCoreTests(unittest.TestCase):
    CONFIRMATION_OFF = {
        "adx_enabled": False,
        "volume_confirmation": False,
        "volatility_filter": False,
    }

    def test_filter_closed_klines_excludes_current_bar(self):
        day = 86400000
        klines = make_bars(2)
        closed = sw.filter_closed_klines(klines, "1d", now_ms=2 * day + 3600000)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["time"], day)

    def test_reliability_filter_defaults_keep_adx_optional(self):
        options = sw.turtle_filter_options({})
        self.assertFalse(options["adx_enabled"])
        self.assertTrue(options["volume_confirmation"])
        self.assertTrue(options["volatility_filter"])

    def test_breakout_requires_buffer_beyond_channel(self):
        klines = make_bars(60)
        klines.append({
            "time": 61 * 86400000,
            "open": 100,
            "high": 101.2,
            "low": 99.5,
            "close": 101.05,
            "volume": 1,
        })
        direction, _, plan = sw.build_turtle_signal(
            klines, "system2", 10000, 0.01, "1d",
            filter_options={"higher_timeframe": False, "breakout_buffer_n": 0.1, **self.CONFIRMATION_OFF}
        )
        self.assertIsNone(direction)
        self.assertEqual(plan["wait"].split()[0], "上破")

    def test_higher_timeframe_trend_uses_ema_direction(self):
        rising = make_bars(210, close=100, high=101, low=99)
        for index, bar in enumerate(rising):
            value = 100 + index * 0.2
            bar["close"] = value
            bar["open"] = value
            bar["high"] = value + 1
            bar["low"] = value - 1
        falling = list(reversed([
            {**bar, "time": (210 - i) * 86400000}
            for i, bar in enumerate(rising)
        ]))
        self.assertEqual(sw.higher_timeframe_trend(rising, 200), "long")
        self.assertEqual(sw.higher_timeframe_trend(falling, 200), "short")

    def test_opposite_higher_timeframe_trend_blocks_breakout(self):
        klines = make_bars(330)
        klines.append({
            "time": 331 * 86400000,
            "open": 100,
            "high": 103,
            "low": 99.5,
            "close": 102,
            "volume": 1,
        })
        direction, reasons, plan = sw.build_turtle_signal(
            klines, "system2", 10000, 0.01, "4h",
            filter_options={"higher_timeframe": True, "breakout_buffer_n": 0.1, **self.CONFIRMATION_OFF},
            higher_trend="short"
        )
        self.assertIsNone(direction)
        self.assertTrue(plan["filtered"])
        self.assertIn("过滤反向突破", reasons[0])

    def test_system2_uses_previous_55_bars_and_20_period_n(self):
        klines = make_bars(60)
        klines.append({
            "time": 61 * 86400000,
            "open": 100,
            "high": 103,
            "low": 99.5,
            "close": 102,
            "volume": 1,
        })
        direction, reasons, plan = sw.build_turtle_signal(
            klines, "system2", 10000, 0.01, "1d", filter_options=self.CONFIRMATION_OFF
        )
        self.assertEqual(direction, "long")
        self.assertGreater(plan["n"], 0)
        self.assertLess(plan["stop"], plan["entry"])
        self.assertGreater(plan["next_add"], plan["entry"])
        self.assertAlmostEqual(plan["unit_quantity"], 10000 * 0.01 / (2 * plan["n"]))

    def test_turtle_unit_quantity_matches_two_n_stop_risk(self):
        self.assertEqual(sw.turtle_unit_quantity(10000, 7.5, 0.01, 2), 10000 * 0.01 / 15)

    def test_intraday_parameters_are_converted_to_bars(self):
        params = sw.turtle_params("system2", "4h")
        self.assertEqual(params["entry_bars"], 330)
        self.assertEqual(params["exit_bars"], 120)
        self.assertEqual(params["n_period"], 120)

    def test_turtle_trade_adds_on_half_n_and_stops_from_latest_unit(self):
        history = make_bars(60)
        history.append({
            "time": 61 * 86400000,
            "open": 100,
            "high": 103,
            "low": 99.5,
            "close": 102,
            "volume": 1,
        })
        direction, _, plan = sw.build_turtle_signal(
            history, "system2", 10000, 0.01, "1d", filter_options=self.CONFIRMATION_OFF
        )
        self.assertEqual(direction, "long")
        trade = {
            "symbol": "TESTUSDT",
            "interval": "1d",
            "direction": "long",
            "entry": plan["entry"],
            "n": plan["n"],
            "unit_quantity": plan["unit_quantity"],
            "system": "system2",
            "strategy_type": "turtle",
            "bar_time": history[-1]["time"],
            "unit_entries": [{"price": plan["entry"], "n": plan["n"]}],
            "units": 1,
            "max_units": 4,
            "add_n": 0.5,
            "stop_n": 2.0,
            "exit_period": 20,
        }
        future = history + [
            {
                "time": 62 * 86400000,
                "open": 102,
                "high": 104.2,
                "low": 101,
                "close": 103,
                "volume": 1,
            },
            {
                "time": 63 * 86400000,
                "open": 103,
                "high": 103,
                "low": 97,
                "close": 99,
                "volume": 1,
            },
        ]
        settled = sw.manage_turtle_trade(trade, future)
        self.assertIsNotNone(settled)
        self.assertEqual(settled["result"], "止损")
        self.assertEqual(settled["units"], 4)
        self.assertLess(settled["pnl_pct"], 0)

    def test_portfolio_capacity_respects_direction_and_group_limits(self):
        state = {
            "open_trades": [
                {"symbol": "BTCUSDT", "direction": "long", "strategy_type": "turtle", "units": 4},
                {"symbol": "ETHUSDT", "direction": "long", "strategy_type": "turtle", "units": 2},
            ]
        }
        config = {
            "strategy": {
                "limits": {
                    "max_symbol_units": 4,
                    "max_strong_group_units": 6,
                    "max_weak_group_units": 10,
                    "max_direction_units": 12,
                },
                "correlation": {"strong_groups": [["BTCUSDT", "ETHUSDT"]]},
            }
        }
        self.assertEqual(sw.turtle_unit_capacity("SOLUSDT", "long", state, config), 4)
        self.assertEqual(sw.turtle_unit_capacity("BTCUSDT", "long", state, config), 0)

    def test_portfolio_capacity_blocks_opposite_position_on_same_symbol(self):
        state = {"open_trades": [{"symbol": "BTCUSDT", "direction": "short", "strategy_type": "turtle", "units": 1}]}
        config = {"strategy": {"limits": {"max_symbol_units": 4, "max_strong_group_units": 6, "max_weak_group_units": 10, "max_direction_units": 12}, "correlation": {"strong_groups": []}}}
        self.assertEqual(sw.turtle_unit_capacity("BTCUSDT", "long", state, config), 0)

    def test_capacity_allocation_is_deterministic_and_applies_total_limit(self):
        config = {"strategy": {"turtle_system": "system2", "limits": {"max_symbol_units": 4, "max_strong_group_units": 6, "max_weak_group_units": 10, "max_direction_units": 1}, "correlation": {"strong_groups": []}}}
        events = [
            {"symbol": "ETHUSDT", "interval": "4h", "direction": "long", "turtle": True, "trade_plan": {"system": "system2"}},
            {"symbol": "BTCUSDT", "interval": "4h", "direction": "long", "turtle": True, "trade_plan": {"system": "system2"}},
        ]
        accepted = sw.allocate_turtle_capacity(events, {"open_trades": []}, config)
        self.assertEqual([event["symbol"] for event in accepted], ["BTCUSDT"])

    def test_realtime_turtle_fill_uses_next_bar_open(self):
        trade = {
            "entry": 100.0, "entry_model": "next_bar_open", "entry_filled": False,
            "bar_time": 60 * 86400000, "interval": "1d", "system": "system2", "direction": "long",
            "n": 2.0, "unit_quantity": 1.0, "unit_entries": [{"price": 100.0, "n": 2.0}],
            "max_units": 1, "add_n": 0.5, "stop_n": 2.0,
        }
        bars = make_bars(60, start=1) + [{"time": 61 * 86400000, "open": 105.0, "high": 106.0, "low": 104.0, "close": 105.0, "volume": 1.0}]
        self.assertIsNone(sw.manage_turtle_trade(trade, bars))
        self.assertTrue(trade["entry_filled"])
        self.assertEqual(trade["entry"], 105.0)
        self.assertEqual(trade["fill_deviation_pct"], 5.0)

    def test_confirmation_filters_block_weak_breakout(self):
        klines = make_bars(60)
        klines.append({
            "time": 61 * 86400000,
            "open": 100,
            "high": 103,
            "low": 99.5,
            "close": 102,
            "volume": 0.1,
        })
        direction, reasons, plan = sw.build_turtle_signal(
            klines, "system2", 10000, 0.01, "1d",
            filter_options={"higher_timeframe": False, "adx_min": 20, "volume_min_ratio": 1.0}
        )
        self.assertIsNone(direction)
        self.assertTrue(plan["filtered"])
        self.assertTrue(any("ADX" in reason or "成交量" in reason for reason in reasons))

    def test_anomaly_filter_blocks_large_gap_and_range(self):
        klines = make_bars(60)
        klines.append({
            "time": 61 * 86400000, "open": 125, "high": 150,
            "low": 95, "close": 130, "volume": 100
        })
        direction, reasons, plan = sw.build_turtle_signal(
            klines, "system2", 10000, 0.01, "1d",
            filter_options={"higher_timeframe": False, "volume_confirmation": False}
        )
        self.assertIsNone(direction)
        self.assertTrue(plan["filtered"])
        self.assertTrue(any("跳空" in reason or "单根波动" in reason for reason in reasons))

    def test_liquidity_filter_uses_quote_volume(self):
        klines = make_bars(60)
        klines.append({
            "time": 61 * 86400000, "open": 100, "high": 130,
            "low": 99, "close": 125, "volume": 1
        })
        direction, reasons, plan = sw.build_turtle_signal(
            klines, "system2", 10000, 0.01, "1d",
            filter_options={"higher_timeframe": False, "volume_confirmation": False,
                            "anomaly_filter": False, "liquidity_filter": True,
                            "min_quote_volume": 1000}
        )
        self.assertIsNone(direction)
        self.assertTrue(any("成交额不足" in reason for reason in reasons))

    def test_settlement_records_net_cost_adjusted_pnl(self):
        trade = {
            "id": "test", "symbol": "BTCUSDT", "interval": "1d",
            "entry": 100.0, "entry_model": "signal_price", "entry_filled": True,
            "entry_ts": 60 * 86400000, "direction": "long", "stop": 95.0,
            "target": 110.0, "fee_rate": 0.001, "slippage_rate": 0.0005,
        }
        state = {"open_trades": [trade], "closed_trades": []}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(sw, "STATE_PATH", os.path.join(directory, "state.json")), \
             mock.patch.object(sw, "fetch_klines_with_fallback", return_value=(
            [{"time": 61 * 86400000, "open": 100, "high": 110, "low": 99, "close": 108, "volume": 1}], "test"
        )):
            production_state = os.path.join(directory, "production-state.json")
            production_stats = os.path.join(directory, "production-stats.json")
            with open(production_state, "w", encoding="utf-8") as file:
                file.write("before")
            with open(production_stats, "w", encoding="utf-8") as file:
                file.write("before")
            settled = sw.settle_trades(state, {}, trade_stats_path=os.path.join(directory, "trade_stats.json"))
            with open(production_state, encoding="utf-8") as file:
                self.assertEqual(file.read(), "before")
            with open(production_stats, encoding="utf-8") as file:
                self.assertEqual(file.read(), "before")
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["gross_pnl_pct"], 10.0)
        self.assertEqual(settled[0]["net_pnl_pct"], 9.7)

    def test_runtime_validator_rejects_test_trade_and_accepts_isolated_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: os.path.join(directory, name) for name in (
                "state.json", "trade.json", "tracking.json", "quality.json", "records.json")}
            state = {"open_trades": [], "closed_trades": [{
                "id": "real-1", "symbol": "BTCUSDT", "interval": "1d", "direction": "long",
                "entry": 100, "entry_ts": 1_700_000_000_000, "status": "closed", "result": "止盈",
                "exit": 110, "closed_at": "2023-11-14 00:00:00", "pnl_pct": 9.7,
            }]}
            trade = {"total": 1, "wins": 1, "losses": 0, "open_count": 0, "trades": [{"id": "real-1"}]}
            tracking = {"total_signals": 0, "pending_signals": 0, "horizons": {"24h": {"observed": 0}, "48h": {"observed": 0}}}
            quality = {"sample": {"closed_trades": 1, "pending_signals": 0}}
            for path, data in ((paths["state.json"], state), (paths["trade.json"], trade),
                               (paths["tracking.json"], tracking), (paths["quality.json"], quality),
                               (paths["records.json"], [])):
                with open(path, "w", encoding="utf-8") as file:
                    json.dump(data, file)
            self.assertEqual(validate_runtime_state.validate_runtime_state(
                paths["state.json"], paths["trade.json"], paths["tracking.json"],
                paths["quality.json"], paths["records.json"]), [])
            state["closed_trades"][0]["id"] = "test"
            with open(paths["state.json"], "w", encoding="utf-8") as file:
                json.dump(state, file)
            self.assertTrue(validate_runtime_state.validate_runtime_state(
                paths["state.json"], paths["trade.json"], paths["tracking.json"],
                paths["quality.json"], paths["records.json"]))

    def test_scan_skips_configured_disabled_symbols_and_reports_reason(self):
        diagnostics = {}
        config = {
            "symbols": ["BTCUSDT", "TONUSDT"],
            "intervals": ["4h"],
            "disabled_symbols": ["TONUSDT"],
            "strategy": {"mode": "turtle", "turtle_system": "system2"},
        }
        with mock.patch.object(sw, "_scan_one", return_value=None) as scan:
            events = sw.scan_once(config, {"open_trades": [], "closed_trades": []}, diagnostics=diagnostics)
        self.assertEqual(events, [])
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(scan.call_args.args[:2], ("BTCUSDT", "4h"))
        self.assertEqual(diagnostics["expected_markets"], 1)
        self.assertEqual(diagnostics["disabled_markets"], 1)
        self.assertEqual(diagnostics["disabled_symbols"], ["TONUSDT"])
        self.assertIn("长期过旧", diagnostics["disabled_reasons"]["TONUSDT"])

    def test_runtime_repair_is_read_only_by_default_and_removes_only_explicit_test_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "signal_watch.state.json")
            state = {"open_trades": [], "closed_trades": [
                {"id": "test", "entry_ts": 1000},
                {"id": "real-1", "entry_ts": 1700000000000},
            ]}
            with open(state_path, "w", encoding="utf-8") as file:
                json.dump(state, file)
            summary = repair_runtime_state.inspect_state(state)
            self.assertEqual(len(summary["closed_test"]), 1)
            repair_runtime_state.repair(directory, apply=False)
            with open(state_path, encoding="utf-8") as file:
                self.assertEqual(len(json.load(file)["closed_trades"]), 2)

    def test_perpetual_risk_snapshot_models_margin_liquidation_and_funding(self):
        snapshot = derivatives_risk.build_risk_snapshot(
            entry_price=100.0, quantity=10.0, direction="long", leverage=2.0,
            funding_rate=0.001, maintenance_margin_rate=0.005,
        )
        self.assertEqual(snapshot.market_type, "linear_perpetual")
        self.assertEqual(snapshot.notional, 1000.0)
        self.assertEqual(snapshot.initial_margin, 500.0)
        self.assertAlmostEqual(snapshot.liquidation_price, 50.5)
        self.assertEqual(snapshot.funding_cashflow, -1.0)

    def test_perpetual_risk_rejects_invalid_market_inputs(self):
        with self.assertRaises(ValueError):
            derivatives_risk.validate_market_type("spot_perpetual")
        with self.assertRaises(ValueError):
            derivatives_risk.liquidation_price(100, "long", 0)
        with self.assertRaises(ValueError):
            derivatives_risk.funding_payment(100, 0.001, "sideways")

    def test_perpetual_public_data_parsing_keeps_market_series_separate(self):
        now = 1_700_020_000_000
        rows = [
            [1_700_000_000_000, "100", "102", "99", "101", "12"],
            [1_700_014_400_000, "101", "103", "100", "102", "13"],
        ]
        def fake_get(url):
            if "fundingRate" in url:
                return [{"fundingTime": 1_700_000_000_000, "fundingRate": "0.0001", "markPrice": "101"}]
            if "openInterestHist" in url:
                return [{"timestamp": 1_700_000_000_000, "sumOpenInterest": "20", "sumOpenInterestValue": "2020"}]
            return rows
        snapshot = derivatives_data.fetch_perpetual_snapshot(
            "btcusdt", "4h", limit=2, http_get=fake_get, closed_only=False
        )
        self.assertEqual(snapshot["market_type"], "linear_perpetual")
        self.assertEqual(snapshot["symbol"], "BTCUSDT")
        self.assertEqual(snapshot["contract_klines"][0]["close"], 101.0)
        self.assertEqual(snapshot["funding_rates"][0]["funding_rate"], 0.0001)
        self.assertEqual(snapshot["open_interest"][0]["open_interest_value"], 2020.0)
        self.assertEqual(
            derivatives_data.validate_perpetual_snapshot(snapshot, "4h", now_ms=now), []
        )

    def test_perpetual_http_defaults_use_extended_timeout_and_retry_budget(self):
        with mock.patch.object(sw, "http_get_json", return_value={}) as getter:
            derivatives_data.perpetual_http_get_json("https://example.test/data")
        getter.assert_called_once_with(
            "https://example.test/data", timeout=derivatives_data.PERPETUAL_HTTP_TIMEOUT,
            attempts=derivatives_data.PERPETUAL_HTTP_ATTEMPTS,
            backoff_seconds=derivatives_data.PERPETUAL_HTTP_BACKOFF_SECONDS,
        )

    def test_perpetual_http_fails_over_to_secondary_binance_endpoint(self):
        with mock.patch.object(sw, "http_get_json", side_effect=[
            TimeoutError("primary unavailable"), {"ok": True}
        ]) as getter:
            result = derivatives_data.perpetual_http_get_json(
                derivatives_data._url("/fapi/v1/time"), attempts=1
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(getter.call_count, 2)
        self.assertTrue(getter.call_args_list[1].args[0].startswith("https://fapi1.binance.com/"))

    def test_perpetual_snapshot_can_report_component_failure_without_hiding_it(self):
        rows = [[1_700_000_000_000, "100", "102", "99", "101", "12"]]
        def partial_get(url):
            if "markPriceKlines" in url:
                raise TimeoutError("mark endpoint timeout")
            if "fundingRate" in url:
                return []
            if "openInterestHist" in url:
                return []
            return rows
        snapshot = derivatives_data.fetch_perpetual_snapshot(
            "BTCUSDT", "4h", limit=1, http_get=partial_get,
            closed_only=False, allow_partial=True,
        )
        self.assertEqual(snapshot["contract_klines"][0]["close"], 101.0)
        self.assertIn("mark_price_klines", snapshot["data_health"]["component_errors"])
        self.assertFalse(snapshot["data_health"]["complete"])

    def test_perpetual_snapshot_validator_rejects_spot_and_bad_ohlc(self):
        bad = {
            "market_type": "spot",
            "contract_klines": [{"time": 1, "open": 100, "high": 90, "low": 80, "close": 100, "volume": 1}],
            "mark_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 0}],
            "index_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 0}],
            "funding_rates": [],
        }
        errors = derivatives_data.validate_perpetual_snapshot(bad, "4h", now_ms=1)
        self.assertTrue(any("market_type" in error for error in errors))
        self.assertTrue(any("quality failed" in error for error in errors))

    def test_perpetual_snapshot_validator_rejects_duplicate_funding_and_invalid_values(self):
        rows = [{"time": 1_700_000_000_000, "open": 100, "high": 101,
                 "low": 99, "close": 100, "volume": 1}]
        bad = {"market_type": "linear_perpetual", "contract_klines": rows,
               "mark_price_klines": rows, "index_price_klines": rows,
               "funding_rates": [{"time": 1, "funding_rate": "nan"},
                                  {"time": 1, "funding_rate": 0.001}],
               "open_interest": [{"time": 1, "open_interest": -1,
                                  "open_interest_value": 1}]}
        errors = derivatives_data.validate_perpetual_snapshot(bad, "4h", now_ms=1)
        self.assertTrue(any("funding_rates contains duplicate" in error for error in errors))
        self.assertTrue(any("invalid rate" in error for error in errors))
        self.assertTrue(any("open_interest contains invalid open_interest" in error for error in errors))

    def test_perpetual_snapshot_rejects_misaligned_market_series(self):
        contract = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                     "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
                    for index in range(3)]
        mark = [dict(row) for row in contract]
        mark[-1]["time"] += 4 * 60 * 60 * 1000
        snapshot = {"market_type": "linear_perpetual", "contract_klines": contract,
                    "mark_price_klines": mark, "index_price_klines": contract,
                    "funding_rates": [], "open_interest": []}
        errors = derivatives_data.validate_perpetual_snapshot(snapshot, "4h", now_ms=1)
        self.assertTrue(any("mark_price_klines timestamps do not align" in error for error in errors))

    def test_perpetual_shadow_state_isolated_from_spot_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"market_type": "spot", "open_trades": []}, file)
            with self.assertRaises(ValueError):
                perp_shadow.load_state(path)

    def test_perpetual_shadow_fills_next_bar_and_uses_mark_price_stop(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
                 "min_notional": 5.0}
        settings = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
            "interval": "4h", "risk_fraction": 0.005, "max_leverage": 2,
            "fee_rate": 0.0004, "slippage_rate": 0.0005,
            "maintenance_margin_rate": 0.005,
        }})
        start = 1_700_000_000_000
        bars = [{"time": start + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
                for index in range(360)]
        marks = [dict(row) for row in bars]
        marks[-2]["close"] = 101
        marks[-1].update({"open": 100, "high": 101, "low": 80, "close": 90})
        indexes = [dict(row) for row in bars]
        indexes[-2]["close"] = 99
        indexes[-1]["close"] = 89
        snapshot = {"market_type": "linear_perpetual", "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": marks,
                    "index_price_klines": indexes, "funding_rates": [],
                    "open_interest": [{"time": bars[-2]["time"], "open_interest": 123,
                                       "open_interest_value": 12300}],
                    "contract_specs": specs}
        state = perp_shadow.empty_state(10000)
        trade = {"id": "perp-test", "market_type": "linear_perpetual", "research_only": True,
                 "symbol": "BTCUSDT", "interval": "4h", "system": "system2", "direction": "long",
                 "status": "pending_entry", "fill_time": bars[-2]["time"],
                 "entry_trigger": 100, "n": 1.0, "risk_fraction": 0.005, "leverage": 2.0}
        state["open_trades"].append(trade)
        result = perp_shadow.update_trade(trade, snapshot, settings, state)
        self.assertEqual(result, "closed")
        self.assertEqual(trade["entry_time"], bars[-2]["time"])
        self.assertEqual(trade["exit_reason"], "stop")
        self.assertLess(trade["exit"], trade["entry"])
        self.assertLess(trade["mae_pct"], 0)
        self.assertEqual(trade["mfe_pct"], 0)
        self.assertEqual(trade["holding_hours"], 4)
        self.assertEqual(trade["entry_market_context"]["mark_close"], 101)
        self.assertEqual(trade["entry_market_context"]["open_interest"], 123)
        self.assertEqual(trade["exit_market_context"]["index_close"], 89)
        self.assertAlmostEqual(trade["entry_market_context"]["basis_pct"], 2.020202)

    def test_perpetual_shadow_excursion_keeps_initial_entry_baseline(self):
        trade = {"direction": "long", "initial_entry": 100, "avg_entry": 105,
                 "mfe_pct": 10, "mae_pct": -1}
        perp_shadow._update_excursion(trade, 120, 90)
        self.assertEqual(trade["mfe_pct"], 20)
        self.assertEqual(trade["mae_pct"], -10)
        perp_shadow._update_exit_excursion(trade, 95)
        self.assertEqual(trade["mfe_pct"], 20)
        self.assertEqual(trade["mae_pct"], -10)

    def test_perpetual_shadow_equity_curve_uses_market_time_and_replaces_duplicates(self):
        state = perp_shadow.empty_state(100)
        state["open_trades"] = [{"status": "open", "direction": "long",
                                 "avg_entry": 100, "quantity": 1,
                                 "last_mark_price": 110}]
        state["last_market_time"] = 1000
        state["updated_at_epoch_ms"] = 999999
        first = perp_shadow.record_equity_snapshot(state)
        self.assertEqual(first["time"], 1000)
        self.assertEqual(first["marked_equity"], 110)
        state["open_trades"][0]["last_mark_price"] = 90
        state["last_market_time"] = 2000
        perp_shadow.record_equity_snapshot(state)
        state["open_trades"][0]["last_mark_price"] = 95
        perp_shadow.record_equity_snapshot(state)
        self.assertEqual(len(state["equity_curve"]), 2)
        self.assertEqual(state["equity_curve"][-1]["marked_equity"], 95)
        self.assertAlmostEqual(perp_shadow.build_stats(state)["max_drawdown_pct"], 13.6364)

    def test_perpetual_shadow_disabled_does_not_write_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            result = perp_shadow.run({"derivatives": {"enabled": False}}, state_path, stats_path)
            self.assertFalse(result["enabled"])
            self.assertFalse(os.path.exists(state_path))
            self.assertFalse(os.path.exists(stats_path))

    def test_perpetual_shadow_stats_report_sample_progress_and_symbol_coverage(self):
        state = perp_shadow.empty_state(10000)
        state["closed_trades"] = [{"net_pnl": 1.0}] * 3
        settings = {"sample_goal_min_trades": 5, "sample_goal_preferred_trades": 10}
        stats = perp_shadow.build_stats(state, settings)
        self.assertEqual(stats["sample_progress_pct"], 30.0)
        self.assertEqual(stats["sample_next_milestone"], "minimum_goal")
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            state["closed_trades"] = []
            result = perp_shadow.run(
                {"derivatives": {"enabled": True, "research_only": True, "symbols": ["BTCUSDT"], "interval": "4h"}},
                state_path, stats_path,
                fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary data failure")),
            )
            self.assertEqual(result["stats"]["successful_symbols"], [])
            self.assertEqual(result["stats"]["requested_symbols"], ["BTCUSDT"])
            self.assertEqual(result["stats"]["sample_next_milestone"], "minimum_goal")

    def test_perpetual_shadow_tracks_data_availability_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            config = {"derivatives": {"enabled": True, "research_only": True, "symbols": ["BTCUSDT"], "interval": "4h"}}
            failing = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("timeout"))
            first = perp_shadow.run(config, state_path, stats_path, fetcher=failing)
            second = perp_shadow.run(config, state_path, stats_path, fetcher=failing)
            self.assertEqual(first["stats"]["data_status"], "unavailable")
            self.assertEqual(second["stats"]["consecutive_unavailable_runs"], 2)
            self.assertEqual(second["stats"]["run_count"], 2)

    def test_perpetual_shadow_rejects_a_missed_next_bar_entry(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
                 "min_notional": 5.0}
        settings = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"], "interval": "4h"
        }})
        start = 1_700_000_000_000
        bars = [{"time": start + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
                for index in range(3)]
        snapshot = {"market_type": "linear_perpetual", "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": [],
                    "open_interest": [], "contract_specs": specs}
        state = perp_shadow.empty_state()
        trade = {"id": "missed", "market_type": "linear_perpetual", "research_only": True,
                 "symbol": "BTCUSDT", "interval": "4h", "system": "system2",
                 "direction": "long", "status": "pending_entry",
                 "fill_time": start - 4 * 60 * 60 * 1000, "n": 1.0,
                 "risk_fraction": 0.005, "leverage": 2.0}
        state["open_trades"].append(trade)
        self.assertEqual(perp_shadow.update_trade(trade, snapshot, settings, state), "rejected")
        self.assertEqual(trade["rejection_reason"], "entry_window_missed")

    def test_perpetual_shadow_validator_rejects_spot_or_test_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            state = perp_shadow.empty_state()
            state["closed_trades"] = [{
                "id": "test", "market_type": "spot", "research_only": True,
                "status": "closed", "entry": 100, "quantity": 1,
                "leverage": 2, "fees": 1, "funding_cashflow": 0,
            }]
            state["seen_signal_ids"] = ["test"]
            stats = perp_shadow.build_stats(state)
            with open(state_path, "w", encoding="utf-8") as file:
                json.dump(state, file)
            with open(stats_path, "w", encoding="utf-8") as file:
                json.dump(stats, file)
            errors = validate_perp_shadow.validate(state_path, stats_path)
        self.assertTrue(any("invalid id" in error for error in errors))
        self.assertTrue(any("mixed market_type" in error for error in errors))

    def test_perpetual_shadow_full_cycle_writes_valid_isolated_files(self):
        interval_ms = 4 * 60 * 60 * 1000
        latest_open = (int(sw.time.time() * 1000) // interval_ms - 1) * interval_ms
        bars = [{"time": latest_open - (339 - index) * interval_ms,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}
                for index in range(340)]
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
                 "min_notional": 5.0}
        snapshot = {"market_type": "linear_perpetual", "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": [dict(row) for row in bars],
                    "index_price_klines": [dict(row) for row in bars], "funding_rates": [],
                    "open_interest": [], "contract_specs": specs}
        config = {"derivatives": {"enabled": True, "research_only": True,
                                   "symbols": ["BTCUSDT"], "interval": "4h"}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(sw, "build_turtle_signal", return_value=(None, [], None)):
            state_path = os.path.join(directory, "perp-state.json")
            stats_path = os.path.join(directory, "perp-stats.json")
            result = perp_shadow.run(
                config, state_path, stats_path,
                fetcher=lambda *_args, **_kwargs: snapshot,
            )
            errors = validate_perp_shadow.validate(state_path, stats_path)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["processed_symbols"], 1)
        self.assertEqual(errors, [])

    def test_perpetual_shadow_trade_uses_frozen_parameters_after_config_changes(self):
        old = {"account_value": 10000.0, "risk_fraction": 0.005, "leverage": 2.0,
               "maintenance_margin_rate": 0.005, "liquidation_fee_rate": 0.0,
               "fee_rate": 0.0004, "slippage_rate": 0.0005,
               "max_total_open_risk": 0.03, "interval": "4h", "system": "system2",
               "max_units": 4, "add_n": 0.5, "stop_n": 2.0, "filters": {},
               "execution_model": "signal_close_next_contract_bar_open",
               "risk_price": "mark_price", "margin_mode": "isolated_approximation"}
        trade = {"market_type": "linear_perpetual", "research_only": True,
                 "status": "open", "parameter_snapshot": old,
                 "parameter_sha256": perp_shadow.parameter_checksum(old)}
        current = dict(old, fee_rate=0.02, leverage=10.0)
        self.assertEqual(perp_shadow._trade_settings(trade, current), old)

    def test_perpetual_shadow_rejects_gap_in_open_position_history(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
                 "min_notional": 5.0}
        settings = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"], "interval": "4h"
        }})
        interval = 4 * 60 * 60 * 1000
        start = 1_700_000_000_000
        times = [start, start + interval, start + 3 * interval]
        bars = [{"time": value, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
                for value in times]
        snapshot = {"market_type": "linear_perpetual", "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": [dict(row) for row in bars],
                    "index_price_klines": [dict(row) for row in bars], "funding_rates": [],
                    "open_interest": [], "contract_specs": specs}
        trade = {"market_type": "linear_perpetual", "research_only": True, "status": "open",
                 "symbol": "BTCUSDT", "interval": "4h", "system": "system2", "direction": "long",
                 "entry_time": start, "avg_entry": 100, "quantity": 1, "leverage": 2,
                 "stop": 90, "n": 1, "latest_entry": 100, "units": [{"price": 100}],
                 "fees": 0, "funding_cashflow": 0, "last_funding_time": 0,
                 "last_processed_bar": start + interval,
                 "parameter_snapshot": perp_shadow.parameter_snapshot(settings),
                 "parameter_sha256": perp_shadow.parameter_checksum(perp_shadow.parameter_snapshot(settings)),
                 "contract_specs": specs}
        state = perp_shadow.empty_state()
        with self.assertRaises(RuntimeError):
            perp_shadow.update_trade(trade, snapshot, settings, state)

    def test_perpetual_data_rejects_invalid_symbol_interval_and_limit(self):
        with self.assertRaises(ValueError):
            derivatives_data.normalize_symbol("BTC/USDT")
        with self.assertRaises(ValueError):
            derivatives_data.validate_interval("8h")
        with self.assertRaises(ValueError):
            derivatives_data.fetch_funding_rates("BTCUSDT", limit=1001, http_get=lambda _url: [])

    def test_perpetual_kline_history_pages_and_deduplicates(self):
        interval = 4 * 60 * 60 * 1000
        pages = {
            None: [[3 * interval, "103", "104", "102", "103", "1"],
                   [2 * interval, "102", "103", "101", "102", "1"]],
            2 * interval - 1: [[2 * interval, "102", "103", "101", "102", "1"],
                                [1 * interval, "101", "102", "100", "101", "1"]],
        }
        def fake_get(url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            end = int(query["endTime"][0]) if "endTime" in query else None
            return pages.get(end, [])
        rows = derivatives_data.fetch_kline_history(
            "contract", "BTCUSDT", "4h", limit=3, http_get=fake_get, closed_only=False
        )
        self.assertEqual([row["time"] for row in rows], [interval, 2 * interval, 3 * interval])

    def test_perpetual_exchange_info_parses_contract_constraints(self):
        document = {"symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "baseAsset": "BTC", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]}
        specs = derivatives_data.parse_contract_specs(document, "btcusdt")
        self.assertEqual(specs["contract_type"], "PERPETUAL")
        self.assertEqual(specs["price_tick"], 0.1)
        self.assertEqual(specs["quantity_step"], 0.001)
        self.assertEqual(specs["min_notional"], 5.0)
        self.assertEqual(derivatives_data.quantize_quantity(1.2349, specs), 1.234)
        self.assertEqual(derivatives_data.quantize_price(100.01, specs, "buy"), 100.1)
        self.assertEqual(derivatives_data.quantize_price(100.09, specs, "sell"), 100.0)
        self.assertTrue(derivatives_data.quantity_is_executable(0.05, 100, specs))
        self.assertFalse(derivatives_data.quantity_is_executable(0.001, 100, specs))

    def test_perpetual_snapshot_rejects_mismatched_contract_specs(self):
        rows = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100 + index, "high": 101 + index,
                 "low": 99 + index, "close": 100 + index, "volume": 1}
                for index in range(3)]
        snapshot = {"market_type": "linear_perpetual", "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": rows, "mark_price_klines": rows,
                    "index_price_klines": rows, "funding_rates": [],
                    "contract_specs": {"symbol": "ETHUSDT", "contract_type": "PERPETUAL",
                                       "quote_asset": "USDT", "price_tick": 0.1,
                                       "quantity_step": 0.001, "min_quantity": 0.001,
                                       "min_notional": 5}}
        errors = derivatives_data.validate_perpetual_snapshot(snapshot, "4h", now_ms=1)
        self.assertIn("contract_specs symbol mismatch", errors)

    def test_perpetual_backtest_returns_cost_and_liquidation_metrics(self):
        bars = []
        for index in range(360):
            price = 100 + index * 0.2
            bars.append({"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                         "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 100})
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": [],}
        result = perp_backtest.backtest_perpetual(
            snapshot, account_value=10000, risk_fraction=0.005,
            leverage=2, fee_rate=0.0004, slippage_rate=0.0005,
        )
        self.assertEqual(result["market_type"], "linear_perpetual")
        self.assertIn("max_drawdown_pct", result)
        self.assertIn("funding_cashflow", result)
        self.assertIn("liquidation_count", result)
        stress = perp_backtest.run_cost_stress_tests(
            snapshot, account_value=10000, risk_fraction=0.005, leverage=2,
            fee_rate=0.0004, slippage_rate=0.0005,
        )
        self.assertIn("baseline", stress)
        self.assertIn("double_cost", stress)
        self.assertIn("quadruple_cost", stress)
        self.assertIn("summary", stress)

    def test_perpetual_entry_fills_on_the_next_bar_open(self):
        bars = []
        for index in range(334):
            price = 100 + index * 0.01
            bars.append({"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                         "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 100})
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        plan = {"n": 1.0, "stop": 90.0, "exit_level": 200.0}
        signal = ("long", ["test breakout"], plan)
        with mock.patch.object(sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 10):
            result = perp_backtest.backtest_perpetual(
                snapshot, account_value=10000, risk_fraction=0.005,
                leverage=2, fee_rate=0.0004, slippage_rate=0.0005,
            )
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["entry_time"], bars[331]["time"])
        self.assertAlmostEqual(result["trades"][0]["entry"], bars[331]["open"] * 1.0005)

    def test_perpetual_channel_exit_uses_the_correct_side_of_mark_bar(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
                for index in range(334)]
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("long", ["test breakout"], {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        levels = {"exit_low": 50.0, "exit_high": 150.0}
        with mock.patch.object(sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 10), \
             mock.patch.object(sw, "turtle_levels", return_value=levels):
            result = perp_backtest.backtest_perpetual(snapshot, fee_rate=0, slippage_rate=0)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["reason"], "end_of_test")

    def test_perpetual_funding_flip_stress_reverses_cashflow(self):
        bars = []
        for index in range(360):
            price = 100 + index * 0.2
            bars.append({"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                         "open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 100})
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars,
                    "funding_rates": [{"time": bars[-1]["time"], "funding_rate": 0.001}],}
        result = perp_backtest.run_funding_flip_stress(
            snapshot, account_value=10000, risk_fraction=0.005, leverage=2,
            fee_rate=0.0004, slippage_rate=0.0005,
        )
        self.assertEqual(result["funding_delta"], 0.0)

    def test_perpetual_snapshot_storage_is_content_addressed_and_detects_tampering(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100 + index, "high": 101 + index,
                 "low": 99 + index, "close": 100 + index, "volume": 1}
                for index in range(3)]
        snapshot = {"market_type": "linear_perpetual", "venue": "binance",
                    "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": [],
                    "open_interest": []}
        with tempfile.TemporaryDirectory() as directory:
            saved = derivatives_snapshots.save_snapshot(directory, snapshot)
            loaded = derivatives_snapshots.load_snapshot(saved["path"])
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["snapshot"]["market_type"], "linear_perpetual")
            with open(saved["path"], encoding="utf-8") as file:
                document = json.load(file)
            document["snapshot"]["contract_klines"][0]["close"] = 999
            with open(saved["path"], "w", encoding="utf-8") as file:
                json.dump(document, file)
            self.assertIsNone(derivatives_snapshots.load_snapshot(saved["path"]))

    def test_collect_perpetual_snapshot_validates_and_writes_manifest(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100 + index, "high": 101 + index,
                 "low": 99 + index, "close": 100 + index, "volume": 1}
                for index in range(3)]
        snapshot = {"market_type": "linear_perpetual", "venue": "binance",
                    "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": [],
                    "open_interest": []}
        with tempfile.TemporaryDirectory() as directory:
            saved = collect_perp_snapshot.collect(
                "BTCUSDT", "4h", limit=3, output_dir=directory,
                allow_stale=True,
                fetcher=lambda *_args, **_kwargs: snapshot,
            )
            self.assertTrue(os.path.isfile(saved["path"]))
            self.assertTrue(os.path.isfile(os.path.join(directory, "manifest.json")))
            with open(os.path.join(directory, "manifest.json"), encoding="utf-8") as file:
                manifest = json.load(file)
            self.assertEqual(manifest["market_type"], "linear_perpetual")
            self.assertEqual(len(manifest["snapshots"]), 1)

    def test_collect_perpetual_snapshot_rejects_stale_data_before_writing(self):
        bars = [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}]
        snapshot = {"market_type": "linear_perpetual", "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": [], "open_interest": []}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                collect_perp_snapshot.collect(
                    "BTCUSDT", "4h", limit=1, output_dir=directory,
                    fetcher=lambda *_args, **_kwargs: snapshot,
                )
            self.assertEqual(os.listdir(directory), [])

    def test_perpetual_backtest_cli_binds_report_to_snapshot_checksum(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100 + index * 0.1, "high": 101 + index * 0.1,
                 "low": 99 + index * 0.1, "close": 100 + index * 0.1,
                 "volume": 1} for index in range(360)]
        snapshot = {"market_type": "linear_perpetual", "venue": "binance",
                    "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": [],
                    "open_interest": []}
        with tempfile.TemporaryDirectory() as directory:
            derivatives_snapshots.save_snapshot(directory, snapshot)
            output = os.path.join(directory, "report.json")
            report = run_perp_backtest.run("BTCUSDT", "4h", directory, output)
            self.assertEqual(report["market_type"], "linear_perpetual")
            self.assertEqual(len(report["input_snapshot"]["sha256"]), 64)
            self.assertFalse(report["risk_model"]["contract_constraints_bound"])
            self.assertEqual(report["risk_model"]["liquidation_model"], "exchange_agnostic_approximation")
            self.assertTrue(os.path.isfile(output))

    def test_shadow_tracking_calculates_long_mfe_mae_and_return(self):
        hour = 60 * 60 * 1000
        record = {
            "signal_bar_time": hour,
            "interval": "1h",
            "direction": "long",
            "entry_price": 100,
        }
        klines = [
            {"time": 2 * hour, "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1},
            {"time": 3 * hour, "open": 105, "high": 108, "low": 98, "close": 102, "volume": 1},
        ]
        result = track_signals.evaluate_horizon(
            record, klines, "24h", now_ms=26 * hour
        )
        self.assertEqual(result["outcome"], "WIN")
        self.assertEqual(result["entry_price"], 100.0)
        self.assertEqual(result["entry_model"], "next_bar_open")
        self.assertEqual(result["mfe_pct"], 10.0)
        self.assertEqual(result["mae_pct"], -5.0)
        self.assertEqual(result["final_return_pct"], 2.0)

    def test_backtest_slippage_is_adverse_for_both_directions(self):
        self.assertEqual(backtest_turtle.execution_price(100, "long", "entry", 0.01), 101)
        self.assertEqual(backtest_turtle.execution_price(100, "long", "exit", 0.01), 99)
        self.assertEqual(backtest_turtle.execution_price(100, "short", "entry", 0.01), 99)
        self.assertEqual(backtest_turtle.execution_price(100, "short", "exit", 0.01), 101)

    def test_gap_adjusted_trigger_never_assumes_a_skipped_price(self):
        down_gap = {"open": 90}
        up_gap = {"open": 110}
        self.assertEqual(backtest_turtle.gap_adjusted_trigger(down_gap, "long", "exit", 100), 90)
        self.assertEqual(backtest_turtle.gap_adjusted_trigger(up_gap, "short", "exit", 100), 110)
        self.assertEqual(backtest_turtle.gap_adjusted_trigger(up_gap, "long", "entry", 100), 110)
        self.assertEqual(backtest_turtle.gap_adjusted_trigger(down_gap, "short", "entry", 100), 90)

    def test_backtest_dataset_round_trip_preserves_checksum_and_quality(self):
        bars = make_bars(4)
        with tempfile.TemporaryDirectory() as directory:
            saved = backtest_data.save_dataset(
                directory, "TESTUSDT", "1d", "test", "spot", bars, 86400000
            )
            loaded = backtest_data.load_dataset(directory, "TESTUSDT", "1d", 4)
        self.assertIsNotNone(loaded)
        self.assertEqual(saved["metadata"]["sha256"], loaded["metadata"]["sha256"])
        self.assertEqual(len(loaded["metadata"]["sha256"]), 64)
        self.assertTrue(loaded["metadata"]["quality"]["continuous"])

    def test_backtest_dataset_quality_rejects_duplicate_and_invalid_ohlc(self):
        bars = make_bars(3)
        bars[1]["time"] = bars[0]["time"]
        bars[2]["high"] = 90.0
        quality = backtest_data.validate_klines(bars, 86400000)
        self.assertFalse(quality["continuous"])
        self.assertGreater(quality["duplicate_bars"], 0)
        self.assertGreater(quality["invalid_ohlc"], 0)

    def test_backtest_dataset_checksum_corruption_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = backtest_data.save_dataset(
                directory, "TESTUSDT", "1d", "test", "spot", make_bars(3), 86400000
            )
            with open(saved["path"], encoding="utf-8") as file:
                document = json.load(file)
            document["klines"][0]["close"] = 999.0
            with open(saved["path"], "w", encoding="utf-8") as file:
                json.dump(document, file)
            self.assertIsNone(backtest_data.load_dataset(directory, "TESTUSDT", "1d", 3))

    def test_backtest_dataset_refresh_keeps_prior_snapshot_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            first = backtest_data.save_dataset(
                directory, "TESTUSDT", "1d", "test", "spot", make_bars(3), 86400000
            )
            changed = make_bars(3, close=101.0)
            second = backtest_data.save_dataset(
                directory, "TESTUSDT", "1d", "test", "spot", changed, 86400000
            )
            loaded = backtest_data.load_dataset(directory, "TESTUSDT", "1d", 3)
            files = [name for name in os.listdir(directory) if name.startswith("TESTUSDT-1d-")]
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(files), 2)
        self.assertEqual(loaded["metadata"]["sha256"], second["metadata"]["sha256"])

    def test_backtest_dataset_refresh_uses_save_sequence_when_timestamps_match(self):
        class FixedDateTime:
            @classmethod
            def now(cls, _timezone):
                class FixedValue:
                    def isoformat(self, timespec=None):
                        return "2026-09-10T00:00:00.000000+00:00"
                return FixedValue()

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(backtest_data.time, "time_ns", side_effect=[100, 200]), \
             mock.patch.object(backtest_data, "datetime", FixedDateTime):
            first = backtest_data.save_dataset(
                directory, "TESTUSDT", "1d", "test", "spot", make_bars(3), 86400000
            )
            second = backtest_data.save_dataset(
                directory, "TESTUSDT", "1d", "test", "spot", make_bars(3, close=101.0), 86400000
            )
            loaded = backtest_data.load_dataset(directory, "TESTUSDT", "1d", 3)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(loaded["metadata"]["sha256"], second["metadata"]["sha256"])

    def test_rolling_validation_does_not_select_variants(self):
        class Args:
            capital = 10000.0
            fee_rate = 0.001
            slippage = 0.0005
            risk_fraction = 0.01
            rolling_train_bars = 400
            rolling_test_bars = 20
            rolling_step_bars = 20

        bars = make_bars(440, start=1)
        daily = make_bars(420, start=1)
        report = backtest_turtle.rolling_validation(
            bars, daily, backtest_turtle.backtest_variants()["production_default"], Args()
        )
        self.assertTrue(report["enabled"])
        self.assertEqual(len(report["windows"]), 2)
        self.assertIn("baseline_cost", report["windows"][0]["validation"])
        self.assertIn("quadruple_cost", report["windows"][0]["validation"])

    def test_portfolio_rolling_validation_uses_shared_capital_windows(self):
        class Args:
            capital = 10000.0
            fee_rate = 0.001
            slippage = 0.0005
            risk_fraction = 0.01
            rolling_train_bars = 400
            rolling_test_bars = 20
            rolling_step_bars = 20
            portfolio_max_symbol_units = 4
            portfolio_max_total_units = 12
            portfolio_max_direction_units = 12
            portfolio_max_strong_group_units = 6
            portfolio_max_weak_group_units = 10
            portfolio_strong_groups = [["BTCUSDT", "ETHUSDT"]]
            portfolio_weak_groups = []

        market_data = {
            "BTCUSDT": (make_bars(440), make_bars(420)),
            "ETHUSDT": (make_bars(440), make_bars(420)),
        }
        report = backtest_turtle.portfolio_rolling_validation(
            market_data, backtest_turtle.backtest_variants()["production_default"], Args()
        )
        self.assertTrue(report["enabled"])
        self.assertEqual(report["symbols"], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(len(report["windows"]), 2)
        metrics = report["windows"][0]["validation"]["baseline_cost"]
        self.assertIn("risk_limits", metrics)
        self.assertEqual(metrics["risk_limits"]["max_total_units"], 12)
        self.assertIn("quadruple_cost", report["windows"][0]["validation"])

    def test_portfolio_risk_snapshot_reports_units_and_stop_risk(self):
        state = {"open_trades": [{
            "symbol": "BTCUSDT", "direction": "long", "strategy_type": "turtle",
            "units": 2, "unit_quantity": 3, "stop": 90,
            "unit_entries": [{"price": 100}, {"price": 105}], "entry_filled": True,
        }]}
        config = {"strategy": {"limits": {"max_symbol_units": 4, "max_direction_units": 12}}}
        snapshot = sw.portfolio_risk_snapshot(state, config)
        self.assertEqual(snapshot["total_units"], 2)
        self.assertEqual(snapshot["long_units"], 2)
        self.assertEqual(snapshot["remaining_long_capacity"], 10)
        self.assertEqual(snapshot["estimated_stop_risk"], 75.0)
        self.assertEqual(snapshot["symbols"][0]["remaining_symbol_capacity"], 2)

    def test_signal_quality_report_marks_small_samples_unreliable(self):
        records = [
            {"status": "complete", "horizons": {"24h": {"outcome": "WIN", "final_return_pct": 1.2}, "48h": {"outcome": "LOSS", "final_return_pct": -0.5}}}
        ]
        report = build_quality_report(records, {"total": 1, "wins": 0, "losses": 1, "win_rate": 0, "payoff": 0})
        self.assertEqual(report["sample"]["reliability"], "insufficient_sample")
        self.assertEqual(report["horizons"]["24h"]["win_rate"], 100.0)
        self.assertLess(report["horizons"]["24h"]["win_rate_95_ci"]["low"], 100.0)

    def test_http_request_retries_transient_failures(self):
        first_error = sw.urllib.error.URLError("temporary")
        response = object()
        with mock.patch.object(sw.urllib.request, "urlopen", side_effect=[first_error, response]) as opener, \
             mock.patch.object(sw.time, "sleep") as sleeper:
            result = sw._urlopen_with_retry("request", 1)
        self.assertIs(result, response)
        self.assertEqual(opener.call_count, 2)
        sleeper.assert_called_once()

    def test_scan_coverage_gate_rejects_partial_market_scan(self):
        ok, coverage, minimum = sw.scan_coverage_ok(
            {"minimum_scan_coverage_pct": 80},
            {"expected_markets": 10, "successful_markets": 7},
        )
        self.assertFalse(ok)
        self.assertEqual(coverage, 70.0)
        self.assertEqual(minimum, 80.0)

    def test_backtest_report_gate_rejects_missing_portfolio_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"generated_at": "now", "interval": "4h", "system": "system2", "data_snapshot": {"datasets": {"BTCUSDT": {"4h": {"sha256": "0" * 64}}}}, "results": {"BTCUSDT": {}}, "portfolio": {"rolling_validation": {"enabled": False}}, "errors": {}}, file)
            errors = validate_reports.validate_backtest_report(path)
        self.assertTrue(any("组合滚动验证" in error for error in errors))

    def test_backtest_report_gate_rejects_schema_and_disabled_symbol_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            snapshot_file = os.path.join(directory, "snapshot.json")
            with open(snapshot_file, "w", encoding="utf-8") as file:
                file.write("{}")
            report = {
                "report_schema_version": 2, "generated_at": "now", "interval": "4h", "system": "system2",
                "sample_reliability_threshold_trades": 10, "symbols_disabled": ["TONUSDT"],
                "data_snapshot": {"directory": ".", "datasets": {"TONUSDT": {"4h": {"sha256": "0" * 64, "file": "snapshot.json"}}}},
                "results": {"TONUSDT": {"full": {"production_default": {"trades": 1, "sample_reliability": "insufficient_sample"}}}},
                "portfolio": {"rolling_validation": {"enabled": True, "windows": [{}]}},
                "errors": {},
            }
            with open(path, "w", encoding="utf-8") as file:
                json.dump(report, file)
            errors = validate_reports.validate_backtest_report(path)
        self.assertTrue(any("停用币种仍出现在" in error for error in errors))

    def test_backtest_load_disabled_symbols_prefers_configured_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"disabled_symbols": ["TONUSDT", " tonusdt ", ""]}, file)
            self.assertEqual(backtest_turtle.load_disabled_symbols(path), {"TONUSDT"})

    def test_backtest_metrics_mark_small_samples_unreliable(self):
        klines = make_bars(440)
        daily = make_bars(420)
        metrics = backtest_turtle.simulate(
            klines, daily, backtest_turtle.backtest_variants()["base"],
        )
        self.assertEqual(metrics["sample_reliability"], "insufficient_sample")
        self.assertIn("至少需要", metrics["sample_reliability_note"])

    def test_backtest_history_fetch_buffers_the_open_candle(self):
        class Args:
            refresh_data = True

        required = 10
        with tempfile.TemporaryDirectory() as directory:
            Args.data_dir = directory

            def fetch(_symbol, _interval, total):
                return make_bars(total)

            with mock.patch.object(sw, "fetch_binance_history", side_effect=fetch) as history, \
                 mock.patch.object(sw, "filter_closed_klines", side_effect=lambda rows, _interval: rows[:-1]):
                rows, _metadata = backtest_turtle.load_or_download_dataset(
                    "BTCUSDT", "1d", required, Args()
                )

        history.assert_called_once_with(
            "BTCUSDT", "1d", required + backtest_turtle.HISTORY_FETCH_BUFFER_BARS
        )
        self.assertEqual(len(rows), required)

    def test_signal_archive_preserves_trimmed_records_without_duplicates(self):
        records = [
            {"id": "old", "signal_bar_time": 1704067200000},
            {"id": "new-1", "signal_bar_time": 1706745600000},
            {"id": "new-2", "signal_bar_time": 1706745600000},
        ]
        with tempfile.TemporaryDirectory() as directory:
            kept, archived = archive_and_trim(records, directory, limit=2)
            self.assertEqual(archived, 1)
            self.assertEqual([item["id"] for item in kept], ["new-1", "new-2"])
            archive_path = os.path.join(directory, "signals-2024-01.json")
            with open(archive_path, "r", encoding="utf-8") as file:
                self.assertEqual([item["id"] for item in json.load(file)], ["old"])
            _, archived_again = archive_and_trim(records, directory, limit=2)
            self.assertEqual(archived_again, 0)

    def test_portfolio_capacity_applies_shared_and_direction_limits(self):
        positions = {
            "BTCUSDT": {"direction": "long", "units": [{}, {}, {}]},
            "ETHUSDT": {"direction": "long", "units": [{}, {}]},
        }
        self.assertFalse(backtest_turtle.portfolio_capacity(
            positions, "SOLUSDT", "long", 4, 8, 5
        ))
        self.assertTrue(backtest_turtle.portfolio_capacity(
            positions, "SOLUSDT", "short", 4, 8, 5
        ))

    def test_portfolio_capacity_reason_applies_strong_group_limit(self):
        class Args:
            portfolio_max_symbol_units = 4
            portfolio_max_total_units = 12
            portfolio_max_direction_units = 12
            portfolio_max_strong_group_units = 2
            portfolio_max_weak_group_units = 10
            portfolio_strong_groups = [["BTCUSDT", "ETHUSDT"]]
            portfolio_weak_groups = []

        positions = {"BTCUSDT": {"direction": "long", "units": [{}, {}]}}
        self.assertEqual(
            backtest_turtle.portfolio_capacity_reason(positions, "ETHUSDT", "long", Args()),
            (False, "strong_group"),
        )

    def test_monitor_health_records_push_failures_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(sw, "HEALTH_PATH", os.path.join(directory, "monitor_health.json")):
            sw.write_monitor_health(
                scan={"expected_markets": 12, "candidate_signals": 1},
                notifications=[{"results": [{"channel": "Server酱", "ok": False, "error": "timeout"}]}],
                status="ok",
            )
            with open(sw.HEALTH_PATH, encoding="utf-8") as file:
                health = json.load(file)
        self.assertEqual(health["scan"]["expected_markets"], 12)
        self.assertEqual(health["push"]["failed"], 1)
        self.assertNotIn("token", json.dumps(health))

    def test_monitor_health_alerts_only_on_status_transition(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(check_monitor_health, "HEALTH_PATH", os.path.join(directory, "monitor_health.json")), \
             mock.patch.object(check_monitor_health, "ALERT_STATE_PATH", os.path.join(directory, "monitor_alert_state.json")), \
             mock.patch.object(check_monitor_health, "send_serverchan", return_value=True) as notify:
            with open(check_monitor_health.HEALTH_PATH, "w", encoding="utf-8") as file:
                json.dump({"updated_at_epoch": 100, "status": "ok"}, file)
            first = check_monitor_health.check(max_age_minutes=20, now=2000, sendkey="SCT-test")
            second = check_monitor_health.check(max_age_minutes=20, now=2001, sendkey="SCT-test")
            self.assertEqual(first["status"], "stale")
            self.assertEqual(second["status"], "stale")
            self.assertEqual(notify.call_count, 1)

    def test_monitor_health_alert_includes_scan_diagnostics(self):
        health = {
            "updated_at": "2026-09-11 13:29:28",
            "scan": {
                "run_id": "abc123", "coverage_pct": 0.0,
                "successful_markets": 0, "expected_markets": 11,
                "failed_markets": 11, "failures": ["BTCUSDT|4h: timeout"],
            },
        }
        detail = check_monitor_health.health_alert_detail(health, 1692)
        self.assertIn("abc123", detail)
        self.assertIn("覆盖率：0.0%", detail)
        self.assertIn("BTCUSDT|4h: timeout", detail)

    def test_empty_portfolio_backtest_returns_a_valid_equity_curve(self):
        class Args:
            capital = 10000.0
            fee_rate = 0.001
            slippage = 0.0005
            portfolio_max_symbol_units = 4
            portfolio_max_total_units = 12
            portfolio_max_direction_units = 12

        bars = [
            {
                "time": index * 4 * 60 * 60 * 1000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
            }
            for index in range(400)
        ]
        daily = [
            {
                "time": index * 24 * 60 * 60 * 1000,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1.0,
            }
            for index in range(420)
        ]
        result = backtest_turtle.simulate_portfolio(
            {"BTCUSDT": (bars, daily), "ETHUSDT": (bars, daily)},
            backtest_turtle.backtest_variants()["production_default"],
            Args(),
        )
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["ending_equity"], Args.capital)
        self.assertTrue(result["equity_curve"])

    def test_portfolio_metrics_include_reliability_exposure_and_cost_fields(self):
        class Args:
            capital = 10000.0
            fee_rate = 0.001
            slippage = 0.0005
            portfolio_max_symbol_units = 4
            portfolio_max_total_units = 12
            portfolio_max_direction_units = 12

        bars = [
            {"time": index * 4 * 60 * 60 * 1000, "open": 100.0,
             "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}
            for index in range(400)
        ]
        daily = [
            {"time": index * 24 * 60 * 60 * 1000, "open": 100.0,
             "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}
            for index in range(420)
        ]
        result = backtest_turtle.simulate_portfolio(
            {"BTCUSDT": (bars, daily), "ETHUSDT": (bars, daily)},
            backtest_turtle.backtest_variants()["production_default"], Args(),
        )
        self.assertEqual(result["sample_reliability"], "insufficient_sample")
        self.assertIn("max_consecutive_losses", result)
        self.assertIn("max_margin_used", result)
        self.assertEqual(result["margin_model"], "spot_notional_proxy")
        self.assertIn("max_direction_exposure", result)
        self.assertIn("direction_exposure_peak", result)

    def test_backtest_report_gate_rejects_incomplete_portfolio_cost_sensitivity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.json")
            snapshot_file = os.path.join(directory, "snapshot.json")
            with open(snapshot_file, "w", encoding="utf-8") as file:
                file.write("{}")
            report = {
                "report_schema_version": 2, "generated_at": "now", "interval": "4h", "system": "system2",
                "sample_reliability_threshold_trades": 10, "symbols_disabled": [],
                "data_snapshot": {"directory": ".", "datasets": {"BTCUSDT": {"4h": {"sha256": "0" * 64, "file": "snapshot.json"}}}},
                "results": {"BTCUSDT": {}},
                "portfolio": {
                    "full": {"trades": 0, "sample_reliability": "insufficient_sample", "max_consecutive_losses": 0,
                              "max_margin_used": 0, "max_direction_exposure": 0, "cost_sensitivity": {"baseline_return": 0}},
                    "rolling_validation": {"enabled": True, "windows": [{}]},
                },
                "errors": {},
            }
            with open(path, "w", encoding="utf-8") as file:
                json.dump(report, file)
            errors = validate_reports.validate_backtest_report(path)
        self.assertTrue(any("成本敏感性缺少字段" in error for error in errors))

    def test_turtle_push_text_distinguishes_trigger_from_shadow_fill(self):
        plan = {
            "system": "system2", "entry": 100.0, "stop": 96.0,
            "next_add": 102.0, "unit_quantity": 25.0, "max_units": 4,
            "exit_days": 20, "exit_level": 90.0,
        }
        text = sw.build_turtle_strategy_text("long", plan)
        self.assertIn("突破触发价 ≈ 100", text)
        self.assertNotIn("入场点位", text)
        message = sw.build_turtle_message({
            "symbol": "BTCUSDT", "interval": "4h", "direction": "long",
            "grade": "海龟S2", "price": "101.00", "change": 1.0,
            "reason": "海龟S2向上突破", "strategy": text,
            "time": "2026-09-04 09:00:00", "trade_plan": plan,
        }, {})
        self.assertIn("影子成交：下一根4h K线开盘价", message)

    def test_turtle_push_text_exposes_timing_and_late_status(self):
        message = sw.build_turtle_message({
            "symbol": "BNBUSDT", "interval": "4h", "direction": "long",
            "grade": "海龟S2", "price": "738.25", "change": 3.32,
            "reason": "突破", "strategy": "策略", "time": "2026-09-05 16:38:33",
            "trade_plan": {"system": "system2", "entry": 729.9, "stop": 714.86,
                            "next_add": 733.66, "max_units": 4},
            "timing": {"shadow_entry_status": "late", "delay_minutes": 38.6,
                       "bar_close_time": "2026-09-05 08:00:00 UTC / 2026-09-05 16:00:00 北京",
                       "generated_time": "2026-09-05 08:38:33 UTC / 2026-09-05 16:38:33 北京"},
        }, {})
        self.assertIn("窗口已错过，禁止追价", message)
        self.assertIn("信号延迟 38.6 分钟", message)
        self.assertIn("币安现货观察，不是永续合约指令", message)

    def test_turtle_push_title_does_not_duplicate_breakout_prefix(self):
        event = {
            "symbol": "BNBUSDT", "interval": "4h", "direction": "long",
            "label": "海龟突破做多", "turtle": True, "divergence": False,
            "grade": "海龟S2", "price": "100", "change": 1.0,
            "reason": "突破", "strategy": "策略", "time": "now",
            "trade_plan": {"system": "system2", "entry": 100, "stop": 90,
                            "next_add": 105, "unit_quantity": 1, "max_units": 4,
                            "exit_days": 20, "exit_level": 80},
        }
        with mock.patch.object(sw, "send_notification") as notify, \
             mock.patch.object(sw, "record_signal_event"), \
             mock.patch.object(sw, "register_trade"):
            sw.process_events([event], {})
        self.assertEqual(notify.call_args.args[0], "CoinPulse BNBUSDT 海龟突破做多")


if __name__ == "__main__":
    unittest.main()
