const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'src/webui_assets/library/library.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(librarySource.split("document.addEventListener('click'")[0], context);
const eligibility = track => {
  context.track = track;
  return JSON.parse(JSON.stringify(vm.runInContext('publishEligibility(track)', context)));
};

function track(overrides = {}) {
  const base = {
    human_review: {status: 'passed', file_id: 'f1', file_signature: '10:20', stale: false, new_version: false},
    files: [{id: 'f1', exists: true, signature: '10:20'}, {id: 'f2', exists: true, signature: '30:40'}],
  };
  if (overrides.human_review) base.human_review = {...base.human_review, ...overrides.human_review};
  if (overrides.files) base.files = overrides.files;
  return base;
}

assert.equal(eligibility(track()).ok, true);
assert.match(eligibility(track({human_review: {status: 'needs_changes'}})).reason, /通过/);
assert.match(eligibility(track({human_review: {stale: true}})).reason, /重新复核/);
assert.match(eligibility(track({human_review: {new_version: true}})).reason, /重新复核/);
assert.match(eligibility(track({files: [{id: 'f1', exists: false, signature: '10:20'}]})).reason, /不可用/);
assert.match(eligibility(track({files: [{id: 'f1', exists: true, signature: '99:99'}]})).reason, /签名/);

const reviewHtml = fs.readFileSync(path.join(root, 'src/webui_assets/library/index.html'), 'utf8');
assert.ok(reviewHtml.includes('待复核高置信'));
assert.ok(!reviewHtml.includes('首批投稿候选'));
assert.ok(reviewHtml.includes('id="publishSelected"'));
assert.ok(reviewHtml.includes('生成投稿包 →'));
assert.ok(librarySource.includes('reviewNavigate(\'publish\''));

const publishSource = fs.readFileSync(path.join(root, 'src/webui_assets/publish/publish.js'), 'utf8');
assert.ok(publishSource.includes('requestedTrackId'));
assert.ok(publishSource.includes('来自成品复核'));
console.log('Publish handoff: approval eligibility, review CTA and publish context wiring passed.');
