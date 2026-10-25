const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function workspace(){
  const nodes={},listeners={},posted={};
  for(const name of ['karaoke','concert','review','publish']){
    posted[name]=[];
    nodes['panel-'+name]={hidden:name!=='karaoke',attributes:{src:name==='karaoke'?'/karaoke':''},
      contentWindow:{postMessage:data=>posted[name].push(data)},
      getAttribute(key){return this.attributes[key];},addEventListener(){}};
    nodes['tab-'+name]={setAttribute(key,value){this[key]=value;},focus(){}};
  }
  nodes['skip-workspace']={};
  const history={replaceState(_a,_b,value){this.value=value;}};
  vm.runInNewContext(fs.readFileSync('src/webui_assets/workspace.js','utf8'),{
    document:{getElementById:id=>nodes[id]},window:{addEventListener:(name,fn)=>listeners[name]=fn},
    location:{origin:'http://127.0.0.1:7870',hash:'#karaoke',search:''},history,URL,URLSearchParams,
  });
  const send=(sender,data,origin='http://127.0.0.1:7870')=>listeners.message({origin,source:nodes['panel-'+sender]?.contentWindow||{},data});
  return {nodes,posted,history,send};
}
{
  const w=workspace();
  w.send('review',{type:'lets-karaoke:navigate',tab:'karaoke',context:{job:'local-abc',version:2}});
  assert.equal(w.posted.karaoke.at(-1).type,'lets-karaoke:ping');
  w.send('karaoke',{type:'lets-karaoke:ready'});
  assert.deepEqual(JSON.parse(JSON.stringify(w.posted.karaoke.at(-1))),{type:'lets-karaoke:context',job:'local-abc',version:2});
  const count=w.posted.karaoke.length;w.send('karaoke',{type:'lets-karaoke:ready'});assert.equal(w.posted.karaoke.length,count);
}
{
  const w=workspace();
  w.send('review',{type:'lets-karaoke:navigate',tab:'concert'},'https://other.example');
  w.send('unknown',{type:'lets-karaoke:navigate',tab:'concert'});
  assert.equal(w.history.value,'#karaoke');
  w.send('review',{type:'lets-karaoke:navigate',tab:'review',context:{track_id:'native/song',extra:'discard',time:NaN}});
  assert.equal(w.history.value,'#review');assert.equal(w.nodes['panel-review'].hidden,false);
  w.send('review',{type:'lets-karaoke:ready'});
  assert.deepEqual(JSON.parse(JSON.stringify(w.posted.review.at(-1))),{type:'lets-karaoke:context',track_id:'native/song'});
}
{
  const w=workspace();
  w.send('review',{type:'lets-karaoke:navigate',tab:'publish',context:{track_id:'native/song'}});
  assert.equal(w.history.value,'#publish');assert.equal(w.nodes['panel-publish'].hidden,false);
  assert.equal(w.nodes['panel-review'].hidden,true);
  w.send('publish',{type:'lets-karaoke:ready'});
  assert.deepEqual(JSON.parse(JSON.stringify(w.posted.publish.at(-1))),{type:'lets-karaoke:context',track_id:'native/song'});
}
{
  const w=workspace();w.send('karaoke',{type:'lets-karaoke:ready'});
  for(const url of ['https://other.example/concert-files/a/export-b/x.mp4','/files/private.mp4','http://['])
    w.send('concert',{type:'lets-karaoke:import-concert-media',url});
  assert.equal(w.posted.karaoke.filter(x=>x.type==='lets-karaoke:import-concert-media').length,0);
  w.send('concert',{type:'lets-karaoke:import-concert-media',url:'/concert-files/a/export-b/song.mp4',name:'song.mp4'});
  assert.equal(w.posted.karaoke.at(-1).concert_id,'a');
}
console.log('Workspace navigation: lazy-frame delivery, source/origin checks and concert imports passed.');
