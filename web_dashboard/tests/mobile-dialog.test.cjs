const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {build}=require('esbuild');
const {parse,compileScript}=require('@vue/compiler-sfc');

// Compile the actual script setup so these tests exercise the same openTasks
// continuation that the button uses, including useCapture's async task read.
global.document={hidden:false,documentElement:{},createElement:()=>({})};
let hash='#capture';
global.location={host:'fixture'};
Object.defineProperty(location,'hash',{get:()=>hash,set:value=>{hash=value.startsWith('#')?value:'#'+value;}});
global.window={addEventListener(){},removeEventListener(){},scrollTo(){}};
global.localStorage={getItem:()=>null};
global.sessionStorage={getItem:()=>null};
global.cancelAnimationFrame=()=>{};
global.history={state:null};
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'mobile-dialog-'));
const outfile=path.join(dir,'component.cjs');
const ready=build({stdin:{contents:`
 import App from './src/mobile/ConsoleApp.vue';
 import {createSSRApp} from 'vue';
 import {renderToString} from 'vue/server-renderer';
 export async function view(){
  let result;
  await renderToString(createSSRApp({setup(props,ctx){
   result=App.setup(props,ctx);return()=>null;
  }}));
  return result;
 }
 `,resolveDir:path.resolve(__dirname,'..')},bundle:true,platform:'node',format:'cjs',outfile,
 plugins:[{name:'actual-vue-setup',setup(builder){builder.onLoad({filter:/\.vue$/},args=>{
  const {descriptor}=parse(fs.readFileSync(args.path,'utf8'),{filename:args.path});
  return {contents:compileScript(descriptor,{id:path.basename(args.path)}).content,loader:'js',resolveDir:path.dirname(args.path)};
 })}}]}).then(()=>{const {view}=require(outfile);fs.rmSync(dir,{recursive:true});return view;});
const response=()=>({ok:true,json:async()=>({tasks:[{task_id:'fixture_a',name:'Task A'}]})});
async function scenario(){
 location.hash='#capture';document.hidden=false;
 const view=await ready;const v=await view();let shown=0;
 v.online.value=true;v.status.value={state:'idle',task_status:{active:true,task:{task_id:'fixture_a'}}};
 v.dialog.value={showModal(){shown++},close(){}};
 return {v,shown:()=>shown};
}
function deferred(){let release;const promise=new Promise(resolve=>release=resolve);return{promise,release};}

test('a task read returning after navigation cannot open the old modal',async()=>{
 const {v,shown}=await scenario(),read=deferred();
 global.fetch=()=>read.promise;
 const open=v.openTasks();
 location.hash='#settings';v.hashChanged();
 read.release(response());await open;
 assert.equal(v.route.value,'settings');assert.equal(shown(),0);
});
test('leaving and returning to capture still cancels the old open request',async()=>{
 const {v,shown}=await scenario(),read=deferred();global.fetch=()=>read.promise;
 const open=v.openTasks();
 location.hash='#settings';v.hashChanged();location.hash='#capture';v.hashChanged();
 read.release(response());await open;
 assert.equal(shown(),0);
});
test('backgrounding the page cancels an outstanding task modal open',async()=>{
 const {v,shown}=await scenario(),read=deferred();global.fetch=()=>read.promise;
 const open=v.openTasks();document.hidden=true;v.visibility();
 read.release(response());await open;assert.equal(shown(),0);document.hidden=false;
});
test('only the newest concurrent open may display the task modal',async()=>{
 const {v,shown}=await scenario(),first=deferred(),second=deferred();let calls=0;
 global.fetch=()=>++calls===1?first.promise:second.promise;
 const a=v.openTasks(),b=v.openTasks();
 second.release(response());await b;assert.equal(shown(),1);
 first.release(response());await a;assert.equal(shown(),1);
 assert.equal(v.openedTaskId.value,'fixture_a');
});
test('a recording started elsewhere while tasks load prevents the modal opening',async()=>{
 const {v,shown}=await scenario(),read=deferred();global.fetch=()=>read.promise;
 const open=v.openTasks();v.status.value={...v.status.value,state:'recording'};
 read.release(response());await open;assert.equal(shown(),0);
});
test('normal task selection still opens with the displayed task identity',async()=>{
 const {v,shown}=await scenario();global.fetch=async()=>response();
 await v.openTasks();assert.equal(shown(),1);assert.equal(v.selectedTask.value,'fixture_a');
});

function navigationFixture(v){
 let backs=0;const storage=new Map();global.sessionStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
 global.history={state:null,replaceState(state){this.state=state;},pushState(state,_,url){this.state=state;location.hash=url;},back(){backs++;}};
 window.scrollX=0;window.scrollY=0;window.scrollTo=(x,y)=>{window.scrollX=x;window.scrollY=y;};
 window.scrollBy=(x,y)=>{window.scrollX+=x;window.scrollY+=y;};
 let focused='';
 const link={dataset:{historyRoute:'history/fixture_a/80'},getBoundingClientRect:()=>({top:200}),focus(){focused='take80';}};
 const heading={focus(){focused='heading';}};
 v.shell.value={querySelector:()=>heading,querySelectorAll:()=>[link]};
 return{backs:()=>backs,focused:()=>focused};
}
async function go(v,value){await v.navigate(value);if(v.route.value!==value)await v.hashChanged();}
test('page Back uses the existing parent history entry',async()=>{
 const{v}=await scenario();global.fetch=async()=>response();const nav=navigationFixture(v);
 await go(v,'history/fixture_a');await go(v,'history/fixture_a/80');v.back();
 assert.equal(nav.backs(),1,'Page Back pushed another list entry instead of traversing history');
});
test('page Back from a direct deep link stays in the app',async()=>{
 const{v}=await scenario();global.fetch=async()=>response();const nav=navigationFixture(v);
 location.hash='#history/fixture_a/80';await v.hashChanged();v.back();
 assert.equal(nav.backs(),0);assert.equal(location.hash,'#history/fixture_a');
});
test('returning to a loaded history list restores its Take control and position',async()=>{
 const{v}=await scenario();global.fetch=async()=>response();const nav=navigationFixture(v);
 await go(v,'history/fixture_a');window.scrollY=12000;
 await go(v,'history/fixture_a/80');location.hash='#history/fixture_a';await v.hashChanged();
 if(v.restoreView)await v.restoreView(true);
 assert.equal(window.scrollY,12000);assert.equal(nav.focused(),'take80');
});
test('leaving before a restored list loads does not replace its saved position with zero',async()=>{
 const{v}=await scenario();global.fetch=async()=>response();const nav=navigationFixture(v);
 await go(v,'history/fixture_a');window.scrollY=12000;await go(v,'history/fixture_a/80');
 location.hash='#history/fixture_a';await v.hashChanged();await go(v,'settings');
 await go(v,'history/fixture_a');await v.restoreView(true);
 assert.equal(window.scrollY,12000);assert.equal(nav.focused(),'take80');
});

test('page hide saves this history entry for reload without losing its parent',async()=>{
 const{v}=await scenario();global.fetch=async()=>response();navigationFixture(v);
 await go(v,'history/fixture_a');window.scrollY=12000;
 v.savePagePosition();assert.equal(history.state.insightCapture.from,'capture');
 assert.equal(v.pagePosition().y,12000);assert.equal(v.pagePosition().route,'history/fixture_a');
 location.hash='#settings';await v.hashChanged();assert.equal(v.pagePosition(),null);
});
