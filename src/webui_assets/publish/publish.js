/* Publish preparation workspace: approved candidates, preflight and local packages. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else {
    root.PublishUI = api;
    if (root.document) api.init();
  }
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const PLATFORM_LABELS = {bilibili: 'B站', douyin: '抖音'};
  const state = {
    library: null, token: '', track: null, platform: 'bilibili', settings: null,
    plan: null, job: null, saveTimer: null, pollTimer: null, busy: false,
  };

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = value, unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size.toFixed(unit === 0 || size >= 10 ? 0 : 1)} ${units[unit]}`;
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    const rest = String(value % 60).padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`;
  }

  function platformName(key) {
    return PLATFORM_LABELS[key] || String(key || '');
  }

  function summarizePlan(plan) {
    let errors = 0, warnings = 0;
    for (const item of Object.values((plan && plan.platforms) || {})) {
      errors += (item.errors || []).length;
      warnings += (item.warnings || []).length;
    }
    return {ok: Boolean(plan && plan.ok), errors, warnings};
  }

  function toast(message, failure = false) {
    const node = $('toast');
    if (!node) return;
    node.textContent = String(message || '');
    node.classList.toggle('failure', Boolean(failure));
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { node.hidden = true; }, 3800);
  }

  async function request(path, options = {}) {
    const headers = {'Accept': 'application/json'};
    if (options.body) {
      headers['Content-Type'] = 'application/json';
      headers['X-Publish-Token'] = state.token;
    }
    const response = await fetch(path, {
      method: options.method || 'GET', headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    let data = {};
    try { data = await response.json(); } catch (error) { /* keep empty */ }
    if (!response.ok) {
      const failure = new Error(data.error || `请求失败（${response.status}）`);
      failure.data = data;
      throw failure;
    }
    return data;
  }

  function libraryTrack(id) {
    if (!state.library) return null;
    return state.library.tracks.find(track => track.id === id) || null;
  }

  async function load(keepSelection = false) {
    try {
      const data = await request('/api/publish/library');
      state.library = data;
      state.token = data.token;
      $('loadError').hidden = true;
      $('candidateCount').textContent = `${data.tracks.length} 首可发布 · 跳过 ${data.skipped} 首`;
      $('updated').textContent = '清单更新 ' + new Date(data.updated).toLocaleString('zh-CN', {hour12: false});
      if (data.content_warning) {
        $('contentWarning').textContent = data.content_warning;
        $('contentWarning').hidden = false;
      }
      const previous = keepSelection && state.track ? state.track.id : null;
      const selected = (previous && libraryTrack(previous)) || data.tracks[0] || null;
      state.track = selected;
      renderCandidates();
      if (!data.tracks.length) {
        $('candidateEmpty').hidden = false;
        $('editor').hidden = true;
        return;
      }
      $('candidateEmpty').hidden = true;
      selectTrack(selected.id);
    } catch (error) {
      $('loadError').textContent = error.message;
      $('loadError').hidden = false;
    }
  }

  function renderCandidates() {
    const list = $('candidateList');
    if (!state.library) return;
    list.innerHTML = state.library.tracks.map(track => {
      const packages = (track.state && track.state.packages) || {};
      const badges = Object.keys(packages).map(platform => {
        const record = packages[platform];
        return `<span class="badge ${record.submitted ? 'ok' : ''}">${esc(platformName(platform))} · ${record.submitted ? '已标记投稿' : '包已生成'}</span>`;
      }).join('');
      const current = state.track && state.track.id === track.id;
      return `<button type="button" class="candidate" role="listitem" data-track="${esc(track.id)}" aria-current="${current}">
        <strong>${esc(track.title)}</strong>
        <div class="candidate-sub">${esc(track.concert || '未标注场次')} · ${formatDuration(track.duration)}</div>
        ${badges ? `<div class="candidate-badges">${badges}</div>` : ''}
      </button>`;
    }).join('');
  }

  function selectTrack(id) {
    const track = libraryTrack(id);
    if (!track) return;
    state.track = track;
    state.settings = structuredClone(track.settings || {});
    state.platform = 'bilibili';
    state.plan = null;
    state.job = null;
    renderCandidates();
    $('editor').hidden = false;
    renderEditor();
    renderPlan(null);
    renderResults();
    renderHistory();
    setBusy(false);
  }

  function renderEditor() {
    const track = state.track;
    $('trackConcert').textContent = track.concert || track.project || '';
    $('trackTitle').textContent = track.title || track.track;
    $('approvalBadge').textContent = '已审批 · 文件签名有效';
    $('approvalBadge').className = 'badge ok';
    $('sourcePath').textContent = track.approved.path;
    const common = state.settings.common || {};
    $('optAudio').value = common.audio || 'original';
    $('optSubtitle').value = common.subtitle || (track.subtitles.length ? 'burn' : 'none');
    $('optSubtitleLanguage').value = common.subtitle_language || 'original';
    $('optVideoMode').value = common.video_mode || 'transcode';
    $('optFraming').value = common.framing || 'source';
    $('optCoverTime').value = Number(common.cover_time ?? 0);
    const hasSecondAudio = Boolean(track.approved.multitrack && track.approved.multitrack.audio_tracks > 1);
    $('optAudio').querySelector('option[value="accompaniment"]').disabled = !hasSecondAudio;
    const hasOriginal = track.subtitles.some(item => !item.bilingual);
    const hasBilingual = track.subtitles.some(item => item.bilingual);
    $('optSubtitle').querySelector('option[value="burn"]').disabled = !track.subtitles.length;
    $('optSubtitleLanguage').querySelector('option[value="original"]').disabled = !hasOriginal;
    $('optSubtitleLanguage').querySelector('option[value="bilingual"]').disabled = !hasBilingual;
    $('optVideoMode').querySelector('option[value="copy"]').disabled = !/\.mp4$/i.test(track.approved.path);
    $('subtitleLanguageLabel').hidden = $('optSubtitle').value !== 'burn';
    const notes = [];
    notes.push(hasSecondAudio ? '源文件包含原唱/伴奏两路音轨，可在投稿包中选择其中一路。' : '源文件只有一个音轨，投稿包将使用该音轨。');
    if (!track.subtitles.length) notes.push('没有可用 ASS 字幕，投稿包只能不烧录字幕。');
    $('optionNote').textContent = notes.join(' ');
    $('platformEnabled').checked = (state.settings.enabled || {})[state.platform] !== false;
    updatePlatformTabs();
    renderPlatformForm();
  }

  function updatePlatformTabs() {
    document.querySelectorAll('.platform-tabs button').forEach(button => {
      button.setAttribute('aria-selected', String(button.dataset.platform === state.platform));
    });
  }

  function renderPlatformForm() {
    const spec = state.library && state.library.platforms[state.platform];
    if (!spec) return;
    const metadata = (state.settings.platforms || {})[state.platform] || {};
    $('platformForm').innerHTML = spec.fields.map(field => {
      const id = 'pf-' + field.key;
      const value = metadata[field.key];
      const hint = field.hint ? `<span class="muted small">${esc(field.hint)}</span>` : '';
      if (field.type === 'textarea') {
        return `<label class="field wide">${esc(field.label)}<textarea id="${id}" data-key="${esc(field.key)}" maxlength="${field.max}" rows="3">${esc(value ?? '')}</textarea>${hint}</label>`;
      }
      if (field.type === 'choice') {
        return `<label class="field">${esc(field.label)}<select id="${id}" data-key="${esc(field.key)}">${field.choices.map(([key, label]) => `<option value="${esc(key)}" ${value === key ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>${hint}</label>`;
      }
      if (field.type === 'bool') {
        return `<label class="field check"><input type="checkbox" id="${id}" data-key="${esc(field.key)}" ${value ? 'checked' : ''}>${esc(field.label)}</label>`;
      }
      return `<label class="field">${esc(field.label)}<input id="${id}" data-key="${esc(field.key)}" type="text" maxlength="${field.max}" value="${esc(value ?? '')}">${hint}</label>`;
    }).join('');
    $('platformEnabled').checked = (state.settings.enabled || {})[state.platform] !== false;
  }

  function collectCommon() {
    const common = state.settings.common || (state.settings.common = {});
    common.audio = $('optAudio').value;
    common.subtitle = $('optSubtitle').value;
    common.subtitle_language = $('optSubtitleLanguage').value;
    common.video_mode = $('optVideoMode').value;
    common.framing = $('optFraming').value;
    const cover = Number($('optCoverTime').value);
    common.cover_time = Number.isFinite(cover) ? cover : 0;
    $('subtitleLanguageLabel').hidden = common.subtitle !== 'burn';
  }

  function collectPlatform() {
    const metadata = (state.settings.platforms || (state.settings.platforms = {}))[state.platform] || {};
    document.querySelectorAll('#platformForm [data-key]').forEach(input => {
      const key = input.dataset.key;
      metadata[key] = input.type === 'checkbox' ? input.checked : input.value;
    });
    state.settings.platforms[state.platform] = metadata;
    state.settings.enabled = state.settings.enabled || {};
    state.settings.enabled[state.platform] = $('platformEnabled').checked;
  }

  function collectSettings() {
    collectCommon();
    collectPlatform();
    return state.settings;
  }

  function saveStateLabel(text, failure = false) {
    const node = $('saveState');
    node.textContent = text;
    node.style.color = failure ? 'var(--lk-danger)' : '';
  }

  function scheduleSave() {
    saveStateLabel('正在保存…');
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => { saveSettings(); }, 700);
  }

  async function saveSettings() {
    if (!state.track) return;
    clearTimeout(state.saveTimer);
    collectSettings();
    try {
      await request('/api/publish/settings', {
        method: 'POST', body: {track_id: state.track.id, settings: state.settings},
      });
      saveStateLabel('已保存 ' + new Date().toLocaleTimeString('zh-CN', {hour12: false}));
    } catch (error) {
      saveStateLabel(error.message, true);
    }
  }

  function enabledPlatforms() {
    const enabled = state.settings.enabled || {};
    const order = (state.library && state.library.platform_order) || Object.keys(PLATFORM_LABELS);
    return order.filter(platform => enabled[platform] !== false);
  }

  function renderPlan(plan) {
    const section = $('preflightSection');
    if (!plan) { section.hidden = true; return; }
    section.hidden = false;
    const summary = summarizePlan(plan);
    $('preflightState').textContent = summary.ok ? '预检通过' :
      summary.errors ? `发现 ${summary.errors} 个错误` : `通过，但有 ${summary.warnings} 条提醒`;
    const chunks = [];
    for (const error of plan.errors || []) chunks.push(`<li>${esc(error)}</li>`);
    if (plan.source) {
      chunks.push(`<div class="platform-check"><h4>源视频</h4><p class="source-summary">
        <span>时长 <b>${formatDuration(plan.source.duration)}</b></span>
        <span>大小 <b>${formatBytes(plan.source.size)}</b></span>
        <span>画面 <b>${plan.source.video.width}×${plan.source.video.height}</b></span>
        <span>帧率 <b>${plan.source.video.fps}</b></span>
        <span>音轨 <b>${plan.source.audio.length}</b></span>
        <span>烧录字幕 <b>${plan.source.subtitle_name ? esc(plan.source.subtitle_name) : '不烧录'}</b></span>
      </p></div>`);
    }
    for (const [platform, item] of Object.entries(plan.platforms || {})) {
      const errors = (item.errors || []).map(text => `<li>${esc(text)}</li>`).join('');
      const warnings = (item.warnings || []).map(text => `<li>${esc(text)}</li>`).join('');
      chunks.push(`<div class="platform-check"><h4>${esc(platformName(platform))} · ${esc(item.output)}</h4>
        ${errors ? `<ul class="issue-list error-list">${errors}</ul>` : '<p class="muted small">字段与视频规格检查通过。</p>'}
        ${warnings ? `<ul class="issue-list warn-list">${warnings}</ul>` : ''}</div>`);
    }
    $('preflightBody').innerHTML = chunks.join('');
  }

  function setBusy(busy) {
    state.busy = Boolean(busy);
    $('preflight').disabled = state.busy;
    $('generate').disabled = state.busy;
  }

  async function runPreflight() {
    if (!state.track) return null;
    collectSettings();
    await saveSettings();
    const platforms = enabledPlatforms();
    if (!platforms.length) { toast('请至少启用一个发布平台', true); return null; }
    setBusy(true);
    $('jobState').textContent = '正在预检…';
    try {
      const plan = await request('/api/publish/preflight', {
        method: 'POST', body: {track_id: state.track.id, platforms, settings: state.settings},
      });
      state.plan = plan;
      renderPlan(plan);
      const summary = summarizePlan(plan);
      $('jobState').textContent = summary.ok ? '预检通过' : '预检发现问题';
      return plan;
    } catch (error) {
      toast(error.message, true);
      $('jobState').textContent = '';
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function generate() {
    if (!state.track || state.busy) return;
    const plan = await runPreflight();
    if (!plan) return;
    const summary = summarizePlan(plan);
    if (!summary.ok) { toast('预检未通过，请先按提示修正', true); return; }
    setBusy(true);
    $('jobState').textContent = '正在启动生成…';
    try {
      const data = await request('/api/publish/start', {
        method: 'POST', body: {track_id: state.track.id, platforms: enabledPlatforms(), settings: state.settings},
      });
      state.job = data.job;
      $('cancel').hidden = false;
      $('progressWrap').hidden = false;
      $('progressBar').style.width = '0%';
      startPolling();
    } catch (error) {
      const failure = error.data || {};
      if (failure.plan) { state.plan = failure.plan; renderPlan(failure.plan); }
      toast(error.message, true);
      setBusy(false);
    }
  }

  function updateProgress(job) {
    $('progressBar').style.width = `${Math.max(0, Math.min(100, job.progress || 0))}%`;
    $('jobState').textContent = `${job.stage || '生成中'} · ${job.progress || 0}%`;
  }

  function startPolling() {
    clearTimeout(state.pollTimer);
    const tick = async () => {
      try {
        const job = await request('/api/publish/job?job=' + encodeURIComponent(state.job));
        updateProgress(job);
        if (job.state === 'running') {
          state.pollTimer = setTimeout(tick, 1000);
          return;
        }
        state.job = null;
        state.pollTimer = null;
        $('cancel').hidden = true;
        setBusy(false);
        if (job.state === 'done') {
          toast('投稿包已生成');
          await load(true);
          renderResults();
          renderHistory();
        } else if (job.state === 'cancelled') {
          toast('已取消生成', true);
          await load(true);
        } else {
          toast(job.error || '生成失败', true);
        }
      } catch (error) {
        state.pollTimer = null;
        $('cancel').hidden = true;
        setBusy(false);
        toast(error.message, true);
      }
    };
    state.pollTimer = setTimeout(tick, 800);
  }

  async function cancelJob() {
    if (!state.job) return;
    try {
      await request('/api/publish/cancel', {method: 'POST', body: {job: state.job}});
      $('cancel').disabled = true;
      $('jobState').textContent = '正在取消…';
    } catch (error) {
      toast(error.message, true);
    }
  }

  function renderResults() {
    const section = $('resultsSection');
    const packages = (state.track && state.track.state && state.track.state.packages) || {};
    const platforms = Object.keys(packages);
    if (!platforms.length) { section.hidden = true; return; }
    section.hidden = false;
    $('resultsBody').innerHTML = platforms.map(platform => {
      const record = packages[platform];
      const files = record.files || {};
      const links = ['video.mp4', 'cover.jpg', 'publish.json', 'upload-info.txt']
        .filter(name => files[name])
        .map(name => `<a href="${esc(files[name])}" target="_blank" rel="noopener">${name === 'video.mp4' ? '预览视频' : name}</a>`).join('');
      const submitted = record.submitted;
      return `<div class="result-card">
        <div class="result-head"><h4>${esc(platformName(platform))}</h4>
          <span class="badge ${submitted ? 'ok' : ''}">${submitted ? '已标记投稿' : '包已生成'}</span></div>
        <p class="muted small">生成于 ${esc(record.generated_at || '')} · 视频 SHA-256：<code>${esc((record.video_sha256 || '').slice(0, 16))}…</code></p>
        <div class="result-links">${links}<button type="button" data-copy-info="${esc(platform)}">复制投稿信息</button></div>
        ${(record.warnings || []).length ? `<ul class="issue-list warn-list">${record.warnings.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}
        ${submitted ? `<p class="submitted-note">已记录投稿：${esc(submitted.url || submitted.bvid || '（未填链接）')}${submitted.note ? ' · ' + esc(submitted.note) : ''}</p>` : `
        <div class="mark-form">
          <label>作品链接<input id="mark-url-${esc(platform)}" type="text" placeholder="https://..."></label>
          <label>稿件号<input id="mark-bvid-${esc(platform)}" type="text" placeholder="BV..."></label>
          <label>备注<input id="mark-note-${esc(platform)}" type="text" placeholder="可选"></label>
          <button type="button" data-mark="${esc(platform)}">标记已投稿</button>
        </div>`}
      </div>`;
    }).join('');
  }

  function renderHistory() {
    const history = (state.track && state.track.state && state.track.state.history) || [];
    const section = $('historySection');
    if (!history.length) { section.hidden = true; return; }
    section.hidden = false;
    const rows = history.slice().reverse().map(event => {
      const detail = event.event === 'submitted'
        ? `${esc(event.url || event.bvid || '')}${event.note ? ' · ' + esc(event.note) : ''}`
        : `${esc((event.video_sha256 || '').slice(0, 16))}…`;
      return `<tr><td>${esc(event.at || '')}</td><td>${event.event === 'submitted' ? '标记投稿' : '生成投稿包'}</td><td>${esc(platformName(event.platform))}</td><td>${detail}</td></tr>`;
    }).join('');
    $('historyBody').innerHTML = `<table class="history-table"><thead><tr><th>时间</th><th>事件</th><th>平台</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('已复制');
    } catch (error) {
      const area = document.createElement('textarea');
      area.value = text;
      document.body.append(area);
      area.select();
      document.execCommand('copy');
      area.remove();
      toast('已复制');
    }
  }

  async function copyUploadInfo(platform) {
    const record = state.track && state.track.state && state.track.state.packages
      && state.track.state.packages[platform];
    if (!record || !record.files || !record.files['upload-info.txt']) {
      toast('没有可复制的投稿信息', true);
      return;
    }
    try {
      const response = await fetch(record.files['upload-info.txt']);
      if (!response.ok) throw new Error('读取投稿信息失败');
      await copyText(await response.text());
    } catch (error) {
      toast(error.message, true);
    }
  }

  async function copySource() {
    if (state.track) await copyText(state.track.approved.path);
  }

  async function markSubmitted(platform) {
    const url = $(`mark-url-${platform}`).value;
    const bvid = $(`mark-bvid-${platform}`).value;
    const note = $(`mark-note-${platform}`).value;
    if (!url.trim() && !bvid.trim()) { toast('请至少填写作品链接或稿件号', true); return; }
    try {
      await request('/api/publish/mark', {
        method: 'POST', body: {track_id: state.track.id, platform, url, bvid, note},
      });
      toast('已记录投稿状态');
      await load(true);
      renderResults();
      renderHistory();
    } catch (error) {
      toast(error.message, true);
    }
  }

  function onTargetChange(event) {
    const target = event.target;
    if (target.closest('#platformForm') && target.dataset.key) {
      collectPlatform();
      scheduleSave();
      return;
    }
    if (target.closest('.option-grid')) {
      collectCommon();
      scheduleSave();
      renderOptionNote();
    }
  }

  function renderOptionNote() {
    const track = state.track;
    if (!track) return;
    if ($('optSubtitle').value === 'burn' && !track.subtitles.length) {
      $('optSubtitle').value = 'none';
      toast('没有可用 ASS 字幕，已改为不烧录', true);
    }
    $('subtitleLanguageLabel').hidden = $('optSubtitle').value !== 'burn';
  }

  function init() {
    $('refresh').onclick = () => load(true);
    $('preflight').onclick = () => { runPreflight(); };
    $('generate').onclick = () => { generate(); };
    $('cancel').onclick = () => { cancelJob(); };
    $('copySource').onclick = () => { copySource(); };
    $('candidateList').addEventListener('click', event => {
      const button = event.target.closest('[data-track]');
      if (button) selectTrack(button.dataset.track);
    });
    document.querySelectorAll('.platform-tabs button').forEach(button => {
      button.onclick = () => {
        collectSettings();
        state.platform = button.dataset.platform;
        updatePlatformTabs();
        renderPlatformForm();
      };
    });
    $('platformEnabled').onchange = () => {
      collectSettings();
      scheduleSave();
    };
    document.addEventListener('input', onTargetChange);
    document.addEventListener('change', onTargetChange);
    $('resultsBody').addEventListener('click', event => {
      const copyButton = event.target.closest('[data-copy-info]');
      if (copyButton) { copyUploadInfo(copyButton.dataset.copyInfo); return; }
      const markButton = event.target.closest('[data-mark]');
      if (markButton) markSubmitted(markButton.dataset.mark);
    });
    window.addEventListener('message', event => {
      if (event.origin !== location.origin) return;
      const data = event.data || {};
      if (data.type === 'lets-karaoke:ping') {
        window.parent.postMessage({type: 'lets-karaoke:ready'}, location.origin);
        return;
      }
      if (data.type === 'lets-karaoke:context' && data.track_id && state.library) {
        const match = state.library.tracks.find(track => track.id === data.track_id || track.track === data.track_id);
        if (match) selectTrack(match.id);
      }
    });
    window.parent.postMessage({type: 'lets-karaoke:ready'}, location.origin);
    load();
  }

  return {init, formatBytes, formatDuration, platformName, summarizePlan};
});
