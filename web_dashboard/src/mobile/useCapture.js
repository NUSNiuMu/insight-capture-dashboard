import { ref, computed, onMounted, onBeforeUnmount } from 'vue';
import { t } from './i18n.js';
export async function api(path, body) {
  const response = await fetch(path, {cache:'no-store', signal:AbortSignal.timeout(12000),
    ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.message || result.error || `HTTP ${response.status}`);
  return result;
}
export function id() { return Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join(''); }
export function useCapture() {
  const status=ref(null), mapping=ref(null), tasks=ref([]), online=ref(false), pending=ref(false), error=ref('');
  const busy=computed(()=>pending.value || ['starting','recording','saving'].includes(status.value?.state));
  let alive=true, timer, mapTimer;
  async function refresh() {
    try { status.value=await api('/api/mobile/status'); online.value=true; }
    catch { online.value=false; }
  }
  async function loadTasks() { tasks.value=(await api('/api/tasks')).tasks; }
  async function action(path,body={}) {
    if(pending.value || !online.value) return false;
    pending.value=true; error.value='';
    try { await api(path,body); await refresh(); await loadTasks(); return true; }
    catch(e) { error.value=e.message; return false; }
    finally { pending.value=false; }
  }
  async function record(actionName) {
    await action(`/api/mobile/${actionName}`, {command_id:id(),output_path:status.value?.output_path});
  }
  async function calibrate() { await action('/api/mapping/reset'); }
  async function poll() { await refresh(); if(alive) timer=setTimeout(poll,document.hidden?4000:1500); }
  async function pollMap() { try { mapping.value=await api('/api/mapping'); } catch { mapping.value=null; }
    if(alive) mapTimer=setTimeout(pollMap,document.hidden?5000:2000); }
  onMounted(()=>{poll();pollMap();loadTasks().catch(e=>error.value=e.message);});
  onBeforeUnmount(()=>{alive=false;clearTimeout(timer);clearTimeout(mapTimer);});
  return {status,mapping,tasks,online,pending,error,busy,action,record,calibrate,loadTasks};
}
export const roleLabel=role=>t({head:'头部相机',left_hand:'左腕相机',right_hand:'右腕相机'}[role]||role);
export const stateLabel=state=>t({idle:'准备就绪',starting:'正在准备',recording:'正在录制',saving:'正在保存',saved:'保存完成',error:'操作失败',playback:'桌面回放中'}[state]||'连接中');
export const size=bytes=>bytes==null?'—':`${(bytes/1024**3).toFixed(1)} GB`;
export function duration(seconds) {seconds=Math.max(0,Math.floor(seconds||0));return `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}
