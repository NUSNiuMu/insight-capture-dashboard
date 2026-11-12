const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { webcrypto } = require('node:crypto');
const { buildSync } = require('esbuild');

// Exercise the real composable without a browser, camera, or mounted polling loop.
global.document = { documentElement: {}, hidden: false, createElement:()=>({}) };
global.crypto = webcrypto;
Object.defineProperty(global, 'localStorage', {value:{getItem:()=>null}, configurable:true});
const sessionValues=new Map();
global.sessionStorage={getItem:key=>sessionValues.get(key)||null,setItem:(key,value)=>sessionValues.set(key,value),removeItem:key=>sessionValues.delete(key)};
const temp = mkdtempSync(path.join(tmpdir(), 'capture-test-'));
const outfile = path.join(temp, 'harness.cjs');
buildSync({ stdin: { contents: `
  import { createSSRApp, createRenderer, nextTick } from 'vue';
  import { renderToString } from 'vue/server-renderer';
  import { useCapture, takeDuration, takeDisplayState } from './src/mobile/useCapture.js';
  export { takeDuration, takeDisplayState };
  export async function capture() {
    let result;
    await renderToString(createSSRApp({setup(){result=useCapture();return ()=>null;}}));
    return result;
  }
  export { nextTick };
  export function liveCapture() {
    let result;
    const renderer=createRenderer({createElement:()=>({}),createComment:()=>({}),createText:()=>({}),insert(){},remove(){},setText(){},setElementText(){},parentNode:()=>null,nextSibling:()=>null});
    const app=renderer.createApp({setup(){result=useCapture();return ()=>null;}});
    app.mount({});
    return {value:result,unmount:()=>app.unmount()};
  }`, resolveDir: path.resolve(__dirname, '..') },
  bundle: true, platform: 'node', format: 'cjs', outfile });
const { capture, liveCapture, nextTick, takeDuration, takeDisplayState } = require(outfile);
rmSync(temp, { recursive: true });
const snapshot = (state='idle', task='qa', output='/take/1') => ({
  state, output_path: output, task_status: { task: { task_id: task } },
});
const response = (value, status=200) => ({ ok: status < 400, status, json: async()=>value });

test('elapsed time uses the recorder start and freezes during saving',()=>{
  const take={task_id:'qa',take_id:1,bag_path:'/take/1',start_epoch_s:100};
  const timing={source:'recorder',started_at_epoch_s:110,stopped_at_epoch_s:null,duration_s:null};
  const status={state:'recording',current_take:take,output_path:take.bag_path,server_time:135.8,recording_timing:timing};
  assert.equal(takeDuration(take,status),'00:25');
  status.state='saving';status.recording_timing={...timing,stopped_at_epoch_s:135.8,duration_s:25.8};
  status.server_time=500;
  assert.equal(takeDuration(take,status),'00:25');
});

test('saved history uses MCAP duration even when task lifecycle lasted longer',()=>{
  const take={task_id:'qa',take_id:1,bag_path:'/take/1',start_epoch_s:100,end_epoch_s:150,
    recording_timing:{source:'mcap_metadata',started_at_epoch_s:110,duration_s:25.541367571}};
  assert.equal(takeDuration(take,{state:'saved',current_take:take,output_path:take.bag_path,recording_timing:{duration_s:25.2}}),'00:25');
});

test('a stale unfinished take cannot borrow another recording clock',()=>{
  const take={task_id:'qa',take_id:1,bag_path:'/take/1',state:'recording',start_epoch_s:100,
    recording_timing:{source:'recorder',started_at_epoch_s:110}};
  const status={state:'recording',current_take:{task_id:'other',take_id:1},output_path:'/take/2',server_time:500};
  assert.equal(takeDuration(take,status),'--:--');
});

test('failed or missing timing stays unknown, while a zero-length saved span is valid',()=>{
  assert.equal(takeDuration({state:'start_failed',start_epoch_s:100,end_epoch_s:120},{}),'--:--');
  assert.equal(takeDuration({recording_timing:{duration_s:NaN}},{}),'--:--');
  assert.equal(takeDuration({recording_timing:{duration_s:0}},{}),'00:00');
});

