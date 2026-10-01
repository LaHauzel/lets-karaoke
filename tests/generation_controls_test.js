'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const TaskClient = require('../src/webui_assets/task_client.js');
const page = fs.readFileSync(path.join(__dirname, '../src/webui_assets/index.html'), 'utf8');
const unavailable = {
  whisper: {ok: false, reason: '缺少 Whisper 依赖'},
  sofa: {ok: false, reason: '缺少 SOFA 依赖'},
  asr: {ok: false, reason: '缺少 ASR 依赖'},
  qwen: {ok: false}, wav2vec2: {ok: false}, whisper_models: [],
};
function functionSource(name) {
  const found = page.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`));
  assert.ok(found, `Missing production function ${name}`);
  return found[0];
}
function element(value = '') {
  const classes = new Set(), listeners = {};
  return {
    value, disabled: false, title: '', textContent: value, dataset: {}, files: [], checked: false,
    children: [], inputs: [], listeners,
    classList: {
      add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
      toggle(x, on) { if (on === undefined) on = !classes.has(x); on ? classes.add(x) : classes.delete(x); },
    },
    addEventListener(name, fn) { listeners[name] = fn; },
    querySelectorAll() { return this.inputs; },
    closest() { return this; },
  };
}
function harness() {
  const elements = new Map(), errors = [], uploads = [], captures = [];
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  const sources = ['whisper', 'sofa', 'asr'].map(source => {
    const button = element(source); button.dataset.v = source;
    button.classList.toggle('on', source === 'whisper'); return button;
  });
  get('#segSrc').children = sources;
  get('#segLang').inputs = ['auto', 'zh', 'ja', 'en'].map(value => {
    const button = element(value); button.dataset.v = value; return button;
  });
  const backend = get('#backend');
  backend.options = ['auto', 'qwen', 'wav2vec2'].map(element);
  backend.value = 'auto';
  backend.querySelector = selector => backend.options.find(option => selector.includes(`"${option.value}"`));
  Object.defineProperty(backend, 'selectedOptions', {get: () => backend.options.filter(option => option.value === backend.value)});
  get('#whisperModel').options = ['large-v3', 'medium'].map(element);
  get('#whisperModel').value = 'large-v3';
  const context = vm.createContext({
    TaskClient, CAPABILITIES: null, SRC: 'whisper', LANG: 'auto', VOCAL: 'remove',
    GENERATION_BUSY: false, EDITING: false, EDITABLE: true, EDIT_OPERATION: null,
    JOB: 'historic', EDIT_DRAFT_ID: null, SAVED_TASKS: {}, BATCH_JOBS: new Set(),
    BATCH_CANCEL_REQUESTED: false, MEDIA_ACTIVE_INDEX: -1, MEDIA_ITEMS: [], SRC_HINT: {},
    document: {querySelector(selector) {
      if (selector === '#segSrc button.on') return sources.find(button => button.classList.contains('on'));
      if (selector === '#segSrc button:not(:disabled)') return sources.find(button => !button.disabled);
      const source = selector.match(/^#segSrc button\[data-v="(\w+)"\]$/);
      return source ? sources.find(button => button.dataset.v === source[1]) : get(selector);
    }},
    $: get, readLocal: () => null, editFailureKey: () => 'edit-failure',
    syncAllTokenEditStates() {}, updateAnchorControls() {}, renderHistory() {},
    showErr: value => errors.push(value), logLine() {}, setProgress() {},
    captureActiveMediaItem: () => captures.push('capture'), mediaFiles: files => files,
    clearFieldError() {}, fieldError: (_field, _error, message) => errors.push(message),
    collectRules: () => ({}), persistDraft() {}, styleOptions: () => ({}),
    startMediaJob: async (media, config) => { uploads.push({media, config}); return 'generated'; },
    rememberTask() {}, refreshHistory: async () => {}, watch: async () => ({state: 'done', result: {}}),
  });
  for (const name of ['applyCapabilities', 'refreshRunAvailability', 'refreshSrcUI', 'seg', 'setEditBusy', 'recoverTasks']) {
    vm.runInContext(functionSource(name), context);
  }
  vm.runInContext("seg('#segSrc', v => { SRC=v; refreshSrcUI(); });", context);
  for (const button of sources) {
    button.click = () => get('#segSrc').listeners.click({target: button});
  }
  const start = page.indexOf("$('#btnRun').addEventListener('click', async () => {");
  const end = page.indexOf('\nfunction styleOptions()', start);
  assert.ok(start >= 0 && end > start, 'Missing production generation handler');
  vm.runInContext(page.slice(start, end), context);
  return {context, get, sources, errors, uploads, captures,
    run: () => get('#btnRun').listeners.click(),
    caps: value => context.applyCapabilities(value),
    media: () => { context.MEDIA_ITEMS = [{file: {name: 'song.wav'}, lyrics: 'hello', lang: 'en'}]; },
  };
}
const tests = {
  unknown_or_invalid_capabilities_fail_closed() {
    assert.equal(TaskClient.generationState(null, 'whisper').allowed, false);
    assert.equal(TaskClient.generationState({}, 'whisper').allowed, false);
    assert.equal(TaskClient.generationState({whisper: {ok: 'yes'}}, 'whisper').allowed, false);
    assert.equal(TaskClient.generationState({other: {ok: true}}, 'other').allowed, false);
    assert.match(page, /id="btnRun" disabled/);
  },
  all_unavailable_sources_disable_generation_and_show_reason() {
    const h = harness(); h.caps(unavailable);
    assert.equal(h.sources.every(button => button.disabled), true);
    assert.equal(h.get('#btnRun').disabled, true);
    assert.equal(h.get('#sourceAvailability').textContent, unavailable.whisper.reason);
    assert.equal(h.get('#sourceAvailability').classList.contains('hidden'), false);
  },
  async unavailable_source_guard_prevents_upload_even_if_handler_is_invoked() {
    const h = harness(); h.media(); h.caps(unavailable); await h.run();
    assert.equal(h.uploads.length, 0); assert.equal(h.captures.length, 0);
    assert.deepEqual(h.errors, [unavailable.whisper.reason]);
  },
  releasing_edit_busy_preserves_capability_lock_and_history_editing() {
    const h = harness(); h.caps(unavailable);
    const input = element(); h.get('#tuneBody').inputs = [input];
    h.context.setEditBusy(true); h.context.setEditBusy(false);
    assert.equal(h.get('#btnRun').disabled, true);
    for (const id of ['btnRetry', 'btnRerender', 'tuneVersion', 'btnReset']) assert.equal(h.get('#' + id).disabled, false);
    assert.equal(input.disabled, false);
  },
  available_fallback_switches_source_and_repeated_capabilities_restore_labels() {
    const h = harness(); h.caps(unavailable);
    const available = {...unavailable, asr: {ok: true}, whisper_models: ['large-v3']};
    h.caps(available); h.caps(available);
    assert.equal(h.context.SRC, 'asr'); assert.equal(h.get('#btnRun').disabled, false);
    assert.equal(h.get('#sourceAvailability').classList.contains('hidden'), true);
    assert.equal(h.sources[2].textContent, 'asr');
    assert.equal(h.get('#whisperModel').options[0].textContent, 'large-v3');
    assert.equal(h.get('#whisperModel').options[1].textContent.split('未下载').length, 2);
  },
  busy_generation_and_editing_are_independent_of_source_availability() {
    const h = harness(); h.caps({...unavailable, whisper: {ok: true}});
    h.context.GENERATION_BUSY = true; h.context.refreshRunAvailability();
    assert.equal(h.get('#btnRun').disabled, true);
    h.context.GENERATION_BUSY = false; h.context.setEditBusy(true);
    assert.equal(h.get('#btnRun').disabled, true);
    h.context.setEditBusy(false); assert.equal(h.get('#btnRun').disabled, false);
  },
  disabled_source_click_is_ignored() {
    const h = harness(); h.caps({...unavailable, whisper: {ok: true}});
    h.sources[2].click(); assert.equal(h.context.SRC, 'whisper');
    assert.equal(h.get('#btnRun').disabled, false);
  },
  async task_recovery_finally_does_not_enable_unavailable_generation() {
    const h = harness(); h.caps(unavailable);
    assert.equal(await h.context.recoverTasks([{job: 'pending', state: 'running', kind: 'generate'}]), true);
    assert.equal(h.context.GENERATION_BUSY, false); assert.equal(h.get('#btnRun').disabled, true);
    assert.equal(h.get('#btnRerender').disabled, false);
  },
  async available_fallback_can_submit_and_batch_finally_keeps_unavailable_lock() {
    const h = harness(); h.media(); h.caps({...unavailable, asr: {ok: true}});
    h.context.watch = async () => { h.caps(unavailable); return {state: 'done', result: {}}; };
    await h.run();
    assert.equal(h.uploads.length, 1); assert.equal(h.uploads[0].config.source, 'asr');
    assert.equal(h.context.GENERATION_BUSY, false); assert.equal(h.get('#btnRun').disabled, true);
    assert.equal(h.get('#btnRerender').disabled, false);
  },
};
(async () => {
  for (const [name, test] of Object.entries(tests)) { await test(); process.stdout.write(`PASS ${name}\n`); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
