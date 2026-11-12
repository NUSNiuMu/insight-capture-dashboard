import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue';
import { t } from './i18n.js';
export function deviceStage(device, status) {
  const name=device.name;
  if(!status?.system?.camera_links?.some(link=>link.namespace===name))return t('等待 USB 连接');
  if(!status?.preflight?.clocks?.devices?.[name]?.ok)return t('正在校时');
  if(status.system.managed_camera_startup&&!status.system.camera_startup?.[name])return t('正在恢复采集');
  if(!status?.preflight?.camera_health?.[name]?.image_fresh)return t('等待图像');
  const imageClock=status?.preflight?.image_clocks?.devices?.[name];
  if(imageClock?.state==='invalid')return t('图像时间戳异常');
  if(imageClock&&!imageClock.ok)return t('等待图像');
  if(!status?.preflight?.camera_health?.[name]?.vio_fresh)return t('等待 VIO');
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
function confirmedRecord(command,current) { return current?.operation?.command_id===command.command_id&&current.operation.action===command.action; }
export function useCapture() {
  const status=ref(null), mapping=ref(null), tasks=ref([]), tasksError=ref(''), online=ref(false), pending=ref(false), recordPending=ref(null), error=ref('');
  const taskDraft=ref(taskDraftFromSession()),unconfirmedRecord=ref(readSession('record-command'));
  if(unconfirmedRecord.value)error.value=t('连接中断，操作结果待确认；重连后会核对设备状态。');
  const busy=computed(()=>pending.value || ['starting','recording','saving'].includes(status.value?.state));
  let alive=true, timer, mapTimer, statusGeneration=0, tasksGeneration=0;
  async function refresh() {
    const generation=++statusGeneration;
    try {
      const value=await api('/api/mobile/status');
      if(alive&&generation===statusGeneration){status.value=value;online.value=true;}
      return value;
    }
    catch { if(alive&&generation===statusGeneration)online.value=false;return null; }
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
  async function action(path,body={},reconcile) {
    if(pending.value || !online.value) return false;
    pending.value=true; error.value='';
    try {
      try { await api(path,body); }
      catch(e) {
        const current=await refresh();
        if(!reconcile?.(e,current)){error.value=e.message;return false;}
      }
      // The command acknowledgement remains successful if a later read fails.
      await refresh();
      await loadTasks().catch(()=>{});
      return true;
    }
    finally { pending.value=false; }
  }
  async function record(actionName) {
    if(pending.value || !online.value) return;
    const taskId=status.value?.task_status?.task?.task_id,outputPath=status.value?.output_path;
    const previous=unconfirmedRecord.value;
    const command=previous?.action===actionName&&previous.task_id===taskId&&previous.output_path===outputPath?previous:{command_id:id(),action:actionName,task_id:taskId,output_path:outputPath};
    unconfirmedRecord.value=command;writeSession('record-command',command);
    let rejected=false;
    recordPending.value=actionName;
    try {
      const ok=await action(`/api/mobile/${actionName}`, {command_id:command.command_id,output_path:command.output_path}, (e,current)=>{
        rejected=Boolean(e.status);
        if(!e.status){
          e.message=confirmedRecord(command,current)&&current.operation.error?current.operation.error:t('连接中断，操作结果待确认；重连后会核对设备状态。');
          return confirmedRecord(command,current)&&!current.operation.error;
        }
        if(e.status!==409||!current)return false;
        if(actionName==='start')return Boolean(taskId)&&current.task_status?.task?.task_id===taskId&&['starting','recording'].includes(current.state);
        return Boolean(outputPath)&&current.output_path===outputPath&&['saving','saved'].includes(current.state);
      });
      if(ok||rejected){unconfirmedRecord.value=null;writeSession('record-command',null);}
      return ok;
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
    if(command&&confirmedRecord(command,current)){
      error.value=current.operation.error||'';
      unconfirmedRecord.value=null;writeSession('record-command',null);
    }
  });
  async function calibrate() { await action('/api/mapping/reset'); }
  async function poll() { await refresh(); if(alive) timer=setTimeout(poll,document.hidden?4000:1500); }
  async function pollMap() { try { mapping.value=await api('/api/mapping'); } catch { mapping.value=null; }
    if(alive) mapTimer=setTimeout(pollMap,document.hidden?5000:2000); }
  watch(online,value=>{if(value)loadTasks().catch(()=>{});});
  onMounted(()=>{poll();pollMap();});
  onBeforeUnmount(()=>{alive=false;clearTimeout(timer);clearTimeout(mapTimer);});
  return {status,mapping,tasks,tasksError,taskDraft,online,pending,recordPending,error,busy,action,record,createAndActivateTask,leaveTaskCreation,calibrate,loadTasks};
}
export const roleLabel=role=>t({head:'头部相机',left_hand:'左腕相机',right_hand:'右腕相机'}[role]||role);
export const stateLabel=state=>t({idle:'准备就绪',starting:'正在准备',recording:'正在录制',saving:'正在保存',saved:'保存完成',error:'操作失败',playback:'桌面回放中'}[state]||'连接中');
export const size=bytes=>bytes==null?'—':`${(bytes/1024**3).toFixed(1)} GB`;
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
