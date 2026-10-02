#!/usr/bin/env python3
"""Read-only, secret-free runtime and Actions timing evidence. Never dispatch workflows."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request


WORKFLOW_SCHEDULE_SECONDS = {
    'signal-monitor.yml': 5 * 60,
    'monitor-health.yml': 15 * 60,
}


def epoch(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def summarize_runs(runs):
    rows = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        created, started = epoch(run.get('created_at')), epoch(run.get('run_started_at'))
        row = {k: run.get(k) for k in ('id', 'run_number', 'event', 'status', 'conclusion', 'created_at', 'run_started_at', 'run_attempt')}
        row['created_to_started_seconds'] = round(started-created, 1) if created is not None and started is not None and started >= created else None
        rows.append(row)
    rows.sort(key=lambda r: epoch(r.get('created_at')) or 0)
    previous = None
    for row in rows:
        created = epoch(row.get('created_at'))
        row['previous_scheduled_created_gap_seconds'] = (round(created-previous, 1)
            if row.get('event') == 'schedule' and created is not None and previous is not None else None)
        if row.get('event') == 'schedule' and created is not None:
            previous = created
    return rows


def summarize_schedule_cadence(rows, expected_interval_seconds):
    scheduled = [row for row in rows if row.get('event') == 'schedule']
    gaps = [row.get('previous_scheduled_created_gap_seconds') for row in scheduled]
    gaps = [gap for gap in gaps if isinstance(gap, (int, float))]
    return {
        'expected_interval_seconds': expected_interval_seconds,
        'observed_schedule_count': len(scheduled),
        'first_created_at': scheduled[0].get('created_at') if scheduled else None,
        'last_created_at': scheduled[-1].get('created_at') if scheduled else None,
        'max_created_gap_seconds': max(gaps) if gaps else None,
        'gaps_over_twice_expected': sum(
            gap > expected_interval_seconds * 2 for gap in gaps
        ),
        'gaps_over_one_hour': sum(gap > 3600 for gap in gaps),
    }


def read_github_runs(url, headers, opener=None):
    with (opener or urllib.request.urlopen)(
            urllib.request.Request(url, headers=headers), timeout=10) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('response too large')
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get('workflow_runs'), list):
            raise ValueError('invalid response')
        return payload['workflow_runs']


def github_history(repository, branch, token=None, opener=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
        return {'status': 'unavailable', 'reason': 'repository_not_configured'}
    result = {'status': 'ok', 'workflows': {}, 'limits': [
        'Recent retained runs only; missing/dropped schedule events are not themselves in this API.',
        'Large gaps between created schedule events are evidence of cadence loss, not proof of a specific platform cause.',
        'created_to_started is not scheduled_due_to_started delay; attempts may affect it.',
        'Shared concurrency may replace pending runs; canceled alone does not establish the cause.',
    ]}
    for filename in ('signal-monitor.yml', 'monitor-health.yml'):
        base_url = 'https://api.github.com/repos/' + repository + '/actions/workflows/' + filename + '/runs?'
        headers = {'User-Agent': 'CoinPulse-ReadOnly-Diagnostics', 'Accept': 'application/vnd.github+json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            recent_url = base_url + urllib.parse.urlencode({'per_page':20, 'branch':branch})
            schedule_url = base_url + urllib.parse.urlencode(
                {'per_page':100, 'branch':branch, 'event':'schedule'})
            recent_rows = summarize_runs(read_github_runs(recent_url, headers, opener))
            schedule_rows = summarize_runs(read_github_runs(schedule_url, headers, opener))
            result['workflows'][filename] = {
                'status':'ok',
                'runs':recent_rows,
                'scheduled_runs':schedule_rows,
                'schedule_cadence':summarize_schedule_cadence(
                    schedule_rows, WORKFLOW_SCHEDULE_SECONDS[filename]),
            }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result['status'] = 'partial'
            result['workflows'][filename] = {'status':'unavailable','error_category':type(exc).__name__,
                'http_status':getattr(exc, 'code', None)}
    return result


def build(source, include_github=False, env=None):
    env = os.environ if env is None else env
    source = Path(source)
    sources = {}
    def read(name):
        try:
            value = json.loads((source/name).read_text(encoding='utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError('not object')
            sources[name] = 'ok'
            return value
        except (OSError, ValueError):
            sources[name] = 'missing_or_invalid'
            return {}
    health = read('monitor_health.json')
    alert = read('monitor_alert_state.json')
    scan = health.get('scan') if isinstance(health.get('scan'), dict) else {}
    try:
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=source,
                                  capture_output=True, text=True, timeout=5)
        checkout_sha = revision.stdout.strip() if revision.returncode == 0 else None
        if not re.fullmatch(r'[0-9a-f]{40,64}', checkout_sha or ''):
            checkout_sha = None
    except (OSError, subprocess.TimeoutExpired):
        checkout_sha = None
    result = {'schema_version':1, 'observed_at':datetime.now(timezone.utc).isoformat(),
        'local_head_sha_at_diagnostics':checkout_sha,
        'workflow':{k:env.get(k) for k in (
            'GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_EVENT_NAME','GITHUB_SHA',
            'GITHUB_REF_NAME','TRIGGERING_WORKFLOW_NAME',
            'TRIGGERING_WORKFLOW_CONCLUSION','TRIGGERING_WORKFLOW_RUN_ID')},
        'health':{k:health.get(k) for k in ('status','updated_at_epoch','health_reasons','notification_summary')},
        'scan':{k:scan.get(k) for k in ('run_id','coverage_pct','data_lag_basis','data_lag_minutes','data_freshness_status','max_closed_bar_overdue_minutes')},
        'alert':{k:alert.get(k) for k in ('status','checked_at_epoch','health_age_seconds','perpetual_age_seconds','ordinary_state','ordinary_reasons')},
        'external_heartbeat_configured':bool(env.get('EXTERNAL_HEARTBEAT_URL','').strip()),
        'sources':sources,
        'limits':['Health alert snapshot is transition-persisted, not necessarily the latest check.',
                  'GITHUB_SHA is the event revision; local_head_sha_at_diagnostics may include newer branch state and state-save commits.',
                  'This report does not send, repair, or prove phone receipt.']}
    if include_github:
        result['actions_history'] = github_history(
            env.get('GITHUB_REPOSITORY'),
            env.get('COINPULSE_STATE_BRANCH') or env.get('GITHUB_REF_NAME','master'),
            env.get('GITHUB_TOKEN'))
    else:
        result['actions_history'] = {'status':'not_requested'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='.')
    parser.add_argument('--github', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = build(args.source, args.github)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as file:
        json.dump(result,file,ensure_ascii=False,indent=2)
        file.write('\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as file:
            file.write('## CoinPulse 运行时序诊断\n\n')
            file.write('外部心跳配置：' + ('已设置（未代表验收通过）' if result['external_heartbeat_configured'] else '未设置') + '\n\n')
            file.write('最近运行的创建/开始间隔见 monitor-diagnostics artifact；不能将其直接当作计划触发延迟。\n\n')
            file.write('API读取状态：' + result['actions_history']['status'] + '。诊断不修改任务调度，不发送通知。\n')
            for filename, evidence in result['actions_history'].get('workflows', {}).items():
                cadence = evidence.get('schedule_cadence') if isinstance(evidence, dict) else None
                if cadence:
                    file.write(
                        f"- {filename}: 最近{cadence['observed_schedule_count']}次定时创建，"
                        f"最大创建间隔{cadence['max_created_gap_seconds']}秒，"
                        f"超过两倍目标间隔{cadence['gaps_over_twice_expected']}次。\n")
    print('Read-only diagnostics saved; history status: ' + result['actions_history']['status'])


if __name__ == '__main__':
    main()
