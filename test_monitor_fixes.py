import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

import signal_watch as sw
import check_monitor_health as health
import validate_notification_outbox as validator
import monitor_reporting as report
import workflow_diagnostics as diagnostics

NOW = 1790146686000  # 2026-09-23 14:58:06 Beijing
OPEN = 1790121600000  # 08:00 Beijing, closes at 12:00


class ReportingFixTests(unittest.TestCase):
    def test_expired_signals_not_delivery_failures_but_still_degraded(self):
        rows = [{'delivered': False, 'expired': True, 'results': []} for _ in range(3)]
        counts = report.notification_round_summary(rows)
        self.assertEqual(counts['round_failed'], 0)
        self.assertEqual(counts['expired_signals'], 3)
        self.assertEqual(counts['channel_attempts'], 0)
        self.assertEqual(report.runtime_health_reasons({}, rows), ['signals_expired_before_dispatch'])

    def test_failed_attempt_expired_outbox_and_skipped_signal_separate(self):
        rows = [{'delivered':False,'results':[{'ok':False}]},
                {'event_id':'old','delivered':False,'expired':True,'results':[]},
                {'delivered':False,'expired':True,'results':[]}]
        counts = report.notification_round_summary(rows)
        self.assertEqual([counts[k] for k in ('round_failed','expired_signals','expired_notifications','channel_failures')], [1,1,1,1])

    def test_backup_success_keeps_channel_failure_but_not_event_failure(self):
        counts = report.notification_round_summary([{'delivered':True,'results':[{'ok':False},{'ok':True}]}])
        self.assertEqual(counts['round_failed'],0)
        self.assertEqual(counts['channel_attempts'],2)
        self.assertEqual(counts['channel_failures'],1)

    def test_dedup_does_not_count_previous_channel_attempt(self):
        counts = report.notification_round_summary([{'delivered':True,'deduplicated':True,'results':[{'ok':True}]}])
        self.assertEqual(counts['channel_attempts'],0)

    def market(self, symbol='BTC', interval=14400000, opened=OPEN, **extra):
        return dict(symbol=symbol, interval='4h', interval_ms=interval, bar_open_epoch_ms=opened, **extra)

    def test_4h_bar_age_is_not_418_minutes_or_a_stale_feed(self):
        result = report.market_freshness([self.market()],NOW)
        self.assertEqual(result['data_lag_minutes'],178.1)
        self.assertEqual(result['data_reference_close_epoch_ms'],OPEN+14400000)
        self.assertEqual(result['max_closed_bar_overdue_minutes'],0)
        self.assertEqual(result['data_freshness_status'],'fresh')

    def test_slowest_market_not_hidden_by_fresh_market(self):
        result = report.market_freshness([self.market(),self.market('OLD',opened=OPEN-14400000)],NOW)
        self.assertEqual(result['data_freshness_status'],'stale')
        self.assertEqual(result['max_closed_bar_overdue_minutes'],178.1)
        self.assertEqual(result['market_freshness'][1]['missing_closed_bars'],1)

    def test_mixed_intervals_use_their_own_close(self):
        result = report.market_freshness([self.market(),self.market('FAST',interval=3600000,opened=OPEN+4*3600000)],NOW)
        self.assertEqual(result['data_lag_minutes'],178.1)
        self.assertEqual(result['max_closed_bar_overdue_minutes'],58.1)
        self.assertEqual(result['data_freshness_status'],'stale')

    def test_close_boundary_and_tolerance(self):
        market=self.market()
        for minutes, expected in [(0,'fresh'),(30,'fresh'),(31,'stale')]:
            value=report.market_freshness([market],OPEN+2*14400000+minutes*60000)
            self.assertEqual(value['data_freshness_status'],expected)

    def test_unknown_future_unclosed_and_nan_not_healthy(self):
        for market in [self.market(opened=NOW),self.market(closed_only=False),self.market(interval=None),self.market(opened=float('nan'))]:
            result=report.market_freshness([market],NOW)
            self.assertEqual(result['data_freshness_status'],'unknown')
            self.assertIsNone(result['data_lag_minutes'])
        self.assertEqual(report.market_freshness([],NOW)['data_freshness_status'],'unknown')

    def test_legacy_open_time_not_silently_reinterpreted(self):
        with patch.object(sw.time,'time',return_value=NOW/1000):
            scan=sw.scan_health_payload('r',{'latest_bar_time':OPEN},100,80,0)
        self.assertIsNone(scan['data_lag_minutes'])
        self.assertEqual(scan['data_freshness_status'],'unknown')

    def test_scan_payload_integrates_per_market_times(self):
        with patch.object(sw.time,'time',return_value=NOW/1000):
            scan=sw.scan_health_payload('r',{'latest_bar_time':OPEN,'market_freshness':{'BTC|4h':self.market()}},100,80,0)
        self.assertEqual(scan['data_lag_minutes'],178.1)
        self.assertNotIn('market_data_stale',report.runtime_health_reasons(scan,[]))

    def test_daily_summary_correct_counts_and_no_false_bar_age_alarm(self):
        scan={'coverage_pct':100,'minimum_coverage_pct':80,**report.market_freshness([self.market()],NOW)}
        rows=[{'delivered':False,'expired':True,'results':[]} for _ in range(3)]
        with patch.object(sw,'formal_runtime_summary',return_value={'data_status':'ok'}),patch.object(sw,'notification_outbox_summary',return_value={'status':'ok','pending':0,'exhausted':0}):
            summary=sw.build_daily_summary(scan,{},deliveries=rows)
        self.assertEqual(summary['notifications']['round_failed'],0)
        self.assertEqual(summary['notifications']['expired_signals'],3)
        self.assertTrue(summary['action_required'])
        self.assertIn('过期未发送',summary['action_reasons'][0])
        self.assertNotIn('通知失败',str(summary['action_reasons']))
        self.assertNotIn('数据延迟超过',str(summary['action_reasons']))

    def test_health_persists_reasons_and_resets_after_recovery(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(sw,'HEALTH_PATH',str(Path(tmp)/'health.json')):
            sw.write_monitor_health(status='degraded',health_reasons=['signals_expired_before_dispatch'],notifications=[{'delivered':False,'expired':True,'results':[]}])
            data=json.loads(Path(sw.HEALTH_PATH).read_text(encoding='utf-8'))
            self.assertEqual(data['push']['failed'],0)
            self.assertEqual(data['notification_summary']['expired_signals'],1)
            state,reasons=health.classify_ordinary_monitor(data,1,35)
            self.assertEqual(state,'signals_expired_before_dispatch')
            self.assertNotIn('notification_degraded',reasons)
            sw.write_monitor_health(status='ok',health_reasons=[],notifications=[])
            data=json.loads(Path(sw.HEALTH_PATH).read_text(encoding='utf-8'))
            self.assertEqual(data['health_reasons'],[])

    def test_heartbeat_stale_still_takes_precedence(self):
        state,reasons=health.classify_ordinary_monitor({'status':'degraded','health_reasons':['signals_expired_before_dispatch']},9005,35)
        self.assertEqual(state,'heartbeat_stale')
        self.assertIn('signals_expired_before_dispatch',reasons)

    def test_expired_signal_does_not_send_or_register(self):
        event={'symbol':'ADAUSDT','interval':'4h','label':'test','timing':{'shadow_entry_status':'late'}}
        with patch.object(sw,'build_message',return_value='offline'),patch.object(sw,'record_signal_event') as record,patch.object(sw,'_load_outbox',return_value={'events':[]}),patch.object(sw,'send_outbox_notification') as send,patch.object(sw,'register_trade') as trade:
            rows=sw.process_events([event],{})
        send.assert_not_called(); trade.assert_not_called(); record.assert_called_once()
        self.assertEqual(report.notification_round_summary(rows)['expired_signals'],1)

    def test_expiry_between_scan_and_dispatch_does_not_register_trade(self):
        event={'symbol':'ADAUSDT','interval':'4h','label':'test','timing':{'shadow_entry_status':'on_time'}}
        with patch.object(sw,'build_message',return_value='offline'),patch.object(sw,'record_signal_event'),patch.object(sw,'_load_outbox',return_value={'events':[]}),patch.object(sw,'send_outbox_notification',return_value={'delivered':False,'expired':True,'results':[]}),patch.object(sw,'register_trade') as trade:
            rows=sw.process_events([event],{})
        trade.assert_not_called()
        self.assertEqual(event['notification_status'],'expired')
        self.assertEqual(report.notification_round_summary(rows)['expired_notifications'],1)

    def test_old_delivered_event_does_not_replay_provider_result(self):
        outbox={'events':[{'event_id':'x','status':'delivered','last_results':[{'ok':True}]}]}
        with patch.object(sw,'_load_outbox',return_value=outbox),patch.object(sw,'_dispatch_outbox_event') as send:
            delivery=sw.send_outbox_notification('x','title','body',{})
        send.assert_not_called()
        self.assertTrue(delivery['deduplicated'])
        self.assertEqual(delivery['results'],[])

    def test_expired_outbox_result_does_not_replay_failed_attempt(self):
        outbox={'events':[{'event_id':'x','status':'pending','expires_epoch_ms':1,'last_results':[{'ok':False}]}]}
        with patch.object(sw,'_load_outbox',return_value=outbox),patch.object(sw,'_save_outbox'),patch.object(sw,'_dispatch_outbox_event') as send:
            delivery=sw.send_outbox_notification('x','title','body',{})
        send.assert_not_called()
        self.assertTrue(delivery['expired'])
        self.assertEqual(delivery['results'],[])

    def test_once_pipeline_keeps_late_signals_degraded_without_channel_failure(self):
        from contextlib import ExitStack
        event={'symbol':'ADAUSDT','interval':'4h','label':'test','timing':{'shadow_entry_status':'late'}}
        def scan(config, state, run_id=None, diagnostics=None):
            diagnostics.update(expected_markets=1, successful_markets=1,
                               market_freshness={'ADA|4h':self.market()})
            return [event]
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch('sys.argv',['signal_watch.py','--once']))
            stack.enter_context(patch.object(sw,'LOG_PATH',str(Path(tmp)/'test.log')))
            stack.enter_context(patch.object(sw.time,'time',return_value=NOW/1000))
            stack.enter_context(patch.object(sw,'load_config',return_value={}))
            stack.enter_context(patch.object(sw,'load_state',return_value={}))
            stack.enter_context(patch.object(sw,'scan_once',side_effect=scan))
            for name in ('save_state','settle_trades','settle_research_trades','record_signal_event'):
                stack.enter_context(patch.object(sw,name))
            stack.enter_context(patch.object(sw,'portfolio_risk_snapshot',return_value={}))
            stack.enter_context(patch.object(sw,'research_runtime_summary',return_value={}))
            stack.enter_context(patch.object(sw,'send_daily_status_digest',return_value=None))
            stack.enter_context(patch.object(sw,'build_message',return_value='offline'))
            stack.enter_context(patch.object(sw,'_load_outbox',return_value={'events':[]}))
            send=stack.enter_context(patch.object(sw,'send_outbox_notification'))
            write=stack.enter_context(patch.object(sw,'write_monitor_health'))
            self.assertEqual(sw.main(),0)
            send.assert_not_called()
            self.assertEqual(write.call_args.kwargs['status'],'degraded')
            self.assertEqual(write.call_args.kwargs['health_reasons'],['signals_expired_before_dispatch'])
            counts=report.notification_round_summary(write.call_args.kwargs['notifications'])
            self.assertEqual(counts['round_failed'],0)
            self.assertEqual(counts['expired_signals'],1)

    def test_failed_early_warning_only_attempted_once_per_scan(self):
        event={'symbol':'BTC','interval':'4h','direction':'long','label':'test','early_warning':True,'bar_time':1}
        eid='spot-warning|BTC|4h|1|long|test'
        with patch.object(sw,'build_early_warning_message',return_value='offline'),patch.object(sw,'send_outbox_notification',return_value={'event_id':eid,'delivered':False,'results':[{'ok':False}]}) as send,patch.object(sw,'_load_outbox',return_value={'events':[{'event_id':eid,'status':'pending'}]}):
            sw.process_events([event],{})
        self.assertEqual(send.call_count,1)


