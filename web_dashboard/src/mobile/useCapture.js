import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue';
import { t } from './i18n.js';
import { phoneTimeSync } from './phoneTime.js';
const vioStages={NOT_INITED:'定位尚未初始化',RESTARTING:'正在恢复定位',STOPPED:'定位已停止',TRACKING_LOST:'定位跟踪丢失',DATA_LOST:'定位输入中断',NO_CALIBRATION:'缺少相机标定'};
const vioHints={
 NOT_INITED:'请确认镜头未遮挡、光线充足且画面有纹理；必要时缓慢移动设备，观察定位是否恢复。',
 RESTARTING:'相机正在恢复定位，请保持供电并等待状态更新。',
 STOPPED:'相机定位已停止，请检查设备工作模式；持续异常时保存诊断。',
 TRACKING_LOST:'请让相机朝向光线充足、有纹理的场景，缓慢移动并观察定位是否恢复。',
 DATA_LOST:'相机报告定位输入中断，请检查供电和连接；持续异常时保存诊断。',
 NO_CALIBRATION:'相机内部标定文件缺失，请恢复相机标定。页面上的重新校准只重建公共地图。',
};
export function deviceHint(device,status) {
 const health=status?.preflight?.camera_health?.[device.name];
 if(!health||health.vio_fresh||!health.image_fresh||!status?.preflight?.clocks?.devices?.[device.name]?.ok||!status?.system?.camera_links?.some(link=>link.namespace===device.name))return '';
 if(status.system.managed_camera_startup&&!status.system.camera_startup?.[device.name])return '';
 const imageClock=status?.preflight?.image_clocks?.devices?.[device.name];
 if(imageClock&&!imageClock.ok)return '';
 if(!health.vio_status_fresh)return t('暂未收到新的相机定位状态，请检查连接；持续异常时保存诊断。');
 return t(vioHints[health.vio_status]||'尚未收到新的定位数据，当前状态不能确认原因；持续异常时保存诊断。');
}
export const cameraClocks=status=>status?.distributed?.clock??status?.preflight?.clocks??{};
export function deviceClock(device,status) {
  const devices=cameraClocks(status).devices||{};
  const host=status?.system?.camera_links?.find(link=>link.namespace===device.name)?.host;
  return devices[device.name]??(host?Object.values(devices).find(clock=>clock.host===host):undefined);
}
export function deviceStage(device, status) {
  const name=device.name;
  if(!status?.system?.camera_links?.some(link=>link.namespace===name))return t('等待 USB 连接');
  if(!deviceClock(device,status)?.ok)return t('正在校时');
  if(status.system.managed_camera_startup&&!status.system.camera_startup?.[name])return t('正在恢复采集');
  if(!status?.preflight?.camera_health?.[name]?.image_fresh)return t('等待图像');
  const imageClock=status?.preflight?.image_clocks?.devices?.[name];
  if(imageClock?.state==='invalid')return t('图像时间戳异常');
  if(imageClock&&!imageClock.ok)return t('等待图像');
  const health=status?.preflight?.camera_health?.[name];
  if(!health?.vio_fresh)return t(health?.vio_status_fresh&&vioStages[health.vio_status]||'等待定位数据');
  return t('已就绪');
}
export async function api(path, body) {
  const response = await fetch(path, {cache:'no-store', signal:AbortSignal.timeout(12000),
    ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.message || result.error || `HTTP ${response.status}`), {status:response.status});
  return result;
}
export function id() { return Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join(''); }
function readSession(key) { try { return JSON.parse(sessionStorage.getItem(`insight-capture.${key}`)); } catch { return null; } }
function writeSession(key,value) { try { if(value)sessionStorage.setItem(`insight-capture.${key}`,JSON.stringify(value));else sessionStorage.removeItem(`insight-capture.${key}`); } catch { /* In-memory retry still works when browser storage is unavailable. */ } }
function taskDraftFromSession() {
  const value=readSession('task-draft');
  return value&&/^task_[a-f0-9]{32}$/.test(value.id)&&typeof value.name==='string'&&typeof value.instruction==='string'?value:null;
}
function sameTask(task,draft) { return task?.task_id===draft.id&&task.name===draft.name&&task.instruction===draft.instruction&&task.capture_profile===draft.capture_profile; }
function confirmedRecord(command,current) { return [current?.operation,current?.requested_operation].find(operation=>operation?.command_id===command.command_id&&operation.action===command.action); }
export function useCapture() {
  const status=ref(null), mapping=ref(null), tasks=ref([]), tasksError=ref(''), online=ref(false), connectionFailed=ref(false), pending=ref(false), recordPending=ref(null), error=ref('');
  const taskDraft=ref(taskDraftFromSession()),unconfirmedRecord=ref(readSession('record-command')),checkingCameraClock=ref(false);
  if(unconfirmedRecord.value)error.value=t('连接中断，操作结果待确认；重连后会核对设备状态。');
  const busy=computed(()=>pending.value || status.value?.time_sync?.state==='syncing' || status.value?.camera_time_sync?.state==='syncing' || ['starting','recording','saving'].includes(status.value?.state));
  let alive=true, timer, mapTimer, statusGeneration=0, tasksGeneration=0;
  const syncPhoneTime=phoneTimeSync(api,{visible:()=>alive&&!document.hidden});
  async function refresh() {
    const generation=++statusGeneration;
    try {
      let value=await api('/api/mobile/status');
      // Show authoritative live state immediately, even if an old receipt
      // lookup is slow. A history lookup is not the device connection itself.
      if(alive&&generation===statusGeneration){status.value=value;online.value=true;connectionFailed.value=false;syncPhoneTime(value);}
      const command=unconfirmedRecord.value;
      if(command&&!confirmedRecord(command,value)){
        // Another client may have replaced the latest operation. Read this
        // exact receipt; never repeat a write merely to discover its result.
        const receipt=await api(`/api/mobile/commands/${encodeURIComponent(command.command_id)}`).catch(()=>null);
        if(receipt)value={...value,requested_operation:receipt.operation};
      }
      if(alive&&generation===statusGeneration){status.value=value;online.value=true;}
      return value;
    }
    catch { if(alive&&generation===statusGeneration){online.value=false;connectionFailed.value=true;syncPhoneTime(null,false);}return null; }
  }
  async function loadTasks() {
    const generation=++tasksGeneration;
    try {
      const value=await api('/api/tasks');
      if(alive&&generation===tasksGeneration){tasks.value=value.tasks;tasksError.value='';}
    } catch(e) {
      if(alive&&generation===tasksGeneration)tasksError.value=t('任务列表暂时无法读取，连接恢复后自动重试。');
      throw e;
    }
  }
  async function action(path,body={},reconcile,acknowledged) {
    if(pending.value || !online.value) return false;
    pending.value=true; error.value='';
    try {
      try { const result=await api(path,body);acknowledged?.(result); }
      catch(e) {
        const current=await refresh();
        if(!await reconcile?.(e,current)){error.value=e.message;return false;}
      }
      // The command acknowledgement remains successful if a later read fails.
      await refresh();
      await loadTasks().catch(()=>{});
      return true;
    }
    finally { pending.value=false; }
  }
  async function deleteTask(task) {
    const ok=await action(`/api/tasks/${encodeURIComponent(task.task_id)}/delete`,
      {expected_recorded_takes:task.stats?.recorded_takes||0},async(e)=>{
        if(e.status)return false;
        const listed=await api('/api/tasks').catch(()=>null);
        return Boolean(listed?.tasks&&!listed.tasks.some(item=>item.task_id===task.task_id));
      });
    if(ok&&taskDraft.value?.id===task.task_id){taskDraft.value=null;writeSession('task-draft',null);}
    return ok;
  }
  async function deleteTake(taskId,take) {
    const path=`/api/tasks/${encodeURIComponent(taskId)}/recordings`;
    return action(`${path}/${take.take_id}/delete`,{bag_name:take.bag_name},async(e)=>{
      if(e.status&&e.status!==404)return false;
      const listed=await api(path).catch(()=>null);
      return Boolean(listed?.takes&&!listed.takes.some(item=>item.take_id===take.take_id));
    });
  }
  async function record(actionName) {
    if(pending.value || !online.value) return;
    const taskId=status.value?.task_status?.task?.task_id,outputPath=status.value?.output_path;
    const previous=unconfirmedRecord.value;
    const command=previous?.action===actionName&&previous.task_id===taskId&&previous.output_path===outputPath?previous:{command_id:id(),action:actionName,task_id:taskId,output_path:outputPath};
    unconfirmedRecord.value=command;writeSession('record-command',command);
    let rejected=false,acknowledgedError='';
    recordPending.value=actionName;
    try {
      const ok=await action(`/api/mobile/${actionName}`, {command_id:command.command_id,task_id:command.task_id,output_path:command.output_path}, (e,current)=>{
        rejected=Boolean(e.status);
        if(!e.status){
          const receipt=confirmedRecord(command,current);
          rejected=Boolean(receipt?.error);
          e.message=receipt?.error||t('连接中断，操作结果待确认；重连后会核对设备状态。');
          return Boolean(receipt&&!receipt.error);
        }
        if(e.status!==409||!current)return false;
        if(actionName==='start')return Boolean(taskId)&&current.task_status?.task?.task_id===taskId&&['starting','recording'].includes(current.state);
        return Boolean(outputPath)&&current.output_path===outputPath&&['saving','saved'].includes(current.state);
      },receipt=>{acknowledgedError=receipt?.error||'';});
      if(ok||rejected){unconfirmedRecord.value=null;writeSession('record-command',null);}
      if(acknowledgedError)error.value=acknowledgedError;
      return ok&&!acknowledgedError;
    }
    finally { recordPending.value=null; }
  }
  async function createAndActivateTask(name,instruction) {
    if(busy.value||!online.value)return false;
    pending.value=true;error.value='';
    if(!taskDraft.value){taskDraft.value={id:`task_${id()}`,name:name.trim(),instruction:instruction.trim(),capture_profile:'dual_arm_umi',created:false};writeSession('task-draft',taskDraft.value);}
    const draft=taskDraft.value;
    try {
      if(!draft.created){
        const {created,...payload}=draft;
        try { await api('/api/tasks',payload); }
        catch(e){
          // The create may already be committed even though its response was lost.
          const listed=await api('/api/tasks').catch(()=>null);
          const existing=listed?.tasks?.find(task=>task.task_id===draft.id);
          if(!sameTask(existing,draft)){
            if(e.status&&listed){taskDraft.value=null;writeSession('task-draft',null);}
            if(!e.status)e.message=t('任务创建结果待确认。重试会核对同一任务，不会重复新建。');
            throw e;
          }
        }
        draft.created=true;writeSession('task-draft',draft);
      }
      try { await api(`/api/tasks/${encodeURIComponent(draft.id)}/activate`,{}); }
      catch(e){
        const current=await refresh();
        if(!current?.task_status?.active||current.task_status.task?.task_id!==draft.id){
          if(!e.status)e.message=t('任务已创建，启用结果待确认。重连后可继续启用同一任务。');
          throw e;
        }
      }
      taskDraft.value=null;writeSession('task-draft',null);
      await refresh();await loadTasks().catch(()=>{});
      return true;
    } catch(e){error.value=e.message;return false;}
    finally {pending.value=false;}
  }
  function leaveTaskCreation() {
    if(pending.value)return false;
    taskDraft.value=null;writeSession('task-draft',null);error.value='';
    return true;
  }
  watch([status,recordPending],([current,request])=>{
    // Do not clear the identity until the request's own catch/finally has settled.
    // A poll can acknowledge the command before the POST response disconnects.
    if(request)return;
    const command=unconfirmedRecord.value;
    const receipt=command&&confirmedRecord(command,current);
    if(receipt){
      error.value=receipt.error||'';
      unconfirmedRecord.value=null;writeSession('record-command',null);
    }
  });
  async function synchronizeTime(recover=false) {
    if(busy.value||!online.value)return;
    pending.value=true;checkingCameraClock.value=true;error.value='';
    try {
      const {clock,...result}=await api('/api/system/time',{resync_cameras:true,...(recover?{recover_cameras:true}:{})});
      ++statusGeneration;
      if(alive){
        const current={...status.value,camera_time_sync:result};
        if(clock){
          if(current.distributed?.clock)current.distributed={...current.distributed,clock};
          else current.preflight={...current.preflight,clocks:clock};
        }
        status.value=current;
      }
    }
    catch(e){error.value=t(e.message);}
    finally {checkingCameraClock.value=false;pending.value=false;await refresh();}
  }
  async function calibrate() { await action('/api/mapping/reset'); }
  async function poll() { await refresh(); if(alive) timer=setTimeout(poll,document.hidden?4000:1500); }
  async function pollMap() { try { mapping.value=await api('/api/mapping'); } catch { mapping.value=null; }
    if(alive) mapTimer=setTimeout(pollMap,document.hidden?5000:2000); }
  watch(online,value=>{if(value)loadTasks().catch(()=>{});});
  onMounted(()=>{poll();pollMap();});
  onBeforeUnmount(()=>{alive=false;clearTimeout(timer);clearTimeout(mapTimer);});
  return {status,mapping,tasks,tasksError,taskDraft,online,connectionFailed,pending,recordPending,error,busy,checkingCameraClock,action,record,createAndActivateTask,deleteTask,deleteTake,leaveTaskCreation,synchronizeTime,calibrate,loadTasks};
}
export const roleLabel=role=>t({head:'头部相机',left_hand:'左腕相机',right_hand:'右腕相机'}[role]||role);
export const stateLabel=state=>t({idle:'准备就绪',starting:'正在准备',recording:'正在录制',saving:'正在保存',saved:'保存完成',error:'操作失败',playback:'桌面回放中'}[state]||'连接中');
export const size=bytes=>bytes==null?'—':`${(bytes/1024**3).toFixed(1)} GiB`;
export function duration(seconds) {seconds=Math.max(0,Math.floor(seconds||0));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}
function isCurrentTake(take,status) {
 const current=status?.current_take;
 return Boolean(take?.bag_path&&take.bag_path===status?.output_path&&take.task_id===current?.task_id&&take.take_id===current?.take_id);
}
export function takeDisplayState(take,status) {
 if(isCurrentTake(take,status)) {
  if(status.state==='recording')return 'recording';
  if(status.state==='saving')return 'finalizing';
  if(status.state==='saved'&&['complete','saved'].includes(status.current_take?.state))return status.current_take.state;
 }
 return take?.state;
}
export function takeDuration(take,status) {
 const finite=value=>typeof value==='number'&&Number.isFinite(value);
 const same=isCurrentTake(take,status);
 const live=same&&['recording','saving'].includes(status?.state);
 const currentTiming=same?status?.current_take?.recording_timing:null;
 const timing=live?(status?.recording_timing||currentTiming||take?.recording_timing)
  :currentTiming?.source==='mcap_metadata'?currentTiming:(take?.recording_timing||currentTiming);
 if(!timing)return '--:--';
 if(finite(timing.duration_s)&&timing.duration_s>=0)return duration(timing.duration_s);
 const start=timing.started_at_epoch_s,end=timing.stopped_at_epoch_s;
 if(!finite(start)||start<=0)return '--:--';
 if(finite(end)&&end>=start)return duration(end-start);
 if(same&&status?.state==='recording'&&finite(status.server_time))return duration(status.server_time-start);
 return '--:--';
}
