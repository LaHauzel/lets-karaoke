/* Shared workflow indicator. Workspace navigation lives in the top tabs. */
(() => {
  const steps = [['concert', '切割整场'], ['karaoke', '制作字幕'], ['review', '成品复核'], ['publish', '发布准备']];
  window.navigateWorkspace = (tab, context = {}) => {
    if (!steps.some(row => row[0] === tab)) return;
    if (window.parent === window) {
      const url = new URL('/', location.origin); url.hash = tab;
      if (context.track_id) url.searchParams.set('track', context.track_id);
      if (context.job) url.searchParams.set('job', context.job);
      location.assign(url); return;
    }
    window.parent.postMessage({type: 'lets-karaoke:navigate', tab, context}, location.origin);
  };
  for (const nav of document.querySelectorAll('[data-workflow]')) {
    steps.forEach(([tab, label], index) => {
      const item = document.createElement('span');
      item.className = 'lk-step';
      item.textContent = label;
      if (nav.dataset.workflow === tab) item.setAttribute('aria-current', 'step');
      nav.append(item);
      if (index < steps.length - 1) {
        const arrow = document.createElement('span');
        arrow.className = 'lk-arrow';
        arrow.textContent = '→';
        nav.append(arrow);
      }
    });
    const note = document.createElement('small');
    note.textContent = '用顶部标签切换工作区';
    nav.append(note);
  }
})();