class ValidatorFixTests(unittest.TestCase):
    def test_empty_queue_does_not_override_degraded_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            (path/'outbox.json').write_text('{"events":[]}',encoding='utf-8')
            (path/'health.json').write_text(json.dumps({'status':'degraded','health_reasons':['signals_expired_before_dispatch']}),encoding='utf-8')
            result=validator.validate(str(path/'outbox.json'),health_path=str(path/'health.json'))
        self.assertEqual(result['notification_queue_status'],'ok')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['monitor_health_reasons'],['signals_expired_before_dispatch'])
        self.assertEqual(result['failure_reasons'],['monitor_health_not_ok'])

    def test_explicit_missing_health_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=validator.validate(str(Path(tmp)/'outbox.json'),health_path=str(Path(tmp)/'health.json'))
        self.assertEqual(result['status'],'failed')
        self.assertIn('monitor_health_missing',result['failure_reasons'])

    def test_backlog_and_exhaustion_still_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'outbox.json'
            p.write_text(json.dumps({'events':[{'event_id':'p','status':'pending','expires_epoch_ms':NOW+1000},{'event_id':'e','status':'exhausted'}]}))
            result=validator.validate(str(p),now_ms=NOW)
        self.assertEqual(result['notification_queue_status'],'failed')
        self.assertEqual(set(result['failure_reasons']),{'notification_backlog','notification_retries_exhausted'})

    def test_cli_malformed_config_does_not_print_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'secret.json'; p.write_text('{SECRET_TOKEN')
            output=io.StringIO()
            with patch('sys.argv',['validate','--config',str(p),'--path',str(Path(tmp)/'outbox.json')]),patch('sys.stdout',output),patch.dict('os.environ',{'GITHUB_STEP_SUMMARY':''}):
                with self.assertRaises(SystemExit) as exc:
                    validator.main()
            self.assertEqual(exc.exception.code,1)
            self.assertNotIn('SECRET_TOKEN',output.getvalue())


