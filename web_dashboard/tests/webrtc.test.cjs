const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtempSync,rmSync}=require('node:fs');
const {tmpdir}=require('node:os');
const path=require('node:path');
const {buildSync}=require('esbuild');
const temp=mkdtempSync(path.join(tmpdir(),'insight-webrtc-'));
const file=path.join(temp,'webrtc.cjs');
buildSync({entryPoints:[path.resolve(__dirname,'../src/preview/webrtc.js')],bundle:true,platform:'node',format:'cjs',outfile:file});
const {startWebRTC}=require(file);rmSync(temp,{recursive:true});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const h264='v=0\r\na=rtpmap:102 H264/90000\r\n';
const vp8='v=0\r\na=rtpmap:96 VP8/90000\r\n';

async function scenario({capabilities='ok',preferences='ok',offer=h264,answer=h264,codec='video/H264',frames=10}={},run){
 const keys=['RTCPeerConnection','RTCRtpReceiver','WebSocket','MediaStream','location','setTimeout','clearTimeout'];
 const original=Object.fromEntries(keys.map(k=>[k,global[k]]));
 const timers=new Set(),sent=[],failures=[],peers=[],sockets=[];let ready=0,preferred=false;
 global.location={href:'http://jetson.local:8765/mobile',protocol:'http:'};
 global.setTimeout=(fn,delay)=>{const timer={fn,delay};timers.add(timer);return timer};global.clearTimeout=t=>timers.delete(t);
 const transceiver={receiver:{}};
 if(preferences!=='missing')transceiver.setCodecPreferences=()=>{if(preferences==='throws')throw Error('Preference unsupported');preferred=true};
 global.RTCRtpReceiver=capabilities==='missing'?undefined:{getCapabilities:()=>{
  if(capabilities==='throws')throw Error('Capability query unavailable');
  return {codecs:capabilities==='empty'?[]:[{mimeType:'video/H264'}]};
 }};
 global.RTCPeerConnection=class{
  constructor(){peers.push(this)}
  addTransceiver(){return transceiver}
  async createOffer(){return {type:'offer',sdp:offer}}
  async setLocalDescription(value){this.localDescription=value}
  async setRemoteDescription(value){this.remoteDescription=value}
  async addIceCandidate(){}
  async getStats(){return new Map([['video',{type:'inbound-rtp',kind:'video',codecId:'codec',framesDecoded:frames,bytesReceived:1000,timestamp:1000}],['codec',{mimeType:codec}]])}
  getReceivers(){return []}
  close(){this.closed=true}
 };
 global.WebSocket=class{constructor(){sockets.push(this)}send(value){sent.push(JSON.parse(value))}close(){this.closed=true}};
 global.MediaStream=class{};
 const video={pause(){},play:()=>Promise.resolve(),srcObject:null};
 let session;
 try{
  session=startWebRTC({video,source:'__overview__',onFailure:e=>failures.push(e),onReady:()=>ready++,onStats:()=>{}});
  await settle();assert.equal(ready,0,'A capability query alone must never mean decoded video');
  if(sockets[0]){
   await sockets[0].onopen();await settle();
   if(!failures.length){sockets[0].onmessage({data:JSON.stringify({type:'webrtc/answer',value:answer})});await settle();}
  }
  if(!failures.length){const timer=[...timers].find(t=>t.delay===1000);if(timer){timers.delete(timer);await timer.fn();}}
  await run({failures,peers,sockets,sent,ready,preferred,timers});
 }finally{session?.close();for(const k of keys){if(original[k]===undefined)delete global[k];else global[k]=original[k]}}
}

for(const options of [{capabilities:'missing'},{capabilities:'throws'},{capabilities:'empty'},{preferences:'missing'},{preferences:'throws'},{}]){
 test(`H.264 readiness uses negotiation and decoded frames: ${JSON.stringify(options)}`,async()=>{
  await scenario(options,({failures,ready,sent})=>{
   assert.deepEqual(failures,[]);assert.equal(ready,1);assert.ok(sent.some(m=>m.type==='webrtc/offer'&&m.value.includes('H264/90000')));
  });
 });
}

test('an offer without H.264 still fails before sending video negotiation',async()=>{
 await scenario({capabilities:'missing',offer:vp8},({failures,ready,sent,peers})=>{
  assert.match(failures[0],/H.264/);assert.equal(ready,0);assert.equal(sent.length,0);assert.equal(peers[0].closed,true);
 });
});

test('an answer without H.264 never reports preview ready',async()=>{
 await scenario({answer:vp8},({failures,ready})=>{assert.match(failures[0],/H.264/);assert.equal(ready,0)});
});

test('a decoded non-H.264 codec remains an error',async()=>{
 await scenario({codec:'video/VP8'},({failures,ready})=>{assert.match(failures[0],/不是 H.264/);assert.equal(ready,0)});
});

test('negotiated H.264 without decoded frames still times out',async()=>{
 await scenario({frames:0},({failures,ready,timers})=>{
  assert.equal(ready,0);[...timers].find(t=>t.delay===10000).fn();assert.match(failures[0],/未解码/);
 });
});
