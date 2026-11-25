// Confirm Jetson's clock once per host boot, when a phone is in the foreground.
export function phoneTimeSync(api, options={}) {
  const now=options.now||Date.now, monotonic=options.monotonic||(()=>performance.now());
  const visible=options.visible||(()=>!document.hidden);
  let pending=false, nextAttempt=0;
  return async function synchronize(status) {
    const state=status?.time_sync?.state;
    if(!state||state==='synced'||state==='syncing'||!visible()||pending)return;
    if(['starting','recording','saving','playback'].includes(status.state)||status.camera_time_sync?.state==='syncing')return;
    const failed=state==='error';
    if(failed&&Math.abs(now()/1000-status.server_time)>1)return;
    if(monotonic()<nextAttempt)return;
    pending=true;nextAttempt=monotonic()+30000;
    try {
      const sent=monotonic();
      const clock=await api('/api/system/time');
      const rtt=monotonic()-sent;
      if(!visible()||clock.state==='synced'||clock.state==='syncing'||rtt>2000)return;
      if(failed&&Math.abs(now()/1000-clock.server_time-rtt/2000)>1)return;
      await api('/api/system/time',{epoch_ms:now(),rtt_ms:rtt});
    } catch { /* Retry an unconfirmed setup with a fresh phone time after reconnect. */ }
    finally {pending=false;}
  };
}
