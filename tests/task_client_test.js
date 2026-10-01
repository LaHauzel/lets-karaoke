'use strict';
const assert = require('node:assert/strict');
const {monitor, recover, cancelJobs, json, identity} = require('../src/webui_assets/task_client.js');

const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
class Clock {
  time = 0; next = 0; timers = new Map();
  set = (fn, delay) => { const id = ++this.next; this.timers.set(id, {at: this.time + delay, fn}); return id; };
  clear = id => this.timers.delete(id);
  async tick(ms) {
    const end = this.time + ms;
    for (;;) {
      const next = [...this.timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      this.time = next[1].at; this.timers.delete(next[0]); next[1].fn(); await flush();
    }
    this.time = end; await flush();
  }
}
function harness(status, options = {}) {
  const clock = new Clock(), sources = [], updates = [];
  class Source {
    constructor() { this.closed = false; sources.push(this); }
    close() { this.closed = true; }
    emit(value) { this.onmessage({data: JSON.stringify(value)}); }
    fail() { this.onerror(); }
  }
  const task = monitor('job1', {pollMs: 2, unavailableMs: 10, onUpdate: value => updates.push(value), ...options}, {
    EventSource: Source, status, now: () => clock.time, setTimeout: clock.set, clearTimeout: clock.clear,
  });
  return {task, clock, sources, updates};
}
const tests = {
  async terminal_is_latched_before_ui_completion() {
    const h = harness(async () => ({state: 'running', lines: ['first']})); await flush();
    h.sources[0].emit({state: 'done', lines: ['first', 'done'], result: {version: 1}});
    assert.equal(h.sources[0].closed, true);
    h.sources[0].emit({state: 'done', lines: ['done'], result: {version: 2}});
    const result = await h.task.promise;
    assert.equal(result.result.version, 1);
    assert.equal(h.updates.filter(x => x.state === 'done').length, 1);
    assert.deepEqual(h.updates.at(-1).lines, ['done']);
    assert.equal(h.clock.timers.size, 0);
  },
  async broken_sse_recovers_terminal_status() {
    let calls = 0;
    const h = harness(async () => ++calls === 1 ? {state: 'running'} : {state: 'done', result: {video: 'ok.mp4'}});
    await flush(); h.sources[0].fail(); await flush();
    assert.equal((await h.task.promise).state, 'done');
    assert.equal(h.sources[0].closed, true);
  },
  async missing_job_settles_instead_of_waiting_forever() {
    const error = Object.assign(new Error('missing'), {status: 404});
    const h = harness(async () => { throw error; });
    assert.equal((await h.task.promise).state, 'interrupted');
    assert.equal(h.sources[0].closed, true);
  },
  async outage_deadline_settles_even_if_status_probe_hangs() {
    let calls = 0;
    const h = harness(() => ++calls === 1 ? Promise.resolve({state: 'queued'}) : new Promise(() => {}));
    await flush(); h.sources[0].fail(); await flush(); await h.clock.tick(10);
    const result = await h.task.promise;
    assert.equal(result.state, 'interrupted'); assert.equal(result.unconfirmed, true);
    assert.equal(h.sources[0].closed, true);
  },
  async transient_outage_is_reset_after_reachable_status() {
    let calls = 0;
    const h = harness(async () => { if (++calls === 2) throw new Error('offline'); return {state: 'running'}; });
    await flush(); h.sources[0].fail(); await flush(); await h.clock.tick(2); await h.clock.tick(15);
    assert.equal(h.sources.length, 2); assert.equal(h.sources[1].closed, false);
    h.sources[1].emit({state: 'cancelled'});
    assert.equal((await h.task.promise).state, 'cancelled');
  },
  async explicit_backend_interruption_and_errors_are_terminal() {
    for (const state of ['interrupted', 'error']) {
      const h = harness(async () => ({state, error: 'worker stopped'}));
      const result = await h.task.promise;
      assert.equal(result.state, state); assert.equal(result.error, 'worker stopped');
      assert.equal(h.sources[0].closed, true);
    }
  },
  async ui_callback_failure_closes_transport() {
    const h = harness(async () => ({state: 'running'}), {onUpdate() { throw new Error('bad UI'); }});
    const result = await h.task.promise;
    assert.equal(result.state, 'error'); assert.equal(result.error, 'bad UI');
    assert.equal(h.clock.timers.size, 0);
  },
  async recovery_keeps_queued_and_unconsumed_completed_jobs() {
    const value = recover([{job: 'done', state: 'done', created: 3}, {job: 'queued', state: 'queued', created: 1},
      {job: 'old', state: 'done', created: 0}, {job: 'edit', kind: 'edit', state: 'running', created: 2}], ['done', 'missing']);
    assert.deepEqual(value.map(x => x.job), ['missing', 'queued', 'edit', 'done']);
    assert.equal(value[2].kind, 'edit');
  },
  async cancellation_attempts_every_job_even_after_failure() {
    const calls = [];
    const failures = await cancelJobs(['one', 'two', 'one', null, 'three'], async (url, value) => {
      calls.push([url, value.job]); if (value.job === 'two') throw new Error('unavailable');
    });
    assert.deepEqual(calls.map(x => x[1]), ['one', 'two', 'three']);
    assert.deepEqual(failures, [{job: 'two', error: 'unavailable'}]);
  },
  async draft_identity_matches_generation_and_history_aliases() {
    const raw = '1001_021928_32d6', path = 'webui/' + raw;
    const alias = 'local-' + Buffer.from(path).toString('base64url');
    assert.equal(identity(raw), path); assert.equal(identity(alias), path);
    assert.equal(identity(raw, [{job: alias, path}]), identity(alias, [{job: alias, path}]));
    const unicodePath = '导入目录/中文歌曲';
    assert.equal(identity('local-' + Buffer.from(unicodePath).toString('base64url')), unicodePath);
  },
  async json_sets_content_type_and_retains_terminal_error_status() {
    const previous = global.fetch; const calls = [];
    global.fetch = async (url, options) => { calls.push(options); return {ok: true, status: 200, json: async () => ({state: 'error', error: 'alignment failed'})}; };
    try {
      const response = await json('/api/job'); assert.equal(response.state, 'error');
      await json('/api/rerender', {async: true});
      assert.equal(calls[1].headers['Content-Type'], 'application/json');
      assert.equal(JSON.parse(calls[1].body).async, true);
    } finally { global.fetch = previous; }
  },
};
(async () => {
  for (const [name, test] of Object.entries(tests)) { await test(); process.stdout.write(`PASS ${name}\n`); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
