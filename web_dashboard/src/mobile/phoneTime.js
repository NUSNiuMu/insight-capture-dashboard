// Offline UTC comes from the foreground phone; elapsed time uses a monotonic clock.
export function phoneTimeSync(api, options={}) {
  const now=options.now||Date.now, monotonic=options.monotonic||(()=>performance.now());
  const visible=options.visible||(()=>!document.hidden);
  let pending=false, nextAttempt=0, connected=false;
  return async function synchronize(status, online=true) {
    if(!online){connected=false;return;}
    if(!status?.time_sync||!visible())return;
    const reconnect=!connected;connected=true;
    if(pending||['starting','recording','saving','playback'].includes(status.state)||status.time_sync.state==='syncing'||status.camera_time_sync?.state==='syncing')return;
    const failed=status.time_sync.state==='error';
    const skew=Math.abs(now()/1000-status.server_time);
    if(failed&&skew>1)return;
    if(!reconnect&&!failed&&status.time_sync.state!=='deferred'&&skew<=2)return;
    if(monotonic()<nextAttempt)return;
    pending=true;nextAttempt=monotonic()+30000;
    try {
      const sent=monotonic();
      const clock=await api('/api/system/time');
      const rtt=monotonic()-sent;
      if(!visible())return;
      if(rtt>2000)throw new Error('网络延迟较大，稍后重新校时。');
      const aligned=Math.abs(now()/1000-clock.server_time-rtt/2000)<=1;
      if(failed!==aligned)return;
      await api('/api/system/time',{epoch_ms:now(),rtt_ms:rtt});
    } catch { /* A later check may confirm the clocks already agree. */ }
    finally {pending=false;}
  };
}
