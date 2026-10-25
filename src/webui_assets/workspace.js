const tabs=['karaoke','concert','review','publish'];
const frames=Object.fromEntries(tabs.map(n=>[n,document.getElementById('panel-'+n)]));
const ready={},pending={};
function selectTab(name){
  if(!tabs.includes(name))return;
  for(const n of tabs){
    const on=n===name,b=document.getElementById('tab-'+n),p=frames[n];
    b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;p.hidden=!on;
    if(on&&!p.getAttribute('src'))p.src='/'+n;
  }
  document.getElementById('skip-workspace').href='#panel-'+name;
  history.replaceState(null,'','#'+name);
  frames[name].contentWindow?.postMessage({type:'lets-karaoke:activated'},location.origin);
}
const workspaceSkipLink=document.getElementById('skip-workspace');
workspaceSkipLink.onclick=event=>{event.preventDefault();document.getElementById(workspaceSkipLink.hash.slice(1))?.focus();};
for(const n of tabs){
  const b=document.getElementById('tab-'+n);
  b.onclick=()=>selectTab(n);
  b.onkeydown=e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;
    e.preventDefault();
    const target=e.key==='Home'?tabs[0]:e.key==='End'?tabs.at(-1):tabs[(tabs.indexOf(n)+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];
    selectTab(target);document.getElementById('tab-'+target).focus();
  };
  frames[n].addEventListener('load',()=>{ready[n]=false;frames[n].contentWindow?.postMessage({type:'lets-karaoke:ping'},location.origin);});
}
function deliver(name){
  if(!pending[name])return;
  if(!ready[name]){frames[name].contentWindow?.postMessage({type:'lets-karaoke:ping'},location.origin);return;}
  frames[name].contentWindow.postMessage(pending[name],location.origin);delete pending[name];
}
function cleanContext(value){
  const result={};
  if(!value||typeof value!=='object')return result;
  for(const key of ['track_id','file_id','job','source','concert_id','title'])
    if(typeof value[key]==='string'&&value[key].length<=1500)result[key]=value[key];
  for(const key of ['version','time'])if(Number.isFinite(value[key])&&value[key]>=0)result[key]=value[key];
  return result;
}
window.addEventListener('message',event=>{
  if(event.origin!==location.origin)return;
  const sender=tabs.find(n=>event.source===frames[n].contentWindow);if(!sender)return;
  const data=event.data;
  if(data?.type==='lets-karaoke:ready'){ready[sender]=true;deliver(sender);return;}
  if(data?.type==='lets-karaoke:navigate'&&tabs.includes(data.tab)){
    const context=cleanContext(data.context);
    if(Object.keys(context).length)pending[data.tab]={type:'lets-karaoke:context',...context};
    selectTab(data.tab);deliver(data.tab);return;
  }
  if(sender!=='concert'||data?.type!=='lets-karaoke:import-concert-media')return;
  let url;try{url=new URL(data.url||'',location.origin);}catch{return;}
  if(url.origin!==location.origin||!/^\/concert-files\/[a-zA-Z0-9_-]+\/export-[a-f0-9]+\/[^/]+$/.test(url.pathname))return;
  pending.karaoke={type:'lets-karaoke:import-concert-media',url:url.pathname,name:String(data.name||'').slice(0,180),size:Number(data.size)||0,concert_id:url.pathname.split('/')[2]};
  selectTab('karaoke');deliver('karaoke');
});
const initial=tabs.includes(location.hash.slice(1))?location.hash.slice(1):'karaoke';
const query=new URLSearchParams(location.search),context={};
if(query.has('track'))context.track_id=query.get('track');
if(query.has('job'))context.job=query.get('job');
if(Object.keys(context).length)pending[initial]={type:'lets-karaoke:context',...cleanContext(context)};
selectTab(initial);
