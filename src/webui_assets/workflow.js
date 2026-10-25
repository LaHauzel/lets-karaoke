/* Shared navigation. Context transfers are routed by the same-origin workspace. */
(() => {
  const names = [['concert','01','切割整场'],['karaoke','02','制作字幕'],['review','03','成品复核'],['publish','04','发布准备']];
  window.navigateWorkspace = (tab, context={}) => {
    if (!names.some(row=>row[0]===tab)) return;
    if (window.parent === window) {
      const url = new URL('/',location.origin);url.hash=tab;
      if(context.track_id)url.searchParams.set('track',context.track_id);
      if(context.job)url.searchParams.set('job',context.job);
      location.assign(url);return;
    }
    window.parent.postMessage({type:'lets-karaoke:navigate',tab,context},location.origin);
  };
  for (const nav of document.querySelectorAll('[data-workflow]')) {
    for (const [tab,number,label] of names) {
      const button=document.createElement('button');button.type='button';
      button.innerHTML=`<span>${number}</span>${label}`;
      if(nav.dataset.workflow===tab){button.setAttribute('aria-current','step');button.disabled=true;}
      else button.onclick=()=>window.navigateWorkspace(tab);
      nav.append(button);
    }
    const note=document.createElement('small');note.textContent='切换标签会保留当前工作区';nav.append(note);
  }
})();
