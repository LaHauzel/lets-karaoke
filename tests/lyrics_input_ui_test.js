'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const TaskClient = require('../src/webui_assets/task_client.js');
const page = fs.readFileSync(path.join(__dirname, '../src/webui_assets/index.html'), 'utf8');
function source(name) {
  const found = page.match(new RegExp(`function ${name}\\([^]*?\\n\\}`));
  assert.ok(found, `Missing production function ${name}`); return found[0];
}
function deferredFile(name) {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {file: {name, text: () => promise}, resolve, reject};
}
function element() {
  const classes = new Set();
  return {files: [], _value: '', textContent: '', children: [], dataset: {}, style: {}, listeners: {},
    get value() { return this._value; },
    set value(value) { this._value = value; if (value === '') this.files = []; },
    classList: {toggle(value, enabled) { enabled ? classes.add(value) : classes.delete(value); }, contains: value => classes.has(value)},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    querySelectorAll() { return this.children; }, replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); }, focus() {},
  };
}
function harness() {
  const elements = new Map(), errors = [], counters = {preview: 0, queue: 0};
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const items = [
    {file: {name: 'a.wav', type: 'audio/wav'}, lyrics: 'A', lyricsFile: null, lang: 'en'},
    {file: {name: 'b.wav', type: 'audio/wav'}, lyrics: 'B', lyricsFile: {name: 'b.txt'}, lyricsFileName: 'b.txt', lang: 'en'},
  ];
  get('#lyrics').value = 'A';
  const context = vm.createContext({
    $: get, document: {createElement: element}, TaskClient,
    MEDIA_ITEMS: items, MEDIA_QUEUE: [], MEDIA_ACTIVE_INDEX: 0, MEDIA_SELECTION_VERSION: 1,
    MEDIA_SWITCHING: false, LYRICS_FILE_READ_VERSION: 0, LYRICS_PENDING_FILE_READ: null,
    LYRICS_LOOKUP_CONTEXT: null, SRC: 'whisper', LANG: 'en', SRC_HINT: {},
    GENERATION_BUSY: false, EDITING: false, CAPABILITIES: {whisper: {ok: true}},
    styleOptions: () => ({}), defaultStyleOptions: () => ({}), applyStyleOptions() {}, setStylePreviewFile() {},
    clearFieldError() {}, showErr: error => errors.push(error),
    renderStylePreview: () => counters.preview++, renderMediaQueue: () => counters.queue++,
    LyricsSearch: {mount(options) { this.options = options; return {refreshContext() {}, refreshAvailability() {}, setQuery() {}}; }},
  });
  for (const name of ['captureActiveMediaItem', 'invalidateLyricsFileRead', 'detachLyricsFile', 'lyricsContextKey',
    'activateMediaItem', 'setMediaSelection', 'mediaFiles', 'refreshSrcUI', 'refreshRunAvailability', 'updateInputSummary']) {
    vm.runInContext(source(name), context);
  }
  const changes = page.match(/\$\('#lyricsFile'\)\.addEventListener\('change', async e => \{[^]*?\n\}\);/);
  const clear = page.match(/\$\('#btnClearLyrics'\)\.addEventListener\('click', \(\) => \{[^]*?\n\}\);/);
  const input = page.match(/\$\('#lyrics'\)\.addEventListener\('input', \(\) => \{[^]*?\}\);/);
  assert.ok(changes && clear && input, 'Missing production lyric input handlers');
  vm.runInContext(changes[0] + '\n' + clear[0] + '\n' + input[0], context);
  const start = page.indexOf('if(globalThis.LyricsSearch){');
  const end = page.indexOf('\nupdateInputSummary();', start);
  assert.ok(start >= 0 && end > start, 'Missing production mount integration');
  vm.runInContext(page.slice(start, end), context);
  const read = file => {
    get('#lyricsFile').files = [file];
    return get('#lyricsFile').listeners.change({target: get('#lyricsFile')});
  };
  const online = (text = 'ONLINE') => context.LyricsSearch.options.onImport({
    text, title: '歌曲', provider: 'LRCLIB', format: 'plain', context_key: context.lyricsContextKey(),
  });
  return {context, get, items, errors, counters, read, online};
}
const tests = {
  async current_file_read_is_applied_and_captured() {
    const h = harness(), f = deferredFile('a.txt'), reading = h.read(f.file);
    f.resolve('LOCAL A'); await reading;
    assert.equal(h.get('#lyrics').value, 'LOCAL A'); assert.equal(h.items[0].lyrics, 'LOCAL A');
    assert.equal(h.items[0].lyricsFile, f.file); assert.equal(h.context.LYRICS_PENDING_FILE_READ, null);
    assert.equal(h.get('#whisperNeedLyrics').classList.contains('hidden'), true);
  },
  async switching_media_does_not_apply_old_file_to_new_item() {
    const h = harness(), f = deferredFile('old.txt'), reading = h.read(f.file);
    h.context.activateMediaItem(1); f.resolve('OLD LOCAL'); await reading;
    assert.equal(h.get('#lyrics').value, 'B'); assert.equal(h.items[1].lyrics, 'B');
    assert.equal(h.items[1].lyricsFile.name, 'b.txt'); assert.equal(h.items[0].lyrics, 'A');
  },
  async new_media_selection_does_not_seed_a_pending_lyric_attachment() {
    const h = harness(), f = deferredFile('old.txt'), reading = h.read(f.file);
    h.context.setMediaSelection({name: 'new.wav', type: 'audio/wav'});
    f.resolve('OLD LOCAL'); await reading;
    assert.equal(h.context.MEDIA_ITEMS[0].lyrics, 'A');
    assert.equal(h.context.MEDIA_ITEMS[0].lyricsFile, null);
    assert.equal(h.get('#lyrics').value, 'A');
  },
  async online_import_invalidates_old_read_and_attachment() {
    const h = harness(), f = deferredFile('old.txt'), reading = h.read(f.file);
    h.online(); f.resolve('OLD LOCAL'); await reading;
    assert.equal(h.get('#lyrics').value, 'ONLINE'); assert.equal(h.items[0].lyrics, 'ONLINE');
    assert.equal(h.items[0].lyricsFile, null); assert.equal(h.get('#lyricsFile').files.length, 0);
    assert.match(h.get('#fnLyrics').textContent, /LRCLIB/);
  },
  async clear_invalidates_old_read_and_refreshes_readiness() {
    const h = harness(), f = deferredFile('old.txt'), reading = h.read(f.file);
    h.get('#btnClearLyrics').listeners.click(); f.resolve('OLD LOCAL'); await reading;
    assert.equal(h.get('#lyrics').value, ''); assert.equal(h.items[0].lyrics, '');
    assert.equal(h.items[0].lyricsFile, null); assert.equal(h.get('#fnLyrics').textContent, '');
    assert.equal(h.get('#whisperNeedLyrics').classList.contains('hidden'), false);
    assert.match(h.get('#inputReadiness').children[1].textContent, /粘贴歌词/);
  },
  async typed_lyrics_cancel_pending_file_and_are_the_submitted_text() {
    const h = harness(), f = deferredFile('old.txt'), reading = h.read(f.file);
    h.get('#lyrics').value = 'MY EDIT'; h.get('#lyrics').listeners.input();
    f.resolve('OLD LOCAL'); await reading;
    assert.equal(h.get('#lyrics').value, 'MY EDIT'); assert.equal(h.items[0].lyrics, 'MY EDIT');
    assert.equal(h.items[0].lyricsFile, null); assert.equal(h.get('#lyricsFile').files.length, 0);
  },
  async editing_an_already_loaded_file_detaches_original_attachment() {
    const h = harness(), f = deferredFile('old.txt'), reading = h.read(f.file);
    f.resolve('LOCAL'); await reading;
    h.get('#lyrics').value = 'EDITED LOCAL'; h.get('#lyrics').listeners.input();
    assert.equal(h.items[0].lyrics, 'EDITED LOCAL'); assert.equal(h.items[0].lyricsFile, null);
    assert.equal(h.items[0].lyricsFileName, ''); assert.equal(h.get('#lyricsFile').files.length, 0);
  },
  async newer_attachment_wins_if_older_read_finishes_last() {
    const h = harness(), old = deferredFile('old.txt'), next = deferredFile('new.txt');
    const first = h.read(old.file), second = h.read(next.file);
    next.resolve('NEW LOCAL'); await second; old.resolve('OLD LOCAL'); await first;
    assert.equal(h.get('#lyrics').value, 'NEW LOCAL'); assert.equal(h.items[0].lyricsFile, next.file);
    assert.equal(h.items[0].lyricsFileName, 'new.txt');
  },
  async changed_input_file_identity_prevents_old_read_application() {
    const h = harness(), old = deferredFile('old.txt'), reading = h.read(old.file);
    h.get('#lyricsFile').files = [{name: 'other.txt'}]; old.resolve('OLD LOCAL'); await reading;
    assert.equal(h.get('#lyrics').value, 'A'); assert.equal(h.items[0].lyricsFile, null);
  },
  async stale_file_read_errors_do_not_replace_online_import_status() {
    const h = harness(), old = deferredFile('old.txt'), reading = h.read(old.file);
    h.online(); old.reject(new Error('delayed read failure')); await reading;
    assert.equal(h.get('#lyrics').value, 'ONLINE'); assert.equal(h.errors.length, 0);
    assert.match(h.get('#fnLyrics').textContent, /LRCLIB/);
  },
  async current_read_failure_preserves_text_and_rejects_attachment() {
    const h = harness(), f = deferredFile('bad.txt'), reading = h.read(f.file);
    f.reject(new Error('cannot read')); await reading;
    assert.equal(h.get('#lyrics').value, 'A'); assert.equal(h.items[0].lyricsFile, null);
    assert.equal(h.get('#lyricsFile').files.length, 0); assert.match(h.errors[0], /cannot read/);
  },
  retained_item_attachment_counts_as_lyrics_when_dom_file_is_empty() {
    const h = harness(); h.context.activateMediaItem(1); h.get('#lyrics').value = '';
    h.context.refreshSrcUI();
    assert.equal(h.get('#lyricsFile').files.length, 0);
    assert.equal(h.get('#whisperNeedLyrics').classList.contains('hidden'), true);
    assert.equal(h.context.LyricsSearch.options.canImport().hasLyrics, true);
  },
  stale_online_import_is_rejected_before_clearing_other_item_attachment() {
    const h = harness(), oldKey = h.context.lyricsContextKey(); h.context.activateMediaItem(1);
    assert.throws(() => h.context.LyricsSearch.options.onImport({context_key: oldKey, text: 'WRONG'}), /已改变/);
    assert.equal(h.items[1].lyricsFile.name, 'b.txt'); assert.equal(h.get('#lyrics').value, 'B');
  },
};
(async () => {
  for (const [name, test] of Object.entries(tests)) { await test(); process.stdout.write(`PASS ${name}\n`); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
