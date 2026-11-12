<script setup>
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue';
import CameraPreview from '../preview/components/CameraPreview.vue';
import RecordsPage from './RecordsPage.vue';
import TrajectoryPanel from './TrajectoryPanel.vue';
import { useCapture, api, id, roleLabel, stateLabel, size, duration } from './useCapture.js';
import { t, language, setLanguage } from './i18n.js';
const {status,mapping,tasks,online,pending,error,busy,action,record,calibrate,loadTasks}=useCapture();
const endpoint=location.host;
const route=ref(location.hash.slice(1)||'capture'),visible=ref(!document.hidden),dialog=ref(null),creating=ref(false),name=ref(''),instruction=ref(''),shell=ref(null),footer=ref(null);
const page=computed(()=>route.value.split('/')[0]);const active=computed(()=>['starting','recording','saving'].includes(status.value?.state));
const showDock=computed(()=>page.value==='capture'||active.value);
const task=computed(()=>status.value?.task_status?.active?status.value.task_status.task:null);
const elapsed=computed(()=>status.value?.state==='recording'?duration(status.value.server_time-status.value.started_at_epoch_s):'00:00');
const health=computed(()=>status.value?.preflight?.camera_health||{});
const clocks=computed(()=>status.value?.preflight?.clocks||{});
const devices=computed(()=>[...(status.value?.devices||[])].sort((a,b)=>({head:0,left_hand:1,right_hand:2}[a.role]-{head:0,left_hand:1,right_hand:2}[b.role])));
const head=computed(()=>devices.value.find(d=>d.role==='head'));
const title=computed(()=>t(page.value==='capture'?'采集':page.value==='history'?'记录':route.value==='settings/language'?'语言':route.value==='settings/diagnostics'?'诊断':'设备'));
const calibration=computed(()=>{
 const h=mapping.value?.statuses?.insight9||{};
 return devices.value.map(d=>{const s=d.role==='head'?h:mapping.value?.statuses?.[d.name]||{};
 const good=online.value&&s.online&&s.localized&&s.clock_ready&&s.state!=='error'&&(d.role==='head'?s.state==='mapping'&&mapping.value?.map_point_count>0:s.map_revision===h.map_revision&&s.tracking_mode!=='vio_only');
 return {name:d.name,label:roleLabel(d.role),good,text:t(!online.value||!s.online?'离线':good?'已定位':s.tracking_mode==='vio_only'?'仅本地 VIO':'等待定位')};});
});
function navigate(value){location.hash=value;}
function hashChanged(){route.value=location.hash.slice(1)||'capture';loadTasks().catch(e=>error.value=e.message);window.scrollTo(0,0);}
function back(){const p=route.value.split('/');navigate(p.length>2?p.slice(0,-1).join('/'):page.value);}
function visibility(){visible.value=!document.hidden;}
async function openTasks(){await loadTasks().catch(e=>error.value=e.message);creating.value=!tasks.value.length;dialog.value.showModal();}
async function selectTask(taskId){if(busy.value)return;if(await action(`/api/tasks/${encodeURIComponent(taskId)}/activate`))dialog.value.close();}
async function createTask(){if(busy.value||!name.value.trim()||!instruction.value.trim())return;
 const taskId=`task_${id()}`;
 if(await action('/api/tasks',{id:taskId,name:name.value.trim(),instruction:instruction.value.trim(),capture_profile:'dual_arm_umi'})){
  if(await action(`/api/tasks/${taskId}/activate`)){dialog.value.close();name.value='';instruction.value='';}}
}
async function diagnostics(){try{const value=await api('/api/diagnostics');value.mobile=status.value;value.client={user_agent:navigator.userAgent};const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='insight-diagnostics.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){error.value=e.message;}}
function stage(d){if(!status.value?.system?.camera_links?.some(p=>p.namespace===d.name))return t('等待 USB 连接');if(!clocks.value.devices?.[d.name]?.ok)return t('正在校时');if(status.value?.system?.managed_camera_startup&&!status.value.system.camera_startup?.[d.name])return t('正在恢复采集');if(!health.value[d.name]?.image_fresh)return t('等待图像');if(!health.value[d.name]?.vio_fresh)return t('等待 VIO');return t('已就绪');}
let observer;
onMounted(()=>{window.addEventListener('hashchange',hashChanged);document.addEventListener('visibilitychange',visibility);observer=new ResizeObserver(()=>shell.value?.style.setProperty('--footer-height',`${footer.value.getBoundingClientRect().height}px`));observer.observe(footer.value);});
onBeforeUnmount(()=>{window.removeEventListener('hashchange',hashChanged);document.removeEventListener('visibilitychange',visibility);observer?.disconnect();});
watch(language,()=>document.title='Insight Capture');
</script>
<template><div ref="shell" class="console-shell" :class="{'has-capture-dock':showDock,'compact-capture-dock':page!=='capture'}">
<header class="topbar"><button v-if="route.includes('/') && route!=='capture/trajectory'" class="icon-button" :aria-label="t('返回')" @click="back">‹</button><div class="topbar-title"><small>Insight Capture</small><h1>{{title}}</h1></div><span id="device-state" class="pill" :class="{online}"><i></i>{{t(online?'在线':status?'离线':'连接中')}}</span></header>
<main>
<section class="card readiness-card" :class="{'readiness-blocked':online&&!status?.preflight?.ok&&!active,'readiness-warning':!online||(status?.preflight?.ok&&status?.preflight?.warnings?.length)}">
<div class="readiness-title"><i class="state-mark"></i><strong>{{!online?t('正在连接设备'):active?stateLabel(status.state):t(status?.preflight?.ok?'可以开始录制':'等待采集就绪')}}</strong><button class="text-link" v-if="page!=='settings'" @click="navigate('settings')">{{t('查看状态')}} ›</button></div>
<div v-if="page==='capture'" class="health-summary"><span v-for="d in devices" :key="d.name" :class="{bad:!health[d.name]?.ready||!clocks.devices?.[d.name]?.ok}">{{roleLabel(d.role)}} · {{online?stage(d):t('离线')}}</span></div>
<p v-if="!online" class="section-description">{{t('网页断开不会停止设备上的录制。')}}</p>
<div v-if="page==='capture' && !active" class="issues" :hidden="!status?.preflight?.failures?.length && !status?.preflight?.warnings?.length"><p v-for="issue in status?.preflight?.failures" :key="issue.code" class="issue error">{{t(issue.message)}}</p><p v-for="issue in status?.preflight?.warnings" :key="issue.code" class="issue warning">{{t(issue.message)}}</p></div></section>
<p v-if="error||status?.operation?.error" role="alert" class="global-error">{{error||status.operation.error}}</p>
<p v-if="status?.state==='saving'" role="status" class="save-progress">{{t('正在保存，请保持设备供电。')}}</p>
<div v-show="page==='capture'">
<section class="card task-current"><div><small>{{t('当前任务')}}</small><strong id="task-name">{{task?.name||t('请选择任务')}}</strong></div><button id="switch-task" class="secondary compact" :disabled="busy||!online" @click="openTasks">{{t(task?'切换':'选择任务')}}</button></section>
<div class="capture-view-switch" role="group" :aria-label="t('采集视图')"><button :aria-pressed="route==='capture'" @click="navigate('capture')">{{t('画面')}}</button><button :aria-pressed="route==='capture/trajectory'" @click="navigate('capture/trajectory')">{{t('轨迹')}}</button></div>
<CameraPreview v-show="route!=='capture/trajectory'" :preview="status?.preview||{}" :online="online" :recording="active" :active="page==='capture'&&route!=='capture/trajectory'&&visible&&status?.state!=='playback'" @notify="error=$event"/>
<TrajectoryPanel v-if="route==='capture/trajectory'"/>
<p class="settings-note">{{t('准备、录制和保存时暂停预览，保存完成后自动恢复。')}}</p>
<section class="card calibration-card"><div class="section-title"><h2>{{t('校准与定位')}}</h2><button id="calibrate" class="secondary compact" :disabled="!online||!mapping||pending||['starting','saving','playback'].includes(status?.state)||!health[head?.name]?.ready" @click="calibrate">{{t('重新校准')}}</button></div>
<div v-for="item in calibration" :key="item.name" class="calibration-row"><span>{{item.label}}</span><strong :class="{bad:!item.good}">{{item.text}}</strong></div>
<p class="section-description">{{t('重新校准会重建当前公共地图。先开始录制再校准，可将校准过程一同保存；原始采集不要求已定位。')}}</p></section>
<section v-if="status?.state==='saved'" class="card save-result"><strong>{{t('保存完成')}}</strong><button class="secondary compact" @click="navigate('history')">{{t('查看记录')}}</button></section>
</div>
<RecordsPage v-if="page==='history'" :route="route" :tasks="tasks" :busy="busy" :online="online" :action="action" @navigate="navigate"/>
<div v-if="page==='settings'">
<template v-if="route==='settings'">
<section class="card device-hero"><span class="device-mark">I</span><h2>Insight Capture</h2><p class="section-description">Jetson · {{devices.map(d=>d.name.split('_')[0].toUpperCase()).join(' / ')}}</p></section>
<section class="card"><div class="section-title"><h2>{{t('连接与启动')}}</h2><span>{{t('运行时间')}} {{duration(status?.system?.uptime_s)}}</span></div><div v-for="d in devices" :key="d.name" class="device-row"><div><strong>{{roleLabel(d.role)}}</strong><small>{{status?.system?.camera_links?.find(p=>p.namespace===d.name)?.host||'—'}}</small></div><span>{{online?stage(d):t('离线')}}</span></div><p class="section-description">{{t('USB 连接 → 时钟就绪 → 图像 / VIO 就绪')}}</p><p class="section-description">{{t('当前访问地址')}}：{{endpoint}}</p></section>
<section class="card"><div class="section-title"><h2>{{t('存储')}}</h2><strong>{{size(status?.preflight?.storage?.free_bytes)}} {{t('可用')}}</strong></div><p :class="status?.storage?.using_fallback?'issue warning':'section-description'">{{t(status?.storage?.using_fallback?'录制盘未连接，当前使用备用存储。':'使用采集存储。')}}</p><p class="storage-path">{{status?.storage?.active_path||'—'}}</p></section>
<section class="card"><div class="section-title"><h2>{{t('时间同步')}}</h2><span :class="{bad:!clocks.ok}">{{t(clocks.ok?'已就绪':'测量中')}}</span></div><div v-for="d in devices" :key="d.name" class="device-row"><strong>{{roleLabel(d.role)}}</strong><span v-if="clocks.devices?.[d.name]?.offset_ms!=null">{{clocks.devices[d.name].offset_ms.toFixed(3)}} ms</span><span v-else>—</span></div><p class="section-description">{{t('相机相对 Jetson 的 NTP 偏差；不代表曝光同步精度。')}}</p><p class="section-description">{{t('两机误差保守上界')}}：{{clocks.pairwise_clock_bound_ms?.toFixed(2)||'—'}} / {{clocks.max_pairwise_clock_bound_ms||20}} ms</p></section>
<section class="card settings-links"><button class="settings-link" @click="navigate('settings/language')"><span>{{t('语言')}}</span><small>{{language==='en'?'English':'简体中文'}}</small><b>›</b></button><button class="settings-link" @click="navigate('settings/diagnostics')"><span>{{t('诊断')}}</span><small>{{t('设备与采集状态')}}</small><b>›</b></button></section>
</template>
<section v-else-if="route==='settings/language'" class="card"><h2>{{t('界面语言')}}</h2><div class="language-options"><button :aria-pressed="language==='zh'" @click="setLanguage('zh')">简体中文</button><button :aria-pressed="language==='en'" @click="setLanguage('en')">English</button></div></section>
<section v-else class="card"><h2>{{t('诊断')}}</h2><p class="section-description">{{t('保存连接、相机、校时、校准和录制状态，便于排查问题。')}}</p><button id="save-diagnostics" class="secondary" @click="diagnostics">{{t('保存诊断')}}</button><details><summary>{{t('当前状态')}}</summary><pre>{{JSON.stringify({state:status?.state,clocks,mapping: mapping?.statuses},null,2)}}</pre></details></section>
</div>
</main>
<footer ref="footer" class="console-footer"><section v-if="showDock" class="capture-dock" :class="{'dock-recording':status?.state==='recording','dock-compact':page!=='capture'}"><div class="dock-status"><span></span><div><strong>{{!active&&!status?.preflight?.ok?t('等待采集就绪'):stateLabel(status?.state)}}</strong><small>{{task?.name||t('请选择任务')}} · Take {{active && status?.state!=='starting' ? Math.max(1,(status?.task_status?.stats?.next_take_id||1)-1) : status?.task_status?.stats?.next_take_id||'—'}}</small></div><time>{{elapsed}}</time></div><div class="dock-actions"><button v-if="status?.state==='recording'" id="stop" class="danger" :disabled="pending||!online||status.recording_mode!=='capture'" @click="record('stop')">{{t('结束并保存')}}</button><button v-else id="start" class="primary" :disabled="busy||!online||!status?.preflight?.ok" @click="record('start')">{{t(status?.state==='starting'?'正在准备':status?.state==='saving'?'正在保存':'开始录制')}}</button></div></section>
<nav class="bottom-nav" :aria-label="t('主导航')"><button v-for="item in [{id:'capture',label:'采集'},{id:'history',label:'记录'},{id:'settings',label:'设备'}]" :key="item.id" :aria-current="page===item.id?'page':undefined" @click="navigate(item.id)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><template v-if="item.id==='capture'"><rect x="3" y="5" width="18" height="14" rx="4"/><circle cx="12" cy="12" r="3.5"/></template><template v-else-if="item.id==='history'"><path d="M4 7h6l2-3h8v16H4z"/><path d="M8 11h8M8 15h6"/></template><template v-else><rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4M10 6h4"/></template></svg><span>{{t(item.label)}}</span></button></nav></footer>
<dialog id="task-dialog" ref="dialog" aria-labelledby="task-dialog-title"><div class="section-title"><h2 id="task-dialog-title">{{t(creating?'新建任务':'选择任务')}}</h2><button class="icon-button" :aria-label="t('关闭')" @click="dialog.close()">×</button></div><template v-if="!creating"><label for="task-select">{{t('切换当前采集任务')}}</label><select id="task-select" :value="task?.task_id||''" :disabled="busy" @change="selectTask($event.target.value)"><option value="" disabled>{{t('请选择任务')}}</option><option v-for="item in tasks" :key="item.task_id" :value="item.task_id">{{item.name}}</option></select><button v-if="task" class="secondary" :disabled="busy" @click="action('/api/tasks/current/end').then(ok=>ok&&dialog.close())">{{t('结束当前任务')}}</button><button id="new-task" class="secondary" :disabled="busy" @click="creating=true">{{t('新建任务')}}</button></template><form v-else @submit.prevent="createTask"><label for="task-input">{{t('任务名称')}}</label><input id="task-input" v-model="name" required maxlength="80" :disabled="busy"><label for="task-instruction">{{t('任务说明')}}</label><textarea id="task-instruction" v-model="instruction" required maxlength="2000" :disabled="busy"></textarea><div class="dialog-actions"><button type="button" class="secondary" @click="dialog.close()">{{t('取消')}}</button><button class="primary" :disabled="busy">{{t('创建任务')}}</button></div></form></dialog>
</div></template>