test('current history follows saving and completion without relabelling another take',()=>{
  const take={task_id:'qa',take_id:1,bag_path:'/take/1',state:'recording'};
  const status={state:'saving',current_take:take,output_path:take.bag_path};
  assert.equal(takeDisplayState(take,status),'finalizing');
  assert.equal(takeDisplayState({...take,task_id:'other'},status),'recording');
  status.state='saved';status.current_take={...take,state:'complete'};
  assert.equal(takeDisplayState(take,status),'complete');
  status.current_take.state='saved';
  assert.equal(takeDisplayState(take,status),'saved');
  assert.equal(take.state,'recording');
});

test('saved timing is already correct when the history request still has the live take',()=>{
  const take={task_id:'qa',take_id:1,bag_path:'/take/1',state:'recording',
    recording_timing:{source:'recorder',started_at_epoch_s:110,duration_s:null}};
  const status={state:'saved',output_path:take.bag_path,current_take:{...take,state:'complete',
    recording_timing:{source:'mcap_metadata',started_at_epoch_s:110,duration_s:25.541367571}}};
  assert.equal(takeDisplayState(take,status),'complete');
  assert.equal(takeDuration(take,status),'00:25');
});
async function harness(initial=snapshot(),keepSession=false) {
  if(!keepSession)sessionValues.clear();
  const value=await capture();
  value.online.value=true;value.status.value=initial;
  return value;
}

test('an acknowledged task change remains successful when task-list refresh disconnects', async()=>{
  const c=await harness();
  global.fetch=async(url, options)=>{
    if(options.method==='POST')return response({});
    if(url==='/api/mobile/status')return response(snapshot('idle','other'));
    throw new TypeError('Failed to fetch');
  };
  assert.equal(await c.action('/api/tasks/other/activate'),true);
  assert.equal(c.status.value.task_status.task.task_id,'other');
  assert.equal(c.error.value,'');
  assert.equal(c.pending.value,false);
});

test('two clients starting the same task converge on the active recording', async()=>{
  const c=await harness();
  global.fetch=async(url, options)=>options.method==='POST'
    ?response({message:'An operation is still running.'},409)
    :response(url==='/api/tasks'?{tasks:[]}:snapshot('starting'));
  assert.equal(await c.record('start'),true);
  assert.equal(c.status.value.state,'starting');
  assert.equal(c.error.value,'');
  assert.equal(c.recordPending.value,null);
});

test('a conflicting start on another task remains an error', async()=>{
  const c=await harness();
  global.fetch=async(url, options)=>options.method==='POST'
    ?response({message:'Conflict'},409):response(snapshot('recording','other'));
  assert.equal(await c.record('start'),false);
  assert.equal(c.error.value,'Conflict');
});

test('two clients stopping the same take converge on the save result', async()=>{
  const c=await harness(snapshot('recording'));
  global.fetch=async(url, options)=>options.method==='POST'
    ?response({message:'No capture recording is running.'},409)
    :response(url==='/api/tasks'?{tasks:[]}:snapshot('saved'));
  assert.equal(await c.record('stop'),true);
  assert.equal(c.error.value,'');
});

test('a stale stop cannot be reconciled against a later take', async()=>{
  const c=await harness(snapshot('recording'));
  global.fetch=async(url, options)=>options.method==='POST'
    ?response({message:'The recording has changed.'},409)
    :response(snapshot('saving','qa','/take/2'));
  assert.equal(await c.record('stop'),false);
  assert.equal(c.error.value,'The recording has changed.');
});

test('an actual rejected mutation is still reported', async()=>{
  const c=await harness();
  global.fetch=async(url, options)=>options.method==='POST'
    ?response({message:'Disk is full'},400):response(snapshot());
  assert.equal(await c.record('start'),false);
  assert.equal(c.error.value,'Disk is full');
});

