#!/usr/bin/env python3
"""Read-only, secret-free runtime and Actions timing evidence. Never dispatch workflows."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request


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


def github_history(repository, branch, token=None, opener=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
        return {'status': 'unavailable', 'reason': 'repository_not_configured'}
    result = {'status': 'ok', 'workflows': {}, 'limits': [
        'Recent retained runs only; missing/dropped schedule events are not in this API.',
        'created_to_started is not scheduled_due_to_started delay; attempts may affect it.',
        'Shared concurrency may replace pending runs; canceled alone does not establish the cause.',
    ]}
    for filename in ('signal-monitor.yml', 'monitor-health.yml'):
        url = 'https://api.github.com/repos/' + repository + '/actions/workflows/' + filename + '/runs?' + urllib.parse.urlencode({'per_page':20, 'branch':branch})
        headers = {'User-Agent': 'CoinPulse-ReadOnly-Diagnostics', 'Accept': 'application/vnd.github+json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with (opener or urllib.request.urlopen)(urllib.request.Request(url, headers=headers), timeout=10) as response:
                body = response.read(2_000_001)
                if len(body) > 2_000_000:
                    raise ValueError('response too large')
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get('workflow_runs'), list):
                    raise ValueError('invalid response')
                result['workflows'][filename] = {'status':'ok', 'runs':summarize_runs(payload['workflow_runs'])}
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
    result = {'schema_version':1, 'observed_at':datetime.now(timezone.utc).isoformat(),
        'workflow':{k:env.get(k) for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_EVENT_NAME','GITHUB_SHA','GITHUB_REF_NAME')},
        'health':{k:health.get(k) for k in ('status','updated_at_epoch','health_reasons','notification_summary')},
        'scan':{k:scan.get(k) for k in ('run_id','coverage_pct','data_lag_basis','data_lag_minutes','data_freshness_status','max_closed_bar_overdue_minutes')},
        'alert':{k:alert.get(k) for k in ('status','checked_at_epoch','health_age_seconds','perpetual_age_seconds','ordinary_state','ordinary_reasons')},
        'external_heartbeat_configured':bool(env.get('EXTERNAL_HEARTBEAT_URL','').strip()),
        'sources':sources,
        'limits':['Health alert snapshot is transition-persisted, not necessarily the latest check.',
                  'This report does not send, repair, or prove phone receipt.']}
    if include_github:
        result['actions_history'] = github_history(env.get('GITHUB_REPOSITORY'), env.get('GITHUB_REF_NAME','master'), env.get('GITHUB_TOKEN'))
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
    print('Read-only diagnostics saved; history status: ' + result['actions_history']['status'])


if __name__ == '__main__':
    main()
