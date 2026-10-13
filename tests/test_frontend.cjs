const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set(['is-hidden']);
    elements.set(id, { value: '', textContent: '', innerHTML: '', disabled: false, open: false, style: {},
      listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }, classList: { add: v => classes.add(v), remove: v => classes.delete(v),
        toggle(v, yes) { if (yes) classes.add(v); else classes.delete(v); }, contains: v => classes.has(v) } });
  }
  return elements.get(id);
}
let response;
const context = vm.createContext({
  document: {querySelector: element, querySelectorAll: () => []},
  window: {connectionStore: {load: async () => null}, spaceMap: {reset() {}, update() {}}},
  setTimeout: () => 1, clearTimeout() {},
  fetch: async () => ({ok: true, json: async () => response}),
});
vm.runInContext(fs.readFileSync('app/static/app.js', 'utf8'), context);
(async () => {
  response = {status: 'running', progress: 5, stage: 'scanning', result: {
    volumes: [], directories: [], large_files: [], large_files_total: 0, hardlinks: [], hardlink_groups_total: 0,
    large_directories: Array.from({length: 201}, (_, i) => ({path: '/volume1/folder'+i, volume: '/volume1', size_bytes: 1048576, file_count: 2})),
    large_directories_total: 201, settings: {large_file_mb: 1}, scanned_files: 402, warnings: [],
  }};
  vm.runInContext('state.page = 2; state.activeTab = "large-directories"', context);
  await vm.runInContext("pollJob('test')", context);
  assert(!element('#results').classList.contains('is-hidden'));
  assert(!element('#progress-panel').classList.contains('is-hidden'));
  assert.match(element('#result-content').innerHTML, /folder100/);
  assert.match(element('#page-status').textContent, /第 2/);
  assert.match(element('#scan-notice').textContent, /正在扫描/);
  response.result.warnings = ['Permission denied\n/volume1/private/<script>not HTML</script>'];
  await vm.runInContext("pollJob('test')", context);
  assert(!element('#scan-warnings').open);
  assert(!element('#scan-warnings').classList.contains('is-hidden'));
  assert.match(element('#scan-warnings summary').textContent, /1 条/);
  assert.match(element('#scan-warnings pre').textContent, /<script>not HTML<\/script>/);
  assert(!element('#scan-notice').textContent.includes('Permission denied'));
  element('#scan-warnings').open = true;
  response.result.warnings.push('Another warning');
  await vm.runInContext("pollJob('test')", context);
  assert(element('#scan-warnings').open);
  assert.match(element('#scan-warnings summary').textContent, /2 条/);
  element('#scan-warnings').open = false;
  await vm.runInContext("pollJob('test')", context);
  assert(!element('#scan-warnings').open);
  response.result.warnings = [];
  await vm.runInContext("pollJob('test')", context);
  assert(element('#scan-warnings').classList.contains('is-hidden'));
  vm.runInContext("setConnectionMessage('SSH login failed', true)", context);
  assert(!element('#connection-error').open);
  assert.equal(element('#connection-error pre').textContent, 'SSH login failed');
  vm.runInContext("setConnectionMessage('Connected')", context);
  assert(element('#connection-error').classList.contains('is-hidden'));
  vm.runInContext("state.jobId = 'test'", context);
  response = {...response, status: 'cancelling'};
  await element('#stop-button').listeners.click();
  await vm.runInContext("pollJob('test')", context);
  assert(element('#stop-button').disabled);
  assert.match(element('#scan-notice').textContent, /正在停止/);
  response = {...response, status: 'cancelled'};
  await vm.runInContext("pollJob('test')", context);
  assert.match(element('#progress-value').textContent, /已停止/);
  assert.match(element('#scan-notice').textContent, /部分结果/);
  assert.match(element('#result-content').innerHTML, /folder100/);
  assert(!element('#analyze-button').disabled);
  response = {...response, status: 'failed', error: 'connection interrupted'};
  await vm.runInContext("pollJob('test')", context);
  assert.match(element('#result-content').innerHTML, /folder100/);
  assert.match(element('#scan-notice').textContent, /部分结果/);
  response = {...response, status: 'completed'};
  assert(!element('#scan-error').open);
  assert.equal(element('#scan-error pre').textContent, 'connection interrupted');
  assert(!element('#progress-stage').textContent.includes('connection interrupted'));
  await vm.runInContext("pollJob('test')", context);
  assert(element('#progress-panel').classList.contains('is-hidden'));
  assert.match(element('#scan-notice').textContent, /扫描结束/);
  console.log('PASS: live results, cancellation, collapsible errors, persistent expanded state, safe error text');
})().catch(error => { console.error(error); process.exitCode = 1; });