class DiagnosticsFixTests(unittest.TestCase):
    def test_scheduled_gaps_do_not_include_manual_runs(self):
        rows=diagnostics.summarize_runs([
            {'id':3,'event':'schedule','created_at':'2026-09-23T09:00:00Z','run_started_at':'2026-09-23T09:00:20Z'},
            {'id':2,'event':'workflow_dispatch','created_at':'2026-09-23T08:30:00Z'},
            {'id':1,'event':'schedule','created_at':'2026-09-23T08:00:00Z','run_started_at':'2026-09-23T08:00:05Z'}])
        self.assertEqual(rows[-1]['previous_scheduled_created_gap_seconds'],3600)
        self.assertEqual(rows[-1]['created_to_started_seconds'],20)
        self.assertIsNone(rows[1]['previous_scheduled_created_gap_seconds'])

    def test_api_failure_sanitized_and_no_write_endpoint(self):
        def opener(request,timeout):
            self.assertEqual(request.get_method(),'GET')
            raise urllib.error.HTTPError(request.full_url,403,'SECRET_URL',{},None)
        data=diagnostics.github_history('owner/repo','master','SECRET_TOKEN',opener)
        self.assertEqual(data['status'],'partial')
        self.assertNotIn('SECRET',json.dumps(data))
        self.assertEqual(data['workflows']['signal-monitor.yml']['http_status'],403)

    def test_invalid_repository_no_request(self):
        with patch('urllib.request.urlopen') as request:
            result=diagnostics.github_history('https://evil.example','master')
        request.assert_not_called()
        self.assertEqual(result['status'],'unavailable')

    def test_default_report_offline_excludes_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen') as request:
            data=diagnostics.build(tmp,env={'GITHUB_TOKEN':'SECRET_TOKEN','EXTERNAL_HEARTBEAT_URL':'SECRET_URL'})
        request.assert_not_called()
        self.assertTrue(data['external_heartbeat_configured'])
        self.assertNotIn('SECRET',json.dumps(data))

    def test_workflow_diagnostics_preserve_gates_and_thresholds(self):
        root=Path(__file__).parent
        for name in ['signal-monitor.yml','monitor-health.yml']:
            text=(root/'.github/workflows'/name).read_text(encoding='utf-8')
            self.assertIn('actions: read',text)
            self.assertIn('workflow_diagnostics.py --github',text)
            self.assertIn('retention-days: 14',text)
            self.assertIn('group: coinpulse-state-writer',text)
        text=(root/'.github/workflows/signal-monitor.yml').read_text(encoding='utf-8')
        self.assertIn('--health monitor_health.json --config signal_watch.config.json',text)
        self.assertIn("steps.validate_notifications.outcome == 'success'",text)
        self.assertIn('research_signal_archive',text)
        text=(root/'.github/workflows/monitor-health.yml').read_text(encoding='utf-8')
        self.assertIn('--max-age-minutes 35 --max-perp-age-minutes 45',text)
        self.assertIn('run: exit 1',text)


if __name__=='__main__':
    unittest.main()
