/* Online lyrics are previewed first and imported only by an explicit action. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.LyricsSearch = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const DEFAULT_SOURCE = 'https://lrclib.net/';

  function trustedSource(value) {
    try {
      const url = new URL(value);
      if (url.protocol === 'https:' && url.hostname === 'lrclib.net'
          && !url.username && !url.password && (!url.port || url.port === '443')) return url.href;
    } catch (_) {}
    return DEFAULT_SOURCE;
  }

  function mount(options) {
    const {root, onImport, canImport = () => true, contextKey = () => 'input'} = options || {};
    if (!root?.ownerDocument || typeof onImport !== 'function') throw new Error('歌词搜索需要容器和导入处理函数。');
    const doc = root.ownerDocument;
    const fetchJson = options.fetch || ((...args) => globalThis.fetch(...args));
    const later = options.setTimeout || setTimeout, clear = options.clearTimeout || clearTimeout;
    const confirmReplace = options.confirmReplace || options.confirm || null;
    const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : 20000;
    let key = readContext(), sequence = 0, pending = null, selected = null;
    let importing = false, destroyed = false, confirmation = null;
    const resultButtons = [], importButtons = [];

    function readContext() { return String(typeof contextKey === 'function' ? contextKey() : contextKey); }
    function node(tag, className, text, role) {
      const value = doc.createElement(tag);
      if (className) value.className = className;
      if (text !== undefined) value.textContent = String(text);
      if (role) value.dataset.lyricsRole = role;
      return value;
    }
    function button(text, role, action) {
      const value = node('button', 'ls-button', text, role);
      value.type = 'button';
      if (action) value.addEventListener('click', action);
      return value;
    }
    function sourceLink(source) {
      const link = node('a', 'ls-source', '来源：LRCLIB');
      link.href = trustedSource(source); link.target = '_blank'; link.rel = 'noopener noreferrer';
      return link;
    }

    const panel = node('section', 'lyrics-search');
    panel.append(node('h3', 'ls-heading', '在线查找歌词'));
    panel.append(node('p', 'ls-note', '输入歌名和歌手，从 LRCLIB 查找。先预览，再导入到当前文件。'));
    panel.append(node('p', 'ls-note', '只有主动搜索或预览时才请求歌词服务；搜索发送歌名与艺人，不发送媒体或已有歌词。', 'privacy'));
    const form = node('form', 'ls-form');
    const song = node('input', 'ls-input', undefined, 'song');
    song.type = 'text'; song.maxLength = 200; song.placeholder = '歌曲名'; song.autocomplete = 'off';
    const artist = node('input', 'ls-input', undefined, 'artist');
    artist.type = 'text'; artist.maxLength = 200; artist.placeholder = '歌手 / 乐队（可选）'; artist.autocomplete = 'off';
    const songLabel = node('label', 'ls-label', '歌名'); songLabel.append(song);
    const artistLabel = node('label', 'ls-label', '歌手'); artistLabel.append(artist);
    form.append(songLabel, artistLabel);
    const actions = node('div', 'ls-actions');
    const search = button('搜索歌词', 'search'); search.type = 'submit';
    const cancelButton = button('取消请求', 'cancel', () => cancel()); cancelButton.hidden = true;
    actions.append(search, cancelButton); form.append(actions); panel.append(form);
    const status = node('p', 'ls-status', '', 'status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const results = node('div', 'ls-results', undefined, 'results');
    const preview = node('section', 'ls-preview', undefined, 'preview'); preview.hidden = true;
    const confirmBox = node('section', 'ls-confirm', undefined, 'confirmation'); confirmBox.hidden = true;
    confirmBox.setAttribute('role', 'group'); confirmBox.setAttribute('aria-label', '确认替换当前歌词');
    const confirmText = node('p', 'ls-confirm-text', '', 'confirm-text');
    const confirmActions = node('div', 'ls-actions');
    const confirmYes = button('确认替换', 'confirm-replace', () => settleConfirmation(true));
    const confirmKeep = button('保留当前歌词', 'confirm-keep', () => settleConfirmation(false));
    confirmActions.append(confirmYes, confirmKeep); confirmBox.append(confirmText, confirmActions);
    confirmBox.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); settleConfirmation(false); } });
    const importHint = node('p', 'ls-note', '', 'import-hint'); importHint.hidden = true;
    panel.append(status, results, preview, confirmBox, importHint);
    panel.append(node('p', 'ls-note', '网络歌词可能与现场版本不同，请核对内容。LRC 通常只有行时间，逐字高亮仍需本地对齐。'));
    root.replaceChildren(panel);

    function say(message, error = false) {
      status.textContent = message; status.classList.toggle('ls-error', error);
    }
    function permission() {
      try {
        const value = typeof canImport === 'function' ? canImport() : canImport;
        return typeof value === 'boolean' ? {allowed: value, hasLyrics: false, reason: ''}
          : {allowed: value?.allowed === true, hasLyrics: value?.hasLyrics === true, reason: String(value?.reason || '')};
      } catch (_) { return {allowed: false, reason: '当前无法导入歌词，请稍后重试。', hasLyrics: false}; }
    }
    function refreshAvailability() {
      if (destroyed) return;
      const allowed = permission();
      search.disabled = importing;
      song.disabled = artist.disabled = importing;
      cancelButton.hidden = !pending && !confirmation;
      cancelButton.textContent = confirmation ? '取消导入确认' : '取消请求';
      resultButtons.forEach(value => { value.disabled = importing || value.dataset.unavailable === 'true'; });
      importButtons.forEach(({value, format}) => {
        value.disabled = !selected || Boolean(pending) || importing || !allowed.allowed;
        value.textContent = (allowed.hasLyrics ? '替换为' : '导入') + (format === 'plain' ? '纯文本歌词' : ' LRC 歌词');
        value.title = !allowed.allowed ? allowed.reason || '任务处理中，暂时无法导入。' : '';
      });
      importHint.hidden = !selected;
      importHint.textContent = !allowed.allowed ? allowed.reason || '任务处理中，暂时无法导入歌词。'
        : allowed.hasLyrics ? '当前文件已有歌词，导入前会确认替换。' : '点击导入后，只更新当前文件的歌词。';
      confirmYes.disabled = !allowed.allowed;
      confirmYes.title = allowed.allowed ? '' : allowed.reason || '任务处理中，暂时无法导入。';
      panel.setAttribute('aria-busy', String(Boolean(pending) || (importing && !confirmation)));
    }
    function stopRequest() {
      if (!pending) return;
      const request = pending; pending = null;
      clear(request.timer); request.controller.abort();
      request.reject(Object.assign(new Error('请求已取消'), {name: 'AbortError'}));
    }
    function clearSelection() {
      settleConfirmation(false);
      selected = null; resultButtons.length = importButtons.length = 0;
      results.replaceChildren(); preview.replaceChildren(); preview.hidden = true;
    }
    function refreshContext() {
      if (destroyed) return false;
      const next = readContext(), changed = next !== key;
      if (changed) {
        key = next; sequence++; stopRequest(); clearSelection();
        say('已切换文件，请为当前文件重新查找歌词。');
      }
      refreshAvailability();
      return changed;
    }
    function current(request) {
      if (destroyed) return false;
      refreshContext();
      return pending === request && request.key === key && request.sequence === sequence;
    }
    function cancel() {
      if (destroyed || (!pending && !confirmation)) return;
      sequence++; stopRequest(); settleConfirmation(false); say('已取消请求。'); refreshAvailability();
    }
    function settleConfirmation(accept) {
      if (!confirmation) return;
      const choice = confirmation; confirmation = null; confirmBox.hidden = true;
      choice.resolve(accept === true); refreshAvailability();
    }
    async function askReplace(message) {
      let resolve;
      const cancelled = new Promise(done => { resolve = done; });
      const choice = {resolve}; confirmation = choice;
      if (!confirmReplace) {
        confirmText.textContent = message; confirmBox.hidden = false;
        confirmKeep.focus();
      }
      refreshAvailability();
      try {
        return (await (confirmReplace ? Promise.race([cancelled, Promise.resolve().then(() => confirmReplace(message))]) : cancelled)) === true;
      } finally {
        if (confirmation === choice) { confirmation = null; confirmBox.hidden = true; refreshAvailability(); }
      }
    }

    async function request(url, body, message, receive) {
      refreshContext(); stopRequest();
      const controller = new AbortController();
      const token = {controller, key, sequence: ++sequence, timer: null, reject: null, timedOut: false};
      const aborted = new Promise((_, reject) => { token.reject = reject; });
      pending = token; say(message); refreshAvailability();
      token.timer = later(() => {
        token.timedOut = true; controller.abort();
        token.reject(Object.assign(new Error('歌词服务响应超时，请重试。'), {name: 'TimeoutError'}));
      }, timeoutMs);
      try {
        const work = (async () => {
          const response = await fetchJson(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body), signal: controller.signal});
          const data = await response.json();
          if (!response.ok || data?.error) {
            let message = String(data?.error || `请求失败（${response.status}）`);
            const retry = Number(data?.retry_after ?? response.headers?.get?.('Retry-After'));
            if ([429, 503].includes(response.status) && Number.isFinite(retry) && retry > 0) message += `（请在 ${Math.ceil(retry)} 秒后重试）`;
            throw new Error(message);
          }
          return data;
        })();
        const data = await Promise.race([work, aborted]);
        if (!current(token)) return;
        if (token.timedOut) throw new Error('歌词服务响应超时，请重试。');
        receive(data);
      } catch (error) {
        if (current(token)) say(token.timedOut ? '歌词服务响应超时，请重试。'
          : error?.name === 'AbortError' ? '已取消请求。' : '获取歌词失败：' + String(error?.message || error), error?.name !== 'AbortError');
      } finally {
        clear(token.timer);
        if (pending === token) { pending = null; refreshAvailability(); }
      }
    }

    function metadata(record) {
      const parts = [record.artist, record.album].filter(value => typeof value === 'string' && value.trim());
      const seconds = Number(record.duration);
      const total = Math.round(seconds);
      if (Number.isFinite(seconds) && seconds > 0) parts.push(`${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`);
      return parts.join(' · ');
    }
    function renderResults(data) {
      if (!data || !Array.isArray(data.results)) throw new Error('歌词服务返回的数据格式不正确。');
      results.replaceChildren(); resultButtons.length = 0;
      const records = data.results.filter(record => record && Number.isSafeInteger(Number(record.id)) && Number(record.id) > 0).slice(0, 30);
      if (!records.length) { say('未找到歌词，请检查歌名，或调整歌手名称后重试。'); return; }
      say(`找到 ${records.length} 条结果，请选择与当前歌曲一致的版本。`);
      for (const record of records) {
        const card = node('article', 'ls-result');
        card.append(node('strong', 'ls-title', record.title || '未命名歌曲'));
        card.append(node('p', 'ls-meta', metadata(record)));
        const tags = node('div', 'ls-tags');
        if (record.has_plain) tags.append(node('span', 'ls-tag', '纯文本'));
        if (record.has_synced) tags.append(node('span', 'ls-tag', 'LRC 时间轴'));
        if (record.instrumental) tags.append(node('span', 'ls-tag', '纯音乐'));
        const row = node('div', 'ls-actions');
        row.append(sourceLink(data.source_url));
        const unavailable = !record.has_plain && !record.has_synced;
        const open = button(unavailable ? '暂无可用歌词' : '预览歌词', 'open-result', () => openRecord(record.id));
        open.dataset.unavailable = String(unavailable); open.disabled = unavailable;
        resultButtons.push(open); row.append(open); card.append(tags, row); results.append(card);
      }
    }
    async function openRecord(id) {
      if (destroyed || importing || refreshContext()) return;
      selected = null; preview.replaceChildren(); preview.hidden = true; importButtons.length = 0;
      await request('/api/lyrics/get', {id: Number(id)}, '正在获取歌词预览…', data => {
        if (!data || Number(data.id) !== Number(id)) throw new Error('歌词服务返回的歌曲编号不匹配。');
        renderPreview(data);
      });
    }
    function renderPreview(record) {
      const plain = typeof record.plain_lyrics === 'string' ? record.plain_lyrics : '';
      const synced = typeof record.synced_lyrics === 'string' ? record.synced_lyrics : '';
      if (!plain.trim() && !synced.trim()) {
        selected = null; preview.hidden = true;
        say(record.instrumental ? '此版本为纯音乐，没有可导入的歌词。' : '此版本暂时没有可导入的歌词。'); return;
      }
      selected = {...record, plain_lyrics: plain, synced_lyrics: synced, context_key: key};
      const recordKey = selected;
      preview.replaceChildren(); importButtons.length = 0; preview.hidden = false;
      preview.append(node('h4', 'ls-title', record.title || '歌词预览'));
      preview.append(node('p', 'ls-meta', metadata(record)));
      preview.append(sourceLink(record.source_url));
      const modes = node('div', 'ls-actions');
      const text = node('pre', 'ls-lyrics', plain.trim() ? plain : synced, 'lyrics');
      const imports = node('div', 'ls-actions');
      for (const [format, content, label] of [['plain', plain, '纯文本'], ['lrc', synced, 'LRC 时间轴']]) {
        if (!content.trim()) continue;
        const mode = button('预览' + label, 'preview-' + format, () => { text.textContent = content; });
        modes.append(mode);
        const value = button('', 'import-' + format, () => importRecord(recordKey, format));
        value.classList.add('ls-import'); importButtons.push({value, format}); imports.append(value);
      }
      preview.append(modes, text, imports); say('已获取预览。确认歌名、歌手及歌词内容后再导入。');
    }
    async function importRecord(record, format) {
      if (destroyed || importing || pending || refreshContext() || selected !== record) return;
      let allowed = permission();
      if (!allowed.allowed) { say(allowed.reason || '任务处理中，暂时无法导入歌词。', true); refreshAvailability(); return; }
      const text = format === 'plain' ? record.plain_lyrics : record.synced_lyrics;
      if (!text?.trim()) return;
      importing = true; refreshAvailability();
      try {
        if (allowed.hasLyrics) {
          let accept;
          try {
            accept = await askReplace(`当前文件已有歌词。确认用「${record.title || '所选歌曲'}」的${format === 'plain' ? '纯文本' : 'LRC'}歌词替换吗？`);
          } catch (_) {
            if (!destroyed && !refreshContext() && selected === record) say('无法确认替换，当前歌词已保留，请重试。', true);
            return;
          }
          if (!accept) {
            if (!destroyed && !refreshContext() && selected === record) say('已保留当前文件的歌词。');
            return;
          }
        }
        // Both inline and injected confirmations may outlive the active file.
        if (destroyed || refreshContext() || selected !== record || record.context_key !== key) return;
        allowed = permission();
        if (!allowed.allowed) { say(allowed.reason || '当前无法导入歌词。', true); refreshAvailability(); return; }
        await onImport({text, title: String(record.title || ''), artist: String(record.artist || ''), format,
          source_url: trustedSource(record.source_url), provider: String(record.provider || 'LRCLIB'), context_key: key});
        if (!destroyed && !refreshContext() && selected === record) say('歌词已导入当前文件，可继续核对并生成。');
      } catch (error) {
        if (!destroyed && !refreshContext() && selected === record) say('导入歌词失败：' + String(error?.message || error), true);
      } finally { importing = false; refreshAvailability(); }
    }
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (destroyed || importing) return;
      refreshContext();
      if (!song.value.trim()) { say('请先输入歌曲名。', true); song.focus(); return; }
      clearSelection();
      await request('/api/lyrics/search', {track_name: song.value.trim(), artist_name: artist.value.trim()},
        '正在搜索歌词…', renderResults);
    });
    refreshAvailability();
    return {
      refreshContext, refreshAvailability, cancel,
      setQuery(query = {}) {
        if (destroyed) return;
        refreshContext();
        if ('track_name' in query) song.value = String(query.track_name || '').slice(0, 200);
        if ('artist_name' in query) artist.value = String(query.artist_name || '').slice(0, 200);
      },
      destroy() {
        if (destroyed) return;
        destroyed = true; sequence++; stopRequest(); settleConfirmation(false); selected = null; root.replaceChildren();
      },
    };
  }
  return {mount, trustedSource};
});
