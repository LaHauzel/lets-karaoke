/* Local envelope preview. Writes drafts through the same boundary form as manual edits. */
window.LyricWaveform={mount(host,{job,lines,player,onBounds,getBounds,canEdit,onLoop}){
  host.replaceChildren();let disposed=false,data=null,row=0,drag=null,pending=null;
  const root=document.createElement('details');root.className='wave-box';
  const summary=document.createElement('summary');summary.textContent='人声波形 · 拖动句界与循环试听';root.append(summary);
  const tools=document.createElement('div');tools.className='wave-tools';
  const select=document.createElement('select');select.setAttribute('aria-label','波形当前歌词');
  lines.forEach((l,i)=>{const o=document.createElement('option');o.value=i;o.textContent=`${i+1} · ${l.raw}`;select.append(o)});
  const zoom=document.createElement('select');zoom.setAttribute('aria-label','波形窗口长度');
  [12,24,48].forEach(s=>{const o=document.createElement('option');o.value=s;o.textContent=s+' 秒窗口';zoom.append(o)});
  const loop=document.createElement('button');loop.type='button';loop.className='ghost';loop.textContent='循环此句';
  const stop=document.createElement('button');stop.type='button';stop.className='ghost';stop.textContent='停止循环';
  tools.append(select,zoom,loop,stop);root.append(tools);
  const canvas=document.createElement('canvas');canvas.tabIndex=0;canvas.setAttribute('aria-label','歌词波形；拖动青色起点或黄色终点，左右键定位');root.append(canvas);
  const note=document.createElement('p');note.className='hint';note.textContent='展开后加载；波形不等于歌词证据。拖动只修改草稿，重新渲染后字幕才更新。';root.append(note);host.append(root);
  let windowStart=0,windowEnd=12,looping=false;
  const bounds=()=>{const b=pending||getBounds(row);return b&&Number.isFinite(b.start)&&Number.isFinite(b.end)&&b.start>=0&&b.end>b.start&&(!data||b.end<=data.duration)?b:lines[row]};
  function layout(){if(!lines.length)return;const b=bounds();const span=Math.max(Number(zoom.value),b.end-b.start+4);windowStart=Math.max(0,(b.start+b.end-span)/2);windowEnd=Math.min(data?.duration||Infinity,windowStart+span);}
  function draw(){if(disposed||!root.open)return;const width=canvas.clientWidth;if(!width)return;const ratio=window.devicePixelRatio||1;canvas.width=width*ratio;canvas.height=170*ratio;const c=canvas.getContext('2d');c.scale(ratio,ratio);c.fillStyle='#0a1720';c.fillRect(0,0,width,170);
    if(!data||!lines.length)return;const span=windowEnd-windowStart;if(span<=0)return;const x=t=>(t-windowStart)/span*width;
    const b=bounds();c.fillStyle='#193e49';c.fillRect(x(b.start),0,x(b.end)-x(b.start),145);
    c.strokeStyle='#61878d';c.beginPath();for(let px=0;px<width;px++){const t=windowStart+px/width*span;const a=Math.floor(t/data.step),z=Math.max(a+1,Math.ceil((t+span/width)/data.step));let peak=0;for(let k=a;k<z&&k<data.peaks.length;k++)peak=Math.max(peak,data.peaks[k]||0);const h=Math.sqrt(peak)*58;c.moveTo(px,75-h);c.lineTo(px,75+h)}c.stroke();
    for(const [t,color,label] of [[b.start,'#6ce5ef','起点'],[b.end,'#ffce6d','终点']]){const px=x(t);c.fillStyle=color;c.fillRect(px-2,0,4,145);c.font='12px sans-serif';c.fillText(label+' '+t.toFixed(3),Math.max(4,Math.min(width-112,px+6)),16)}
    c.fillStyle='#d8ecf0';c.fillRect(x(player.currentTime),22,1,122);c.font='11px sans-serif';for(let i=0;i<=4;i++){const t=windowStart+span*i/4;c.fillText(t.toFixed(1)+'s',Math.min(width-42,i*width/4),162)}
  }
  async function load(){if(data||disposed)return;note.textContent='正在读取音频波形…';try{const r=await fetch('/api/waveform?job='+encodeURIComponent(job));const value=await r.json();if(value.error)throw Error(value.error);if(disposed)return;data=value;note.textContent='声源：'+data.source+'。拖动句界后点「应用并重新渲染」；循环试听使用草稿起止，画面字幕仍是已渲染版。';layout();draw()}catch(e){if(!disposed)note.textContent='波形不可用：'+e.message+'；仍可在表格中修改起止时间。'}}
  root.ontoggle=()=>{if(root.open){load();layout();draw()}else{looping=false}};
  select.onchange=()=>{looping=false;row=Number(select.value);pending=null;layout();draw()};zoom.onchange=()=>{layout();draw()};
  const time=e=>Math.max(0,Math.min(data.duration,windowStart+(e.clientX-canvas.getBoundingClientRect().left)/canvas.clientWidth*(windowEnd-windowStart)));
  canvas.onpointerdown=e=>{if(!data||!lines.length)return;const t=time(e),b=bounds(),tolerance=(windowEnd-windowStart)*12/canvas.clientWidth;drag=Math.abs(t-b.start)<tolerance?'start':Math.abs(t-b.end)<tolerance?'end':null;if(drag&&canEdit()){pending={start:b.start,end:b.end};canvas.setPointerCapture(e.pointerId)}else{drag=null;player.currentTime=t;draw()}};
  canvas.onpointermove=e=>{if(!drag||!pending||!canEdit())return;const t=time(e);pending[drag]=Number((drag==='start'?Math.min(t,pending.end-.02):Math.max(t,pending.start+.02)).toFixed(3));draw()};
  canvas.onpointerup=()=>{if(drag&&pending&&canEdit())onBounds(row,pending.start,pending.end);drag=null;pending=null;draw()};
  canvas.onpointercancel=()=>{drag=null;pending=null;draw()};
  canvas.onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();player.currentTime=Math.max(0,Math.min(data?.duration||Infinity,player.currentTime+(e.key==='ArrowLeft'?-.1:.1)));draw()}};
  loop.onclick=()=>{if(!lines.length)return;onLoop();looping=true;player.currentTime=bounds().start;player.play().catch(()=>{})};stop.onclick=()=>{looping=false;player.pause()};
  const tick=()=>{if(looping){const b=bounds();if(player.currentTime>=b.end||player.currentTime<b.start-.1)player.currentTime=b.start}draw()};player.addEventListener('timeupdate',tick);
  const observer=new ResizeObserver(()=>{layout();draw()});observer.observe(canvas);
  return {stopLoop(){looping=false},destroy(){disposed=true;looping=false;observer.disconnect();player.removeEventListener('timeupdate',tick)},refresh(){pending=null;layout();draw()}};
}};