test('late task-list responses cannot replace newer task counts', async()=>{
  const c=await harness();let release;
  global.fetch=()=>new Promise(resolve=>{release=resolve;});
  const old=c.loadTasks(),resolveOld=release;
  const newer=c.loadTasks(),resolveNew=release;
  resolveNew(response({tasks:[{task_id:'qa',stats:{recorded_takes:2}}]}));await newer;
  resolveOld(response({tasks:[{task_id:'qa',stats:{recorded_takes:1}}]}));await old;
  assert.equal(c.tasks.value[0].stats.recorded_takes,2);
});

test('a restored task read clears its own warning without clearing command errors', async()=>{
  const c=await harness();c.error.value='A real command failure';
  global.fetch=async()=>{throw new TypeError('Failed to fetch');};
  await assert.rejects(c.loadTasks());
  assert.notEqual(c.tasksError.value,'');
  global.fetch=async()=>response({tasks:[]});
  await c.loadTasks();
  assert.equal(c.tasksError.value,'');
  assert.equal(c.error.value,'A real command failure');
});

test('a lost recording ACK is confirmed by its exact operation ID', async()=>{
  const c=await harness();let command;
  global.fetch=async(url,options)=>{
    if(options.method==='POST'){command=JSON.parse(options.body);throw new TypeError('Failed to fetch');}
    return response(url==='/api/tasks'?{tasks:[]}:{...snapshot('starting'),operation:{command_id:command.command_id,action:'start',state:'running'}});
  };
  assert.equal(await c.record('start'),true);
  assert.equal(c.error.value,'');
  assert.equal(sessionValues.has('insight-capture.record-command'),false);
});

test('lost recording ACK must not be attributed to a different operation', async()=>{
  const c=await harness();
  global.fetch=async(url,options)=>{
    if(options.method==='POST')throw new TypeError('Failed to fetch');
    return response({...snapshot('starting'),operation:{command_id:'another-command',action:'start',state:'running'}});
  };
  assert.equal(await c.record('start'),false);
  assert.match(c.error.value,/待确认/);
  assert.equal(sessionValues.has('insight-capture.record-command'),true);
});

test('a retried unconfirmed recording uses the same command after page reload', async()=>{
  const c=await harness();let first;
  global.fetch=async(url,options)=>{if(options.method==='POST')first=JSON.parse(options.body);throw new TypeError('offline');};
  assert.equal(await c.record('start'),false);
  const restored=await harness(snapshot(),true);let retried;
  global.fetch=async(url,options)=>{
    if(options.method==='POST'){retried=JSON.parse(options.body);return response({});}
    return response(url==='/api/tasks'?{tasks:[]}:snapshot('starting'));
  };
  assert.equal(await restored.record('start'),true);
  assert.equal(retried.command_id,first.command_id);
});

test('lost creation ACK is recovered without creating a second task', async()=>{
  const c=await harness();let task,posts=0;
  global.fetch=async(url,options)=>{
    if(url==='/api/tasks'&&options.method==='POST'){
      const payload=JSON.parse(options.body);task={...payload,task_id:payload.id};posts++;throw new TypeError('lost ACK');
    }
    if(url==='/api/tasks')return response({tasks:[task]});
    if(options.method==='POST')return response({});
    return response(snapshot('idle',task.task_id));
  };
  assert.equal(await c.createAndActivateTask('QA','instruction'),true);
  assert.equal(posts,1);assert.equal(c.taskDraft.value,null);assert.equal(c.error.value,'');
});

test('unknown creation outcome survives reload and retries the same task ID', async()=>{
  const c=await harness();let created,ids=[];
  global.fetch=async(url,options)=>{
    if(options.method==='POST'){created=JSON.parse(options.body);ids.push(created.id);}
    throw new TypeError('offline');
  };
  assert.equal(await c.createAndActivateTask('QA','instruction'),false);
  const restored=await harness(snapshot(),true);
  global.fetch=async(url,options)=>{
    if(url==='/api/tasks'&&options.method==='POST'){ids.push(JSON.parse(options.body).id);return response({message:'Task already exists'},400);}
    if(url==='/api/tasks')return response({tasks:[{...created,task_id:created.id}]});
    if(options.method==='POST')return response({});
    return response(snapshot('idle',created.id));
  };
  assert.equal(await restored.createAndActivateTask('QA','instruction'),true);
  assert.equal(ids.length,2);assert.equal(new Set(ids).size,1);assert.equal(restored.taskDraft.value,null);
});

