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
global.location={hash:'#capture',host:'fixture'};
global.window={addEventListener(){},removeEventListener(){},scrollTo(){}};
global.localStorage={getItem:()=>null};
global.sessionStorage={getItem:()=>null};
global.cancelAnimationFrame=()=>{};
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
