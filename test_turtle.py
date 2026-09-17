import unittest
import json
import os
import tempfile
import time
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
import okx_data
import perp_backtest
import derivatives_snapshots
import collect_perp_snapshot
import run_perp_backtest
import perp_shadow
import validate_perp_shadow
import check_monitor_health
import console_output
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
    def test_console_output_configures_supported_streams(self):
        stdout = mock.Mock()
        stderr = mock.Mock()
        with mock.patch.object(console_output.sys, "stdout", stdout), \
             mock.patch.object(console_output.sys, "stderr", stderr):
            console_output.configure_utf8_output()
        stdout.reconfigure.assert_called_once_with(encoding="utf-8")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8")

    def test_workflow_publishes_perpetual_state_only_after_successful_validation(self):
        workflow = os.path.join(os.path.dirname(__file__), ".github", "workflows", "signal-monitor.yml")
        with open(workflow, encoding="utf-8") as file:
            text = file.read()
        ordinary = text.split("- name: Save ordinary signal state", 1)[1].split(
            "- name: Save perpetual shadow state", 1
        )[0]
        perpetual = text.split("- name: Save perpetual shadow state", 1)[1].split(
            "- name: Fail when perpetual shadow processing failed", 1
        )[0]
        self.assertNotIn("perp_shadow_state.json", ordinary)
        self.assertIn("perp_shadow_state.json", perpetual)
        self.assertIn("steps.perpetual_shadow.outcome == 'success'", perpetual)
        self.assertIn("steps.validate_perpetual_shadow.outcome == 'success'", perpetual)
        self.assertIn("git config rebase.autoStash true", ordinary)
        self.assertIn("validate_perp_shadow.py --max-age-minutes 30", text)
        self.assertIn("perpetual shadow risk tier health:", text)

    def test_workflow_sends_optional_external_heartbeat_after_state_writes(self):
        workflow = os.path.join(os.path.dirname(__file__), ".github", "workflows", "signal-monitor.yml")
        with open(workflow, encoding="utf-8") as file:
            text = file.read()
        heartbeat = text.split("- name: Send external success heartbeat", 1)[1].split(
            "- name: Fail when perpetual shadow processing failed", 1
        )[0]
        self.assertIn("steps.save_ordinary_state.outcome == 'success'", heartbeat)
        self.assertIn("steps.save_perpetual_state.outcome == 'success'", heartbeat)
        self.assertIn("steps.validate_perpetual_shadow.outcome == 'success'", heartbeat)
        self.assertIn('parsed.scheme != "https"', heartbeat)
        self.assertIn("if not raw_url:", heartbeat)
        self.assertIn('method="GET"', heartbeat)
        self.assertIn("continue-on-error: true", heartbeat)
        self.assertIn("test_push != 'true'", heartbeat)

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
            with mock.patch("builtins.print"):
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

    def test_liquidation_price_uses_notional_maintenance_tier(self):
        flat = derivatives_risk.liquidation_price(100, "long", 2, 0.005)
        tiered = derivatives_risk.liquidation_price(
            100, "long", 2, 0.005, quantity=200,
            maintenance_margin_tiers=[{"max_notional": 10000, "rate": 0.005},
                                       {"max_notional": 50000, "rate": 0.01}],
        )
        self.assertGreater(tiered, flat)
        with self.assertRaises(ValueError):
            derivatives_risk.liquidation_price(
                100, "long", 2, maintenance_margin_tiers=[{"max_notional": 10000, "rate": 0.01}]
            )
        with self.assertRaisesRegex(ValueError, "must not decrease"):
            derivatives_risk.normalize_maintenance_margin_tiers([
                {"max_notional": 50000, "rate": 0.005},
                {"max_notional": 100000, "rate": 0.004},
            ])
        with self.assertRaisesRegex(ValueError, "outside supported bounds"):
            derivatives_risk.normalize_maintenance_margin_tiers([
                {"max_notional": float("nan"), "rate": 0.005},
            ])

    def test_liquidation_price_supports_quantity_maintenance_tiers(self):
        low_quantity = derivatives_risk.liquidation_model_metadata(
            100, 5, "long", 2, maintenance_margin_tiers=[
                {"max_quantity": 10, "rate": 0.004},
                {"max_quantity": 20, "rate": 0.01},
            ],
        )
        high_quantity = derivatives_risk.liquidation_model_metadata(
            100, 15, "long", 2, maintenance_margin_tiers=[
                {"max_quantity": 10, "rate": 0.004},
                {"max_quantity": 20, "rate": 0.01},
            ],
        )
        self.assertEqual(low_quantity["maintenance_margin_rate"], 0.004)
        self.assertEqual(high_quantity["maintenance_margin_rate"], 0.01)
        self.assertEqual(high_quantity["maintenance_tier_dimension"], "quantity")
        self.assertGreater(high_quantity["liquidation_price"], low_quantity["liquidation_price"])
        with self.assertRaisesRegex(ValueError, "one cap dimension"):
            derivatives_risk.normalize_maintenance_margin_tiers([
                {"max_notional": 1000, "max_quantity": 10, "rate": 0.005},
            ])
        with self.assertRaisesRegex(ValueError, "one cap dimension"):
            derivatives_risk.normalize_maintenance_margin_tiers([
                {"max_notional": 1000, "rate": 0.005},
                {"max_quantity": 20, "rate": 0.01},
            ])

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
        self.assertEqual(snapshot["data_health"]["latest_data_times"]["contract_klines"], rows[-1][0] + 4 * 60 * 60 * 1000)
        self.assertEqual(snapshot["data_health"]["latest_bar_open_times"]["contract_klines"], rows[-1][0])
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

    def test_http_json_classifies_empty_and_non_json_responses(self):
        class Response:
            def __init__(self, body, content_type):
                self.status = 200
                self.headers = {"Content-Type": content_type}
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch.object(sw, "_urlopen_with_retry", return_value=Response(b"", "application/json")):
            with self.assertRaisesRegex(RuntimeError, r"EmptyResponse: status=200.*response_length=0"):
                sw.http_get_json("https://example.test/api/v1/time", attempts=1)
        with mock.patch.object(sw, "_urlopen_with_retry", return_value=Response(
            b"<html>blocked by policy</html>", "text/html; charset=utf-8"
        )):
            with self.assertRaisesRegex(RuntimeError, r"UnexpectedContentType: status=200.*text/html") as caught:
                sw.http_get_json("https://example.test/api/v1/time", attempts=1)
        self.assertIn("response_length=30", str(caught.exception))
        self.assertNotIn("example.test?", str(caught.exception))

    def test_okx_perpetual_adapter_emits_normalized_snapshot(self):
        rows = [[1700000000000, "100", "102", "99", "101", "12", "0", "0", "0"]]
        def fake_get(url):
            if "instruments" in url:
                return {"code": "0", "data": [{"state": "live", "baseCcy": "BTC", "tickSz": "0.1", "lotSz": "0.001", "minSz": "0.001", "ctVal": "0.01", "ctValCcy": "BTC"}]}
            if "position-tiers" in url:
                return {"code": "0", "data": [
                    {"instFamily": "BTC-USDT", "maxSz": "1000", "mmr": "0.004"},
                    {"instFamily": "BTC-USDT", "maxSz": "5000", "mmr": "0.005"},
                ]}
            if "funding-rate-history" in url:
                return {"code": "0", "data": [{"fundingTime": "1700000000000", "fundingRate": "0.0001"}]}
            if "open-interest" in url:
                return {"code": "0", "data": [["1700000000000", "12", "0.12", "1212"]]}
            return {"code": "0", "data": rows}
        snapshot = okx_data.fetch_perpetual_snapshot("btcusdt", "4h", limit=1,
                                                      http_get=fake_get, closed_only=False,
                                                      include_contract_specs=True,
                                                      open_interest_limit=2)
        self.assertEqual(snapshot["venue"], "okx")
        self.assertEqual(snapshot["contract_klines"][0]["close"], 101.0)
        self.assertEqual(snapshot["contract_klines"][0]["volume"], 0.12)
        self.assertEqual(snapshot["contract_klines"][0]["volume_contracts"], 12.0)
        self.assertEqual(snapshot["contract_specs"]["quote_asset"], "USDT")
        self.assertEqual(snapshot["contract_specs"]["quantity_step"], 0.00001)
        self.assertEqual(snapshot["open_interest"][0]["open_interest"], 0.12)
        self.assertEqual(snapshot["open_interest"][0]["open_interest_contracts"], 12.0)
        self.assertEqual(snapshot["collection"]["open_interest_limit"], 2)
        self.assertEqual(snapshot["collection"]["open_interest_coverage"], "historical")
        self.assertEqual(snapshot["maintenance_margin_tiers"][0], {
            "max_quantity": 10.0, "maintenance_margin_rate": 0.004,
        })
        self.assertEqual(
            snapshot["maintenance_margin_tier_metadata"]["source_parameters"]["tdMode"],
            "isolated",
        )
        self.assertEqual(snapshot["funding_rates"][0]["funding_rate"], 0.0001)

    def test_okx_position_tier_cache_uses_fresh_snapshot_without_network(self):
        rows = {"code": "0", "data": [
            {"instFamily": "BTC-USDT", "maxSz": "1000", "mmr": "0.004"},
        ]}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(okx_data.time, "time", return_value=1000):
            live = okx_data._resolve_position_tiers(
                "BTC-USDT-SWAP", "BTCUSDT", 0.01, mock.Mock(return_value=rows),
                cache_dir=directory, now_ms=1_000_000,
            )
            blocked = mock.Mock(side_effect=RuntimeError("network should not be used"))
            cached = okx_data._resolve_position_tiers(
                "BTC-USDT-SWAP", "BTCUSDT", 0.01, blocked,
                cache_dir=directory, cache_ttl_minutes=360,
                now_ms=1_000_000 + 30 * 60 * 1000,
            )
        self.assertEqual(live["cache_status"], "live_refresh")
        self.assertEqual(cached["cache_status"], "fresh_cache")
        self.assertEqual(cached["tiers"], live["tiers"])
        blocked.assert_not_called()

    def test_okx_position_tier_cache_rejects_nonofficial_provenance(self):
        rows = {"code": "0", "data": [
            {"instFamily": "BTC-USDT", "maxSz": "1000", "mmr": "0.004"},
        ]}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(okx_data.time, "time", return_value=1000):
            okx_data._resolve_position_tiers(
                "BTC-USDT-SWAP", "BTCUSDT", 0.01, mock.Mock(return_value=rows),
                cache_dir=directory, now_ms=1_000_000,
            )
            cache_path = okx_data._tier_cache_path(directory, "BTCUSDT")
            with open(cache_path, encoding="utf-8") as file:
                payload = json.load(file)
            payload["binding"]["source"] = "https://untrusted.example/tiers"
            with open(cache_path, "w", encoding="utf-8") as file:
                json.dump(payload, file)
            refreshed = mock.Mock(return_value=rows)
            result = okx_data._resolve_position_tiers(
                "BTC-USDT-SWAP", "BTCUSDT", 0.01, refreshed,
                cache_dir=directory, now_ms=1_000_000 + 30 * 60 * 1000,
            )
        self.assertEqual(result["cache_status"], "live_refresh")
        refreshed.assert_called_once()

    def test_okx_position_tier_cache_marks_bounded_stale_fallback(self):
        rows = {"code": "0", "data": [
            {"instFamily": "BTC-USDT", "maxSz": "1000", "mmr": "0.004"},
        ]}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(okx_data.time, "time", return_value=1000):
            okx_data._resolve_position_tiers(
                "BTC-USDT-SWAP", "BTCUSDT", 0.01, mock.Mock(return_value=rows),
                cache_dir=directory, now_ms=1_000_000,
            )
            failed = mock.Mock(side_effect=TimeoutError("temporary outage"))
            stale = okx_data._resolve_position_tiers(
                "BTC-USDT-SWAP", "BTCUSDT", 0.01, failed,
                cache_dir=directory, cache_ttl_minutes=360,
                cache_max_stale_minutes=1440,
                now_ms=1_000_000 + 400 * 60 * 1000,
            )
            with self.assertRaises(TimeoutError):
                okx_data._resolve_position_tiers(
                    "BTC-USDT-SWAP", "BTCUSDT", 0.01, failed,
                    cache_dir=directory, cache_ttl_minutes=360,
                    cache_max_stale_minutes=1440,
                    now_ms=1_000_000 + 1500 * 60 * 1000,
                )
        self.assertEqual(stale["cache_status"], "stale_fallback")
        self.assertEqual(stale["cache_age_minutes"], 400.0)
        self.assertEqual(stale["refresh_error_category"], "TimeoutError")

    def test_risk_tier_health_tracks_stale_failures_and_recovery(self):
        state = perp_shadow.empty_state()
        settings = {
            "risk_tier_cache_ttl_minutes": 360,
            "risk_tier_cache_max_stale_minutes": 1440,
        }
        state["risk_tier_metadata_by_symbol"]["BTCUSDT"] = {
            "scope": "provider_symbol_official_snapshot",
            "cache_status": "stale_fallback",
            "cache_age_minutes": 1400,
            "retrieved_at_epoch_ms": 1_000_000,
            "tier_version": "v1",
            "tier_checksum": "a" * 64,
            "refresh_error_category": "TimeoutError",
        }
        snapshot = {"venue": "okx"}
        first = perp_shadow._update_risk_tier_health(
            state, "BTCUSDT", snapshot, settings,
            {"maintenance_margin_tiers_refresh": "TimeoutError"}, 1000,
        )
        second = perp_shadow._update_risk_tier_health(
            state, "BTCUSDT", snapshot, settings,
            {"maintenance_margin_tiers_refresh": "TimeoutError"}, 2000,
        )
        summary = perp_shadow._risk_tier_health_summary(
            state, now_ms=1_000_000 + 1400 * 60 * 1000,
        )
        self.assertEqual(first["consecutive_refresh_failures"], 1)
        self.assertEqual(second["consecutive_refresh_failures"], 2)
        self.assertEqual(second["remaining_stale_minutes"], 40.0)
        self.assertEqual(second["expiry_risk"], "warning")
        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["degraded_symbols"], ["BTCUSDT"])
        self.assertEqual(summary["minimum_remaining_stale_minutes"], 40.0)

        state["risk_tier_metadata_by_symbol"]["BTCUSDT"].update({
            "cache_status": "live_refresh", "cache_age_minutes": 0,
            "retrieved_at_epoch_ms": 3_000_000,
            "refresh_error_category": None,
        })
        recovered = perp_shadow._update_risk_tier_health(
            state, "BTCUSDT", snapshot, settings, {}, 3000,
        )
        self.assertEqual(recovered["consecutive_refresh_failures"], 0)
        self.assertEqual(recovered["expiry_risk"], "normal")
        self.assertEqual(
            perp_shadow._risk_tier_health_summary(state, now_ms=3_000_000)["status"],
            "healthy",
        )

    def test_risk_tier_health_respects_configured_override_precedence(self):
        state = perp_shadow.empty_state()
        state["risk_tier_metadata_by_symbol"]["BTCUSDT"] = {
            "scope": "provider", "provider": "okx", "tier_checksum": "b" * 64,
        }
        snapshot = {
            "venue": "okx",
            "maintenance_margin_tier_metadata": {
                "scope": "provider_symbol_official_snapshot",
                "cache_status": "stale_fallback",
            },
        }
        health = perp_shadow._update_risk_tier_health(
            state, "BTCUSDT", snapshot, {
                "maintenance_margin_tiers_by_provider": {
                    "okx": [{"max_notional": 50000, "maintenance_margin_rate": 0.01}],
                },
            }, {"maintenance_margin_tiers_refresh": "TimeoutError"}, 1000,
        )
        self.assertEqual(health["status"], "configured_override")
        self.assertEqual(health["scope"], "provider")
        self.assertEqual(health["consecutive_refresh_failures"], 0)
        self.assertIsNone(health["refresh_error_category"])

    def test_risk_tier_health_does_not_treat_empty_global_tiers_as_override(self):
        state = perp_shadow.empty_state()
        state["risk_tier_metadata_by_symbol"]["BTCUSDT"] = {
            "scope": "global", "tier_checksum": perp_shadow._tier_checksum([]),
        }
        health = perp_shadow._update_risk_tier_health(
            state, "BTCUSDT", {"venue": "okx"}, {
                "maintenance_margin_tiers": [],
                "maintenance_margin_tiers_by_provider": {"okx": []},
                "maintenance_margin_tiers_by_provider_symbol": {"okx": {}},
            }, {"maintenance_margin_tiers": "TimeoutError"}, 1000,
        )
        self.assertEqual(health["status"], "official_unavailable")
        self.assertEqual(health["consecutive_refresh_failures"], 1)

    def test_perpetual_stats_recognize_active_official_tier_model(self):
        state = perp_shadow.empty_state()
        state["risk_tier_metadata_by_symbol"] = {
            "BTCUSDT": {"scope": "provider_symbol_official_snapshot"},
        }
        stats = perp_shadow.build_stats(state, {
            "risk_tier_cache_ttl_minutes": 120,
            "risk_tier_cache_max_stale_minutes": 720,
        })
        self.assertEqual(
            stats["liquidation_model"]["model_version"],
            derivatives_risk.LIQUIDATION_MODEL_VERSION_TIERED,
        )
        self.assertEqual(stats["liquidation_model"]["official_tier_cache_policy"], {
            "ttl_minutes": 120.0, "max_stale_minutes": 720.0,
        })

    def test_okx_closed_only_flag_controls_current_candle_filter(self):
        interval_ms = sw.INTERVAL_MS["4h"]
        current = int(time.time() * 1000)
        rows = [[current - interval_ms // 2, "100", "102", "99", "101", "12"]]

        def fake_get(_url):
            return {"code": "0", "data": rows}

        kept = okx_data._paged_candles(
            "BTC-USDT-SWAP", "4H", 1, "/api/v5/market/history-candles",
            fake_get, "4h", closed_only=False,
        )
        filtered = okx_data._paged_candles(
            "BTC-USDT-SWAP", "4H", 1, "/api/v5/market/history-candles",
            fake_get, "4h", closed_only=True,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(filtered, [])

    def test_okx_funding_and_open_interest_history_paginate(self):
        calls = []
        def fake_get(url):
            calls.append(url)
            if "funding-rate-history" in url:
                if len([item for item in calls if "funding-rate-history" in item]) > 1:
                    return {"code": "0", "data": [{"fundingTime": "1699999000000", "fundingRate": "0.0002"}]}
                return {"code": "0", "data": [{"fundingTime": "1700000000000", "fundingRate": "0.0001"}]}
            if "open-interest" in url:
                count = len([item for item in calls if "open-interest" in item])
                timestamp = 1700000000000 if count == 1 else 1699999000000
                return {"code": "0", "data": [[str(timestamp), "12", "0.12", "1212"]]}
            return {"code": "0", "data": []}
        funding = okx_data._funding("BTC-USDT-SWAP", 2, fake_get)
        self.assertEqual(len(funding), 2)
        self.assertEqual(len([url for url in calls if "funding-rate-history" in url]), 2)
        oi = okx_data._open_interest("BTC-USDT-SWAP", "4H", 2, fake_get)
        self.assertEqual([row["time"] for row in oi], [1699999000000, 1700000000000])
        self.assertEqual(oi[-1]["open_interest"], 0.12)
        self.assertEqual(oi[-1]["open_interest_contracts"], 12.0)
        oi_urls = [url for url in calls if "open-interest" in url]
        self.assertEqual(len(oi_urls), 2)
        self.assertNotIn("end=", oi_urls[0])
        self.assertIn("end=1699999999999", oi_urls[1])

    def test_okx_specs_infers_base_asset_when_api_leaves_base_currency_blank(self):
        def fake_get(_url):
            return {"code": "0", "data": [{
                "instId": "BTC-USDT-SWAP", "ctType": "linear",
                "settleCcy": "USDT", "baseCcy": "", "ctValCcy": "BTC",
                "ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01",
                "tickSz": "0.1", "state": "live",
            }]}

        specs = okx_data._specs("BTC-USDT-SWAP", fake_get)
        self.assertEqual(specs["base_asset"], "BTC")
        self.assertEqual(specs["contract_value_currency"], "BTC")
        self.assertAlmostEqual(specs["quantity_step"], 0.0001)

    def test_okx_open_interest_requires_exchange_timestamp(self):
        with self.assertRaises(RuntimeError):
            okx_data._open_interest(
                "BTC-USDT-SWAP", "4H", 1,
                lambda _url: {"code": "0", "data": [["bad", "12", "0.12", "1212"]]},
            )

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

    def test_perpetual_shadow_does_not_silently_reset_corrupt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as file:
                file.write("{not-json")
            with self.assertRaises(ValueError):
                perp_shadow.load_state(path)

    def test_perpetual_shadow_rejects_invalid_state_collection_types(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"market_type": "linear_perpetual", "open_trades": {}}, file)
            with self.assertRaises(ValueError):
                perp_shadow.load_state(path)

    def test_perpetual_shadow_rejects_unknown_state_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            with open(path, "w", encoding="utf-8") as file:
                json.dump({"schema_version": 99, "market_type": "linear_perpetual"}, file)
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
        self.assertEqual(stats["sample_remaining_to_minimum"], 2)
        self.assertEqual(stats["sample_remaining_to_preferred"], 7)
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

    def test_perpetual_shadow_stats_break_down_research_samples(self):
        state = perp_shadow.empty_state(10000)
        state["closed_trades"] = [
            {"provider": "binance", "symbol": "BTCUSDT", "net_pnl": 10,
             "return_pct": 1.0, "market_state": {"state": "trend"},
             "funding_settlement_count": 1, "funding_mark_estimated_count": 0},
            {"provider": "okx", "symbol": "ETHUSDT", "net_pnl": -5,
             "return_pct": -0.5, "market_state": {"state": "range"},
             "funding_settlement_count": 2, "funding_mark_estimated_count": 2},
        ]
        stats = perp_shadow.build_stats(state, {
            "sample_group_goal_min_trades": 3,
            "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        })
        breakdown = stats["sample_breakdown"]
        self.assertEqual(breakdown["provider"]["binance"]["wins"], 1)
        self.assertEqual(breakdown["symbol"]["ETHUSDT"]["net_pnl"], -5.0)
        self.assertEqual(breakdown["market_state"]["trend"]["average_return_pct"], 1.0)
        self.assertEqual(
            breakdown["funding_mark_quality"]["estimated_mark_only"]["count"], 1
        )
        self.assertEqual(breakdown["provider"]["okx"]["remaining_to_minimum"], 2)
        self.assertEqual(
            stats["configured_symbol_sample_coverage"]["SOLUSDT"]["remaining_to_minimum"], 3
        )

    def test_perpetual_notification_contains_execution_parameters(self):
        trade = {
            "id": "perp-notify-test", "symbol": "BTCUSDT", "interval": "4h",
            "direction": "long", "provider": "binance", "status": "pending_entry",
            "entry_trigger": 100.0, "fill_time": 200, "n": 2.0,
            "leverage": 3.0, "risk_fraction": 0.005,
        }
        content = perp_shadow._perpetual_notification_content("signal_created", trade)
        self.assertIn("BTCUSDT", content)
        self.assertIn("触发价：100.0", content)
        self.assertIn("杠杆：3.0", content)
        self.assertIn("不会自动下单", content)

    def test_perpetual_notifications_are_deduplicated(self):
        state = perp_shadow.empty_state()
        trade = {
            "id": "perp-notify-test", "symbol": "BTCUSDT", "interval": "4h",
            "direction": "long", "provider": "binance", "status": "pending_entry",
            "entry_trigger": 100.0, "fill_time": 200, "n": 2.0,
            "leverage": 3.0, "risk_fraction": 0.005,
        }
        config = {"channels": {"generic": {"webhook": "https://example.invalid"}}}
        with mock.patch.object(sw, "has_channel", return_value=True), \
             mock.patch.object(sw, "send_notification", return_value=[{"channel": "mock", "ok": True}]) as notify:
            first = perp_shadow.dispatch_perpetual_notifications(
                [("signal_created", trade, {})], state, config
            )
            second = perp_shadow.dispatch_perpetual_notifications(
                [("signal_created", trade, {})], state, config
            )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        notify.assert_called_once()

    def test_perpetual_shadow_rejects_unsafe_risk_parameters(self):
        with self.assertRaises(ValueError):
            perp_shadow.shadow_settings({"derivatives": {"max_leverage": 21}})
        with self.assertRaises(ValueError):
            perp_shadow.shadow_settings({"derivatives": {"risk_fraction": 1.1}})

    def test_perpetual_risk_tiers_are_bound_to_snapshot_provider(self):
        settings = perp_shadow.shadow_settings({"derivatives": {
            "maintenance_margin_tiers": [{"max_notional": 10000, "rate": 0.005}],
            "maintenance_margin_tiers_by_provider": {
                "binance": [{"max_notional": 50000, "rate": 0.01}],
                "okx": [{"max_notional": 50000, "rate": 0.02}],
            },
        }})
        binance = perp_shadow._provider_settings(settings, "binance")
        okx = perp_shadow._provider_settings(settings, "okx")
        self.assertEqual(binance["maintenance_margin_tiers"][0]["maintenance_margin_rate"], 0.01)
        self.assertEqual(okx["maintenance_margin_tiers"][0]["maintenance_margin_rate"], 0.02)
        self.assertEqual(perp_shadow.parameter_snapshot(okx)["risk_parameter_provider"], "okx")
        self.assertEqual(settings["maintenance_margin_tiers"][0]["maintenance_margin_rate"], 0.005)

    def test_perpetual_risk_tiers_bind_to_provider_and_symbol_with_provenance(self):
        settings = perp_shadow.shadow_settings({"derivatives": {
            "maintenance_margin_tiers_by_provider": {
                "binance": [{"max_notional": 50000, "rate": 0.01}],
            },
            "maintenance_margin_tiers_by_provider_symbol": {
                "binance": {
                    "BTCUSDT": {
                        "tiers": [{"max_notional": 100000, "rate": 0.02}],
                        "source": "https://example.test/binance-risk-table",
                        "effective_at": "2026-09-16T00:00:00Z",
                        "tier_version": "example-v1",
                    },
                },
            },
        }})
        btc = perp_shadow._provider_settings(settings, "binance", "BTCUSDT")
        eth = perp_shadow._provider_settings(settings, "binance", "ETHUSDT")
        self.assertEqual(btc["maintenance_margin_tiers"][0]["maintenance_margin_rate"], 0.02)
        self.assertEqual(eth["maintenance_margin_tiers"][0]["maintenance_margin_rate"], 0.01)
        snapshot = perp_shadow.parameter_snapshot(btc)
        metadata = snapshot["maintenance_margin_tier_metadata"]
        self.assertEqual(snapshot["risk_parameter_symbol"], "BTCUSDT")
        self.assertEqual(metadata["scope"], "provider_symbol")
        self.assertEqual(metadata["provider"], "binance")
        self.assertEqual(metadata["tier_version"], "example-v1")
        self.assertEqual(metadata["tier_checksum"],
                         perp_shadow._tier_checksum(snapshot["maintenance_margin_tiers"]))

    def test_okx_official_quantity_tiers_fill_only_unconfigured_risk_binding(self):
        official_snapshot = {
            "maintenance_margin_tiers": [
                {"max_quantity": 10, "maintenance_margin_rate": 0.004},
            ],
            "maintenance_margin_tier_metadata": {
                "source": "https://www.okx.com/api/v5/public/position-tiers",
                "tier_version": "official-v1",
            },
        }
        settings = perp_shadow.shadow_settings({"derivatives": {}})
        bound = perp_shadow._provider_settings(
            settings, "okx", "BTCUSDT", snapshot=official_snapshot,
        )
        self.assertEqual(bound["maintenance_margin_tiers"][0]["max_quantity"], 10.0)
        self.assertEqual(
            bound["maintenance_margin_tier_metadata"]["scope"],
            "provider_symbol_official_snapshot",
        )
        configured = perp_shadow.shadow_settings({"derivatives": {
            "maintenance_margin_tiers_by_provider": {
                "okx": [{"max_notional": 50000, "rate": 0.02}],
            },
        }})
        explicit = perp_shadow._provider_settings(
            configured, "okx", "BTCUSDT", snapshot=official_snapshot,
        )
        self.assertIn("max_notional", explicit["maintenance_margin_tiers"][0])
        self.assertEqual(explicit["maintenance_margin_tier_metadata"]["scope"], "provider")

    def test_perpetual_symbol_risk_tiers_require_versioned_provenance(self):
        binding = {"maintenance_margin_tiers_by_provider_symbol": {
            "binance": {"BTCUSDT": {
                "tiers": [{"max_notional": 100000, "rate": 0.02}],
            }},
        }}
        with self.assertRaisesRegex(ValueError, "require source"):
            perp_shadow.shadow_settings({"derivatives": binding})
        binding["maintenance_margin_tiers_by_provider_symbol"]["binance"]["BTCUSDT"].update({
            "source": "official", "effective_at": "2026-09-16T00:00:00Z",
            "tier_version": "v1", "tier_checksum": "0" * 64,
        })
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            perp_shadow.shadow_settings({"derivatives": binding})

    def test_perpetual_cohort_blocks_new_samples_after_parameter_drift(self):
        state = perp_shadow.empty_state()
        baseline = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
            "interval": "4h", "cohort_id": "baseline-v1",
            "strategy_version": "turtle-v1", "risk_fraction": 0.005,
        }})
        bound = perp_shadow._bind_strategy_cohort(state, baseline)
        self.assertTrue(bound["new_entries_enabled"])
        drifted = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
            "interval": "4h", "cohort_id": "baseline-v1",
            "strategy_version": "turtle-v1", "risk_fraction": 0.01,
        }})
        blocked = perp_shadow._bind_strategy_cohort(state, drifted)
        self.assertFalse(blocked["new_entries_enabled"])
        self.assertEqual(state["active_cohort"]["status"], "parameter_mismatch")

        bars = make_bars(400, close=100, high=101, low=99)
        snapshot = {"symbol": "BTCUSDT", "venue": "binance",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": [],
                    "open_interest": [], "contract_specs": {"symbol": "BTCUSDT"}}
        plan = {"entry": 101, "stop": 99, "next_add": 102, "exit_level": 98,
                "n": 1, "unit_quantity": 1, "max_quantity": 4, "exit_days": 20,
                "system": "system2"}
        market_state = {"state": "trend", "risk_multiplier": 1.0,
                        "flags": [], "metrics": {}}
        with mock.patch.object(sw, "build_turtle_signal", return_value=("long", ["breakout"], plan)), \
             mock.patch.object(perp_shadow, "classify_market_state", return_value=market_state):
            signal = perp_shadow.create_signal(snapshot, blocked, state)
        self.assertIsNone(signal)
        self.assertEqual(state["open_trades"], [])
        self.assertEqual(state["signal_funnel"]["cohort_rejections"], 1)

        new_cohort = dict(drifted)
        new_cohort["cohort_id"] = "experiment-v2"
        enabled = perp_shadow._bind_strategy_cohort(state, new_cohort)
        self.assertTrue(enabled["new_entries_enabled"])
        self.assertEqual(len(state["strategy_cohorts"]), 2)

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

    def test_perpetual_provider_failure_preserves_component_root_causes(self):
        snapshot = {
            "market_type": "linear_perpetual", "venue": "binance",
            "symbol": "BTCUSDT", "interval": "4h",
            "contract_klines": [], "mark_price_klines": [],
            "index_price_klines": [], "contract_specs": None,
            "data_health": {"component_errors": {
                "contract_klines": "HTTP 451", "contract_specs": "timeout",
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            result = perp_shadow.run({"derivatives": {
                "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
                "interval": "4h",
            }}, os.path.join(directory, "state.json"),
                os.path.join(directory, "stats.json"),
                fetcher=lambda *_args, **_kwargs: snapshot)
        attempt = result["stats"]["symbol_health"]["BTCUSDT"]["provider_attempts"][0]
        self.assertEqual(attempt["component_errors"]["contract_klines"], "HTTP 451")
        self.assertIn("component_errors", attempt["error"])

    def test_perpetual_full_research_mode_rejects_missing_history_components(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            snapshot = {"market_type": "linear_perpetual", "venue": "binance", "symbol": "BTCUSDT",
                        "interval": "4h", "contract_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
                        "mark_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
                        "index_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
                        "funding_rates": [], "open_interest": [], "contract_specs": {"symbol": "BTCUSDT"},
                        "collection": {"open_interest_coverage": "latest_only"},
                        "data_health": {"component_errors": {}, "data_lag_minutes": {}}}
            result = perp_shadow.run({"derivatives": {"enabled": True, "research_only": True,
                "research_data_mode": "full_perpetual_research", "symbols": ["BTCUSDT"], "interval": "4h"}},
                state_path, stats_path, fetcher=lambda *_args, **_kwargs: snapshot)
        self.assertEqual(result["stats"]["successful_symbols"], [])
        self.assertEqual(result["stats"]["symbol_health"]["BTCUSDT"]["status"], "unavailable")

    def test_perpetual_full_research_mode_rejects_sparse_binance_open_interest(self):
        interval = 4 * 60 * 60 * 1000
        start = 1_700_000_000_000
        bars = [{"time": start + index * interval, "open": 100, "high": 101,
                 "low": 99, "close": 100, "volume": 1} for index in range(3)]
        snapshot = {
            "market_type": "linear_perpetual", "venue": "binance", "symbol": "BTCUSDT",
            "interval": "4h", "contract_klines": bars, "mark_price_klines": bars,
            "index_price_klines": bars,
            "funding_rates": [{"time": start, "funding_rate": 0.0},
                               {"time": start + 2 * interval, "funding_rate": 0.0}],
            "open_interest": [{"time": start, "open_interest": 1.0,
                                "open_interest_value": 100.0}],
            "contract_specs": {"symbol": "BTCUSDT"},
            "collection": {},
            "data_health": {"component_errors": {}, "data_lag_minutes": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            result = perp_shadow.run(
                {"derivatives": {"enabled": True, "research_only": True,
                    "research_data_mode": "full_perpetual_research", "symbols": ["BTCUSDT"],
                    "interval": "4h"}},
                os.path.join(directory, "state.json"), os.path.join(directory, "stats.json"),
                fetcher=lambda *_args, **_kwargs: snapshot,
            )
        self.assertEqual(result["stats"]["successful_symbols"], [])
        component_errors = result["stats"]["symbol_health"]["BTCUSDT"].get("component_errors", {})
        self.assertIn("open_interest_coverage", " ".join(component_errors.values()))

    def test_perpetual_research_coverage_accepts_historical_funding_and_open_interest(self):
        interval = 4 * 60 * 60 * 1000
        start = 1_700_000_000_000
        bars = [{"time": start + index * interval} for index in range(3)]
        snapshot = {
            "contract_klines": bars,
            "funding_rates": [{"time": start, "funding_rate": 0.0},
                               {"time": start + 2 * interval, "funding_rate": 0.0}],
            "open_interest": [{"time": start, "open_interest": 1.0},
                               {"time": start + 2 * interval, "open_interest": 1.1}],
        }
        self.assertTrue(perp_shadow._historical_coverage(snapshot, "funding_rates", interval))
        self.assertTrue(perp_shadow._historical_coverage(snapshot, "open_interest", interval))
        self.assertTrue(perp_shadow._historical_density_ok(snapshot, "funding_rates", interval))
        self.assertTrue(perp_shadow._historical_density_ok(snapshot, "open_interest", interval))

    def test_perpetual_full_research_mode_rejects_large_internal_funding_gap(self):
        interval = 4 * 60 * 60 * 1000
        start = 1_700_000_000_000
        bars = [{"time": start + index * interval, "open": 100, "high": 101,
                 "low": 99, "close": 100, "volume": 1} for index in range(10)]
        snapshot = {
            "market_type": "linear_perpetual", "venue": "binance", "symbol": "BTCUSDT",
            "interval": "4h", "contract_klines": bars, "mark_price_klines": bars,
            "index_price_klines": bars,
            "funding_rates": [{"time": start, "funding_rate": 0.0},
                               {"time": start + 9 * interval, "funding_rate": 0.0}],
            "open_interest": [{"time": start, "open_interest": 1.0},
                               {"time": start + 9 * interval, "open_interest": 1.1}],
            "contract_specs": {"symbol": "BTCUSDT"}, "collection": {},
            "data_health": {"component_errors": {}, "data_lag_minutes": {}},
        }
        with tempfile.TemporaryDirectory() as directory:
            result = perp_shadow.run(
                {"derivatives": {"enabled": True, "research_only": True,
                    "research_data_mode": "full_perpetual_research", "symbols": ["BTCUSDT"],
                    "interval": "4h"}},
                os.path.join(directory, "state.json"), os.path.join(directory, "stats.json"),
                fetcher=lambda *_args, **_kwargs: snapshot,
            )
        component_errors = result["stats"]["symbol_health"]["BTCUSDT"].get("component_errors", {})
        self.assertIn("funding_history_gaps", " ".join(component_errors.values()))

    def test_perpetual_shadow_uses_common_market_time_for_portfolio_curve(self):
        state = perp_shadow.empty_state()
        state["market_time_by_symbol"] = {"BTCUSDT": 200, "ETHUSDT": 100}
        state["last_market_time"] = 100
        point = perp_shadow.record_equity_snapshot(state)
        self.assertEqual(point["time"], 100)

    def test_perpetual_shadow_does_not_append_out_of_order_equity_point(self):
        state = perp_shadow.empty_state()
        state["equity_curve"] = [{
            "time": 200, "realized_equity": 10000.0,
            "marked_equity": 10000.0, "unrealized_pnl": 0.0,
            "open_count": 0, "open_margin": 0.0, "open_notional": 0.0,
            "long_notional": 0.0, "short_notional": 0.0,
            "open_risk_fraction": 0.0,
        }]
        state["last_market_time"] = 100
        point = perp_shadow.record_equity_snapshot(state)
        self.assertEqual(point["time"], 200)
        self.assertEqual(len(state["equity_curve"]), 1)

    def test_perpetual_shadow_equity_snapshot_includes_portfolio_exposure(self):
        state = perp_shadow.empty_state(10000)
        state["last_market_time"] = 100
        state["open_trades"] = [{"status": "open", "direction": "long",
                                  "avg_entry": 100, "quantity": 2, "leverage": 2,
                                  "risk_fraction": 0.005, "units": [{}, {}],
                                  "last_mark_price": 101}]
        point = perp_shadow.record_equity_snapshot(state)
        self.assertEqual(point["open_notional"], 200.0)
        self.assertEqual(point["open_margin"], 100.0)
        self.assertEqual(point["long_notional"], 200.0)
        stats = perp_shadow.build_stats(state)
        self.assertEqual(stats["max_margin_used"], 100.0)
        self.assertEqual(stats["max_direction_exposure"], 200.0)

    def test_perpetual_shadow_uses_recent_cache_for_health_only(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = os.path.join(directory, "cache")
            os.makedirs(cache_dir)
            cached = {"symbol": "BTCUSDT", "interval": "4h",
                      "fetched_at_epoch_ms": int(time.time() * 1000),
                      "contract_klines": [], "data_health": {}}
            with open(os.path.join(cache_dir, "BTCUSDT-4h.json"), "w", encoding="utf-8") as file:
                json.dump(cached, file)
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            result = perp_shadow.run({"derivatives": {
                "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
                "interval": "4h", "cache_dir": cache_dir,
            }}, state_path, stats_path,
                fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
            self.assertEqual(result["stats"]["symbol_health"]["BTCUSDT"]["status"], "stale_cache")
            self.assertEqual(result["stats"]["symbol_health"]["BTCUSDT"]["signal_status"], "not_evaluated")
            self.assertEqual(result["stats"]["cached_symbols"], ["BTCUSDT"])

    def test_perpetual_symbol_processing_rolls_back_partial_state_on_error(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001}
        row = {"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
        snapshot = {"market_type": "linear_perpetual", "venue": "binance",
                    "symbol": "BTCUSDT", "interval": "4h",
                    "contract_klines": [row], "mark_price_klines": [row],
                    "index_price_klines": [row], "contract_specs": specs,
                    "funding_rates": [], "open_interest": [],
                    "data_health": {"component_errors": {}, "data_lag_minutes": {}}}

        def fail_after_mutation(_snapshot, _settings, candidate):
            candidate["equity"] = 1
            candidate["closed_trades"].append({"partial": True})
            raise RuntimeError("processing failed")

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(perp_shadow, "process_snapshot", side_effect=fail_after_mutation):
            state_path = os.path.join(directory, "state.json")
            result = perp_shadow.run({"derivatives": {
                "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
                "interval": "4h", "cache_dir": os.path.join(directory, "cache"),
            }}, state_path, os.path.join(directory, "stats.json"),
                fetcher=lambda *_args, **_kwargs: snapshot)
            state = perp_shadow.load_json(state_path, {})
        self.assertEqual(state["equity"], 10000)
        self.assertEqual(state["closed_trades"], [])
        self.assertEqual(result["stats"]["data_status"], "unavailable")

    def test_perpetual_auto_provider_fails_over_without_mixing_sources(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001, "min_notional": 5.0}
        snapshot = {"market_type": "linear_perpetual", "venue": "okx", "symbol": "BTCUSDT",
                    "interval": "4h", "contract_klines": [{"time": 1, "close": 100}],
                    "mark_price_klines": [{"time": 1, "close": 100}],
                    "index_price_klines": [{"time": 1, "close": 100}],
                    "funding_rates": [], "open_interest": [], "contract_specs": specs,
                    "data_health": {"component_errors": {}, "data_lag_minutes": {}}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(derivatives_data, "fetch_perpetual_snapshot", side_effect=RuntimeError("binance down")), \
             mock.patch.object(okx_data, "fetch_perpetual_snapshot", return_value=snapshot) as okx_fetch, \
             mock.patch.object(perp_shadow, "process_snapshot", return_value=None):
            result = perp_shadow.run({"derivatives": {"enabled": True, "research_only": True,
                "provider": "auto", "symbols": ["BTCUSDT"], "interval": "4h"}},
                os.path.join(directory, "state.json"), os.path.join(directory, "stats.json"))
        self.assertEqual(result["stats"]["symbol_health"]["BTCUSDT"]["provider"], "okx")
        self.assertEqual(result["stats"]["errors"], {})
        health = result["stats"]["symbol_health"]["BTCUSDT"]
        self.assertEqual([item["provider"] for item in health["provider_attempts"]], ["binance", "okx"])
        self.assertEqual(health["provider_switch_reason"], "fallback_after_1_failure(s)")
        self.assertIsInstance(health["provider_latency_ms"], float)
        self.assertEqual(result["stats"]["data_status"], "degraded")
        self.assertEqual(result["stats"]["provider_redundancy"]["status"],
                         "degraded_redundancy")
        self.assertEqual(result["stats"]["provider_redundancy"]["degraded_symbols"],
                         ["BTCUSDT"])
        self.assertEqual(okx_fetch.call_args.kwargs["open_interest_limit"], 2)

    def test_perpetual_signal_funnel_deduplicates_same_closed_bar(self):
        bars = make_bars(400, close=100, high=101, low=99)
        snapshot = {
            "market_type": "linear_perpetual", "venue": "binance",
            "symbol": "BTCUSDT", "interval": "4h", "contract_klines": bars,
            "mark_price_klines": bars, "index_price_klines": bars,
            "funding_rates": [], "open_interest": [],
            "contract_specs": {"symbol": "BTCUSDT", "contract_type": "PERPETUAL",
                               "quote_asset": "USDT", "price_tick": 0.1,
                               "quantity_step": 0.001, "min_quantity": 0.001,
                               "min_notional": 5.0},
        }
        settings = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
            "interval": "4h",
        }})
        state = perp_shadow.empty_state()
        self.assertIsNone(perp_shadow.create_signal(snapshot, settings, state))
        self.assertIsNone(perp_shadow.create_signal(snapshot, settings, state))
        funnel = state["signal_funnel"]
        self.assertEqual(funnel["evaluated_bars"], 1)
        self.assertEqual(funnel["no_breakout"], 1)
        self.assertEqual(funnel["raw_breakout_candidates"], 0)
        diagnostic = state["last_signal_diagnostics_by_symbol"]["BTCUSDT"]
        self.assertEqual(diagnostic["outcome"], "no_breakout")
        self.assertIsNotNone(diagnostic["distance_to_long_breakout_pct"])

    def test_perpetual_market_freshness_uses_required_price_series_only(self):
        settings = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"],
            "interval": "4h", "market_data_max_lag_intervals": 1.05,
        }})
        snapshot = {"data_health": {"data_lag_minutes": {
            "contract_klines": 230, "mark_price_klines": 231,
            "index_price_klines": 232, "funding_rates": 600,
            "open_interest": 800,
        }}}
        fresh = perp_shadow._market_data_freshness(snapshot, settings)
        self.assertEqual(fresh["status"], "fresh")
        self.assertEqual(fresh["lag_minutes"], 232)
        snapshot["data_health"]["data_lag_minutes"]["mark_price_klines"] = 253
        stale = perp_shadow._market_data_freshness(snapshot, settings)
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["max_lag_minutes"], 252)

    def test_perpetual_stale_price_snapshot_cannot_mutate_trading_state(self):
        row = {"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
        snapshot = {"market_type": "linear_perpetual", "venue": "binance",
                    "symbol": "BTCUSDT", "interval": "4h", "contract_klines": [row],
                    "mark_price_klines": [row], "index_price_klines": [row],
                    "contract_specs": {"symbol": "BTCUSDT"},
                    "data_health": {"component_errors": {}, "data_lag_minutes": {
                        "contract_klines": 260, "mark_price_klines": 260,
                        "index_price_klines": 260,
                    }}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(perp_shadow, "process_snapshot") as process:
            result = perp_shadow.run(
                {"derivatives": {"enabled": True, "research_only": True,
                    "symbols": ["BTCUSDT"], "interval": "4h",
                    "market_data_max_lag_intervals": 1.05}},
                os.path.join(directory, "state.json"),
                os.path.join(directory, "stats.json"),
                fetcher=lambda *_args, **_kwargs: snapshot,
            )
        process.assert_not_called()
        health = result["stats"]["symbol_health"]["BTCUSDT"]
        self.assertEqual(health["status"], "stale")
        self.assertEqual(health["market_data_freshness"], "stale")
        self.assertEqual(result["stats"]["successful_symbols"], [])

    def test_perpetual_stale_primary_price_fails_over_before_processing(self):
        row = {"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
        base = {"market_type": "linear_perpetual", "symbol": "BTCUSDT", "interval": "4h",
                "contract_klines": [row], "mark_price_klines": [row],
                "index_price_klines": [row], "funding_rates": [], "open_interest": [],
                "contract_specs": {"symbol": "BTCUSDT"}}
        stale = {**base, "venue": "binance", "data_health": {
            "component_errors": {}, "data_lag_minutes": {
                "contract_klines": 260, "mark_price_klines": 260,
                "index_price_klines": 260,
            }}}
        fresh = {**base, "venue": "okx", "data_health": {
            "component_errors": {}, "data_lag_minutes": {
                "contract_klines": 10, "mark_price_klines": 10,
                "index_price_klines": 10,
            }}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(derivatives_data, "fetch_perpetual_snapshot", return_value=stale), \
             mock.patch.object(okx_data, "fetch_perpetual_snapshot", return_value=fresh), \
             mock.patch.object(perp_shadow, "process_snapshot", return_value=None) as process:
            result = perp_shadow.run(
                {"derivatives": {"enabled": True, "research_only": True,
                    "provider": "auto", "symbols": ["BTCUSDT"], "interval": "4h"}},
                os.path.join(directory, "state.json"),
                os.path.join(directory, "stats.json"),
            )
        process.assert_called_once()
        health = result["stats"]["symbol_health"]["BTCUSDT"]
        self.assertEqual(health["provider"], "okx")
        self.assertEqual(health["market_data_freshness"], "fresh")
        self.assertIn("stale perpetual market data", health["provider_attempts"][0]["error"])

    def test_perpetual_provider_cooldown_skips_repeated_primary_failure(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001, "min_notional": 5.0}
        snapshot = {"market_type": "linear_perpetual", "venue": "okx", "symbol": "BTCUSDT",
                    "interval": "4h", "contract_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
                    "mark_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
                    "index_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}],
                    "funding_rates": [], "open_interest": [], "contract_specs": specs,
                    "data_health": {"component_errors": {}, "data_lag_minutes": {}}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(derivatives_data, "fetch_perpetual_snapshot", side_effect=RuntimeError("binance down")), \
             mock.patch.object(okx_data, "fetch_perpetual_snapshot", return_value=snapshot) as okx_fetch, \
             mock.patch.object(perp_shadow, "process_snapshot", return_value=None):
            config = {"derivatives": {"enabled": True, "research_only": True,
                "provider": "auto", "symbols": ["BTCUSDT"], "interval": "4h",
                "provider_cooldown_seconds": 3600}}
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            perp_shadow.run(config, state_path, stats_path)
            second = perp_shadow.run(config, state_path, stats_path)
        attempts = second["stats"]["symbol_health"]["BTCUSDT"]["provider_attempts"]
        self.assertEqual(attempts[0]["status"], "skipped_cooldown")
        self.assertEqual(attempts[1]["provider"], "okx")
        self.assertEqual(second["stats"]["symbol_health"]["BTCUSDT"]["provider_switch_reason"],
                         "fallback_after_provider_cooldown")
        self.assertEqual(okx_fetch.call_count, 2)

    def test_perpetual_provider_redundancy_records_degradation_and_recovery(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
                 "min_notional": 5.0}
        row = {"time": 1, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
        binance_snapshot = {"market_type": "linear_perpetual", "venue": "binance",
                            "symbol": "BTCUSDT", "interval": "4h",
                            "contract_klines": [row], "mark_price_klines": [row],
                            "index_price_klines": [row], "funding_rates": [],
                            "open_interest": [], "contract_specs": specs,
                            "data_health": {"component_errors": {}, "data_lag_minutes": {}}}
        okx_snapshot = {**binance_snapshot, "venue": "okx"}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(derivatives_data, "fetch_perpetual_snapshot", side_effect=[
                 RuntimeError("binance down"), binance_snapshot,
             ]), \
             mock.patch.object(okx_data, "fetch_perpetual_snapshot", return_value=okx_snapshot), \
             mock.patch.object(perp_shadow, "process_snapshot", return_value=None):
            config = {"derivatives": {"enabled": True, "research_only": True,
                "provider": "auto", "symbols": ["BTCUSDT"], "interval": "4h",
                "provider_cooldown_seconds": 0}}
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            first = perp_shadow.run(config, state_path, stats_path)
            second = perp_shadow.run(config, state_path, stats_path)
        self.assertEqual(first["stats"]["provider_redundancy"]["status"],
                         "degraded_redundancy")
        self.assertEqual(second["stats"]["provider_redundancy"]["status"], "healthy")
        transitions = [(event["from"], event["to"])
                       for event in second["stats"]["provider_redundancy_events"]]
        self.assertIn(("degraded_redundancy", "healthy"), transitions)

    def test_perpetual_open_position_hold_and_recovery_persist_across_runs(self):
        specs = {"symbol": "BTCUSDT", "contract_type": "PERPETUAL", "quote_asset": "USDT",
                 "price_tick": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
                 "min_notional": 5.0}
        interval_ms = 4 * 60 * 60 * 1000
        previous_close = 1_700_000_000_000
        bars = [{"time": previous_close + index * interval_ms, "open": 100,
                 "high": 101, "low": 99, "close": 100, "volume": 1}
                for index in range(3)]
        snapshot = {"market_type": "linear_perpetual", "venue": "binance",
                    "symbol": "BTCUSDT", "interval": "4h", "contract_klines": bars,
                    "mark_price_klines": bars, "index_price_klines": bars,
                    "funding_rates": [], "open_interest": [], "contract_specs": specs,
                    "data_health": {"component_errors": {}, "data_lag_minutes": {}}}
        state = perp_shadow.empty_state()
        state["market_time_by_symbol"] = {"BTCUSDT": previous_close}
        state["open_trades"] = [{"id": "open-1", "market_type": "linear_perpetual",
                                 "research_only": True, "symbol": "BTCUSDT",
                                 "provider": "binance", "status": "open"}]
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            stats_path = os.path.join(directory, "stats.json")
            sw.atomic_write_json(state_path, state)
            config = {"derivatives": {"enabled": True, "research_only": True,
                                       "symbols": ["BTCUSDT"], "interval": "4h"}}
            failing = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
            held = perp_shadow.run(config, state_path, stats_path, fetcher=failing)
            with mock.patch.object(perp_shadow, "process_snapshot", return_value=None):
                recovered = perp_shadow.run(
                    config, state_path, stats_path,
                    fetcher=lambda *_args, **_kwargs: snapshot,
                )
        self.assertEqual(held["stats"]["data_unavailable_hold_symbols"], ["BTCUSDT"])
        health = recovered["stats"]["symbol_health"]["BTCUSDT"]
        self.assertEqual(health["position_status"], "data_recovered_reconcile")
        event = recovered["stats"]["data_recovery_events"][-1]
        self.assertEqual(event["symbol"], "BTCUSDT")
        self.assertTrue(event["reconciliation_uncertain"])
        self.assertEqual(event["replayed_bar_count"], 3)
        self.assertEqual(recovered["stats"]["reconciliation_uncertain_count"], 1)

    def test_perpetual_snapshot_cache_is_separated_by_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            binance = {"symbol": "BTCUSDT", "interval": "4h", "venue": "binance",
                       "fetched_at_epoch_ms": int(time.time() * 1000), "contract_klines": []}
            okx = {**binance, "venue": "okx", "fetched_at_epoch_ms": binance["fetched_at_epoch_ms"] + 1}
            perp_shadow._save_snapshot_cache(directory, binance)
            perp_shadow._save_snapshot_cache(directory, okx)
            files = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
            self.assertEqual(files, ["BTCUSDT-4h-binance.json", "BTCUSDT-4h-okx.json"])
            cached, _ = perp_shadow._load_snapshot_cache(directory, "BTCUSDT", "4h", 10)
            self.assertEqual(cached["venue"], "okx")

    def test_perpetual_open_trade_is_not_updated_from_another_provider(self):
        settings = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"], "interval": "4h"}})
        state = perp_shadow.empty_state()
        state["open_trades"].append({"symbol": "BTCUSDT", "provider": "binance", "status": "open"})
        snapshot = {"market_type": "linear_perpetual", "venue": "okx", "symbol": "BTCUSDT",
                    "interval": "4h", "contract_klines": [], "mark_price_klines": [],
                    "index_price_klines": [], "contract_specs": {"symbol": "BTCUSDT"}}
        with mock.patch.object(derivatives_data, "validate_perpetual_snapshot", return_value=[]), \
             mock.patch.object(perp_shadow, "update_trade") as update, \
             mock.patch.object(perp_shadow, "create_signal", return_value=None):
            perp_shadow.process_snapshot(snapshot, settings, state)
        update.assert_not_called()

    def test_perpetual_provider_mismatch_does_not_advance_market_time(self):
        settings = perp_shadow.shadow_settings({"derivatives": {
            "enabled": True, "research_only": True, "symbols": ["BTCUSDT"], "interval": "4h"}})
        state = perp_shadow.empty_state()
        state["market_time_by_symbol"] = {"BTCUSDT": 2_000}
        state["open_trades"].append({"symbol": "BTCUSDT", "provider": "binance", "status": "open"})
        snapshot = {"market_type": "linear_perpetual", "venue": "okx", "symbol": "BTCUSDT",
                    "interval": "4h", "contract_klines": [{"time": 10_000, "open": 100,
                    "high": 101, "low": 99, "close": 100}], "mark_price_klines": [],
                    "index_price_klines": [], "contract_specs": {"symbol": "BTCUSDT"}}
        with mock.patch.object(derivatives_data, "validate_perpetual_snapshot", return_value=[]):
            self.assertIsNone(perp_shadow.process_snapshot(snapshot, settings, state))
        self.assertEqual(state["market_time_by_symbol"]["BTCUSDT"], 2_000)

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
            stale_errors = validate_perp_shadow.validate(
                state_path, stats_path, max_age_minutes=30,
                now_ms=int(time.time() * 1000) + 31 * 60 * 1000,
            )
            with open(stats_path, encoding="utf-8") as file:
                tampered_stats = json.load(file)
            tampered_stats["sample_breakdown"]["provider"]["binance"] = {
                "count": 1, "wins": 1, "losses": 0,
            }
            with open(stats_path, "w", encoding="utf-8") as file:
                json.dump(tampered_stats, file)
            tampered_errors = validate_perp_shadow.validate(state_path, stats_path)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["processed_symbols"], 1)
        self.assertEqual(errors, [])
        self.assertIn("stats generated_at_utc is stale or invalid", stale_errors)
        self.assertTrue(any("provider does not cover closed trades" in error
                            for error in tampered_errors))

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

    def test_perpetual_market_state_flags_extreme_derivatives_conditions(self):
        bars = [{"time": 1_700_000_000_000 + i * 4 * 60 * 60 * 1000,
                 "open": 100 + i * 0.2, "high": 101 + i * 0.2,
                 "low": 99 + i * 0.2, "close": 100 + i * 0.2}
                for i in range(30)]
        snapshot = {
            "contract_klines": bars,
            "mark_price_klines": [{**row, "close": row["close"] * 1.02} for row in bars],
            "index_price_klines": bars,
            "funding_rates": [{"time": bars[-1]["time"], "funding_rate": 0.002}],
            "open_interest": [{"time": bars[-2]["time"], "open_interest": 100},
                              {"time": bars[-1]["time"], "open_interest": 130}],
        }
        state = perp_shadow.classify_market_state(snapshot, perp_shadow.shadow_settings({"derivatives": {}}))
        self.assertEqual(state["state"], "extreme_risk")
        self.assertEqual(state["risk_multiplier"], 0.0)
        self.assertIn("basis_extreme", state["flags"])
        self.assertIn("funding_extreme", state["flags"])
        self.assertIn("oi_shock", state["flags"])

    def test_perpetual_market_state_identifies_range_and_reduces_risk(self):
        bars = [{"time": 1_700_000_000_000 + i * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100}
                for i in range(30)]
        state = perp_shadow.classify_market_state(
            {"contract_klines": bars}, perp_shadow.shadow_settings({"derivatives": {}})
        )
        self.assertEqual(state["state"], "range")
        self.assertEqual(state["risk_multiplier"], 0.5)

    def test_market_state_direct_call_uses_same_move_threshold_as_settings(self):
        bars = [{"time": 1_700_000_000_000 + i * 4 * 60 * 60 * 1000,
                 "open": 100 + i, "high": 101 + i, "low": 99 + i,
                 "close": 100 + i} for i in range(30)]
        state = perp_shadow.classify_market_state({"contract_klines": bars})
        self.assertEqual(state["state"], "trend")

    def test_full_research_rejects_interval_without_oi_support(self):
        with mock.patch.object(derivatives_data, "OPEN_INTEREST_PERIODS", {"4h"}), \
             self.assertRaisesRegex(ValueError, "no Binance OI support"):
            perp_shadow.shadow_settings({"derivatives": {
                "enabled": True, "research_data_mode": "full_perpetual_research",
                "interval": "1d",
            }})

    def test_perpetual_volume_impact_slippage_is_capped_and_auditable(self):
        low = derivatives_risk.execution_slippage(
            0.0005, 1, 10000, "volume_impact", 0.001, 0.01
        )
        high = derivatives_risk.execution_slippage(
            0.0005, 100, 100, "volume_impact", 0.1, 0.01
        )
        fallback = derivatives_risk.execution_slippage(
            0.0005, 1, None, "volume_impact", 0.001, 0.01
        )
        self.assertGreater(low["rate"], low["base_rate"])
        self.assertEqual(high["rate"], 0.01)
        self.assertEqual(fallback["model"], "fixed_fallback")
        self.assertIsNone(fallback["participation_rate"])
        with self.assertRaises(ValueError):
            derivatives_risk.execution_slippage(float("nan"), 1, 100)

    def test_perpetual_backtest_records_volume_impact_slippage(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
                for index in range(334)]
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("long", ["test breakout"], {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        with mock.patch.object(sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 10), \
             mock.patch.object(sw, "turtle_levels", return_value={"exit_low": 50, "exit_high": 150}):
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0, slippage_rate=0.0005,
                slippage_model="volume_impact", slippage_impact_coefficient=0.001,
            )
        self.assertEqual(result["slippage_model"], "volume_impact")
        self.assertGreater(result["average_effective_slippage_pct"], 0.05)
        self.assertEqual(result["trades"][0]["entry_slippage"]["model"], "volume_impact")
        self.assertEqual(
            result["trades"][0]["entry_slippage"]["liquidity_proxy_time"],
            bars[330]["time"],
        )
        with self.assertRaisesRegex(ValueError, "finite and > 0"):
            perp_backtest._slippage(
                0.0005, 1, bars[0], "volume_impact", 0.001, 0.01,
                float("nan"),
            )

    def test_perpetual_signal_applies_market_state_risk_multiplier(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
                for index in range(30)]
        snapshot = {
            "symbol": "BTCUSDT", "venue": "binance", "contract_klines": bars,
            "contract_specs": {"quantity_step": 0.001},
        }
        settings = perp_shadow.shadow_settings({"derivatives": {"risk_fraction": 0.005}})
        state = perp_shadow.empty_state()
        market_state = {"state": "range", "risk_multiplier": 0.5, "flags": [], "metrics": {}}
        with mock.patch.object(perp_shadow, "classify_market_state", return_value=market_state), \
             mock.patch.object(sw, "build_turtle_signal", return_value=(
                 "long", ["test"], {"entry": 100, "n": 2, "stop": 96, "exit_level": 90}
             )):
            trade = perp_shadow.create_signal(snapshot, settings, state)
        self.assertEqual(trade["risk_fraction"], 0.0025)
        self.assertEqual(trade["configured_risk_fraction"], 0.005)
        self.assertEqual(trade["estimated_max_loss"], 25.0)

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

    def test_perpetual_snapshot_rejects_non_trading_contract_status(self):
        snapshot = {
            "market_type": "linear_perpetual", "symbol": "BTCUSDT",
            "contract_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100}],
            "mark_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100}],
            "index_price_klines": [{"time": 1, "open": 100, "high": 101, "low": 99, "close": 100}],
            "funding_rates": [], "open_interest": [],
            "contract_specs": {"symbol": "BTCUSDT", "contract_type": "PERPETUAL",
                                "quote_asset": "USDT", "status": "BREAK",
                                "price_tick": 0.1, "quantity_step": 0.001},
        }
        errors = derivatives_data.validate_perpetual_snapshot(snapshot, "4h", now_ms=1)
        self.assertIn("contract_specs status must be TRADING", errors)

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
            maintenance_margin_tiers=[{"max_notional": 50000, "rate": 0.01}],
        )
        self.assertIn("baseline", stress)
        self.assertIn("double_cost", stress)
        self.assertIn("quadruple_cost", stress)
        self.assertIn("summary", stress)
        self.assertEqual(
            stress["baseline"]["liquidation_model"]["model_version"],
            "isolated_linear_tiered_v1",
        )
        liquidity = perp_backtest.run_liquidity_stress_tests(
            snapshot, account_value=10000, risk_fraction=0.005, leverage=2,
            fee_rate=0.0004, slippage_rate=0.0005,
            slippage_model="volume_impact",
        )
        self.assertTrue(liquidity["summary"]["proxy_only"])
        self.assertEqual(liquidity["volume_20pct"]["liquidity_volume_multiplier"], 0.2)
        self.assertGreaterEqual(
            liquidity["volume_20pct"]["average_effective_slippage_pct"],
            liquidity["baseline"]["average_effective_slippage_pct"],
        )
        self.assertEqual(
            liquidity["summary"]["difference_definition"],
            "scenario_minus_baseline",
        )
        self.assertIn("volume_50pct_trade_count_delta", liquidity["summary"])
        self.assertIn("volume_20pct_max_drawdown_delta_pct", liquidity["summary"])
        self.assertIn(
            "volume_20pct_max_effective_slippage_delta_pct",
            liquidity["summary"],
        )

    def test_stress_helpers_reuse_precomputed_baseline(self):
        baseline = {
            "return_pct": 1.0, "trade_count": 2, "liquidation_count": 0,
            "max_drawdown_pct": 3.0, "ending_equity": 10100.0,
            "max_effective_slippage_pct": 0.1, "funding_cashflow": -2.0,
            "liquidation_model": {"model_version": "isolated_linear_tiered_v1"},
        }
        flipped = {**baseline, "return_pct": 0.8, "funding_cashflow": -4.0}
        with mock.patch.object(
            perp_backtest, "backtest_perpetual", return_value=flipped
        ) as backtest:
            funding = perp_backtest.run_funding_flip_stress(
                {"funding_rates": []}, baseline_result=baseline
            )
        self.assertIs(funding["normal"], baseline)
        self.assertEqual(backtest.call_count, 1)

        volume_50 = {
            **baseline, "return_pct": 0.5, "trade_count": 3,
            "max_drawdown_pct": 4.0, "max_effective_slippage_pct": 0.2,
        }
        volume_20 = {
            **baseline, "return_pct": -0.5, "trade_count": 4,
            "max_drawdown_pct": 5.5, "max_effective_slippage_pct": 0.4,
        }
        with mock.patch.object(
            perp_backtest, "backtest_perpetual", side_effect=[volume_50, volume_20]
        ) as backtest:
            liquidity = perp_backtest.run_liquidity_stress_tests(
                {}, baseline_result=baseline
            )
        self.assertIs(liquidity["baseline"], baseline)
        self.assertEqual(backtest.call_count, 2)
        self.assertEqual(liquidity["summary"]["volume_20pct_trade_count_delta"], 2)
        self.assertEqual(
            liquidity["summary"]["volume_20pct_max_drawdown_delta_pct"], 2.5
        )
        self.assertEqual(
            liquidity["summary"]["volume_20pct_max_effective_slippage_delta_pct"],
            0.3,
        )

    def test_maintenance_margin_stress_requires_configured_tiers(self):
        snapshot = {"market_type": "linear_perpetual", "interval": "4h"}
        result = perp_backtest.run_maintenance_margin_stress(snapshot)
        self.assertFalse(result["enabled"])
        self.assertEqual(
            result["reason"], "maintenance_margin_tiers_not_configured"
        )

    def test_maintenance_margin_stress_compares_tiered_minus_flat(self):
        flat = {
            "return_pct": 1.25, "trade_count": 3, "liquidation_count": 1,
            "max_drawdown_pct": 2.5, "ending_equity": 10125.0,
            "liquidation_model": {"model_version": "isolated_linear_v1"},
        }
        tiered = {
            "return_pct": 0.75, "trade_count": 3, "liquidation_count": 2,
            "max_drawdown_pct": 3.0, "ending_equity": 10075.0,
            "liquidation_model": {"model_version": "isolated_linear_tiered_v1"},
        }
        with mock.patch.object(
            perp_backtest, "backtest_perpetual", side_effect=[flat, tiered]
        ) as backtest:
            result = perp_backtest.run_maintenance_margin_stress(
                {}, maintenance_margin_rate=0.005,
                maintenance_margin_tiers=[{"max_notional": 50000, "rate": 0.01}],
            )
        self.assertTrue(result["enabled"])
        self.assertEqual(backtest.call_args_list[0].kwargs["maintenance_margin_tiers"], None)
        self.assertEqual(
            backtest.call_args_list[1].kwargs["maintenance_margin_tiers"][0]["maintenance_margin_rate"],
            0.01,
        )
        self.assertEqual(result["difference_definition"], "tiered_minus_flat")
        self.assertEqual(result["differences"]["return_pct"], -0.5)
        self.assertEqual(result["differences"]["liquidation_count"], 1)
        self.assertEqual(result["differences"]["ending_equity"], -50.0)

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

    def test_perpetual_entry_can_fill_on_final_known_bar(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100,
                 "volume": 100} for index in range(333)]
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("long", ["test breakout"],
                  {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        with mock.patch.object(
            sw, "build_turtle_signal", side_effect=[(None, [], None), signal]
        ):
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0, slippage_rate=0
            )
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["entry_time"], bars[-1]["time"])
        self.assertEqual(result["trades"][0]["reason"], "end_of_test")

    def test_perpetual_final_bar_checks_stop_before_forced_close(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100,
                 "volume": 100} for index in range(333)]
        bars[-1] = {**bars[-1], "low": 97}
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("long", ["test breakout"],
                  {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        levels = {"exit_low": 50.0, "exit_high": 150.0}
        with mock.patch.object(
            sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 5
        ), mock.patch.object(sw, "turtle_levels", return_value=levels):
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0, slippage_rate=0
            )
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["exit_time"], bars[-1]["time"])
        self.assertEqual(result["trades"][0]["reason"], "stop")

    def test_perpetual_forced_close_cost_is_in_final_drawdown(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100,
                 "volume": 100} for index in range(333)]
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("long", ["test breakout"],
                  {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        with mock.patch.object(
            sw, "build_turtle_signal", side_effect=[(None, [], None), signal]
        ):
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0.001, slippage_rate=0.001
            )
        self.assertLess(result["return_pct"], 0)
        self.assertAlmostEqual(
            result["max_drawdown_pct"], -result["return_pct"], places=4
        )

    def test_perpetual_backtest_pyramids_at_half_n_to_four_units(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 100.1, "low": 99.9, "close": 100,
                 "volume": 1000} for index in range(334)]
        bars[332] = {**bars[332], "high": 102, "close": 102}
        bars[333] = {**bars[333], "open": 102, "high": 102.1,
                     "low": 101.9, "close": 102}
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("long", ["test breakout"],
                  {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        levels = {"exit_low": 50.0, "exit_high": 150.0}
        with mock.patch.object(
            sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 5
        ), mock.patch.object(sw, "turtle_levels", return_value=levels):
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0, slippage_rate=0,
                risk_fraction=0.005, max_total_open_risk=0.04,
            )
        trade = result["trades"][0]
        self.assertEqual(len(trade["units"]), 4)
        self.assertEqual([unit["price"] for unit in trade["units"]],
                         [100.0, 100.5, 101.0, 101.5])
        self.assertEqual(trade["quantity"], 100.0)
        self.assertEqual(trade["avg_entry"], 100.75)
        self.assertEqual(result["execution_fill_count"], 5)

    def test_perpetual_backtest_pyramiding_respects_total_risk_cap(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 100.1, "low": 99.9, "close": 100,
                 "volume": 1000} for index in range(334)]
        bars[332] = {**bars[332], "high": 102, "close": 102}
        bars[333] = {**bars[333], "open": 102, "high": 102.1,
                     "low": 101.9, "close": 102}
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("long", ["test breakout"],
                  {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        levels = {"exit_low": 50.0, "exit_high": 150.0}
        with mock.patch.object(
            sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 5
        ), mock.patch.object(sw, "turtle_levels", return_value=levels):
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0, slippage_rate=0,
                risk_fraction=0.005, max_total_open_risk=0.01,
            )
        self.assertEqual(len(result["trades"][0]["units"]), 2)
        self.assertEqual(result["max_total_open_risk"], 0.01)

    def test_perpetual_backtest_pyramids_short_positions_symmetrically(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 100.1, "low": 99.9, "close": 100,
                 "volume": 1000} for index in range(334)]
        bars[332] = {**bars[332], "low": 98, "close": 98}
        bars[333] = {**bars[333], "open": 98, "high": 98.1,
                     "low": 97.9, "close": 98}
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        signal = ("short", ["test breakout"],
                  {"n": 1.0, "stop": 110.0, "exit_level": 150.0})
        levels = {"exit_low": 50.0, "exit_high": 150.0}
        with mock.patch.object(
            sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 5
        ), mock.patch.object(sw, "turtle_levels", return_value=levels):
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0, slippage_rate=0,
                risk_fraction=0.005, max_total_open_risk=0.04,
            )
        trade = result["trades"][0]
        self.assertEqual([unit["price"] for unit in trade["units"]],
                         [100.0, 99.5, 99.0, 98.5])
        self.assertEqual(trade["avg_entry"], 99.25)
        self.assertEqual(result["execution_fill_count"], 5)

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
            result = perp_backtest.backtest_perpetual(
                snapshot, fee_rate=0, slippage_rate=0,
                max_total_open_risk=0.005,
            )
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
        self.assertGreaterEqual(result["funding_delta"], 0.0)

    def test_perpetual_backtest_recomputes_stop_from_actual_gap_fill(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
                for index in range(334)]
        bars[331]["open"] = 110
        bars[331]["high"] = 111
        bars[331]["low"] = 109
        bars[331]["close"] = 110
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars, "funding_rates": []}
        with mock.patch.object(sw, "build_turtle_signal", side_effect=[
            ("long", ["test breakout"], {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        ] + [(None, [], None)] * 10):
            result = perp_backtest.backtest_perpetual(snapshot, fee_rate=0, slippage_rate=0,
                                                      account_value=10000, risk_fraction=0.005)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["entry"], 110)

    def test_perpetual_backtest_settles_funding_on_final_mark_bar(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
                for index in range(334)]
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars,
                    "funding_rates": [{"time": bars[-1]["time"], "funding_rate": 0.001}]}
        signal = ("long", ["test breakout"], {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        with mock.patch.object(sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 10), \
             mock.patch.object(sw, "turtle_levels", return_value={"exit_low": 1.0, "exit_high": 200.0}):
            result = perp_backtest.backtest_perpetual(
                snapshot, account_value=10000, risk_fraction=0.005,
                leverage=2, fee_rate=0, slippage_rate=0,
            )
        self.assertEqual(result["trade_count"], 1)
        self.assertLess(result["funding_cashflow"], 0)
        self.assertEqual(result["funding_mark_sources"]["prior_closed_mark_candle"], 1)

    def test_funding_mark_fallback_never_uses_future_candle(self):
        marks = [
            {"time": 1000, "close": 100},
            {"time": 2000, "close": 200},
        ]
        event = {"time": 2000, "funding_rate": 0.001, "mark_price": None}
        value, estimated, source = derivatives_risk.funding_mark_at_settlement(
            event, marks, 1000, 999
        )
        self.assertEqual(value, 100)
        self.assertTrue(estimated)
        self.assertEqual(source, "prior_closed_mark_candle")

    def test_shadow_funding_records_estimated_mark_source(self):
        trade = {"entry_time": 1000, "direction": "long", "quantity": 1,
                 "last_funding_time": 0, "funding_cashflow": 0}
        state = {"equity": 1000}
        funding = [{"time": 2000, "funding_rate": 0.001, "mark_price": None}]
        marks = [{"time": 1000, "close": 100}, {"time": 2000, "close": 200}]
        perp_shadow._funding_until(trade, funding, 2000, state, fallback_mark=200,
                                   mark_rows=marks, interval_ms=1000)
        self.assertEqual(trade["funding_settlement_count"], 1)
        self.assertEqual(trade["funding_mark_estimated_count"], 1)
        self.assertEqual(trade["funding_mark_sources"]["prior_closed_mark_candle"], 1)
        self.assertAlmostEqual(trade["funding_cashflow"], -0.1)

    def test_perpetual_backtest_excludes_funding_at_exact_entry_time(self):
        bars = [{"time": 1_700_000_000_000 + index * 4 * 60 * 60 * 1000,
                 "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
                for index in range(334)]
        snapshot = {"market_type": "linear_perpetual", "interval": "4h",
                    "contract_klines": bars, "mark_price_klines": bars,
                    "index_price_klines": bars,
                    "funding_rates": [{"time": bars[331]["time"], "funding_rate": 0.001}]}
        signal = ("long", ["test breakout"], {"n": 1.0, "stop": 90.0, "exit_level": 50.0})
        with mock.patch.object(sw, "build_turtle_signal", side_effect=[signal] + [(None, [], None)] * 10), \
             mock.patch.object(sw, "turtle_levels", return_value={"exit_low": 1.0, "exit_high": 200.0}):
            result = perp_backtest.backtest_perpetual(
                snapshot, account_value=10000, risk_fraction=0.005,
                leverage=2, fee_rate=0, slippage_rate=0,
            )
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["funding_cashflow"], 0.0)

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
            self.assertEqual(report["risk_model"]["liquidation_model"], "isolated_linear_v1")
            self.assertEqual(report["input_snapshot"]["research_mode"], "historical_snapshot_backtest")
            self.assertEqual(
                report["input_snapshot"]["last_contract_close_epoch_ms"],
                bars[-1]["time"] + 4 * 60 * 60 * 1000,
            )
            self.assertGreater(report["input_snapshot"]["data_age_hours"], 0)
            self.assertEqual(
                report["liquidity_stress"]["summary"]["model"],
                "contract_kline_volume_sensitivity_v1",
            )
            self.assertIs(
                report["funding_flip_stress"]["normal"],
                report["cost_stress"]["baseline"],
            )
            self.assertIs(
                report["liquidity_stress"]["baseline"],
                report["cost_stress"]["baseline"],
            )
            self.assertFalse(report["maintenance_margin_stress"]["enabled"])
            self.assertEqual(
                report["maintenance_margin_stress"]["reason"],
                "maintenance_margin_tiers_not_configured",
            )
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

    def test_single_scan_failure_publishes_failed_health_and_exit_code(self):
        original_argv = list(__import__("sys").argv)
        try:
            __import__("sys").argv = ["signal_watch.py", "--once"]
            with mock.patch.object(sw, "load_config", return_value={}), \
                 mock.patch.object(sw, "load_state", return_value={"open_trades": []}), \
                 mock.patch.object(sw, "scan_once", side_effect=RuntimeError("feed unavailable")), \
                 mock.patch.object(sw, "write_monitor_health") as health_writer, \
                 mock.patch.object(sw, "save_state"), \
                 mock.patch.object(sw, "portfolio_risk_snapshot", return_value={}), \
                 mock.patch.object(sw.logging, "basicConfig"), \
                 mock.patch.object(sw.logging, "FileHandler"), \
                 mock.patch.object(sw.logging, "StreamHandler"), \
                 mock.patch.object(sw.logging, "exception"):
                result = sw.main()
            self.assertEqual(result, 1)
            health_writer.assert_called_once()
            self.assertEqual(health_writer.call_args.kwargs["status"], "failed")
        finally:
            __import__("sys").argv = original_argv

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
             mock.patch.object(check_monitor_health, "PERP_STATS_PATH", os.path.join(directory, "perp_shadow_stats.json")), \
             mock.patch.object(check_monitor_health, "ALERT_STATE_PATH", os.path.join(directory, "monitor_alert_state.json")), \
             mock.patch.object(check_monitor_health, "send_serverchan", return_value=True) as notify:
            with open(check_monitor_health.HEALTH_PATH, "w", encoding="utf-8") as file:
                json.dump({"updated_at_epoch": 100, "status": "ok"}, file)
            with open(check_monitor_health.PERP_STATS_PATH, "w", encoding="utf-8") as file:
                json.dump({"generated_at_utc": "1970-01-01T00:33:10Z"}, file)
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

    def test_monitor_health_detects_perpetual_heartbeat_transition_once(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(check_monitor_health, "HEALTH_PATH", os.path.join(directory, "monitor_health.json")), \
             mock.patch.object(check_monitor_health, "PERP_STATS_PATH", os.path.join(directory, "perp_shadow_stats.json")), \
             mock.patch.object(check_monitor_health, "ALERT_STATE_PATH", os.path.join(directory, "monitor_alert_state.json")), \
            mock.patch.object(check_monitor_health, "send_serverchan", return_value=True) as notify:
            with open(check_monitor_health.HEALTH_PATH, "w", encoding="utf-8") as file:
                json.dump({"updated_at_epoch": 3590, "status": "ok"}, file)
            with open(check_monitor_health.PERP_STATS_PATH, "w", encoding="utf-8") as file:
                json.dump({"generated_at_utc": "1970-01-01T00:00:00Z"}, file)
            first = check_monitor_health.check(
                max_age_minutes=20, max_perp_age_minutes=30,
                now=3600, sendkey="SCT-test",
            )
            second = check_monitor_health.check(
                max_age_minutes=20, max_perp_age_minutes=30,
                now=3601, sendkey="SCT-test",
            )
        self.assertEqual(first["failed_components"], ["perpetual_shadow"])
        self.assertEqual(second["failed_components"], ["perpetual_shadow"])
        self.assertEqual(notify.call_count, 1)
        self.assertIn("perp_shadow_stats.json", notify.call_args.args[2])

    def test_monitor_health_alerts_risk_tier_degradation_once(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(check_monitor_health, "HEALTH_PATH", os.path.join(directory, "monitor_health.json")), \
             mock.patch.object(check_monitor_health, "PERP_STATS_PATH", os.path.join(directory, "perp_shadow_stats.json")), \
             mock.patch.object(check_monitor_health, "ALERT_STATE_PATH", os.path.join(directory, "monitor_alert_state.json")), \
             mock.patch.object(check_monitor_health, "send_serverchan", return_value=True) as notify:
            with open(check_monitor_health.HEALTH_PATH, "w", encoding="utf-8") as file:
                json.dump({"updated_at_epoch": 3590, "status": "ok"}, file)
            stats = {
                "generated_at_utc": "1970-01-01T00:59:50Z",
                "risk_tier_health": {
                    "status": "degraded",
                    "degraded_symbols": ["BTCUSDT"],
                    "unavailable_symbols": [],
                    "max_consecutive_refresh_failures": 1,
                    "minimum_remaining_stale_minutes": 100,
                    "by_symbol": {"BTCUSDT": {
                        "status": "stale_fallback", "cache_age_minutes": 1340,
                        "remaining_stale_minutes": 100,
                        "refresh_error_category": "TimeoutError",
                    }},
                },
            }
            with open(check_monitor_health.PERP_STATS_PATH, "w", encoding="utf-8") as file:
                json.dump(stats, file)
            first = check_monitor_health.check(now=3600, sendkey="SCT-test")
            second = check_monitor_health.check(now=3601, sendkey="SCT-test")
        self.assertEqual(first["status"], "degraded")
        self.assertEqual(first["advisory_components"], ["risk_tiers"])
        self.assertEqual(second["status"], "degraded")
        self.assertEqual(notify.call_count, 1)
        self.assertIn("风险档位降级", notify.call_args.args[1])
        self.assertIn("BTCUSDT", notify.call_args.args[2])

    def test_monitor_health_risk_tier_failure_buckets_limit_repeat_alerts(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(check_monitor_health, "HEALTH_PATH", os.path.join(directory, "monitor_health.json")), \
             mock.patch.object(check_monitor_health, "PERP_STATS_PATH", os.path.join(directory, "perp_shadow_stats.json")), \
             mock.patch.object(check_monitor_health, "ALERT_STATE_PATH", os.path.join(directory, "monitor_alert_state.json")), \
             mock.patch.object(check_monitor_health, "send_serverchan", return_value=True) as notify:
            with open(check_monitor_health.HEALTH_PATH, "w", encoding="utf-8") as file:
                json.dump({"updated_at_epoch": 3590, "status": "ok"}, file)
            stats = {
                "generated_at_utc": "1970-01-01T00:59:50Z",
                "risk_tier_health": {
                    "status": "degraded", "degraded_symbols": ["BTCUSDT"],
                    "unavailable_symbols": [], "minimum_remaining_stale_minutes": 130,
                    "by_symbol": {},
                },
            }
            for failures, now in ((1, 3600), (2, 3601), (3, 3602)):
                stats["risk_tier_health"]["max_consecutive_refresh_failures"] = failures
                with open(check_monitor_health.PERP_STATS_PATH, "w", encoding="utf-8") as file:
                    json.dump(stats, file)
                check_monitor_health.check(now=now, sendkey="SCT-test")
        self.assertEqual(notify.call_count, 2)

    def test_monitor_health_alerts_risk_tier_recovery_once(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(check_monitor_health, "HEALTH_PATH", os.path.join(directory, "monitor_health.json")), \
             mock.patch.object(check_monitor_health, "PERP_STATS_PATH", os.path.join(directory, "perp_shadow_stats.json")), \
             mock.patch.object(check_monitor_health, "ALERT_STATE_PATH", os.path.join(directory, "monitor_alert_state.json")), \
             mock.patch.object(check_monitor_health, "send_serverchan", return_value=True) as notify:
            with open(check_monitor_health.HEALTH_PATH, "w", encoding="utf-8") as file:
                json.dump({"updated_at_epoch": 3590, "status": "ok"}, file)
            degraded = {
                "generated_at_utc": "1970-01-01T00:59:50Z",
                "risk_tier_health": {
                    "status": "degraded", "degraded_symbols": ["BTCUSDT"],
                    "unavailable_symbols": [], "max_consecutive_refresh_failures": 1,
                    "minimum_remaining_stale_minutes": 100, "by_symbol": {},
                },
            }
            healthy = {
                "generated_at_utc": "1970-01-01T00:59:50Z",
                "risk_tier_health": {
                    "status": "healthy", "degraded_symbols": [],
                    "unavailable_symbols": [], "max_consecutive_refresh_failures": 0,
                    "minimum_remaining_stale_minutes": 1400, "by_symbol": {},
                },
            }
            with open(check_monitor_health.PERP_STATS_PATH, "w", encoding="utf-8") as file:
                json.dump(degraded, file)
            check_monitor_health.check(now=3600, sendkey="SCT-test")
            with open(check_monitor_health.PERP_STATS_PATH, "w", encoding="utf-8") as file:
                json.dump(healthy, file)
            recovered = check_monitor_health.check(now=3601, sendkey="SCT-test")
            repeated = check_monitor_health.check(now=3602, sendkey="SCT-test")
        self.assertEqual(recovered["status"], "healthy")
        self.assertEqual(repeated["status"], "healthy")
        self.assertEqual(notify.call_count, 2)
        self.assertIn("风险档位恢复", notify.call_args.args[1])

    def test_monitor_recovery_reports_remaining_risk_tier_degradation(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(check_monitor_health, "HEALTH_PATH", os.path.join(directory, "monitor_health.json")), \
             mock.patch.object(check_monitor_health, "PERP_STATS_PATH", os.path.join(directory, "perp_shadow_stats.json")), \
             mock.patch.object(check_monitor_health, "ALERT_STATE_PATH", os.path.join(directory, "monitor_alert_state.json")), \
             mock.patch.object(check_monitor_health, "send_serverchan", return_value=True) as notify:
            with open(check_monitor_health.ALERT_STATE_PATH, "w", encoding="utf-8") as file:
                json.dump({
                    "status": "stale", "failed_components": ["ordinary_monitor"],
                    "advisory_components": [], "risk_tier_signature": "not_monitored",
                    "last_health_status": "stale",
                }, file)
            with open(check_monitor_health.HEALTH_PATH, "w", encoding="utf-8") as file:
                json.dump({"updated_at_epoch": 3590, "status": "ok"}, file)
            with open(check_monitor_health.PERP_STATS_PATH, "w", encoding="utf-8") as file:
                json.dump({
                    "generated_at_utc": "1970-01-01T00:59:50Z",
                    "risk_tier_health": {
                        "status": "degraded", "degraded_symbols": ["BTCUSDT"],
                        "unavailable_symbols": [], "max_consecutive_refresh_failures": 1,
                        "minimum_remaining_stale_minutes": 100, "by_symbol": {},
                    },
                }, file)
            result = check_monitor_health.check(now=3600, sendkey="SCT-test")
        self.assertEqual(result["status"], "degraded")
        self.assertIn("监控恢复", notify.call_args.args[1])
        self.assertIn("仍降级", notify.call_args.args[1])

    def test_monitor_health_tolerates_malformed_risk_tier_counters(self):
        tier_state = check_monitor_health.risk_tier_alert_state({
            "risk_tier_health": {
                "status": "degraded", "degraded_symbols": ["BTCUSDT"],
                "unavailable_symbols": [],
                "max_consecutive_refresh_failures": "invalid",
                "minimum_remaining_stale_minutes": {"bad": "value"},
                "by_symbol": {},
            },
        })
        self.assertTrue(tier_state["active"])
        self.assertEqual(tier_state["max_consecutive_refresh_failures"], 0)
        self.assertIsNone(tier_state["minimum_remaining_stale_minutes"])
        self.assertIn("unknown", tier_state["signature"])

    def test_monitor_health_risk_tier_advisory_does_not_fail_workflow(self):
        tier_state = check_monitor_health.risk_tier_alert_state({
            "risk_tier_health": {
                "status": "unavailable", "degraded_symbols": ["BTCUSDT"],
                "unavailable_symbols": ["BTCUSDT"],
                "max_consecutive_refresh_failures": 12,
                "minimum_remaining_stale_minutes": 0,
                "by_symbol": {},
            },
        })
        self.assertTrue(tier_state["active"])
        self.assertIn("12_plus", tier_state["signature"])
        self.assertIn("expired", tier_state["signature"])

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