test('activation interrupted after creation resumes without creating again', async()=>{
  const c=await harness();let created,createPosts=0,activatePosts=0;
  global.fetch=async(url,options)=>{
    if(url==='/api/tasks'&&options.method==='POST'){createPosts++;created=JSON.parse(options.body);return response({});}
    throw new TypeError('offline');
  };
  assert.equal(await c.createAndActivateTask('QA','instruction'),false);
  assert.equal(c.taskDraft.value.created,true);
  const restored=await harness(snapshot(),true);
  global.fetch=async(url,options)=>{
    if(options.method==='POST'){assert.equal(url,`/api/tasks/${created.id}/activate`);activatePosts++;return response({});}
    return response(url==='/api/tasks'?{tasks:[]}:{...snapshot('idle',created.id),task_status:{active:true,task:{task_id:created.id}}});
  };
  assert.equal(await restored.createAndActivateTask('QA','instruction'),true);
  assert.equal(createPosts,1);assert.equal(activatePosts,1);
});

test('an activation ACK lost after success is confirmed from current task', async()=>{
  const c=await harness();let created;
  global.fetch=async(url,options)=>{
    if(url==='/api/tasks'&&options.method==='POST'){created=JSON.parse(options.body);return response({});}
    if(options.method==='POST')throw new TypeError('lost ACK');
    return response(url==='/api/tasks'?{tasks:[]}:{...snapshot('idle',created.id),task_status:{active:true,task:{task_id:created.id}}});
  };
  assert.equal(await c.createAndActivateTask('QA','instruction'),true);
  assert.equal(c.taskDraft.value,null);assert.equal(c.error.value,'');
});

test('a definite creation rejection unlocks the form and preserves the reason', async()=>{
  const c=await harness();
  global.fetch=async(url,options)=>options.method==='POST'?response({message:'Invalid task'},400):response({tasks:[]});
  assert.equal(await c.createAndActivateTask('QA','instruction'),false);
  assert.equal(c.taskDraft.value,null);assert.equal(c.error.value,'Invalid task');
});

test('poll acknowledgement arriving before a lost POST response must win', async()=>{
  sessionValues.clear();let command,rejectPost,offline=false;
  global.fetch=async(url,options)=>{
    if(options.method==='POST'){
      command=JSON.parse(options.body);
      return new Promise((resolve,reject)=>{rejectPost=reject;});
    }
    if(offline)throw new TypeError('offline');
    return response(url==='/api/tasks'?{tasks:[]}:snapshot('recording'));
  };
  const live=liveCapture(),c=live.value;
  try{
    await new Promise(resolve=>setImmediate(resolve));
    const stopping=c.record('stop');
    c.status.value={...snapshot('saving'),operation:{command_id:command.command_id,action:'stop',state:'running'}};
    await nextTick();
    offline=true;rejectPost(new TypeError('lost stop ACK'));
    await stopping;await nextTick();
    assert.equal(c.error.value,'');
    assert.equal(sessionValues.has('insight-capture.record-command'),false);
  }finally{live.unmount();}
});

test('leaving unfinished setup clears only the browser draft and sends no delete', async()=>{
  const c=await harness();
  global.fetch=async()=>{throw new TypeError('offline');};
  assert.equal(await c.createAndActivateTask('QA','instruction'),false);
  assert.notEqual(c.taskDraft.value,null);
  global.fetch=()=>{assert.fail('Leaving setup must not mutate server data');};
  assert.equal(c.leaveTaskCreation(),true);
  assert.equal(c.taskDraft.value,null);
  assert.equal(sessionValues.has('insight-capture.task-draft'),false);
});
