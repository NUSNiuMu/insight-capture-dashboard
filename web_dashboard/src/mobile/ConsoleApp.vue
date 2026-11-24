<script setup>
import { computed, ref, watch, nextTick, onMounted, onBeforeUnmount } from 'vue';
import CameraPreview from '../preview/components/CameraPreview.vue';
import { guardModalScroll } from '../modal-scroll.js';
import RecordsPage from './RecordsPage.vue';
import TrajectoryPanel from './TrajectoryPanel.vue';
import { useCapture, api, roleLabel, stateLabel, size, duration, takeDuration, deviceStage, deviceHint } from './useCapture.js';
import { t, language, setLanguage } from './i18n.js';
const {status,mapping,tasks,tasksError,taskDraft,online,connectionFailed,pending,recordPending,error,busy,action,record,createAndActivateTask,deleteTask,deleteTake,leaveTaskCreation,synchronizeTime,calibrate,loadTasks}=useCapture();
const endpoint=location.host;
const selectedTask=ref(''),openedTaskId=ref(''),taskDialogKey=ref(0),fieldErrors=ref({}),fieldAnnouncement=ref('');
let fieldFrame,announceTimer,taskOpenGeneration=0,releaseModalScroll;
const editedFields=new WeakSet(),blurredFields=new WeakSet(),submittedForms=new WeakSet();
function fieldMessage(field){return field.id==='task-select'?'请选择任务。':field.id==='task-input'?'请输入任务名称。':'请输入任务说明。';}
function syncField(event){
 const field=event.target;if(!field.matches('input,textarea,select'))return;
 if(event.type==='input'||event.type==='change')editedFields.add(field);
 if(event.type==='focusout')blurredFields.add(field);
 if(event.type==='invalid'&&field.form)submittedForms.add(field.form);
 field.setCustomValidity(field.value&&!field.value.trim()?t(fieldMessage(field)):'');
 const announce=event.type==='focusout'||event.type==='change';
 // Native user-invalid is set after event delivery; sample after that update.
 cancelAnimationFrame(fieldFrame);
 fieldFrame=requestAnimationFrame(()=>{
  const controls=dialog.value?.querySelectorAll('input,textarea,select')||[];
  const previous=fieldErrors.value;const errors={};
  for(const control of controls){
   const invalid=CSS.supports('selector(:user-invalid)')?control.matches(':user-invalid'):
    !control.validity.valid&&(submittedForms.has(control.form)||(blurredFields.has(control)&&editedFields.has(control)));
   if(invalid)errors[control.id]=fieldMessage(control);
  }
  fieldErrors.value=errors;
  if(!errors[field.id]){clearTimeout(announceTimer);fieldAnnouncement.value='';}
  else if(announce&&!previous[field.id]){
   clearTimeout(announceTimer);fieldAnnouncement.value='';
   announceTimer=setTimeout(()=>fieldAnnouncement.value=errors[field.id],500);
  }
 });
}

