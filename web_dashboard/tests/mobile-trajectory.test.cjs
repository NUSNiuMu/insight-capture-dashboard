const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {build}=require('esbuild'),{parse,compileScript}=require('@vue/compiler-sfc');
global.document={hidden:false,documentElement:{},createElement:()=>({})};
global.location={protocol:'http:',host:'fixture'};global.localStorage={getItem:()=>null};
const sockets=[];global.WebSocket=class{constructor(){sockets.push(this);}close(){this.closed=true;}};
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'mobile-trace-')),outfile=path.join(dir,'component.cjs');
const ready=build({stdin:{contents:`
 import App from './src/mobile/TrajectoryPanel.vue';
 import {createSSRApp} from 'vue';import {renderToString} from 'vue/server-renderer';
 export async function view(){let result;await renderToString(createSSRApp({setup(p,c){result=App.setup(p,c);return()=>null;}}));return result;}
 `,resolveDir:path.resolve(__dirname,'..')},bundle:true,platform:'node',format:'cjs',outfile,
 plugins:[{name:'actual-vue-setup',setup(builder){builder.onLoad({filter:/\.vue$/},args=>{const {descriptor}=parse(fs.readFileSync(args.path,'utf8'),{filename:args.path});return{contents:compileScript(descriptor,{id:path.basename(args.path)}).content,loader:'js',resolveDir:path.dirname(args.path)}})}}]}).then(()=>{const {view}=require(outfile);fs.rmSync(dir,{recursive:true});return view;});
async function scenario(){document.hidden=false;const v=await (await ready)();v.connect();return{v,socket:sockets.at(-1)};}
const point=n=>[n,n%2,0];
function send(socket,{mode='delta',generation=1,from,to,drop=1}){const points=Array.from({length:Math.max(0,to-from+1)},(_,i)=>point(from+i));socket.onmessage({data:JSON.stringify({type:'pose_update',poses:[{name:'cam',role:'head',visible:true,trace_update:{mode,generation,from_seq:from,to_seq:to,drop_before_seq:drop,points}}]})});}
const xs=v=>v.poses.value[0]?.points.map(p=>p[0]);
test('a late viewer does not reappend shared deltas already present in its snapshot',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:6});send(socket,{from:4,to:6});assert.deepEqual(xs(v),[1,2,3,4,5,6]);
});
test('overlapping deltas append only genuinely newer points',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:6});send(socket,{from:4,to:8});assert.deepEqual(xs(v),[1,2,3,4,5,6,7,8]);
});
test('empty heartbeat updates remove points outside the server retention window',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:6});send(socket,{from:7,to:6,drop:4});assert.deepEqual(xs(v),[4,5,6]);
});
test('a sequence gap requests a fresh snapshot instead of drawing a false bridge',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:3});send(socket,{from:6,to:8});assert.equal(socket.closed,true);assert.deepEqual(v.poses.value,[]);
});
test('a new-generation delta cannot connect to the previous map',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:3});send(socket,{generation:2,from:4,to:5});assert.equal(socket.closed,true);assert.deepEqual(v.poses.value,[]);
});
test('a reset snapshot replaces old points, including an empty map reset',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:6});send(socket,{mode:'snapshot',generation:2,from:7,to:6});assert.deepEqual(xs(v),[]);send(socket,{generation:2,from:7,to:8,drop:7});assert.deepEqual(xs(v),[7,8]);
});
test('the existing 1500-point display bound remains correct after overlapping updates',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:1600});send(socket,{from:1599,to:1602});assert.equal(xs(v).length,1500);assert.equal(xs(v)[0],103);assert.equal(xs(v).at(-1),1602);
});
