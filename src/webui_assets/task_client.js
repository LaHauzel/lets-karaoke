/* Durable task monitoring shared by generation and timeline edits. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TaskClient = api;
})(typeof window === 'object' ? window : globalThis, function () {
  const ACTIVE = new Set(['queued', 'running']);
  const TERMINAL = new Set(['done', 'error', 'cancelled', 'interrupted']);

  async function json(url, data, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 20000);
    try {
      const response = await fetch(url, {
        method: data === undefined ? 'GET' : 'POST',
        headers: data === undefined ? undefined : {'Content-Type': 'application/json'},
        body: data === undefined ? undefined : JSON.stringify(data),
        signal: controller.signal,
      });
      const value = await response.json();
      if (!response.ok || (value.error && !value.state)) {
        const error = new Error(value.error || `请求失败（${response.status}）`);
        error.status = response.status;
        throw error;
      }
      return value;
    } finally { clearTimeout(timer); }
  }

  function monitor(job, options = {}, dependencies = {}) {
    const Source = dependencies.EventSource || globalThis.EventSource;
    const status = dependencies.status || (id => json('/api/job?job=' + encodeURIComponent(id), undefined, {timeout: 8000}));
    const now = dependencies.now || Date.now;
    const later = dependencies.setTimeout || setTimeout;
    const clear = dependencies.clearTimeout || clearTimeout;
    const pollMs = options.pollMs || 2500;
    const unavailableMs = options.unavailableMs || 60000;
    let source = null, pollTimer = null, deadline = null, outageAt = null;
    let settled = false, probing = false, resolve;
    const promise = new Promise(done => { resolve = done; });
    const seenLogs = new Set();

    function stopTransport() {
      if (source) { source.close(); source = null; }
      if (pollTimer != null) clear(pollTimer);
      if (deadline != null) clear(deadline);
      pollTimer = deadline = null;
    }
    function complete(value) {
      if (settled) return;
      // Latch and close before any asynchronous UI work can receive another done.
      settled = true;
      stopTransport();
      resolve(value);
    }
    function markReachable() {
      outageAt = null;
      if (deadline != null) clear(deadline);
      deadline = null;
      options.onConnection?.(true);
    }
    function markUnavailable() {
      if (settled || outageAt != null) return;
      outageAt = now();
      options.onConnection?.(false);
      deadline = later(() => complete({state: 'interrupted', job, unconfirmed: true,
        error: '本地服务已连续 60 秒无法连接。任务状态尚未确认；服务恢复后可在历史记录继续查看或重新运行。'}), unavailableMs);
    }
    function receive(value) {
      if (settled) return;
      markReachable();
      const lines = (value.lines || []).filter(line => {
        if (seenLogs.has(line)) return false;
        seenLogs.add(line); return true;
      });
      const update = {...value, lines};
      try { options.onUpdate?.(update); }
      catch (error) { complete({state: 'error', job, error: error.message}); return; }
      if (TERMINAL.has(value.state)) complete({...value, job: value.job || job});
    }
    function connect() {
      if (settled || source || !Source) return;
      try {
        source = new Source('/api/events?job=' + encodeURIComponent(job));
        source.onmessage = event => {
          try { receive(JSON.parse(event.data)); }
          catch (_) { probe(); }
        };
        source.onerror = () => {
          if (source) { source.close(); source = null; }
          markUnavailable(); probe();
        };
      } catch (_) { markUnavailable(); }
    }
    async function probe() {
      if (settled || probing) return;
      probing = true;
      try {
        const value = await status(job);
        if (settled) return;
        receive(value);
        if (!settled) connect();
      } catch (error) {
        if (settled) return;
        if (error.status === 404) {
          complete({state: 'interrupted', job, error: '后台已找不到此任务。请刷新历史记录，查看中断状态并重新运行。'});
        } else { markUnavailable(); }
      } finally {
        probing = false;
        if (!settled) {
          if (pollTimer != null) clear(pollTimer);
          pollTimer = later(probe, pollMs);
        }
      }
    }
    connect();
    // Query immediately: a queued task may have finished before monitoring begins.
    probe();
    return {promise, probe, close() { complete({state: 'interrupted', job, unconfirmed: true, error: '已停止查看此任务；后台仍会继续处理。'}); }};
  }

  function recover(serverJobs, savedIds = []) {
    const remembered = new Set(savedIds);
    const selected = serverJobs.filter(job => ACTIVE.has(job.state) || remembered.has(job.job));
    const known = new Set(selected.map(job => job.job));
    for (const id of remembered) if (!known.has(id)) selected.push({job: id, state: 'unknown', created: 0});
    return selected.sort((a, b) => (a.created || 0) - (b.created || 0));
  }

  async function cancelJobs(ids, request = json) {
    const unique = [...new Set(ids.filter(Boolean))];
    const results = await Promise.allSettled(unique.map(job => request('/api/cancel', {job})));
    return results.flatMap((result, index) => result.status === 'rejected'
      ? [{job: unique[index], error: result.reason?.message || String(result.reason)}] : []);
  }
  function identity(job, history = []) {
    const known = history.find(record => record.job === job || record.path === 'webui/' + job);
    if (known) return known.path;
    if (String(job).startsWith('local-')) try {
      const encoded = String(job).slice(6).replace(/-/g, '+').replace(/_/g, '/');
      const bytes = Uint8Array.from(atob(encoded + '='.repeat((4 - encoded.length % 4) % 4)), char => char.charCodeAt(0));
      return new TextDecoder().decode(bytes);
    } catch (_) {}
    return 'webui/' + job;
  }
  return {json, monitor, recover, cancelJobs, identity, active: state => ACTIVE.has(state)};
});
