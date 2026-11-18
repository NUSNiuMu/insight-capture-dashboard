const {test}=require('node:test');
const assert=require('node:assert/strict');
const {buildSync}=require('esbuild');
const source=buildSync({entryPoints:[require('node:path').resolve(__dirname,'../src/mobile/phoneTime.js')],bundle:true,platform:'node',format:'cjs',write:false}).outputFiles[0].text;
const moduleObject={exports:{}};new Function('module','exports',source)(moduleObject,moduleObject.exports);
const {phoneTimeSync}=moduleObject.exports;
const status=(state='idle')=>({state,server_time:100,time_sync:{state:'idle'}});
function fixture(){let mono=0,visible=true;const calls=[];const api=async(path,body)=>{calls.push({path,body});mono+=10;return {server_time:100}};return {calls,sync:phoneTimeSync(api,{now:()=>1794900000000,monotonic:()=>mono,visible:()=>visible}),advance:()=>mono+=31000,hide:()=>visible=false};}
test('foreground connection sends current epoch and measured round trip',async()=>{const f=fixture();await f.sync(status());assert.equal(f.calls.length,2);assert.equal(f.calls[1].body.epoch_ms,1794900000000);assert.equal(f.calls[1].body.rtt_ms,10);});
test('recording, saving, preparing and hidden tabs never send time',async()=>{const f=fixture();for(const state of ['starting','recording','saving','playback'])await f.sync(status(state));f.hide();await f.sync(status());assert.equal(f.calls.length,0);});
test('retry is throttled and uses a new request after reconnect',async()=>{const f=fixture();await f.sync(status());await f.sync(status());assert.equal(f.calls.length,2);await f.sync(null,false);f.advance();await f.sync(status());assert.equal(f.calls.length,4);});
test('a corrected clock needs no repeated writes',async()=>{const calls=[];const sync=phoneTimeSync(async(path,body)=>{calls.push(body);return {server_time:1794900000}},{now:()=>1794900000000,monotonic:()=>10,visible:()=>true});await sync({...status(),server_time:1794900000});assert.equal(calls.length,1);assert.equal(calls[0],undefined);});

test('manual resync sends a fresh sample even when the host clock already matches',async()=>{
 const calls=[];const sync=phoneTimeSync(async(path,body)=>{calls.push(body);return {server_time:1794900000}},{now:()=>1794900000000,monotonic:()=>10,visible:()=>true});
 await sync({...status(),server_time:1794900000},true,true);
 assert.equal(calls.length,2);assert.equal(calls[1].resync_cameras,true);
});
test('manual resync still refuses recording and reports failed requests',async()=>{
 const f=fixture();await f.sync(status('recording'),true,true);assert.equal(f.calls.length,0);
 const sync=phoneTimeSync(async()=>{throw Error('offline')},{now:()=>1794900000000,monotonic:()=>10,visible:()=>true});
 await assert.rejects(sync(status(),true,true),/offline/);
});
