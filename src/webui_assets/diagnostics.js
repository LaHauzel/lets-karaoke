/* Shared by the live result page and offline benchmark review. No HTML interpolation of lyrics. */
window.AlignmentDiagnostics={mount(host,data,player){
  host.replaceChildren();host.className='ad';
  const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;};
  const time=s=>Number.isFinite(s)?`${Math.floor(s/60)}:${(s%60).toFixed(1).padStart(4,'0')}`:'—';
  const val=(n,suffix='')=>Number.isFinite(n)?n.toFixed(2)+suffix:'未采集';
  const rows=data.lines||[],missing=data.missing_input_rows||[];
  const outer=el('details');const summary=el('summary');summary.append(el('span','ad-title','逐句对齐 · 依据与复核'),el('span','ad-count',rows.length?`${rows.length} 句 · ${rows.filter(r=>r.reasons?.length).length} 句有提示${data.source==='structural_only'?' · 仅结构检查':''}`:'声学依据不可用'));outer.append(summary);host.append(outer);
  if(data.unavailable)outer.append(el('p','ad-muted',data.unavailable));
  if(data.source==='mixed')summary.append(el('span','ad-count',`${rows.filter(r=>Number.isFinite(r.confidence)).length} 句有声学依据`));
  outer.append(el('p','ad-muted','模型置信与风险提示不等于准确率。多个模型可能一起对错；能量只能辅助判断，未触发提示也不保证正确。'));
  if(!rows.length){outer.append(el('div','ad-empty',data.unavailable||'此任务未保存逐句声学依据。新生成的 Whisper 对齐任务会提供诊断。'));return;}
  const overview=el('div','ad-overview');for(const p of ['重点复核','建议复核','未触发提示'])overview.append(el('span','ad-chip',`${p} ${rows.filter(r=>r.priority===p).length}`));overview.append(el('span','ad-chip',`未输出歌词 ${missing.length} 行`));outer.append(overview);
  if(missing.length)outer.append(el('p','ad-reasons','未输出原行号：'+missing.map(r=>r.row).join('、')+'。是否现场未唱仍需确认。'));
  if(data.historical?.length)outer.append(el('p','ad-muted',`历史检查 ${data.historical.filter(r=>r.pass).length}/${data.historical.length} 通过，仅覆盖指定句界；已通过的句子仍可能触发风险提示。`));
  const seek=(r)=>{if(!player)return;const go=()=>{player.currentTime=Math.max(0,r.start-2);const p=player.play();if(p?.catch)p.catch(()=>{});};if(player.readyState)go();else{player.addEventListener('loadedmetadata',go,{once:true});player.load();}player.scrollIntoView({behavior:'smooth',block:'center'});};
  outer.append(el('div','ad-muted','全曲分布 · 按歌词顺序排列，点击试听（每格一句，非时长比例）'));
  const strip=el('div','ad-strip');rows.forEach(r=>{const b=el('button');b.type='button';b.dataset.level=r.priority;b.title=`第${r.row}句 · ${time(r.start)} · ${r.text}`;b.setAttribute('aria-label',b.title);b.onclick=()=>seek(r);strip.append(b);});outer.append(strip);
  const tools=el('div','ad-tools'),search=el('input'),filter=el('select'),fold=el('button','ad-seek','收起所有句子');search.placeholder='搜索歌词或原行号';search.setAttribute('aria-label','搜索歌词或原行号');filter.setAttribute('aria-label','复核筛选');['全部句子','重点复核','建议复核','未触发提示'].forEach(s=>filter.append(el('option','',s)));tools.append(search,filter,fold);outer.append(tools);const list=el('div','ad-list');outer.append(list);fold.onclick=()=>list.querySelectorAll('details').forEach(d=>d.open=false);
  const render=()=>{list.replaceChildren();const selected=rows.filter(r=>(filter.value==='全部句子'||r.priority===filter.value)&&(`${r.row} ${r.text}`.toLowerCase().includes(search.value.toLowerCase())));if(!selected.length){list.append(el('div','ad-empty','没有匹配的句子'));return;}
    selected.forEach(r=>{const d=el('details','ad-row'),s=el('summary');const score=el('span','ad-muted',Number.isFinite(r.confidence)?`置信 ${r.confidence.toFixed(2)}`:'置信 —');const meter=el('div','ad-meter'),bar=el('i');bar.style.width=`${Number.isFinite(r.confidence)?Math.min(100,Math.max(0,r.confidence*100)):0}%`;meter.append(bar);score.append(meter);s.append(el('span','ad-number',r.row),el('span','ad-lyric',r.text),score,el('span','ad-status',r.priority));d.append(s);
      const body=el('div','ad-body');body.append(el('div','ad-full',r.text));const b=el('button','ad-seek',`▶ ${time(r.start)} — ${time(r.end)} · 提前2秒试听`);b.type='button';b.onclick=()=>seek(r);body.append(b);
      const metrics=el('div','ad-metrics');[[val(r.confidence),'模型置信 · 非准确率'],[val(r.dual_disagreement_s,'s'),'双路边界分歧'],[val(r.postprocess_boundary_shift_s,'s'),'后处理边界变化'],[Number.isFinite(r.outside_vocal_proxy)?(r.outside_vocal_proxy*100).toFixed(1)+'%':'未采集','低人声区高亮 · 代理']].forEach(([v,k])=>{const m=el('div','ad-metric');m.append(el('b','',v),el('span','',k));metrics.append(m);});body.append(metrics);
      body.append(el('p','ad-reasons',r.reasons?.length?r.reasons.join(' · '):'未触发当前规则，仍不能据此判定逐字准确。'));
      if(Number.isFinite(r.nearest_medium_difference_s))body.append(el('p','ad-muted',`与 medium 边界最近差异：${val(r.nearest_medium_difference_s,'s')}（模型间并非独立证据）`));
      if(r.tokens?.length){body.append(el('span','ad-muted','字词时长分布 · 悬停查看时间'));const tokens=el('div','ad-tokenbar');r.tokens.forEach(t=>{const x=el('span','',t.disp||t.text);x.style.flexGrow=Math.max(.01,t.end-t.start);x.style.flexBasis='0';x.title=`${t.disp||t.text} ${time(t.start)}—${time(t.end)}`;tokens.append(x);});body.append(tokens);}
      d.append(body);list.append(d);
    });};search.oninput=render;filter.onchange=render;render();
}};
