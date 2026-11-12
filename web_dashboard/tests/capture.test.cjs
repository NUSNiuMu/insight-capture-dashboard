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
const temp = mkdtempSync(path.join(tmpdir(), 'capture-test-'));
const outfile = path.join(temp, 'harness.cjs');
buildSync({ stdin: { contents: `
  import { createSSRApp } from 'vue';
  import { renderToString } from 'vue/server-renderer';
  import { useCapture } from './src/mobile/useCapture.js';
  export async function capture() {
    let result;
    await renderToString(createSSRApp({setup(){result=useCapture();return ()=>null;}}));
    return result;
  }`, resolveDir: path.resolve(__dirname, '..') },
  bundle: true, platform: 'node', format: 'cjs', outfile });
const { capture } = require(outfile);
rmSync(temp, { recursive: true });
const snapshot = (state='idle', task='qa', output='/take/1') => ({
  state, output_path: output, task_status: { task: { task_id: task } },
});
const response = (value, status=200) => ({ ok: status < 400, status, json: async()=>value });
async function harness(initial=snapshot()) {
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
