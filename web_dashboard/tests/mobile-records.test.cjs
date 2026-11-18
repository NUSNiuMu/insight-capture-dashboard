const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {build}=require('esbuild');
const {parse,compileScript}=require('@vue/compiler-sfc');
global.document=Object.assign(new EventTarget(),{hidden:false,documentElement:{},createElement:()=>({})});
global.localStorage={getItem:()=>null};
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'mobile-records-')),outfile=path.join(dir,'component.cjs');
const ready=build({stdin:{contents:`
 import App from './src/mobile/RecordsPage.vue';
 import {createSSRApp} from 'vue';
 import {renderToString} from 'vue/server-renderer';
 export async function view(props){let result;await renderToString(createSSRApp({setup(_,ctx){result=App.setup(props,ctx);return()=>null;}}));return result;}
 `,resolveDir:path.resolve(__dirname,'..')},bundle:true,platform:'node',format:'cjs',outfile,
 plugins:[{name:'actual-records-setup',setup(builder){builder.onLoad({filter:/\.vue$/},args=>{const{descriptor}=parse(fs.readFileSync(args.path,'utf8'),{filename:args.path});return{contents:compileScript(descriptor,{id:path.basename(args.path)}).content,loader:'js',resolveDir:path.dirname(args.path)};});}}]
}).then(()=>{const{view}=require(outfile);fs.rmSync(dir,{recursive:true});return view;});

test('an acknowledged merge survives a failed follow-up transfer read',async()=>{
 const commands=[];global.fetch=async url=>{assert.equal(url,'/api/postprocessing/recordings');return{ok:false,status:503,json:async()=>({error:'Temporary read failure'})};};
 const view=await ready,v=await view({route:'history',tasks:[],tasksError:'',status:null,busy:false,online:true,action:async(path,body)=>{commands.push({path,body});return true;}});
 v.transfers.value={enabled:true,recordings:[{session_id:'saved-session',phase:'saved'}]};const cached=v.transfers.value;
 await v.merge('saved-session');
 assert.deepEqual(commands,[{path:'/api/postprocessing/merge',body:{session_id:'saved-session'}}]);
 assert.equal(v.transfers.value,cached,'A failed read must retain the previously loaded list for retry');
});

test('a slow background list read cannot replace a newer post-merge state',async()=>{
 const reads=[Promise.withResolvers(),Promise.withResolvers()],commands=[];let requests=0;
 global.fetch=async url=>{assert.equal(url,'/api/postprocessing/recordings');return reads[requests++].promise;};
 const view=await ready,v=await view({route:'history',tasks:[],tasksError:'',status:null,busy:false,online:true,action:async(path,body)=>{commands.push({path,body});return true;}});
 const timeout=global.setTimeout;global.setTimeout=()=>0;
 try{
  const poll=v.refresh(),merge=v.merge('saved-session');await Promise.resolve();assert.equal(requests,2);
  const response=phase=>({ok:true,json:async()=>({enabled:true,recordings:[{session_id:'saved-session',phase}]})});
  reads[1].resolve(response('copying'));await merge;assert.equal(v.transfers.value.recordings[0].phase,'copying');
  reads[0].resolve(response('saved'));await poll;
  assert.equal(v.transfers.value.recordings[0].phase,'copying','An older poll replaced the accepted merge status');
  assert.equal(commands.length,1);
 }finally{global.setTimeout=timeout;}
});

test('deleting a task preserves scroll position and focuses the adjacent task',async()=>{
 global.window={scrollY:1200};let focused='';
 const next={focus(options){focused='next';if(!options?.preventScroll)window.scrollY=0;}};
 const beforeQuery=document.querySelectorAll;document.querySelectorAll=()=>[next];
 const props={route:'history',tasks:[{task_id:'first'},{task_id:'removed'},{task_id:'next'}],status:null,busy:false,online:true,deleteTask:async()=>{props.tasks.splice(1,1);return true;}};
 try{
  const view=await ready,v=await view(props);
  v.deleteTarget.value={task:{task_id:'removed',name:'Removed'},take:null,origin:'history'};
  v.deleteDialog.value={close(){}};
  v.createButton.value={focus(options){focused='create';if(!options?.preventScroll)window.scrollY=0;}};
  await v.removeSelected();
  assert.equal(window.scrollY,1200,'Deleting the task scrolled the list to the top');
  assert.equal(focused,'next');
 }finally{document.querySelectorAll=beforeQuery;}
});
