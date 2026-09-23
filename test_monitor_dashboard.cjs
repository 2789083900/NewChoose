const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const code = fs.readFileSync('app.js', 'utf8');
const start = code.indexOf('function loadMonitorHealth()');
const end = code.indexOf('\nfunction ', start + 1);
const source = code.slice(start, end < 0 ? undefined : end);
async function run(health, alert) {
  const nodes = Object.fromEntries(['monitorHealthMeta','monitorHealthSummary','monitorHealthDetails'].map(key => [key,{}]));
  const context = {
    $: key => nodes[key], fetchJSON: async path => path === 'monitor_health.json' ? health : alert,
    escapeHtml: value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  };
  vm.createContext(context);
  vm.runInContext(source + '\nloadMonitorHealth();', context);
  await new Promise(resolve => setImmediate(resolve));
  assert(!nodes.monitorHealthMeta.textContent.includes('加载失败'));
  return nodes;
}
(async () => {
  const old = JSON.parse(fs.readFileSync('monitor_health.json','utf8'));
  let nodes = await run(old, {status:'stale', ordinary_state:'heartbeat_stale'});
  assert(nodes.monitorHealthSummary.innerHTML.includes('未知（旧口径）'));
  const fresh = {...old,
    scan: {...old.scan, freshness_schema_version:2, data_lag_minutes:178.1, max_closed_bar_overdue_minutes:0, data_freshness_status:'fresh'},
    health_reasons:['signals_expired_before_dispatch','<bad>'],
    notification_summary:{round_failed:0, expired_signals:3, expired_notifications:0}};
  nodes = await run(fresh, {status:'degraded', ordinary_state:'signals_expired_before_dispatch'});
  assert(nodes.monitorHealthSummary.innerHTML.includes('178.1 分钟'));
  assert(nodes.monitorHealthDetails.innerHTML.includes('信号过期未发送 3'));
  assert(nodes.monitorHealthDetails.innerHTML.includes('投递失败 0'));
  assert(nodes.monitorHealthDetails.innerHTML.includes('&lt;bad&gt;'));
  assert(nodes.monitorHealthMeta.textContent.includes('信号发现过晚'));
  console.log('Dashboard DOM smoke tests passed: legacy/new schema, separate counts, escaped reasons.');
})().catch(err => {console.error(err); process.exit(1);});