const previewError=ref('');
const route=ref(location.hash.slice(1)||'capture'),visible=ref(!document.hidden),dialog=ref(null),creating=ref(Boolean(taskDraft.value)),name=ref(taskDraft.value?.name||''),instruction=ref(taskDraft.value?.instruction||''),shell=ref(null),footer=ref(null);
const page=computed(()=>route.value.split('/')[0]);
const savedTake=computed(()=>status.value?.state==='saved' && status.value.current_take?.state==='complete' && status.value.current_take.bag_path===status.value.output_path ? status.value.current_take : null);
const uiState=computed(()=>recordPending.value==='start'?'starting':recordPending.value==='stop'?'saving':status.value?.state==='saved'&&!savedTake.value?'idle':status.value?.state);
const active=computed(()=>['starting','recording','saving'].includes(uiState.value));
const liveAlerts=computed(()=>['recording','saving'].includes(uiState.value)?status.value?.live_alerts||[]:[]);
const criticalAlert=computed(()=>liveAlerts.value.some(item=>item.severity==='critical'));
const captureTake=computed(()=>active.value && (uiState.value!=='starting'||['reserved','starting','recording'].includes(status.value?.current_take?.state)) ? status.value?.current_take?.take_id : null);
const captureError=computed(()=>uiState.value==='error'?status.value?.error:'');
watch(active,value=>{if(value)previewError.value='';});
const showDock=computed(()=>page.value==='capture'||active.value);
const task=computed(()=>status.value?.task_status?.active?status.value.task_status.task:null);
const elapsed=computed(()=>takeDuration(status.value?.current_take,status.value));
const health=computed(()=>status.value?.preflight?.camera_health||{});
const clocks=computed(()=>status.value?.preflight?.clocks||{});
const devices=computed(()=>[...(status.value?.devices||[])].sort((a,b)=>({head:0,left_hand:1,right_hand:2}[a.role]-{head:0,left_hand:1,right_hand:2}[b.role])));
const attentionDevice=computed(()=>online.value&&!active.value&&task.value?devices.value.find(d=>stage(d)!==t('已就绪')):null);
const readinessIssues=computed(()=>[...(status.value?.preflight?.failures||[]),...(status.value?.preflight?.warnings||[])]);
const readinessSummary=computed(()=>t(readinessIssues.value.find(issue=>issue.code!=='task_inactive')?.message||''));
const offline=computed(()=>!online.value&&connectionFailed.value);
const captureNeedsAttention=computed(()=>online.value&&!active.value&&(attentionDevice.value||readinessSummary.value||(task.value&&!status.value?.preflight?.ok)||uiState.value==='playback'));
const dockLabel=computed(()=>!online.value?t('连接中，录制状态待确认'):stateLabel(uiState.value));
const head=computed(()=>devices.value.find(d=>d.role==='head'));
const title=computed(()=>t(page.value==='capture'?'采集':page.value==='history'?'记录':route.value==='settings/language'?'语言':route.value==='settings/diagnostics'?'诊断':'设备'));
const calibration=computed(()=>{
 const h=mapping.value?.statuses?.insight9||{};
 return devices.value.map(d=>{const s=d.role==='head'?h:mapping.value?.statuses?.[d.name]||{};
 const good=online.value&&s.online&&s.localized&&s.clock_ready&&s.state!=='error'&&(d.role==='head'?s.state==='mapping'&&mapping.value?.map_point_count>0:s.map_revision===h.map_revision&&s.tracking_mode!=='vio_only');
 return {name:d.name,label:roleLabel(d.role),good,text:t(!online.value||!s.online?'离线':good?'已定位':s.tracking_mode==='vio_only'?'仅本地 VIO':'等待定位')};});
});
const viewPositions=new Map();
let pendingView,viewGeneration=0,previousScrollRestoration;
function historyLink(value){return [...(shell.value?.querySelectorAll('[data-history-route]')||[])].find(node=>node.dataset.historyRoute===value);}
function rememberView(destination){
 // A cancelled loading view has not yet replaced its saved content position.
 if(pendingView?.saved&&page.value==='history')return;
 const focus=destination||document.activeElement?.dataset?.historyRoute;
 const link=focus&&historyLink(focus);
 // Section navigation has no originating Take control. Keep a visible record
 // as the scroll anchor while retaining the section heading as the focus target.
 const target=!link&&page.value==='history'?document.elementFromPoint?.(window.innerWidth/2,(footer.value?.getBoundingClientRect().top??window.innerHeight)/2):null;
 const anchor=link||target?.closest('[data-history-route]')||target?.closest('.record-list > li')?.querySelector('[data-history-route]');
 viewPositions.delete(route.value);
 viewPositions.set(route.value,{x:window.scrollX||0,y:window.scrollY||0,focus:link?focus:null,anchor:anchor?.dataset.historyRoute,top:anchor?.getBoundingClientRect().top});
 if(viewPositions.size>64)viewPositions.delete(viewPositions.keys().next().value);
}
function pagePosition(){
 try{const saved=JSON.parse(sessionStorage.getItem('insight-capture.view-position'));return saved?.route===route.value?saved:null;}catch{return null;}
}
function savePagePosition(){
 rememberView();
 try{sessionStorage.setItem('insight-capture.view-position',JSON.stringify({...(pendingView?.saved||viewPositions.get(route.value)),route:route.value}));}catch{ /* In-page return still works without browser storage. */ }
}
async function restoreView(recordsReady=false){
 const plan=pendingView;
 if(!plan)return;
 await nextTick();
 if(pendingView!==plan||plan.generation!==viewGeneration)return;
 // A remounted history list may still be loading; do not clamp its saved
 // position to the temporary empty page or steal focus again on every poll.
 if(plan.saved&&page.value==='history'&&!recordsReady){shell.value?.querySelector('h1')?.focus({preventScroll:true});return;}
 pendingView=null;
 const saved=plan.saved,origin=saved?.focus&&historyLink(saved.focus),link=historyLink(saved?.anchor||saved?.focus);
 window.scrollTo(saved?.x||0,saved?.y||0);
 (origin||shell.value?.querySelector('h1'))?.focus({preventScroll:true});
 // Offscreen rows can have estimated heights. Restore the actual Take's
 // viewport position as well as the scroll offset once its layout is known.
 if(link&&Number.isFinite(saved.top)){
  const align=()=>window.scrollBy(0,link.getBoundingClientRect().top-saved.top);
  align();
  // Deferred rows can reveal their real heights on the next painted frame.
  // A later route or user gesture owns the viewport instead of this restore.
  const input=new AbortController();let interrupted=false;
  for(const type of ['pointerdown','wheel','keydown'])document.addEventListener(type,()=>{interrupted=true;},{capture:true,passive:true,signal:input.signal});
  try{
   await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
   if(!interrupted&&!document.hidden&&plan.generation===viewGeneration&&link.isConnected)align();
  }finally{input.abort();}
 }
}
function navigate(value,event){
 if(event){
  if(event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
  event.preventDefault();
 }
 if(value===route.value)return;
 rememberView(value);
 history.pushState({...history.state,insightCapture:{from:route.value}},'',`#${value}`);
 return hashChanged(true);
}
function hashChanged(remembered=false){
 if(remembered!==true)rememberView();
 taskOpenGeneration++;dialog.value?.close();
 const value=location.hash.slice(1)||'capture';
 pendingView={generation:++viewGeneration,saved:viewPositions.get(value)};
 route.value=value;
 return restoreView();
}
function back(){
 const p=route.value.split('/'),parent=p.length>2?p.slice(0,-1).join('/'):page.value;
 if(history.state?.insightCapture?.from===parent)history.back();
 else navigate(parent);
}
function visibility(){visible.value=!document.hidden;if(!visible.value)taskOpenGeneration++;}
function canOpenTasks(generation){return generation===taskOpenGeneration&&['capture','history'].includes(page.value)&&visible.value&&!busy.value&&online.value;}
function showTaskDialog(){
 dialog.value.showModal();
 releaseModalScroll?.();
 releaseModalScroll=guardModalScroll(dialog.value,{fitViewport:true});
}
async function openTasks(forceCreate=false){
 const generation=++taskOpenGeneration;
 await loadTasks().catch(()=>{});
 if(!canOpenTasks(generation))return;
 selectedTask.value=openedTaskId.value=task.value?.task_id||'';
 creating.value=forceCreate===true||Boolean(taskDraft.value)||!tasks.value.length;
 taskDialogKey.value++;fieldErrors.value={};fieldAnnouncement.value='';
 cancelAnimationFrame(fieldFrame);clearTimeout(announceTimer);
 await nextTick();
 if(canOpenTasks(generation))showTaskDialog();
}
async function selectTask(taskId){if(busy.value)return;if(await action(`/api/tasks/${encodeURIComponent(taskId)}/activate`,{},(e,current)=>!e.status&&current?.task_status?.active&&current.task_status.task?.task_id===taskId))dialog.value.close();}
async function endTask(){const taskId=openedTaskId.value;if(busy.value)return;if(await action('/api/tasks/current/end',{task_id:taskId},(e,current)=>!e.status&&current?.task_status?.active===false&&current.task_status.last_task_id===taskId))dialog.value.close();}
async function createTask(){if(busy.value||!name.value.trim()||!instruction.value.trim())return;
 if(await createAndActivateTask(name.value,instruction.value)){dialog.value.close();name.value='';instruction.value='';}
}
function leaveCreation(){if(leaveTaskCreation()){name.value='';instruction.value='';creating.value=false;loadTasks().catch(()=>{});}}
async function diagnostics(){try{const value=await api('/api/diagnostics');value.mobile=status.value;value.client={user_agent:navigator.userAgent};const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='insight-diagnostics.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){error.value=e.message;}}
function stage(d){return deviceStage(d,status.value);}
function alertText(item){
 const details=item.details||{};
 if(item.code.startsWith('localization:')){
  const device=devices.value.find(d=>d.name===details.service||(details.service==='insight9'&&d.role==='head'));
  if(device)return t('{0}定位异常，本条已标记复检。',roleLabel(device.role));
 }
 if(item.code.startsWith('camera_stale:')){
  const device=devices.value.find(d=>d.name===details.camera);
  if(device)return t('{0}图像中断，本条已记录异常。',roleLabel(device.role));
  const camera=status.value?.preview_cameras?.find(c=>c.name===details.camera);
  if(camera)return t('{0}图像中断，本条已记录异常。',t(camera.label));
 }
 return t(item.message);
}
function showLiveAlerts(){const node=document.getElementById('live-alerts');node?.scrollIntoView({block:'start'});node?.focus({preventScroll:true});}
let observer,layoutFrame;
function updateLayout(){
 cancelAnimationFrame(layoutFrame);
 layoutFrame=requestAnimationFrame(()=>{
  const footerHeight=footer.value.getBoundingClientRect().height;
  shell.value.style.setProperty('--footer-height',`${footerHeight}px`);
 });
}
onMounted(()=>{previousScrollRestoration=history.scrollRestoration;history.scrollRestoration='manual';const saved=pagePosition();if(saved){pendingView={generation:++viewGeneration,saved};restoreView();}window.addEventListener('pagehide',savePagePosition);if(taskDraft.value)showTaskDialog();window.addEventListener('hashchange',hashChanged);document.addEventListener('visibilitychange',visibility);observer=new ResizeObserver(updateLayout);observer.observe(footer.value);});
onBeforeUnmount(()=>{releaseModalScroll?.();viewGeneration++;pendingView=null;history.scrollRestoration=previousScrollRestoration;window.removeEventListener('pagehide',savePagePosition);taskOpenGeneration++;window.removeEventListener('hashchange',hashChanged);document.removeEventListener('visibilitychange',visibility);observer?.disconnect();cancelAnimationFrame(layoutFrame);cancelAnimationFrame(fieldFrame);clearTimeout(announceTimer);});
watch(page,updateLayout,{flush:'post'});
watch(title,value=>document.title=`${value} · Insight Capture`,{immediate:true});
watch(creating,async value=>{if(!value)selectedTask.value=openedTaskId.value=task.value?.task_id||'';fieldErrors.value={};fieldAnnouncement.value='';clearTimeout(announceTimer);await nextTick();(dialog.value?.querySelector('input:not(:disabled),select:not(:disabled)')||dialog.value?.querySelector('button'))?.focus();});
</script>
<template><div ref="shell" class="console-shell" :class="{'has-capture-dock':showDock,'active-capture-dock':active}">
<a class="skip-navigation" href="#primary-navigation" @click.prevent="footer.querySelector('nav').focus()">{{t('跳到主导航')}}</a><header class="topbar"><button v-if="route.includes('/') && route!=='capture/trajectory'" class="icon-button" :aria-label="t('返回')" @click="back">‹</button><h1 tabindex="-1">{{title}}</h1><span id="device-state" class="pill" :class="{online}"><i></i>{{t(online?'在线':connectionFailed?'离线':'连接中')}}</span></header>
<main>
<div class="capture-intro">
<section v-if="offline||(page==='capture'&&captureNeedsAttention)" class="card readiness-card" :class="offline||status?.preflight?.ok?'readiness-warning':'readiness-blocked'">
<div class="readiness-title">
<span class="readiness-mark" aria-hidden="true"></span>
<strong role="status">
<template v-if="offline">{{t('已离线')}}</template>
<template v-else-if="attentionDevice">{{roleLabel(attentionDevice.role)}}<small>{{stage(attentionDevice)}}</small></template>
<template v-else>{{uiState==='playback'?stateLabel(uiState):readinessSummary||t('等待采集就绪')}}</template>
</strong>
<a v-if="page!=='settings'" id="view-device-status" href="#settings" class="route-link text-link" @click="navigate('settings',$event)">{{t(!offline&&readinessIssues.length?'查看状态（{0}）':'查看状态',readinessIssues.length)}} <span aria-hidden="true">›</span></a>
</div>
<template v-if="offline">
<p class="section-description">{{t('网页断开不会停止设备上的录制。')}}</p>
<p class="section-description">{{t('请检查手机是否切换了 Wi-Fi；使用采集热点时，系统提示无法上网也请选择保持连接。')}}</p>
</template>
</section>
<p v-if="error||captureError||status?.operation?.error||(page==='capture'&&!active&&previewError)" role="alert" class="global-error">{{t(error||captureError||status?.operation?.error||previewError)}}</p>
<p v-if="status?.state==='saving'" role="status" class="save-progress">{{t('正在保存，请保持设备供电。')}}</p>
<section v-if="liveAlerts.length" id="live-alerts" class="card live-alerts" :class="{'has-critical':criticalAlert}" tabindex="-1" role="status" aria-live="polite"><h2>{{t('采集提醒')}}</h2><p v-for="item in liveAlerts" :key="item.code" class="issue" :class="item.severity==='critical'?'error':'warning'">{{alertText(item)}}</p><p v-if="!online" class="section-description">{{t('连接已断开，以下提醒来自最后一次设备状态。')}}</p></section>
<div v-show="page==='capture'" class="capture-controls">
<button id="switch-task" type="button" class="card capture-task" aria-haspopup="dialog" :title="task?.name" :aria-label="task?t('切换任务：{0}',task.name):undefined" :disabled="busy||!online" @click="openTasks">
<span id="task-name">{{task?.name||t('选择任务')}}</span><span aria-hidden="true">›</span>
</button>
<div class="capture-view-switch" role="group" :aria-label="t('采集视图')"><button :aria-pressed="route==='capture'" @click="navigate('capture')">{{t('画面')}}</button><button :aria-pressed="route==='capture/trajectory'" @click="navigate('capture/trajectory')">{{t('轨迹')}}</button></div>
</div>
</div>
<div v-show="page==='capture'">
<section v-if="savedTake" class="card save-result"><strong role="status">Take {{savedTake.take_id}} · {{t('保存完成')}}</strong><a class="route-link secondary compact" :href="`#history/${savedTake.task_id}/${savedTake.take_id}`" @click="navigate(`history/${savedTake.task_id}/${savedTake.take_id}`,$event)">{{t('查看记录')}}</a></section>
<CameraPreview v-show="route!=='capture/trajectory'" :preview="status?.preview||{}" :online="online" :recording="active" :active="page==='capture'&&route!=='capture/trajectory'&&visible&&status?.state!=='playback'" @notify="previewError=$event" @recovered="previewError=''"/>
<TrajectoryPanel v-if="route==='capture/trajectory'"/>
<section class="card calibration-card"><div class="section-title"><h2>{{t('校准与定位')}}</h2><button id="calibrate" class="secondary compact" :disabled="!online||!mapping||pending||['starting','saving','playback'].includes(status?.state)||!health[head?.name]?.ready" @click="calibrate">{{t('重新校准')}}</button></div>
<div v-for="item in calibration" :key="item.name" class="calibration-row"><span>{{item.label}}</span><strong :class="{bad:!item.good}">{{item.text}}</strong></div>
<p class="section-description">{{t('重新校准会重建公共地图。')}}</p><details class="secondary-details"><summary>{{t('校准说明')}}</summary><p class="section-description">{{t('先开始录制再校准，可将校准过程一同保存。原始采集不要求已定位。')}}</p></details></section>
</div>
<RecordsPage v-if="page==='history'" :route="route" :tasks="tasks" :tasks-error="tasksError" :status="status" :busy="busy" :online="online" :action="action" :delete-task="deleteTask" :delete-take="deleteTake" :action-error="error" @create-task="openTasks(true)" @navigate="navigate" @ready="restoreView(true)" @refresh-tasks="loadTasks().catch(()=>{})"/>
<div v-if="page==='settings'">
<template v-if="route==='settings'">
<section class="card device-cameras"><h2>Jetson</h2>
<div v-for="d in devices" :key="d.name" class="device-status-item">
<div class="device-row"><strong>{{roleLabel(d.role)}}</strong><span>{{online?stage(d):t('离线')}}</span></div>
<p v-if="online&&deviceHint(d,status)" class="device-hint">{{deviceHint(d,status)}}</p>
</div>
<details class="secondary-details"><summary>{{t('连接详情')}}</summary><dl class="device-facts">
<div><dt>{{t('当前访问地址')}}</dt><dd>{{endpoint}}</dd></div>
<div><dt>{{t('运行时间')}}</dt><dd>{{status?.system?.uptime_s==null?'—':duration(status.system.uptime_s)}}</dd></div>
<div v-for="d in devices" :key="d.name"><dt>{{roleLabel(d.role)}}</dt><dd>{{status?.system?.camera_links?.find(p=>p.namespace===d.name)?.host||'—'}}</dd></div>
</dl></details></section>
<section v-if="!active&&readinessIssues.length" id="readiness-details" class="card"><h2>{{t('采集前检查')}}</h2><p v-if="!online" class="section-description">{{t('连接已断开，以下提醒来自最后一次设备状态。')}}</p><p v-for="(issue,index) in status?.preflight?.failures" :key="`failure-${index}`" class="issue error">{{t(issue.message)}}</p><p v-for="(issue,index) in status?.preflight?.warnings" :key="`warning-${index}`" class="issue warning">{{t(issue.message)}}</p></section>
<section class="card"><div class="section-title"><h2>{{t('存储')}}</h2><strong>{{size(status?.preflight?.storage?.free_bytes)}} {{t('可用')}}</strong></div><details class="secondary-details"><summary>{{t('数据位置')}}</summary><p class="storage-path">{{status?.storage?.active_path||'—'}}</p></details></section>
<section class="card"><div class="section-title"><h2>{{t('时间同步')}}</h2><button id="resync-time" type="button" class="secondary compact" :disabled="busy||!online||status?.state==='playback'" @click="synchronizeTime">{{t('重新校时')}}</button></div>
<p class="section-description sync-state" :class="{bad:!clocks.ok}" role="status">{{t(status?.time_sync?.state==='syncing'?'正在校时':clocks.ok?'已就绪':'测量中')}}</p>
<p v-if="status?.time_sync?.state==='error'" class="issue error" role="alert">{{t('校时未完成，请重试。')}}</p>
<details class="secondary-details"><summary>{{t('NTP 偏差')}}</summary>
<div v-for="d in devices" :key="d.name" class="device-row"><strong>{{roleLabel(d.role)}}</strong><span v-if="clocks.devices?.[d.name]?.offset_ms!=null">{{clocks.devices[d.name].offset_ms.toFixed(3)}} ms</span><span v-else>—</span></div>
<p class="section-description">{{t('相机相对 Jetson 的 NTP 偏差；不代表曝光同步精度。')}}</p><p class="section-description">{{t('两机误差保守上界')}}：{{clocks.pairwise_clock_bound_ms?.toFixed(2)||'—'}} / {{clocks.max_pairwise_clock_bound_ms||20}} ms</p></details></section>
<section class="card settings-links"><a href="#settings/language" class="route-link settings-link" @click="navigate('settings/language',$event)"><span>{{t('语言')}}</span><small>{{language==='en'?'English':'简体中文'}}</small><b aria-hidden="true">›</b></a><a href="#settings/diagnostics" class="route-link settings-link" @click="navigate('settings/diagnostics',$event)"><span>{{t('诊断')}}</span><b aria-hidden="true">›</b></a></section>
</template>
<section v-else-if="route==='settings/language'" class="card" :aria-label="t('界面语言')"><div class="language-options"><button :aria-pressed="language==='zh'" @click="setLanguage('zh')">简体中文</button><button :aria-pressed="language==='en'" @click="setLanguage('en')">English</button></div></section>
<section v-else class="card" :aria-label="t('诊断')"><button id="save-diagnostics" class="secondary" @click="diagnostics">{{t('保存诊断')}}</button><details><summary>{{t('当前状态')}}</summary><pre tabindex="0" role="region" :aria-label="t('当前状态')"><code>{{JSON.stringify({state:status?.state,clocks,mapping: mapping?.statuses},null,2)}}</code></pre></details></section>
</div>
</main>
<footer ref="footer" class="console-footer">
<button v-if="liveAlerts.length" id="show-live-alerts" class="live-alert-link" :class="{'has-critical':criticalAlert}" @click="showLiveAlerts">{{t('采集提醒（{0}） · 查看',liveAlerts.length)}}</button>
<section v-if="showDock" class="capture-dock" :class="{'dock-recording':status?.state==='recording','dock-active':active}">
<div v-if="active" class="dock-status">
<strong role="status">{{dockLabel}}</strong>
<span v-if="page!=='capture'&&task" class="dock-task">{{task.name}}</span>
<span v-if="captureTake" class="dock-take">Take {{captureTake}}</span>
<time v-if="uiState!=='starting'">{{online?elapsed:'—'}}</time>
</div>
<div class="dock-actions"><button v-if="status?.state==='recording'" id="stop" class="danger" :disabled="pending||!online||status.recording_mode!=='capture'" @click="record('stop')">{{t('结束并保存')}}</button><button v-else id="start" class="primary" :disabled="busy||!online||!status?.preflight?.ok" @click="record('start')">{{t(uiState==='starting'?'正在准备':uiState==='saving'?'正在保存':'开始录制')}}</button></div>
</section>
<nav id="primary-navigation" tabindex="-1" class="bottom-nav" :aria-label="t('主导航')"><a class="route-link" :href="`#${item.id}`" v-for="item in [{id:'capture',label:'采集'},{id:'history',label:'记录'},{id:'settings',label:'设备'}]" :key="item.id" :aria-current="page===item.id?'page':undefined" @click="navigate(item.id,$event)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><template v-if="item.id==='capture'"><rect x="3" y="5" width="18" height="14" rx="4"/><circle cx="12" cy="12" r="3.5"/></template><template v-else-if="item.id==='history'"><path d="M4 7h6l2-3h8v16H4z"/><path d="M8 11h8M8 15h6"/></template><template v-else><rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4M10 6h4"/></template></svg><span>{{t(item.label)}}</span></a></nav></footer>
<dialog id="task-dialog" ref="dialog" aria-labelledby="task-dialog-title"><div class="section-title"><h2 id="task-dialog-title">{{t(creating?'新建任务':'选择任务')}}</h2><button class="icon-button" :aria-label="t('关闭')" @click="dialog.close()">×</button></div><p v-if="tasksError" class="issue warning">{{tasksError}}</p><p v-if="error" class="issue error" role="alert">{{error}}</p><template v-if="!creating"><form :key="`select-${taskDialogKey}`" @submit.prevent="selectTask(selectedTask)" @input="syncField" @change="syncField" @focusout="syncField" @invalid.capture="syncField"><label for="task-select">{{t('切换当前采集任务')}}</label><select id="task-select" name="task" v-model="selectedTask" :aria-invalid="!!fieldErrors['task-select']" :aria-describedby="fieldErrors['task-select']?'task-select-error':undefined" required :disabled="busy"><option value="" disabled>{{t('请选择任务')}}</option><option v-for="item in tasks" :key="item.task_id" :value="item.task_id">{{item.name}}</option></select><p v-if="fieldErrors['task-select']" id="task-select-error" class="field-error">{{t(fieldErrors['task-select'])}}</p><button id="activate-task" type="submit" class="primary" :disabled="busy||!online">{{t('切换')}}</button></form><button v-if="task" class="secondary" :disabled="busy" @click="endTask">{{t('结束当前任务')}}</button><button id="new-task" class="secondary" :disabled="busy" @click="creating=true">{{t(taskDraft?'继续未完成任务':'新建任务')}}</button></template><form v-else :key="`create-${taskDialogKey}`" @submit.prevent="createTask" @change="syncField" @input="syncField" @focusout="syncField" @invalid.capture="syncField"><button v-if="tasks.length" type="button" class="secondary compact" :disabled="pending" @click="creating=false">{{t('返回任务列表')}}</button><p v-if="taskDraft" class="section-description">{{t('任务操作尚未完成。重试会继续同一任务，不会重复新建。')}}</p><template v-if="taskDraft"><button type="button" class="secondary compact" :disabled="pending" @click="leaveCreation">{{t('结束创建流程')}}</button><p class="section-description">{{t('已创建的任务仍保留在任务列表。')}}</p></template><label for="task-input">{{t('任务名称')}}</label><input id="task-input" name="name" v-model="name" required maxlength="80" :aria-invalid="!!fieldErrors['task-input']" :aria-describedby="fieldErrors['task-input']?'task-input-error':undefined" :disabled="busy||!!taskDraft"><p v-if="fieldErrors['task-input']" id="task-input-error" class="field-error">{{t(fieldErrors['task-input'])}}</p><label for="task-instruction">{{t('任务说明')}}</label><textarea id="task-instruction" name="instruction" v-model="instruction" required maxlength="2000" :aria-invalid="!!fieldErrors['task-instruction']" :aria-describedby="fieldErrors['task-instruction']?'task-instruction-error':undefined" :disabled="busy||!!taskDraft"></textarea><p v-if="fieldErrors['task-instruction']" id="task-instruction-error" class="field-error">{{t(fieldErrors['task-instruction'])}}</p><div class="dialog-actions"><button type="button" class="secondary" @click="dialog.close()">{{t(taskDraft?'稍后继续':'取消')}}</button><button class="primary" :disabled="busy||!online">{{t(taskDraft?.created?'继续启用任务':taskDraft?'重试创建任务':'创建任务')}}</button></div></form><p class="visually-hidden" aria-live="polite">{{t(fieldAnnouncement)}}</p></dialog>
</div></template>
