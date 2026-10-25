const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function run(workflowTab) {
  const appended = [], posted = [];
  const nav = {dataset: {workflow: workflowTab}, append(node) { appended.push(node); }};
  const window = {
    parent: {postMessage(data) { posted.push(JSON.parse(JSON.stringify(data))); }},
    location: {origin: 'http://127.0.0.1:7870'},
  };
  const context = {
    window,
    location: window.location,
    URL,
    URLSearchParams,
    document: {
      querySelectorAll: selector => selector === '[data-workflow]' ? [nav] : [],
      createElement: tag => ({
        tag, className: '', textContent: '', onclick: null,
        setAttribute(key, value) { this[key] = value; },
      }),
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/webui_assets/workflow.js'), 'utf8'), context);
  return {appended, posted, window};
}

const result = run('review');
assert.ok(!result.appended.some(node => node.tag === 'button'), 'workflow bar must not create navigation buttons');
assert.ok(!result.appended.some(node => node.onclick), 'workflow bar must not attach click navigation');
const steps = result.appended.filter(node => node.className === 'lk-step');
const arrows = result.appended.filter(node => node.className === 'lk-arrow');
assert.deepEqual(steps.map(node => node.textContent), ['切割整场', '制作字幕', '成品复核', '发布准备']);
assert.equal(steps.filter(node => node['aria-current'] === 'step').length, 1);
assert.equal(steps.find(node => node['aria-current'] === 'step').textContent, '成品复核');
assert.equal(arrows.length, 3);
const note = result.appended.find(node => node.tag === 'small');
assert.equal(note.textContent, '用顶部标签切换工作区');

result.window.navigateWorkspace('publish', {track_id: 'demo/01', title: '示例'});
assert.deepEqual(result.posted.at(-1), {
  type: 'lets-karaoke:navigate', tab: 'publish', context: {track_id: 'demo/01', title: '示例'},
});
result.window.navigateWorkspace('unknown');
assert.equal(result.posted.length, 1, 'unknown tabs must not navigate');
console.log('Workflow indicator: read-only steps, current marker, arrows and context navigation passed.');
