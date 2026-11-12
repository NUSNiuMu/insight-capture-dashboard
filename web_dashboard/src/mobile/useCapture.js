import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue';
import { t } from './i18n.js';
export async function api(path, body) {
  const response = await fetch(path, {cache:'no-store', signal:AbortSignal.timeout(12000),
    ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.message || result.error || `HTTP ${response.status}`), {status:response.status});
  return result;
}
export function id() { return Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join(''); }
export function useCapture() {
  const status=ref(null), mapping=ref(null), tasks=ref([]), tasksError=ref(''), online=ref(false), pending=ref(false), recordPending=ref(null), error=ref('');
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
    recordPending.value=actionName;
    try {
      return await action(`/api/mobile/${actionName}`, {command_id:id(),output_path:outputPath}, (e,current)=>{
        if(e.status!==409||!current)return false;
        if(actionName==='start')return Boolean(taskId)&&current.task_status?.task?.task_id===taskId&&['starting','recording'].includes(current.state);
        return Boolean(outputPath)&&current.output_path===outputPath&&['saving','saved'].includes(current.state);
      });
    }
    finally { recordPending.value=null; }
  }
  async function calibrate() { await action('/api/mapping/reset'); }
  async function poll() { await refresh(); if(alive) timer=setTimeout(poll,document.hidden?4000:1500); }
  async function pollMap() { try { mapping.value=await api('/api/mapping'); } catch { mapping.value=null; }
    if(alive) mapTimer=setTimeout(pollMap,document.hidden?5000:2000); }
  watch(online,value=>{if(value)loadTasks().catch(()=>{});});
  onMounted(()=>{poll();pollMap();});
  onBeforeUnmount(()=>{alive=false;clearTimeout(timer);clearTimeout(mapTimer);});
  return {status,mapping,tasks,tasksError,online,pending,recordPending,error,busy,action,record,calibrate,loadTasks};
}
export const roleLabel=role=>t({head:'头部相机',left_hand:'左腕相机',right_hand:'右腕相机'}[role]||role);
export const stateLabel=state=>t({idle:'准备就绪',starting:'正在准备',recording:'正在录制',saving:'正在保存',saved:'保存完成',error:'操作失败',playback:'桌面回放中'}[state]||'连接中');
export const size=bytes=>bytes==null?'—':`${(bytes/1024**3).toFixed(1)} GB`;
export function duration(seconds) {seconds=Math.max(0,Math.floor(seconds||0));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}
