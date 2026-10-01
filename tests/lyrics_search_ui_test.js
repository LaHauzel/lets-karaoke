'use strict';
const assert = require('node:assert/strict');
const {mount, trustedSource} = require('../src/webui_assets/lyrics_search.js');

class Element {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.children = [];
    this.dataset = {}; this.attributes = {}; this.listeners = {}; this.value = '';
    this.disabled = false; this.hidden = false; this.className = ''; this._text = '';
    this.classList = {
      add: value => { this.className = [...new Set([...this.className.split(' '), value])].join(' ').trim(); },
      toggle: (value, enabled) => {
        const classes = new Set(this.className.split(' ').filter(Boolean));
        if (enabled === undefined) enabled = !classes.has(value);
        enabled ? classes.add(value) : classes.delete(value); this.className = [...classes].join(' ');
      },
      contains: value => this.className.split(' ').includes(value),
    };
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(_) { throw new Error('HTML rendering is forbidden in lyrics search'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  focus() { this.focused = true; }
  async invoke(name) {
    const event = {preventDefault() {}};
    for (const callback of this.listeners[name] || []) await callback(event);
  }
}
class Clock {
  next = 0; timers = new Map();
  set = (fn, delay) => { const id = ++this.next; this.timers.set(id, {fn, delay}); return id; };
  clear = id => this.timers.delete(id);
  tick() { for (const [id, timer] of [...this.timers]) { this.timers.delete(id); timer.fn(); } }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const flush = async () => { for (let i = 0; i < 14; i++) await Promise.resolve(); };
const response = (value, code = 200) => ({ok: code >= 200 && code < 300, status: code, json: async () => value});
const record = (overrides = {}) => ({id: 10, title: '歌曲', artist: '歌手', album: '专辑', duration: 119.8,
  instrumental: false, has_plain: true, has_synced: true, ...overrides});
const fullRecord = (overrides = {}) => ({...record(), provider: 'LRCLIB', source_url: 'https://lrclib.net/api/get/10',
  plain_lyrics: '第一句\n第二句', synced_lyrics: '[00:01.00]第一句\n[00:02.00]第二句', ...overrides});

function harness(options = {}) {
  const doc = {createElement: tag => new Element(tag, doc)};
  const root = new Element('div', doc), calls = [], imports = [], confirms = [], clock = new Clock(), queued = [];
  const state = {key: 'file-a', allowed: true, hasLyrics: false, confirmed: true};
  const api = mount({root, contextKey: () => state.key,
    canImport: () => ({allowed: state.allowed, hasLyrics: state.hasLyrics, reason: '当前正在生成，请等待完成。'}),
    confirm: message => { confirms.push(message); return state.confirmed; },
    onImport: value => { imports.push(value); }, setTimeout: clock.set, clearTimeout: clock.clear, timeoutMs: 30,
    fetch: (url, config) => {
      calls.push({url, config, body: JSON.parse(config.body)});
      if (!queued.length) throw new Error('Unexpected network request in test');
      const next = queued.shift(); return typeof next === 'function' ? next(url, config) : Promise.resolve(next);
    }, ...options,
  });
  function all(element = root) { return [element, ...element.children.flatMap(child => all(child))]; }
  const role = (name, index = 0) => all().filter(element => element.dataset.lyricsRole === name)[index];
  const form = () => all().find(element => element.tagName === 'FORM');
  const search = async (rows = [record()], overrides = {}) => {
    api.setQuery({track_name: '歌曲', artist_name: '歌手'});
    queued.push(response({provider: 'LRCLIB', source_url: 'https://lrclib.net/', results: rows, ...overrides}));
    await form().invoke('submit');
  };
  const preview = async (value = fullRecord()) => {
    if (!role('open-result')) await search();
    queued.push(response(value)); await role('open-result').invoke('click');
  };
  return {root, api, state, calls, imports, confirms, queued, clock, all, role, form, search, preview};
}

const tests = {
  mount_and_query_prefill_do_not_connect_to_network() {
    const h = harness(); h.api.setQuery({track_name: '片名', artist_name: '歌手'});
    assert.equal(h.role('song').value, '片名'); assert.equal(h.role('artist').value, '歌手');
    assert.match(h.role('privacy').textContent, /只有主动搜索或预览/);
    assert.match(h.role('privacy').textContent, /不发送媒体或已有歌词/);
    assert.equal(h.calls.length, 0); assert.equal(h.imports.length, 0);
  },
  async empty_query_is_local_validation() {
    const h = harness(); await h.form().invoke('submit');
    assert.equal(h.calls.length, 0); assert.match(h.role('status').textContent, /歌曲名/);
    assert.equal(h.role('song').focused, true);
  },
  trusted_source_rejects_script_credentials_and_other_hosts() {
    for (const value of ['javascript:alert(1)', 'http://lrclib.net/', 'https://lrclib.net.evil/',
      'https://evil.test/lrclib.net', 'https://user:pass@lrclib.net/', 'https://lrclib.net:8443/', '//lrclib.net/', null]) {
      assert.equal(trustedSource(value), 'https://lrclib.net/', String(value));
    }
    assert.equal(trustedSource('https://lrclib.net/api/get/10'), 'https://lrclib.net/api/get/10');
  },
  async untrusted_titles_and_lyrics_are_rendered_as_text() {
    const h = harness(), attack = '<img src=x onerror=alert(1)><script>alert(1)</script>';
    await h.search([record({title: attack, artist: attack, album: attack})], {source_url: 'javascript:alert(1)'});
    assert.ok(h.role('results').textContent.includes(attack));
    assert.equal(h.all().some(element => ['IMG', 'SCRIPT'].includes(element.tagName)), false);
    const source = h.all().find(element => element.tagName === 'A');
    assert.equal(source.href, 'https://lrclib.net/'); assert.equal(source.rel, 'noopener noreferrer');
    await h.preview(fullRecord({title: attack, plain_lyrics: attack, source_url: 'https://evil.test/'}));
    assert.equal(h.role('lyrics').textContent, attack);
    await h.role('import-plain').invoke('click');
    assert.equal(h.imports[0].text, attack); assert.equal(h.imports[0].source_url, 'https://lrclib.net/');
  },
  async search_and_preview_never_import_automatically_and_formats_stay_distinct() {
    const h = harness(); await h.search(); await h.preview();
    assert.equal(h.imports.length, 0);
    assert.deepEqual(h.calls.map(call => call.url), ['/api/lyrics/search', '/api/lyrics/get']);
    assert.deepEqual(h.calls[0].body, {track_name: '歌曲', artist_name: '歌手'});
    assert.equal(h.calls[0].config.headers['Content-Type'], 'application/json');
    assert.deepEqual(h.calls[1].body, {id: 10});
    await h.role('preview-lrc').invoke('click');
    assert.equal(h.role('lyrics').textContent, fullRecord().synced_lyrics);
    await h.role('import-lrc').invoke('click');
    assert.deepEqual(h.imports[0], {text: fullRecord().synced_lyrics, title: '歌曲', artist: '歌手', format: 'lrc',
      source_url: 'https://lrclib.net/api/get/10', provider: 'LRCLIB', context_key: 'file-a'});
    assert.ok(h.role('results').textContent.includes('2:00'));
  },
  async instrumental_and_empty_lyrics_cannot_be_imported() {
    const h = harness(); await h.search([record({instrumental: true, has_plain: false, has_synced: false})]);
    assert.equal(h.role('open-result').disabled, true); assert.ok(h.role('results').textContent.includes('纯音乐'));
    await h.preview(fullRecord({instrumental: true, plain_lyrics: '', synced_lyrics: ''}));
    assert.equal(h.role('preview').hidden, true); assert.equal(h.role('import-plain'), undefined);
    assert.match(h.role('status').textContent, /纯音乐/); assert.equal(h.imports.length, 0);
  },
  async busy_state_blocks_import_even_when_disabled_handler_is_invoked() {
    const h = harness(); await h.preview(); h.state.allowed = false;
    h.api.refreshContext(); assert.equal(h.role('import-plain').disabled, true);
    await h.role('import-plain').invoke('click'); assert.equal(h.imports.length, 0);
    assert.match(h.role('import-hint').textContent, /生成/);
    h.state.allowed = true; h.api.refreshAvailability();
    assert.equal(h.role('import-plain').disabled, false);
    await h.role('import-plain').invoke('click'); assert.equal(h.imports.length, 1);
  },
  async existing_lyrics_require_an_explicit_replace_confirmation() {
    const h = harness(); await h.preview(); h.state.hasLyrics = true; h.state.confirmed = false;
    h.api.refreshAvailability(); assert.match(h.role('import-plain').textContent, /替换/);
    await h.role('import-plain').invoke('click'); assert.equal(h.imports.length, 0);
    assert.match(h.confirms[0], /当前文件已有歌词/);
    h.state.confirmed = true; await h.role('import-plain').invoke('click');
    assert.equal(h.imports.length, 1); assert.equal(h.confirms.length, 2);
  },
  async newer_search_response_wins_when_older_request_ignores_abort() {
    const h = harness(), old = deferred(); h.api.setQuery({track_name: '旧歌'});
    h.queued.push(() => old.promise); const olderSearch = h.form().invoke('submit'); await flush();
    const signal = h.calls[0].config.signal;
    await h.search([record({id: 20, title: '新歌'})]);
    assert.equal(signal.aborted, true);
    old.resolve(response({results: [record({title: '旧歌'})]})); await olderSearch; await flush();
    assert.ok(h.role('results').textContent.includes('新歌'));
    assert.equal(h.role('results').textContent.includes('旧歌'), false);
  },
  async context_switch_during_get_discards_response_and_aborts_request() {
    const h = harness(), pending = deferred(); await h.search();
    h.queued.push(() => pending.promise); const opening = h.role('open-result').invoke('click'); await flush();
    const signal = h.calls[1].config.signal;
    h.state.key = 'file-b'; h.api.refreshContext();
    assert.equal(signal.aborted, true); assert.equal(h.role('results').children.length, 0);
    pending.resolve(response(fullRecord())); await opening; await flush();
    assert.equal(h.role('preview').hidden, true); assert.equal(h.imports.length, 0);
    assert.match(h.role('status').textContent, /切换文件/);
  },
  async file_change_before_import_is_detected_without_manual_refresh() {
    const h = harness(); await h.preview(); const staleButton = h.role('import-plain');
    h.state.key = 'file-b'; await staleButton.invoke('click');
    assert.equal(h.imports.length, 0); assert.equal(h.role('preview').hidden, true);
  },
  async context_and_busy_are_rechecked_after_replace_confirmation() {
    let change = () => {}; const h = harness({confirm: () => { change(); return true; }});
    await h.preview(); h.state.hasLyrics = true;
    change = () => { h.state.key = 'file-b'; };
    await h.role('import-plain').invoke('click'); assert.equal(h.imports.length, 0);
    await h.preview(); change = () => { h.state.allowed = false; };
    await h.role('import-plain').invoke('click'); assert.equal(h.imports.length, 0);
  },
  async cancellation_settles_even_when_fetch_ignores_abort() {
    const h = harness(), pending = deferred(); h.api.setQuery({track_name: '歌曲'});
    h.queued.push(() => pending.promise); const searching = h.form().invoke('submit'); await flush();
    h.api.cancel(); await searching;
    assert.equal(h.calls[0].config.signal.aborted, true); assert.equal(h.clock.timers.size, 0);
    assert.match(h.role('status').textContent, /已取消/);
    pending.resolve(response({results: [record()]})); await flush();
    assert.equal(h.role('results').children.length, 0); assert.match(h.role('status').textContent, /已取消/);
  },
  async timeout_settles_without_fetch_cooperation_and_late_results_are_ignored() {
    const h = harness(), pending = deferred(); h.api.setQuery({track_name: '歌曲'});
    h.queued.push(() => pending.promise); const searching = h.form().invoke('submit'); await flush();
    h.clock.tick(); await searching;
    assert.equal(h.calls[0].config.signal.aborted, true); assert.match(h.role('status').textContent, /超时/);
    assert.equal(h.role('cancel').hidden, true); assert.equal(h.clock.timers.size, 0);
    pending.resolve(response({results: [record()]})); await flush();
    assert.equal(h.role('results').children.length, 0); assert.match(h.role('status').textContent, /超时/);
  },
  async empty_http_network_and_invalid_responses_leave_search_retryable() {
    const h = harness(); await h.search([]); assert.match(h.role('status').textContent, /未找到/);
    h.queued.push(response({error: '<b>服务暂时不可用</b>'}, 503)); await h.form().invoke('submit');
    assert.match(h.role('status').textContent, /<b>服务暂时不可用<\/b>/);
    h.queued.push(() => Promise.reject(new Error('offline'))); await h.form().invoke('submit');
    assert.match(h.role('status').textContent, /offline/);
    h.queued.push(response({results: 'invalid'})); await h.form().invoke('submit');
    assert.match(h.role('status').textContent, /格式/);
    await h.search(); assert.equal(h.role('search').disabled, false); assert.equal(h.clock.timers.size, 0);
    await h.preview(fullRecord({id: 99})); assert.match(h.role('status').textContent, /编号不匹配/);
    assert.equal(h.role('preview').hidden, true); assert.equal(h.imports.length, 0);
  },
  async duplicate_imports_are_blocked_and_late_import_completion_keeps_new_context_message() {
    const pending = deferred(), imported = [];
    const h = harness({onImport: value => { imported.push(value); return pending.promise; }});
    await h.preview(); const button = h.role('import-plain');
    const first = button.invoke('click'); await flush(); await button.invoke('click');
    assert.equal(imported.length, 1); assert.equal(button.disabled, true);
    h.state.key = 'file-b'; h.api.refreshContext(); pending.resolve(); await first;
    assert.match(h.role('status').textContent, /切换文件/);
    assert.equal(h.role('search').disabled, false);
  },
  async import_and_confirmation_failures_preserve_preview_and_allow_retry() {
    let fail = true;
    const h = harness({onImport: () => { if (fail) throw new Error('无法保存当前歌词'); }});
    await h.preview(); await h.role('import-plain').invoke('click');
    assert.match(h.role('status').textContent, /无法保存当前歌词/);
    assert.equal(h.role('preview').hidden, false); assert.equal(h.role('import-plain').disabled, false);
    fail = false; await h.role('import-plain').invoke('click'); assert.match(h.role('status').textContent, /已导入/);
    const p = harness({confirm: () => { throw new Error('confirmation unavailable'); }});
    await p.preview(); p.state.hasLyrics = true; await p.role('import-plain').invoke('click');
    assert.equal(p.imports.length, 0); assert.match(p.role('status').textContent, /当前歌词已保留/);
  },
  async default_inline_confirmation_can_keep_or_replace_without_native_dialog() {
    const h = harness({confirm: undefined}); await h.preview(); h.state.hasLyrics = true;
    const keep = h.role('import-plain').invoke('click'); await flush();
    assert.equal(h.role('confirmation').hidden, false);
    assert.equal(h.role('confirmation').attributes.role, 'group');
    assert.equal(h.role('confirm-keep').focused, true);
    assert.equal(h.role('import-plain').disabled, true); assert.equal(h.imports.length, 0);
    await h.role('import-plain').invoke('click');
    await h.role('confirm-keep').invoke('click'); await keep;
    assert.equal(h.role('confirmation').hidden, true); assert.equal(h.imports.length, 0);
    assert.match(h.role('status').textContent, /保留/);
    const replace = h.role('import-lrc').invoke('click'); await flush();
    await h.role('confirm-replace').invoke('click'); await replace;
    assert.equal(h.imports.length, 1); assert.equal(h.imports[0].format, 'lrc');
    assert.equal(h.role('confirmation').hidden, true);
  },
  async switching_files_cancels_confirmation_and_immediately_prefills_new_query() {
    const h = harness({confirm: undefined}); await h.preview(); h.state.hasLyrics = true;
    const importing = h.role('import-plain').invoke('click'); await flush();
    h.state.key = 'file-b'; h.api.refreshContext();
    h.api.setQuery({track_name: '新文件歌名', artist_name: '新歌手'});
    assert.equal(h.role('song').value, '新文件歌名'); assert.equal(h.role('artist').value, '新歌手');
    await importing;
    assert.equal(h.role('confirmation').hidden, true); assert.equal(h.imports.length, 0);
    assert.match(h.role('status').textContent, /切换文件/);
    assert.equal(h.role('song').disabled, false); assert.equal(h.calls.length, 2);
    await h.role('confirm-replace').invoke('click'); assert.equal(h.imports.length, 0);
  },
  async processing_state_is_rechecked_after_inline_confirmation() {
    const h = harness({confirm: undefined}); await h.preview(); h.state.hasLyrics = true;
    const importing = h.role('import-plain').invoke('click'); await flush();
    h.state.allowed = false; h.api.refreshAvailability();
    assert.equal(h.role('confirm-replace').disabled, true);
    await h.role('confirm-replace').invoke('click'); await importing;
    assert.equal(h.imports.length, 0); assert.match(h.role('status').textContent, /生成/);
    assert.equal(h.role('confirmation').hidden, true);
  },
  async injected_async_confirmation_supports_acceptance_and_context_cancellation() {
    const choice = deferred(); let asked = 0;
    const h = harness({confirmReplace: () => { asked++; return choice.promise; }});
    await h.preview(); h.state.hasLyrics = true;
    const importing = h.role('import-plain').invoke('click'); await flush();
    await h.role('import-plain').invoke('click'); assert.equal(asked, 1);
    h.state.key = 'file-b'; h.api.refreshContext(); await importing;
    assert.equal(h.imports.length, 0); choice.resolve(true); await flush();
    assert.equal(h.imports.length, 0); assert.match(h.role('status').textContent, /切换文件/);
    const accepted = deferred(), p = harness({confirmReplace: () => accepted.promise});
    await p.preview(); p.state.hasLyrics = true;
    const operation = p.role('import-plain').invoke('click'); await flush(); accepted.resolve(true); await operation;
    assert.equal(p.imports.length, 1);
  },
  async cancellation_and_destroy_settle_an_unanswered_inline_confirmation() {
    for (const action of ['cancel', 'destroy']) {
      const h = harness({confirm: undefined}); await h.preview(); h.state.hasLyrics = true;
      const importing = h.role('import-plain').invoke('click'); await flush();
      h.api[action](); await importing; assert.equal(h.imports.length, 0);
      if (action === 'cancel') {
        assert.equal(h.role('confirmation').hidden, true); assert.equal(h.role('import-plain').disabled, false);
      } else assert.equal(h.root.children.length, 0);
    }
  },
  async rate_limit_and_unavailability_messages_show_retry_after() {
    const h = harness(); h.api.setQuery({track_name: '歌曲'});
    for (const [code, retry, shown] of [[429, 12, 12], [503, 12.4, 13]]) {
      h.queued.push(response({error: '服务繁忙', retry_after: retry}, code));
      await h.form().invoke('submit'); assert.ok(h.role('status').textContent.includes(`${shown} 秒后重试`));
      assert.equal(h.role('search').disabled, false); assert.equal(h.clock.timers.size, 0);
    }
    h.queued.push({...response({error: '稍后再试'}, 503), headers: {get: () => '9'}});
    await h.form().invoke('submit'); assert.match(h.role('status').textContent, /9 秒后重试/);
  },
  async destroyed_widget_cancels_pending_work_and_detached_handlers_cannot_import() {
    const h = harness(); await h.preview(); const detached = h.role('import-plain');
    h.api.destroy(); await detached.invoke('click'); assert.equal(h.imports.length, 0); assert.equal(h.root.children.length, 0);
    const p = harness(), pending = deferred(); p.api.setQuery({track_name: '歌曲'});
    p.queued.push(() => pending.promise); const searching = p.form().invoke('submit'); await flush();
    p.api.destroy(); await searching; pending.resolve(response({results: [record()]})); await flush();
    assert.equal(p.root.children.length, 0); assert.equal(p.clock.timers.size, 0);
  },
};
(async () => {
  for (const [name, test] of Object.entries(tests)) { await test(); process.stdout.write(`PASS ${name}\n`); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
