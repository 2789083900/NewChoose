import unittest
import json
import os
import tempfile
from unittest import mock

import signal_watch as sw
import track_signals
import backtest_turtle
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
        self.assertAlmostEqual(plan["unit_quantity"], 10000 * 0.01 / plan["n"])

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
