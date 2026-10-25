'use strict';
const reviewNames={unchecked:'未检查',passed:'通过',needs_changes:'需要修改'};
const reviewEditor={dirty:false,saving:false,base:null,issues:[],conflict:null};
function preciseTime(seconds){
  if(seconds===null||seconds===undefined)return '';
  const ms=Math.round(seconds*1000),minutes=Math.floor(ms/60000);
  return `${String(minutes).padStart(2,'0')}:${String(Math.floor(ms/1000)%60).padStart(2,'0')}.${String(ms%1000).padStart(3,'0')}`;
}
function parseReviewTime(value){
  const raw=value.trim();if(!raw)return null;
  if(/^\d+(?:\.\d{1,3})?$/.test(raw))return Number(raw);
  if(!/^\d+:\d{2}(?:\.\d{1,3})?$/.test(raw)&&!/^\d+:\d{2}:\d{2}(?:\.\d{1,3})?$/.test(raw))throw new Error('时间请填写秒数、分:秒或时:分:秒，最多三位小数。');
  const parts=raw.split(':').map(Number);
  if(parts.slice(1).some(n=>n>=60))throw new Error('时间中分钟／秒的对应部分须小于 60。');
  return parts.reduce((total,n)=>total*60+n,0);
}
function preferredHumanFile(track){
  const r=track.human_review;
  return r?.status==='passed'&&!r.stale&&!r.new_version&&r.file_id?r.file_id:track.default_file;
}
function matchesHumanReview(track,filter){
  const r=track.human_review;if(!filter)return true;
  if(filter==='stale')return r.stale||r.new_version;
  if(filter==='ready')return r.status==='passed'&&!r.stale&&!r.new_version&&track.files.some(f=>f.id===r.file_id&&f.exists&&isMP4(f));
  return r.status===filter;
}
function humanReviewSummary(track){
  const r=track.human_review,unresolved=r.issues.filter(i=>!i.resolved).length;
  const approved=track.files.find(f=>f.id===r.file_id);
  return `<div class="human-summary"><button class="human-badge ${esc(r.status)}" data-review-track="${esc(track.id)}" title="编辑人工复核记录">人工：${esc(reviewNames[r.status])}</button>${unresolved?`<span class="risk-count small">${unresolved} 条未解决</span>`:''}${r.new_version?'<span class="pill old">新修订版 · 待复核</span>':''}${r.stale?'<span class="pill old">视频已变化 · 待复核</span>':''}</div>${r.status==='passed'&&approved?`<div class="review-excerpt">已确认：${esc(fileLabel(approved))}</div>`:''}${r.notes?`<div class="review-excerpt" title="${esc(r.notes)}">${esc(r.notes.slice(0,90))}${r.notes.length>90?'…':''}</div>`:''}`;
}
function renderReviewCounts(){
  const count={unchecked:0,passed:0,needs_changes:0};let stale=0;
  library.tracks.forEach(t=>{count[t.human_review.status]++;if(t.human_review.stale)stale++;});
  $('reviewCounts').textContent=`人工复核：未检查 ${count.unchecked} · 通过 ${count.passed} · 需要修改 ${count.needs_changes}${stale?' · 视频变化待复核 '+stale:''}`;
  $('recommendedCount').textContent=library.tracks.filter(isCandidate).length;
  $('reviewStorage').textContent=`本地复核记录：${library.human_reviews.path}（保存时保留上一版 .bak 备份）`;
}
function openHumanReview(track){
  const r=track.human_review;reviewEditor.base=structuredClone(r);reviewEditor.dirty=false;reviewEditor.conflict=null;
  reviewEditor.issues=r.issues.map(i=>({...i,rowText:i.row===null?'':String(i.row),timeText:preciseTime(i.time)}));
  $('humanStatus').value=r.status;$('reviewNotes').value=r.notes;
  $('reviewFile').innerHTML='<option value="">未指定版本</option>'+track.files.map(f=>`<option value="${esc(f.id)}" ${!f.exists?'disabled':''}>${esc(fileLabel(f))}${!f.exists?' · 文件缺失':''}</option>`).join('');
  $('reviewFile').value=r.file_id||selectedFile?.id||'';
  if(!$('reviewFile').value)$('reviewFile').value='';
  $('reviewError').hidden=true;$('reviewConflict').hidden=true;$('reviewCloseGuard').hidden=true;
  $('reviewSavedAt').textContent=r.updated_at?`上次保存 ${new Date(r.updated_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}`:'尚未保存过';
  const previous=track.review_history||[];
  $('reviewHistorySection').hidden=!previous.length;
  $('reviewHistory').innerHTML=previous.slice().reverse().map(h=>`<p><strong>${esc(reviewNames[h.status])}</strong> · ${esc(h.updated_at)}<br><span class="path">${esc(h.file_path||'未指定视频')}</span><br>${esc(h.notes||'无备注')} · ${h.issues.length} 条问题</p>`).join('');
  renderReviewIssues();updateReviewSaveState();
}
function updateReviewSaveState(){
  $('saveReview').disabled=reviewEditor.saving;
  $('saveReviewClose').disabled=reviewEditor.saving;
  $('reviewFields').disabled=reviewEditor.saving;
  $('reviewSaveStatus').textContent=reviewEditor.saving?'正在写入本地文件…':reviewEditor.dirty?'有未保存的修改':reviewEditor.base?.revision?'已保存到本地':'未检查 · 尚无人工记录';
  $('reviewSaveStatus').classList.toggle('unsaved',reviewEditor.dirty);
}
function markReviewDirty(){reviewEditor.dirty=true;$('reviewError').hidden=true;updateReviewSaveState();}
function evidenceForFile(track,file){
  if(!file)return [];
  if(Array.isArray(file.evidence))return file.evidence;
  if(file.revision){
    const revision=revisionForFile(track,file);
    return revision?.evidence||[];
  }
  return track.evidence||[];
}
function issueLyricContext(track,issue,source){
  if(!source)return {message:'未绑定可用的视频版本，无法确定对应歌词。'};
  const rows=evidenceForFile(track,source),raw=String(issue.rowText??'').trim();
  let row;
  if(raw){
    row=rows.find(r=>r.row===Number(raw));
    if(!row)return {message:'此版本没有对应行号，请核对行号。'};
  }else{
    let stamp;try{stamp=parseReviewTime(issue.timeText||'');}catch{return {message:'请填写有效时间点以查看对应歌词。'};}
    if(stamp===null)return {message:'填写行号或时间点后，这里会显示对应歌词。'};
    row=rows.find(r=>r.start<=stamp&&stamp<r.end);
    if(!row)return {message:'此时间点没有对应歌词，可能位于前奏、间奏或歌词间隔。'};
  }
  return {row,message:row.text?'':'此版本缺少这一行的歌词文本。'};
}
function issueLyricsHTML(issue,source){
  const {row,message}=issueLyricContext(selectedTrack,issue,source);
  return `${row?`<span class="score-sub">${issue.rowText?'对应歌词':'时间点对应歌词'} · 第 ${row.row} 行 · ${preciseTime(row.start)}–${preciseTime(row.end)}</span>`:''}${row?.text?`<span class="lyric-text" lang="und">${esc(row.text)}</span>`:`<span class="muted small">${esc(message)}</span>`}`;
}
function renderReviewIssues(){
  $('issueCount').textContent=`（${reviewEditor.issues.length} 条）`;
  $('reviewIssues').innerHTML=reviewEditor.issues.map((issue,index)=>{
    const source=fullFiles(selectedTrack).find(f=>f.id===issue.file_id);
    return `<div class="issue-card ${issue.resolved?'resolved':''}" data-issue-index="${index}">
      <div class="issue-fields"><label>歌词行号<input data-field="rowText" type="number" min="1" max="${Math.max(1,...evidenceForFile(selectedTrack,source).map(r=>r.row))}" step="1" value="${esc(issue.rowText)}" placeholder="可留空" aria-label="问题 ${index+1} 的歌词行号"></label>
      <label>歌曲内时间点<input data-field="timeText" inputmode="decimal" value="${esc(issue.timeText)}" placeholder="02:20.500" aria-label="问题 ${index+1} 的时间点"></label>
      <label class="inline"><input data-field="resolved" type="checkbox" ${issue.resolved?'checked':''} aria-label="问题 ${index+1} 已解决">已解决</label>
      <button type="button" data-remove-issue="${index}" aria-label="移除问题 ${index+1}">移除</button></div>
      <div class="issue-lyrics" aria-live="polite">${issueLyricsHTML(issue,source)}</div>
      <label>问题说明<textarea data-field="note" rows="2" maxlength="2000" aria-label="问题 ${index+1} 的说明" placeholder="例如：这句提前约 1 秒亮起，末尾拖得太长。">${esc(issue.note)}</textarea></label>
      <div class="issue-footer"><span class="muted small">${source?'记录版本：'+esc(fileLabel(source)):'未绑定视频版本'}</span><button type="button" data-jump-issue="${index}" class="text-button">跳到此问题</button></div></div>`;
  }).join('')||'<p class="muted small">尚未记录问题。也可以在下方对齐证据表中点击“记录此行问题”。</p>';
}
function syncReviewIssue(event){
  const field=event.target.dataset.field,card=event.target.closest('[data-issue-index]');
  if(!field||!card)return;
  const issue=reviewEditor.issues[Number(card.dataset.issueIndex)];
  issue[field]=field==='resolved'?event.target.checked:event.target.value;
  if(field==='resolved')card.classList.toggle('resolved',issue.resolved);
  if(field==='rowText'||field==='timeText'){
    const source=fullFiles(selectedTrack).find(f=>f.id===issue.file_id);
    card.querySelector('.issue-lyrics').innerHTML=issueLyricsHTML(issue,source);
  }
  markReviewDirty();
}
function addReviewIssue(row=null,stamp=null){
  if(reviewEditor.saving)return;
  if(reviewEditor.issues.length>=100)return toast('每首歌最多记录 100 条问题。',true);
  reviewEditor.issues.push({rowText:row===null?'':String(row),timeText:preciseTime(stamp),note:'',resolved:false,file_id:selectedFile?.id||null});
  renderReviewIssues();markReviewDirty();
  const cards=$('reviewIssues').querySelectorAll('.issue-card');
  cards[cards.length-1].scrollIntoView({behavior:'smooth',block:'center'});
  cards[cards.length-1].querySelector('textarea').focus({preventScroll:true});
}
function captureReviewTime(){
  const player=$('player');
  if(!isMP4(selectedFile)||player.readyState<1)return toast('请先加载 MP4；MKV 可以手动填写时间点。',true);
  const stamp=player.currentTime+(selectedFile.kind==='experiment'?selectedTrack.experiment.start:0);
  player.pause();addReviewIssue(null,stamp);
}
function reviewPayload(){
  return {track_id:selectedTrack.id,revision:reviewEditor.base.revision,status:$('humanStatus').value,
    notes:$('reviewNotes').value,file_id:$('reviewFile').value||null,
    file_signature:selectedTrack.files.find(f=>f.id===$('reviewFile').value)?.signature||null,
    issues:reviewEditor.issues.map((i,index)=>{
      if(i.rowText.trim()&&!/^\d+$/.test(i.rowText.trim()))throw new Error(`问题 ${index+1} 的行号须为整数。`);
      return {row:i.rowText.trim()?Number(i.rowText):null,time:parseReviewTime(i.timeText),note:i.note,resolved:i.resolved,file_id:i.file_id};
    })};
}
async function saveHumanReview(){
  if(reviewEditor.saving)return false;
  $('reviewError').hidden=true;
  let payload;
  try{payload=reviewPayload();}catch(error){$('reviewError').textContent=error.message;$('reviewError').hidden=false;return false;}
  reviewEditor.saving=true;updateReviewSaveState();
  try{
    const send=()=>fetch('/api/review/reviews',{method:'POST',headers:{'Content-Type':'application/json','X-Library-Token':library.reveal_token},body:JSON.stringify(payload)});
    let response=await send();
    // A service restart invalidates the session token, but must not discard the draft.
    if(response.status===403){
      const fresh=await fetch('/api/review/library',{cache:'no-store'});
      if(fresh.ok){library.reveal_token=(await fresh.json()).reveal_token;response=await send();}
    }
    const result=await response.json();
    if(response.status===409){reviewEditor.conflict=result.current;$('reviewConflict').hidden=false;throw new Error(result.error);}
    if(!response.ok)throw new Error(result.error||'保存失败，修改仍保留在页面中。');
    const savedFile=selectedTrack.files.find(f=>f.id===result.review.file_id);
    if(reviewEditor.base.revision)selectedTrack.review_history=[...(selectedTrack.review_history||[]),structuredClone(reviewEditor.base)];
    selectedTrack.human_review={...result.review,stale:false,new_version:!!(selectedTrack.revision&&savedFile?.revision!==selectedTrack.revision.revision)};
    openHumanReview(selectedTrack);renderReviewCounts();renderTracks();
    if(typeof updatePublishAction==='function')updatePublishAction();
    $('acceptanceStatus').textContent=`人工复核：${reviewNames[result.review.status]}。自动验收：${selectedTrack.acceptance.label}。`;
    toast('人工复核记录已保存到本地。');return true;
  }catch(error){
    $('reviewError').textContent=error.message;$('reviewError').hidden=false;
    $('reviewError').scrollIntoView({behavior:'smooth',block:'nearest'});return false;
  }finally{reviewEditor.saving=false;updateReviewSaveState();}
}
function requestCloseTrack(){
  if(reviewEditor.saving)return toast('正在保存，请稍候。');
  if(reviewEditor.dirty){$('reviewCloseGuard').hidden=false;$('reviewCloseGuard').scrollIntoView({behavior:'smooth',block:'center'});return;}
  $('trackDialog').close();
}
function downloadReviewDraft(){
  const draft={track_id:selectedTrack.id,base_revision:reviewEditor.base.revision,status:$('humanStatus').value,
    notes:$('reviewNotes').value,file_id:$('reviewFile').value,issues:reviewEditor.issues};
  const url=URL.createObjectURL(new Blob([JSON.stringify(draft,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download=selectedTrack.track+'-review-draft.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
}
function initHumanReview(){
  for(const id of ['humanStatus','reviewFile','reviewNotes'])$(id).addEventListener('input',markReviewDirty);
  $('reviewIssues').addEventListener('input',syncReviewIssue);
  $('addIssue').addEventListener('click',()=>addReviewIssue());$('captureIssue').addEventListener('click',captureReviewTime);
  $('usePlayingVersion').addEventListener('click',()=>{
    if(selectedFile.kind==='experiment')return toast('实验片段不能作为整首通过的依据，请选择完整歌曲版本。',true);
    $('reviewFile').value=selectedFile.id;markReviewDirty();
  });
  $('saveReview').addEventListener('click',saveHumanReview);
  $('saveReviewClose').addEventListener('click',async()=>{if(await saveHumanReview())$('trackDialog').close();});
  $('continueReview').addEventListener('click',()=>$('reviewCloseGuard').hidden=true);
  $('discardReview').addEventListener('click',()=>{reviewEditor.dirty=false;$('trackDialog').close();});
  $('downloadReviewDraft').addEventListener('click',downloadReviewDraft);
  $('loadLatestReview').addEventListener('click',()=>{
    if(!reviewEditor.conflict)return;
    const current=reviewEditor.conflict,file=selectedTrack.files.find(f=>f.id===current.file_id);
    selectedTrack.human_review={...current,stale:!!(current.file_id&&(!file?.exists||file.signature!==current.file_signature)),
      new_version:!!(selectedTrack.revision&&file?.revision!==selectedTrack.revision.revision)};
    openHumanReview(selectedTrack);renderReviewCounts();renderTracks();
  });
  $('trackDialog').addEventListener('cancel',event=>{event.preventDefault();requestCloseTrack();});
  window.addEventListener('beforeunload',event=>{if(reviewEditor.dirty||reviewEditor.saving){event.preventDefault();event.returnValue='';}});
  $('trackDialog').addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();saveHumanReview();}});
  document.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.dataset.reviewTrack){openTrack(button.dataset.reviewTrack);$('humanReviewSection').scrollIntoView({behavior:'smooth',block:'start'});}
    if(button.dataset.removeIssue!==undefined&&!reviewEditor.saving){reviewEditor.issues.splice(Number(button.dataset.removeIssue),1);renderReviewIssues();markReviewDirty();}
    if(button.dataset.jumpIssue!==undefined){
      const issue=reviewEditor.issues[Number(button.dataset.jumpIssue)];
      try{
        const source=fullFiles(selectedTrack).find(f=>f.id===issue.file_id);
        const evidence=evidenceForFile(selectedTrack,source);
        const stamp=parseReviewTime(issue.timeText)??evidence.find(r=>r.row===Number(issue.rowText))?.start;
        if(stamp===undefined||stamp===null)return toast('请填写时间点或有效行号。',true);
        if(!isMP4(selectedFile))return toast('当前为 MKV，请在本机播放器中跳到 '+preciseTime(stamp)+'。');
        // Stored issue timestamps belong to their source version. Experiments
        // are already stored on the original full-song timeline.
        const origin=source?.kind==='experiment'?0:(source?.timeline_offset||0);
        const target=selectedFile.kind==='experiment'?0:(selectedFile.timeline_offset||0);
        seekTo(stamp+origin-target);
      }catch(error){toast(error.message,true);}
    }
  });
}
