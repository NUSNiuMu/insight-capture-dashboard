// Offline UTC comes from the foreground phone; elapsed time uses a monotonic clock.
export function phoneTimeSync(api, options={}) {
  const now=options.now||Date.now, monotonic=options.monotonic||(()=>performance.now());
  const visible=options.visible||(()=>!document.hidden);
  let pending=false, nextAttempt=0, connected=false;
  return async function synchronize(status, online=true) {
    if(!online){connected=false;return;}
    if(!status?.time_sync||!visible())return;
    const reconnect=!connected;connected=true;
    if(pending||['starting','recording','saving','playback'].includes(status.state)||status.time_sync.state==='syncing')return;
    const retry=['error','deferred'].includes(status.time_sync.state);
    if(!reconnect&&!retry&&Math.abs(now()/1000-status.server_time)<=2)return;
    if(monotonic()<nextAttempt)return;
    pending=true;nextAttempt=monotonic()+30000;
    try {
      const sent=monotonic();
      const clock=await api('/api/system/time');
      const rtt=monotonic()-sent;
      if(!visible()||rtt>2000)return;
      if(!retry&&Math.abs(now()/1000-clock.server_time-rtt/2000)<=1)return;
      await api('/api/system/time',{epoch_ms:now(),rtt_ms:rtt});
    } catch { /* Status polling confirms completion; retry with a fresh phone time. */ }
    finally {pending=false;}
  };
}
