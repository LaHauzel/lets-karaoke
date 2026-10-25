const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ui = require(path.join(__dirname, '../src/webui_assets/publish/publish.js'));
const html = fs.readFileSync(path.join(__dirname, '../src/webui_assets/publish/index.html'), 'utf8');

assert.equal(ui.formatBytes(0), '0 B');
assert.equal(ui.formatBytes(512), '512 B');
assert.equal(ui.formatBytes(1536), '1.5 KB');
assert.equal(ui.formatBytes(5 * 1024 ** 3), '5.0 GB');

assert.equal(ui.formatDuration(0), '0:00');
assert.equal(ui.formatDuration(65), '1:05');
assert.equal(ui.formatDuration(3725), '1:02:05');

assert.equal(ui.platformName('bilibili'), 'B站');
assert.equal(ui.platformName('douyin'), '抖音');
assert.equal(ui.platformName('unknown'), 'unknown');

assert.deepEqual(ui.summarizePlan({
  ok: false,
  platforms: {bilibili: {errors: ['a'], warnings: ['b', 'c']}, douyin: {errors: [], warnings: ['d']}},
}), {ok: false, errors: 1, warnings: 3});

assert.ok(html.includes('/publish-assets/publish.js'));
assert.ok(html.includes('发布准备'));
assert.ok(html.includes('data-workflow="publish"'));
console.log('Publish UI helpers: bytes, duration, platform names, plan summary and page wiring passed.');
