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

test('the 3D view displays vertical motion with identical X/Y and fits all three dimensions',async()=>{
 const{v,socket}=await scenario();socket.onmessage({data:JSON.stringify({type:'pose_update',poses:[{name:'cam',role:'head',visible:true,trace_update:{mode:'snapshot',generation:1,from_seq:1,to_seq:2,drop_before_seq:1,points:[[0,0,0],[0,0,2]]}}]})});
 const line=v.plotted.value[0].line.split(' ').map(p=>p.split(',').map(Number));assert.notDeepEqual(line[0],line[1]);assert.equal(v.bounds.value.span,2);assert.equal(v.axes.value.length,3);assert.ok(line.flat().every(Number.isFinite));
});
test('3D touch orbit, pinch/pan, capture loss and view reset preserve source coordinates',async()=>{
 const{v,socket}=await scenario();send(socket,{mode:'snapshot',from:1,to:3});const raw=JSON.stringify(v.poses.value),before=v.plotted.value[0].line;
 const target={setPointerCapture(){},getBoundingClientRect:()=>({width:320})};const e=(id,x,y)=>({pointerId:id,clientX:x,clientY:y,pointerType:'touch',button:0,currentTarget:target});
 v.pointerDown(e(1,50,50));v.pointerMove(e(1,80,70));assert.notEqual(v.plotted.value[0].line,before);
 v.pointerDown(e(2,150,70));v.pointerMove(e(2,200,70));assert.ok(v.zoom.value>1);assert.notDeepEqual(v.pan.value,[0,0]);
 v.pointerEnd(e(1,80,70));v.pointerEnd(e(2,200,70));const last=v.plotted.value[0].line;v.pointerMove(e(1,300,300));assert.equal(v.plotted.value[0].line,last);
 v.reset();assert.equal(v.plotted.value[0].line,before);assert.equal(JSON.stringify(v.poses.value),raw);
});
test('3D keyboard controls include pan, zoom, and reset without consuming unrelated keys',async()=>{
 const{v}=await scenario();let prevented=0;const e=(key,shiftKey=false)=>({key,shiftKey,preventDefault(){prevented++;}});
 v.keydown(e('ArrowRight'));assert.notEqual(v.yaw.value,-.7);v.keydown(e('ArrowDown',true));assert.equal(v.pan.value[1],12);v.keydown(e('+'));assert.ok(v.zoom.value>1);v.keydown(e('Home'));assert.equal(v.zoom.value,1);assert.deepEqual(v.pan.value,[0,0]);v.keydown(e('Tab'));assert.equal(prevented,4);
});


test('a disconnected trajectory hides old poses until a new connection supplies its snapshot',async()=>{
 const {v,socket}=await scenario();socket.onopen();send(socket,{mode:'snapshot',from:1,to:3});
 try {
  socket.onclose();assert.equal(v.connected.value,false);assert.deepEqual(v.poses.value,[]);assert.equal(v.traces.size,0);
  document.hidden=true;v.visibility();document.hidden=false;v.visibility();const next=sockets.at(-1);next.onopen();
  assert.deepEqual(v.poses.value,[]);send(next,{mode:'snapshot',from:10,to:11});assert.deepEqual(xs(v),[10,11]);
  send(socket,{mode:'snapshot',from:1,to:3});assert.deepEqual(xs(v),[10,11]);
 } finally {document.hidden=true;v.visibility();document.hidden=false;}
});
test('suspending the trajectory removes displayed poses before reconnecting on return',async()=>{
 const {v,socket}=await scenario();socket.onopen();send(socket,{mode:'snapshot',from:1,to:3});
 try {
  document.hidden=true;v.visibility();assert.equal(socket.closed,true);assert.equal(v.connected.value,false);assert.deepEqual(v.poses.value,[]);assert.equal(v.traces.size,0);
  document.hidden=false;v.visibility();const next=sockets.at(-1);next.onopen();assert.deepEqual(v.poses.value,[]);
  send(next,{mode:'snapshot',from:20,to:21});assert.deepEqual(xs(v),[20,21]);
 } finally {document.hidden=true;v.visibility();document.hidden=false;}
});
