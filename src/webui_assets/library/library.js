'use strict';
const $ = id => document.getElementById(id);
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const gradeNames = {unknown:'未评分',high:'相对较高', medium:'中等', low:'较低', fix:'需修复'};
const gradeRank = {unknown:4,high:0, medium:1, low:2, fix:3};
const kindNames = {generated:'项目生成 · 硬字幕 MP4',updated:'反馈修订 · 硬字幕 MP4',reviewed:'修订双语 · 硬字幕 MP4', hard:'原批次 · 硬字幕 MP4', soft:'ASS 字幕轨 MKV', experiment:'模型实验片段'};
let library, gradeFilter = '', selectedTrack, selectedFile, toastTimer;
const time = n => {const s=Math.max(0,Math.floor(n||0));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;};
const size = n => n>=1073741824?`${(n/1073741824).toFixed(2)} GiB`:`${(n/1048576).toFixed(0)} MiB`;
const badge = grade => `<span class="badge ${esc(grade)}">${esc(gradeNames[grade])}</span>`;
const fullFiles = track => [...track.files, ...track.supporting, ...(track.experiment?.files || [])];
const availableFiles = track => track.files.filter(f=>f.exists);
const isCandidate = track => track.grade==='high' && track.human_review.status!=='needs_changes' && !track.human_review.stale && !track.human_review.new_version && track.files.some(f=>f.kind==='reviewed' && f.variant===3 && f.exists);
const isMP4 = file => file?.name.toLowerCase().endsWith('.mp4');
const distinctFiles = files => [...new Map(files.map(f=>[f.path,f])).values()];
function trackSelection(file) {
  const m=file.multitrack;
  return m?`音轨 ${m.audio+1}（${m.audio===0?'原唱':'伴奏'}）＋字幕轨 ${m.subtitle+1}（${m.subtitle===0?'原文':'双语'}）`:'';
}
function approvalScope(track,file) {
  const r=track.human_review;
  if(r.status==='passed'&&r.file_id!==file.id)return '（针对另一个版本，当前选择未确认）';
  return r.new_version?'（有新修订版，待复核）':r.stale?'（视频已变化，待重新复核）':'';
}
function fileLabel(file) {
  if(file.native_version!==undefined)return `项目版本 ${file.native_version} · ${file.name}`;
  if(file.kind==='experiment') return `${file.label} · 实验片段`;
  const v=library.variants[file.variant];
  return v?`${file.retained_for_review?'审批关联原片 · ':file.previous_release?'历史版 · ':file.revision?'新修订 · ':''}V${file.variant} ${v} · ${file.multitrack?'多轨 MKV · '+trackSelection(file):kindNames[file.kind]}${file.superseded?'（已有修订版）':file.older_translation?'（修订前译文）':''}`:file.name;
}
function toast(message, failure=false) {
  clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('failure',failure);$('toast').hidden=false;
  toastTimer=setTimeout(()=>$('toast').hidden=true,5000);
}
function setGrade(grade) {
  gradeFilter=grade;
  document.querySelectorAll('[data-grade]').forEach(button=>{
    const active=button.dataset.grade===grade;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));
  });
}
function resetFilters() {
  $('search').value='';$('project').value='';$('availability').value='';$('reviewFilter').value='';$('sort').value='confidence';setGrade('');
}
function renderTracks() {
  if(!library)return;
  const needle=$('search').value.trim().toLocaleLowerCase();
  const project=$('project').value, availability=$('availability').value;
  const tracks=library.tracks.filter(t=>{
    if(!matchesHumanReview(t,$('reviewFilter').value))return false;
    if(needle && !`${t.track} ${t.title} ${t.concert}`.toLocaleLowerCase().includes(needle))return false;
    if(project && project!==t.project)return false;
    if(gradeFilter && (gradeFilter==='low'?!['low','fix'].includes(t.grade):t.grade!==gradeFilter))return false;
    const files=availableFiles(t);
    if(availability==='recommended'&&!isCandidate(t))return false;
    if(availability==='hard'&&!files.some(f=>isMP4(f)))return false;
    if(availability==='reviewed'&&!files.some(f=>f.kind==='reviewed'))return false;
    if(availability==='soft'&&!files.some(f=>f.kind==='soft'))return false;
    if(availability==='experiment'&&!t.experiment)return false;
    return true;
  });
  const sort=$('sort').value, projects=Object.keys(library.projects);
  tracks.sort((a,b)=>{
    if(sort==='confidence')return gradeRank[a.grade]-gradeRank[b.grade] || b.score-a.score;
    if(sort==='risk')return gradeRank[b.grade]-gradeRank[a.grade] || a.score-b.score;
    if(sort==='title')return a.title.localeCompare(b.title,'ja');
    return projects.indexOf(a.project)-projects.indexOf(b.project) || a.track.localeCompare(b.track,'ja');
  });
  $('resultCount').textContent=`显示 ${tracks.length} / ${library.tracks.length} 首`;
  $('empty').hidden=tracks.length>0;
  $('trackRows').innerHTML=tracks.map(t=>{
    const files=distinctFiles(availableFiles(t)), hard=files.filter(isMP4).length, soft=files.filter(f=>f.kind==='soft').length;
    return `<tr><td><button class="song-button" data-track="${esc(t.id)}"><span class="song-number">${esc(t.number)}</span>${esc(t.title)}</button><div class="song-sub">${esc(t.concert)}</div>${humanReviewSummary(t)}</td>
    <td>${badge(t.grade)}<span class="score-sub">中位分 ${t.score===null?'—':Number(t.score).toFixed(3)}</span></td>
    <td class="number">${t.high_rows.length} <span class="muted">/ ${t.total}</span><div class="meter" aria-hidden="true"><i style="width:${Math.round(t.high_rows.length/Math.max(1,t.total)*100)}%"></i></div></td>
    <td><span class="${t.low_rows.length?'risk-count':'muted'}">${t.low_rows.length} 行</span></td>
    <td><div class="file-pills">${t.reviewed_rows?'<span class="pill revised">中文已修订</span>':''}${hard?`<span class="pill">MP4 × ${hard}</span>`:''}${soft?`<span class="pill">MKV × ${soft}</span>`:''}${t.experiment?'<span class="pill">有实验</span>':''}</div></td>
    <td class="number muted">${time(t.duration)}</td><td><div class="row-actions"><button class="${hard?'primary':''}" data-track="${esc(t.id)}">${hard?'预览 / 版本':'查看版本'}</button><button data-reveal="${esc(preferredHumanFile(t))}" title="打开文件夹并选中已通过版本；未复核时选中推荐版本">定位文件</button></div></td></tr>`;
  }).join('');
}
async function load() {
  if(reviewEditor.dirty||reviewEditor.saving){toast('请先保存当前人工复核修改，再刷新清单。',true);return;}
  $('refresh').disabled=true;$('loadError').hidden=true;
  try {
    const response=await fetch('/api/review/library',{cache:'no-store'});
    if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.error||`服务返回 ${response.status}，请确认本地服务正在运行。`);}
    const next=await response.json();
    $('revisionReportLink').hidden=!next.revision_report;if(next.revision_report)$('revisionReportLink').href=next.revision_report;
    if(!Array.isArray(next.tracks))throw new Error('清单格式不正确。');
    library=next;
    renderReviewCounts();
    const project=$('project').value;
    $('project').innerHTML='<option value="">全部演唱会</option>'+Object.entries(library.projects).map(([id,label])=>`<option value="${esc(id)}">${esc(label)}</option>`).join('');
    $('project').value=project;
    const summary=library.summary;
    $('totalCount').textContent=summary.tracks;$('recommendedCount').textContent=library.tracks.filter(isCandidate).length;
    $('hardCount').textContent=summary.hard;$('softCount').textContent=summary.soft;
    const feedbackCount=library.tracks.flatMap(t=>t.files).filter(f=>f.exists&&f.kind==='updated'&&!f.previous_release).length;
    $('hardNote').textContent=feedbackCount?`当前修订 ${feedbackCount} 个`:'可直接预览';
    $('chipAll').textContent=summary.tracks;$('chipHigh').textContent=summary.grades.high||0;
    $('chipMedium').textContent=summary.grades.medium||0;$('chipLow').textContent=(summary.grades.low||0)+(summary.grades.fix||0);
    $('updated').textContent='清单更新 '+new Date(library.updated).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
    $('inventoryNote').textContent=`${summary.files} 个完整歌曲视频 · ${size(summary.bytes)} · 另有 ${summary.experiments} 个模型实验片段`;
    $('dataWarning').hidden=!library.diagnostics.length;
    $('dataWarning').textContent=library.diagnostics.length?`${library.diagnostics.length} 个文件已移动或缺失，请在详情中查看。`:'';
    renderTracks();
    if($('trackDialog').open && selectedTrack) openTrack(selectedTrack.id,selectedFile?.id);
    else if(location.hash) {
      let id;try{id=decodeURIComponent(location.hash.slice(1));}catch{return;}
      if(library.tracks.some(t=>t.id===id))openTrack(id);
    }
  } catch(error) {
    $('loadError').textContent=`无法读取成品库：${error.message} 请从项目工作台的“成品与人工复核”标签打开，而不是双击 HTML 文件。`;
    $('loadError').hidden=false;$('resultCount').textContent=library?'保留上次清单':'清单加载失败';
  } finally {$('refresh').disabled=false;}
}
function renderEvidence() {
  if(!selectedTrack)return;
  const t=selectedTrack;
  const release=revisionForFile(t,selectedFile);
  const acceptance=release?(release.acceptance||{issues:[]}):(t.baseline_acceptance||t.acceptance);
  let rows=evidenceForFile(t,selectedFile);
  const lowRows=release?rows.filter(r=>(r.score??0)<.6||(r.disagreement??0)>2).map(r=>r.row):(t.baseline_quality||t).low_rows;
  const riskRows=new Set([...lowRows,...acceptance.issues.flatMap(i=>i.rows||[])]);
  if($('onlyRisk').checked)rows=rows.filter(r=>riskRows.has(r.row)||r.disagreement===null||r.disagreement>1||r.priority==='重点复核');
  const seekable=isMP4(selectedFile);
  $('evidenceRows').innerHTML=rows.map(r=>`<tr><td class="evidence-lyric"><span class="lyric-text" lang="und">${r.text?esc(r.text):'<span class="muted">此版本暂无歌词文本</span>'}</span><span class="score-sub">第 ${r.row} 行</span></td><td><button class="seek" data-seek="${r.start}" ${!seekable?'disabled':''} aria-label="跳到第 ${r.row} 行 ${time(r.start)}">${time(r.start)}–${time(r.end)}</button></td><td class="number">${r.score===null?'缺失':r.score.toFixed(3)}</td><td class="number ${r.disagreement>1?'risk-count':''}">${r.disagreement===null?'缺失':r.disagreement.toFixed(2)+' 秒'}</td><td>${esc(r.priority)}${r.reasons?`<span class="score-sub">${esc(r.reasons)}</span>`:''}<button class="text-button add-row-issue" data-issue-row="${r.row}">记录此行问题</button></td></tr>`).join('')||'<tr><td colspan="5" class="muted">此筛选下没有记录；可取消“只看待复核行”查看全部。</td></tr>';
}
function renderFileList(files) {
  return files.map(f=>`<div class="file-row"><div><strong>${esc(fileLabel(f))}</strong>${f.superseded?'<span class="pill old">旧译文</span>':''}<small>${esc(f.name)} · ${f.exists?size(f.bytes):'文件缺失'}${f.multitrack?'（四种组合共用此文件）':''}${f.duration?' · '+time(f.duration):''}</small></div><div class="actions">${f.variant||f.kind==='experiment'||f.native_version!==undefined?`<button data-select="${esc(f.id)}" ${!f.exists?'disabled':''}>${isMP4(f)?'预览':'选择'}</button>`:`<a class="button" href="${esc(f.url)}" target="_blank" rel="noopener">查看文件 ↗</a>`}<button data-reveal="${esc(f.id)}" ${!f.exists?'disabled':''}>定位文件</button><button data-copy="${esc(f.id)}">复制路径</button></div></div>`).join('');
}
function openTrack(id, fileId) {
  const t=library.tracks.find(t=>t.id===id);if(!t)return;
  selectedTrack=t;
  $('dialogTitle').textContent=t.title;$('dialogConcert').textContent=`${t.concert} / ${t.number}`;
  $('detailGrade').innerHTML=badge(t.grade);$('detailScore').textContent=t.score===null?'—':Number(t.score).toFixed(3);
  $('detailHigh').textContent=`${t.high_rows.length} / ${t.total}`;$('detailLow').textContent=`${t.low_rows.length} 行`;
  $('translationStatus').textContent=t.native?'项目生成的字幕；未自动添加或标记中文翻译。':t.reviewed_rows?`已修订 ${t.reviewed_rows} 行中文。当前新修订版与 reviewed MP4 使用该译文；历史 MKV 仍保留旧译文。`:'中文为本地机器翻译稿，尚未完成本轮逐行文字复核。英文行保留原文。';
  $('acceptanceStatus').textContent=`人工复核：${reviewNames[t.human_review.status]}${t.human_review.new_version?'（有新修订版，待复核）':t.human_review.stale?'（视频已变化，待重新复核）':''}。自动验收：${t.acceptance.label}。`;
  $('auditStatus').hidden=!t.audit?.notes?.length;$('auditStatus').textContent=t.audit?.notes?.join('；')||'';
  $('revisionStatus').textContent=t.revision?`本轮处理：${t.revision.summary}。旧审批保留；新版本请重新检查。`:'';
  $('repairStatus').hidden=!t.repair;
  if(t.repair){
    const r=t.repair;
    $('repairStatus').innerHTML=`<strong>${r.previous_revision?'此前自动修复记录':'自动修复候选'}</strong><p>该次实验修改 ${r.changed} 行；剩余阻断 ${r.blocking} 行、超长或断档 ${r.duration} 行。${r.render_gate?'该次时间结构检查通过，仍需听看。':'该次未通过整曲导出检查。'} 人工审批保持不变。</p><a href="${esc(r.asset.url)}#${esc(r.anchor)}" target="_blank" rel="noopener">查看歌词与修复前后时间 ↗</a>`;
  }
  $('structureStatus').textContent=`时间结构检查：${t.health.ok===true?'未发现已知阻断问题':t.health.ok===false?'有问题待修复':'尚无检查记录'}。伴奏版本仍需试听分离效果。`;
  $('riskDetails').innerHTML=`<p>较低置信行：${esc(t.low_rows.join('、')||'无')}</p><p>旧版译文自动风险行：${esc(t.translation_flags.join('、')||'未标记')}（未标记不代表无误译）</p><ul>${t.acceptance.issues.map(i=>`<li>${esc(i.message)}${i.rows?.length?'；第 '+esc(i.rows.join('、'))+' 行':''}</li>`).join('')}</ul>`;
  const groups=['generated','updated','reviewed','hard','soft','experiment'];
  const media=[...t.files,...(t.experiment?.files||[])];
  $('version').innerHTML=groups.map(kind=>{
    const files=media.filter(f=>f.kind===kind);
    return files.length?`<optgroup label="${esc(kindNames[kind])}">${files.map(f=>`<option value="${esc(f.id)}" ${!f.exists?'disabled':''}>${esc(fileLabel(f))}${!f.exists?' · 文件缺失':''}</option>`).join('')}</optgroup>`:'';
  }).join('');
  $('filesSummary').textContent=`所有完整歌曲视频 · ${distinctFiles(t.files.filter(f=>f.exists)).length} 个文件 / ${t.files.filter(f=>f.exists).length} 种版本与组合`;
  $('allFiles').innerHTML=renderFileList(t.files);$('supportFiles').innerHTML=renderFileList(t.supporting);
  $('translationNotes').innerHTML=Object.keys(t.translation_notes).length?'<h4>译文修订说明</h4><ul>'+Object.entries(t.translation_notes).map(([row,note])=>`<li>第 ${esc(row)} 行：${esc(note)}</li>`).join('')+'</ul>':'';
  $('experimentSection').hidden=!t.experiment;
  if(t.experiment) {
    $('experimentNote').textContent=`${t.experiment.purpose}。原曲 ${time(t.experiment.start)} 起，时长 ${time(t.experiment.duration)}。这些是对照试片，不是完整歌曲，也未替换成品时间轴。`;
    $('experimentFiles').innerHTML=renderFileList(t.experiment.files);
  } else $('experimentFiles').textContent='';
  const preferred=media.find(f=>f.id===fileId&&f.exists)||media.find(f=>f.id===preferredHumanFile(t))||media[0];
  selectFile(preferred.id, false);
  openHumanReview(t);
  if(!$('trackDialog').open)$('trackDialog').showModal();
  history.replaceState(null,'','#'+encodeURIComponent(t.id));
}
function selectFile(id, preservePosition=true) {
  const file=fullFiles(selectedTrack).find(f=>f.id===id);if(!file)return;
  const player=$('player');
  const songTime=preservePosition&&isMP4(selectedFile)?player.currentTime+(selectedFile.kind==='experiment'?selectedTrack.experiment.start:(selectedFile.timeline_offset||0)):0;
  selectedFile=file;$('version').value=id;
  const quality=file.revision?selectedTrack:(selectedTrack.baseline_quality||selectedTrack);
  const acceptance=file.revision?selectedTrack.acceptance:(selectedTrack.baseline_acceptance||selectedTrack.acceptance);
  const health=file.revision?selectedTrack.health:(selectedTrack.baseline_health||selectedTrack.health);
  $('acceptanceStatus').textContent=`人工复核：${reviewNames[selectedTrack.human_review.status]}${approvalScope(selectedTrack,file)}。当前版本自动验收：${acceptance.label}。`;
  $('structureStatus').textContent=`当前版本时间结构：${acceptance.status==='needs_fix'||health.ok===false?'有问题待修复':health.ok===true?'未发现已知阻断问题':'尚无检查记录'}。伴奏版本仍需试听分离效果。`;
  const rowText=row=>evidenceForFile(selectedTrack,file).find(e=>e.row===row)?.text;
  const rowLabel=row=>`${rowText(row)||'暂无歌词'}（第 ${row} 行）`;
  $('riskDetails').innerHTML=`<p>较低置信行：${quality.low_rows.map(row=>esc(rowLabel(row))).join('；')||'无'}</p><ul>${acceptance.issues.map(i=>`<li>${esc(i.message)}${i.rows?.length?'；'+i.rows.map(row=>esc(rowLabel(row))).join('；'):''}</li>`).join('')}</ul>`;
  $('detailGrade').innerHTML=badge(quality.grade);$('detailScore').textContent=quality.score===null?'—':Number(quality.score).toFixed(3);
  $('detailHigh').textContent=`${quality.high_rows.length} / ${quality.total}`;$('detailLow').textContent=`${quality.low_rows.length} 行`;
  player.onloadedmetadata=null;player.pause();player.removeAttribute('src');player.load();
  const playable=isMP4(file)&&file.exists;
  player.hidden=!playable;$('playerPlaceholder').hidden=playable;$('playbackError').hidden=true;
  if(playable){
    player.onloadedmetadata=()=>{
      const target=songTime-(file.kind==='experiment'?selectedTrack.experiment.start:(file.timeline_offset||0));
      if(target>0&&target<player.duration)player.currentTime=target;
      player.onloadedmetadata=null;
    };
    player.src=file.url;
  }
  $('openVideo').hidden=!playable;$('openVideo').href=playable?file.url:'#';
  $('trackSelectionHint').hidden=!file.multitrack;
  $('trackSelectionHint').textContent=file.multitrack?`已选择 ${trackSelection(file)}。四种组合共用一个文件；请用支持 ASS 的本机播放器打开，并在播放器中选择上述音轨和字幕轨。文件默认是原唱＋原文；网页选择不会自动改变外部播放器的选轨。审批只对应所选组合。`:'';
  $('revealSelected').disabled=!file.exists;$('selectedPath').textContent=file.path;
  $('fileStatus').textContent=[file.kind==='experiment'?'实验片段 · 置信度表仍显示正式成品的证据':file.kind==='reviewed'?'最新中文修订版 · 完整歌曲':file.kind==='soft'?'完整歌曲 · 内封 ASS 字幕轨':'完整歌曲 · 已烧录字幕',
    file.superseded||file.older_translation?'此版本使用旧译文，双语投稿优先选择 reviewed MP4':'',size(file.bytes),file.duration?time(file.duration):''].filter(Boolean).join(' / ');
  renderEvidence();
  $('captureIssue').disabled=!playable;
  const workflow=file.workflow||selectedTrack.workflow||{};
  $('editSelected').disabled=!workflow.editable||file.kind==='experiment';
  $('sourceSelected').disabled=!workflow.source&&!workflow.concert_id;
  $('workflowHint').textContent=selectedTrack.native?'继续编辑会打开此项目版本；复核记录保留在成品库。':workflow.editable?'修改原文字幕会建立独立工作区，沿用此版本的时间轴与原唱素材；现有成品和审批记录保留。':'此版本没有可继续编辑的对齐数据。';
}
async function reveal(id, button) {
  if(!library)return;
  const previous=button?.disabled;if(button)button.disabled=true;
  try {
    const response=await fetch('/api/review/reveal',{method:'POST',headers:{'Content-Type':'application/json','X-Library-Token':library.reveal_token},body:JSON.stringify({id})});
    const result=await response.json();if(!response.ok)throw new Error(result.error||'打开失败');
    toast('已请求资源管理器打开文件夹并选中文件。');
  } catch(error) {toast(error.message+'；也可以复制完整路径。',true);}
  finally {if(button)button.disabled=previous;}
}
async function copyPath(id) {
  const file=library.tracks.flatMap(fullFiles).find(f=>f.id===id);if(!file)return;
  try {await navigator.clipboard.writeText(file.path);toast('已复制完整 Windows 文件路径。');}
  catch {toast('剪贴板不可用，请从视频下方选中并复制完整路径。',true);}
}
function seekTo(songTime) {
  if(!isMP4(selectedFile))return;
  const offset=selectedFile.kind==='experiment'?selectedTrack.experiment.start:(selectedFile.timeline_offset||0);
  const target=songTime-(selectedFile.kind==='experiment'?offset:0);
  if(target<0||(selectedFile.kind==='experiment'&&target>=selectedTrack.experiment.duration))return toast('此行不在当前实验片段内，请选择完整歌曲视频。',true);
  const player=$('player');
  const seek=()=>{player.currentTime=Math.max(0,target-.5);player.scrollIntoView({behavior:'smooth',block:'center'});};
  if(player.readyState>=1)seek();else player.addEventListener('loadedmetadata',seek,{once:true});
}
document.addEventListener('click',event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.track)openTrack(button.dataset.track);
  if(button.dataset.reveal)reveal(button.dataset.reveal,button);
  if(button.dataset.copy)copyPath(button.dataset.copy);
  if(button.dataset.select){selectFile(button.dataset.select);$('version').scrollIntoView({behavior:'smooth',block:'center'});}
  if(button.dataset.seek!==undefined)seekTo(Number(button.dataset.seek));
  if(button.dataset.issueRow){const row=evidenceForFile(selectedTrack,selectedFile).find(r=>r.row===Number(button.dataset.issueRow));if(row)addReviewIssue(row.row,row.start);}
  if(button.dataset.grade!==undefined){setGrade(button.dataset.grade);renderTracks();}
  if(button.dataset.preset){resetFilters();$('availability').value=button.dataset.preset==='all'?'':button.dataset.preset;renderTracks();}
});
for(const id of ['project','availability','sort','reviewFilter'])$(id).addEventListener('change',renderTracks);
$('search').addEventListener('input',renderTracks);
$('clear').addEventListener('click',()=>{resetFilters();renderTracks();});
$('refresh').addEventListener('click',load);
$('version').addEventListener('change',()=>selectFile($('version').value));
$('onlyRisk').addEventListener('change',renderEvidence);
$('revealSelected').addEventListener('click',()=>reveal(selectedFile.id,$('revealSelected')));
$('copySelected').addEventListener('click',()=>copyPath(selectedFile.id));
$('closeDialog').addEventListener('click',requestCloseTrack);
$('trackDialog').addEventListener('close',()=>{$('player').pause();$('player').removeAttribute('src');$('player').load();history.replaceState(null,'',location.pathname+location.search);});
$('player').addEventListener('error',()=>{if(!$('player').getAttribute('src'))return;$('playbackError').textContent='浏览器未能播放该视频。可以尝试独立预览，或在资源管理器中定位后用本机播放器打开。';$('playbackError').hidden=false;});
initHumanReview();
let libraryLoading=load();
function reviewNavigate(tab,context){$('player').pause();window.navigateWorkspace(tab,context);}
$('editSelected').onclick=async()=>{
  if(reviewEditor.dirty||reviewEditor.saving)return toast('请先保存人工复核记录，再进入修改。',true);
  const button=$('editSelected');button.disabled=true;
  try{
    const response=await fetch('/api/review/edit',{method:'POST',headers:{'Content-Type':'application/json','X-Library-Token':library.reveal_token},body:JSON.stringify({track_id:selectedTrack.id,file_id:selectedFile.id,file_signature:selectedFile.signature})});
    const result=await response.json();if(!response.ok)throw Error(result.error||'无法建立编辑工作区');
    reviewNavigate('karaoke',{job:result.job,version:result.version,track_id:selectedTrack.id,title:selectedTrack.title});
  }catch(error){toast(error.message,true);}finally{button.disabled=false;}
};
$('sourceSelected').onclick=()=>{
  const context=selectedFile.workflow||selectedTrack.workflow||{};
  const elapsed=isMP4(selectedFile)?($('player').currentTime||0):0;
  const excerptStart=selectedFile.kind==='experiment'?(selectedTrack.experiment?.start||0):0;
  reviewNavigate('concert',{source:context.source,concert_id:context.concert_id,time:(context.source_start||0)+excerptStart+elapsed,title:selectedTrack.title});
};
window.addEventListener('message',async event=>{
  if(event.origin!==location.origin||event.source!==window.parent)return;
  if(event.data?.type==='lets-karaoke:ping'){window.parent.postMessage({type:'lets-karaoke:ready'},location.origin);return;}
  if(event.data?.type==='lets-karaoke:activated'){
    if(!reviewEditor.dirty&&!reviewEditor.saving&&!$('trackDialog').open){await libraryLoading;libraryLoading=load();}
    return;
  }
  if(event.data?.type!=='lets-karaoke:context')return;
  if(reviewEditor.dirty||reviewEditor.saving)return toast('当前复核草稿已保留；请保存后再打开其他歌曲。',true);
  await libraryLoading;libraryLoading=load();await libraryLoading;
  const data=event.data;
  const track=library?.tracks.find(t=>t.id===data.track_id||data.job&&t.workflow?.job===data.job);
  if(!track)return toast('这条制作记录暂未出现在成品库，请确认已生成完整视频后刷新。',true);
  resetFilters();$('search').value=track.title;renderTracks();
  const file=track.files.find(f=>f.id===data.file_id)||track.files.find(f=>f.native_version===data.version);
  openTrack(track.id,file?.id);
});
window.parent.postMessage({type:'lets-karaoke:ready'},location.origin);
