<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue';
import { api, takeDuration, takeDisplayState, roleLabel, duration } from './useCapture.js';
import { t, language } from './i18n.js';
import { guardModalScroll } from '../modal-scroll.js';
const props=defineProps({route:String,tasks:Array,tasksError:String,status:Object,busy:Boolean,online:Boolean,action:Function,deleteTask:Function,deleteTake:Function,actionError:String});
const emit=defineEmits(['navigate','refresh-tasks','ready','create-task']);
const recordSet=ref(null),error=ref(''),loading=ref(false),transfers=ref(null);
const deleteDialog=ref(null),deleteTarget=ref(null),deleting=ref(false),deleteError=ref(''),notice=ref(''),createButton=ref(null),recordsHeading=ref(null);
let releaseDeleteScroll;
async function confirmDelete(task,take=null){
 if(props.busy||!props.online||deleting.value)return;
 const origin=props.route;
 deleteTarget.value={task:{...task,stats:{...task.stats}},take:take?{take_id:take.take_id,bag_name:take.bag_name}:null,origin};
 deleteError.value='';notice.value='';
 await nextTick();
 if(!alive||props.route!==origin)return;
 deleteDialog.value.showModal();
 releaseDeleteScroll=guardModalScroll(deleteDialog.value,{fitViewport:true});
}
function closeDelete(){releaseDeleteScroll?.();releaseDeleteScroll=null;}
async function removeSelected(){
 if(deleting.value||props.busy||!props.online)return;
 const {task,take,origin}=deleteTarget.value;
 const rowIndex=take?renderedTakes.value.findIndex(item=>item.take_id===take.take_id):props.tasks.findIndex(item=>item.task_id===task.task_id);
 deleting.value=true;deleteError.value='';
 try{
  const ok=take?await props.deleteTake(task.task_id,take):await props.deleteTask(task);
  if(!alive)return;
  if(ok){
   deleteDialog.value.close();deleting.value=false;
   notice.value=take?t('已删除 Take {0}。',take.take_id):t('已删除任务“{0}”。',task.name);
   if(take&&props.route===origin){
    generation++;
    recordSet.value={...recordSet.value,takes:recordSet.value.takes.filter(item=>item.take_id!==take.take_id)};
    if(origin.split('/')[2])emit('navigate',`history/${task.task_id}`);
    else{
     await nextTick();
     if(alive&&props.route===origin){
      const buttons=document.querySelectorAll('.take-delete');
      (buttons[Math.min(rowIndex,buttons.length-1)]||recordsHeading.value)?.focus({preventScroll:true});
     }
    }
    refreshRecords();
   }else if(!take&&props.route===origin){
    await nextTick();
    if(alive&&props.route===origin){
     const buttons=document.querySelectorAll('.task-record-row .task-delete');
     (buttons[Math.min(rowIndex,buttons.length-1)]||createButton.value)?.focus({preventScroll:true});
    }
   }
   emit('refresh-tasks');
  }else{
   deleteError.value=t(take?'录制未删除，请查看提示后重试。':'任务未删除，请查看提示后重试。');
   emit('refresh-tasks');
  }
 }finally{if(alive)deleting.value=false;}
}
watch(()=>props.route,()=>{if(deleteDialog.value?.open)deleteDialog.value.close();});
const takesPerFrame=64,renderLimit=ref(0);
let generation=0,transfersGeneration=0,renderGeneration=0,renderFrame,timer,alive=true;
const selectedTakes=computed(()=>{
 if(!props.route.includes('/'))return [];
 const takeId=props.route.split('/')[2];
 return (recordSet.value?.takes||[]).filter(take=>!takeId||String(take.take_id)===takeId)
  .map(take=>({...take,state:takeDisplayState(take,props.status)}));
});
const renderedTakes=computed(()=>selectedTakes.value.slice(0,renderLimit.value));
function recordsReady(){
 if(!props.route.includes('/')||(recordSet.value&&renderLimit.value>=selectedTakes.value.length))emit('ready');
}
watch([()=>props.route,()=>selectedTakes.value.length],([route,length],[previousRoute]=[])=>{
 const gen=++renderGeneration;
 if(renderFrame!==undefined)cancelAnimationFrame(renderFrame);
 renderLimit.value=Math.min(length,route===previousRoute?Math.max(takesPerFrame,renderLimit.value):takesPerFrame);
 const ready=()=>nextTick().then(()=>{if(alive&&gen===renderGeneration)recordsReady();});
 const more=()=>{
  if(!alive||gen!==renderGeneration)return;
  renderLimit.value=Math.min(length,renderLimit.value+takesPerFrame);
  if(renderLimit.value<length)renderFrame=requestAnimationFrame(more);
  else ready();
 };
 if(renderLimit.value<length)renderFrame=requestAnimationFrame(more);
 else ready();
},{immediate:true});
async function refreshRecords(clear=false){
 const taskId=props.route.split('/')[1],gen=++generation;
 if(clear){recordSet.value=null;error.value='';}
 if(!taskId){loading.value=false;return;}
 if(clear)loading.value=true;
 try{
  const value=await api(`/api/tasks/${encodeURIComponent(taskId)}/recordings`);
  if(alive&&gen===generation){recordSet.value=value;error.value='';}
 }catch(e){if(alive&&gen===generation)error.value=e.message;}
 finally{if(alive&&gen===generation){loading.value=false;if(recordSet.value)recordsReady();}}
}
watch(()=>props.route,(route,previous)=>{
 const changed=route.split('/')[1]!==previous?.split('/')[1];
 refreshRecords(changed);
 if(previous&&!route.includes('/'))emit('refresh-tasks');
 if(!route.includes('/')||(!changed&&recordSet.value))recordsReady();
},{immediate:true});
watch(()=>props.tasks,()=>{if(!props.route.includes('/'))emit('ready');});
watch([()=>props.status?.state,()=>props.status?.output_path,()=>props.online],()=>{
 if(props.online)refreshRecords();
});
async function refreshTransfers(){
 const gen=++transfersGeneration;
 try{
  const value=await api('/api/postprocessing/recordings');
  if(alive&&gen===transfersGeneration)transfers.value=value;
 }catch{ /* Keep the last list while polling retries a transient read. */ }
}
async function refresh(){
 if(props.online&&!document.hidden){
  if(props.route.includes('/'))await refreshRecords();
  else emit('refresh-tasks');
  await refreshTransfers();
 }
 if(alive)timer=setTimeout(refresh,5000);
}
function visibility(){if(!document.hidden&&props.online)refreshRecords();}
onMounted(()=>{refresh();document.addEventListener('visibilitychange',visibility);});
onBeforeUnmount(()=>{alive=false;releaseDeleteScroll?.();generation++;renderGeneration++;if(renderFrame!==undefined)cancelAnimationFrame(renderFrame);clearTimeout(timer);document.removeEventListener('visibilitychange',visibility);});
async function merge(session_id){if(await props.action('/api/postprocessing/merge',{session_id})){await refreshTransfers();refreshRecords();}}
const date=value=>value?new Date(value*1000).toLocaleString(): '—';
const takeState=s=>t({complete:'已保存',saved:'等待传输',finalization_failed:'保存异常',recording:'正在录制',finalizing:'正在保存',reserved:'正在准备',starting:'正在准备',start_failed:'录制失败',failed:'录制失败'}[s]||s);
const quality=take=>t(take.operator_valid===false?'已作废':{pass:'快速检查通过',suspect:'需要复检',fail:'检查未通过',pending:'待检查'}[take.quick_qc?.state]||'未检查');
const recoveryUrl=(take,file)=>`/api/tasks/${encodeURIComponent(recordSet.value.task.task_id)}/recordings/${take.take_id}/recovered/${file.kind}`;
const fileLabel=kind=>t({data:'录制数据（MCAP）',metadata:'回放元数据（YAML）',manifest:'恢复说明（JSON）'}[kind]||kind);
function fileSize(bytes){let value=bytes;for(const unit of ['B','KB','MB','GB']){if(value<1024||unit==='GB')return `${value.toFixed(unit==='B'?0:1)} ${unit}`;value/=1024;}}
function qualityReasons(take){
 const anomalies=(take.anomaly_timeline||[]).filter(item=>item.event==='started'&&item.affects_quality!==false);
 const details=anomalies.map(item=>{
  const device=props.status?.devices?.find(d=>d.name===item.details?.service);
  if(item.code?.startsWith('localization:')&&device)return t('{0}未完成公共地图定位，本条需要复检。',roleLabel(device.role));
  const camera=props.status?.devices?.find(d=>d.name===item.details?.camera);
  if(item.code?.startsWith('camera_stale:')&&camera)return t('{0}图像曾中断，本条需要复检。',roleLabel(camera.role));
  return t(item.message);
 }).filter(Boolean);
 const reasons=(take.quick_qc?.reasons||[]).filter(reason=>reason!=='critical live QC anomaly'||!details.length).map(reason=>t({
  'critical live QC anomaly':'录制期间出现质量异常，请复检。',
  'recording network audit failed':'录制期间接收链路出现错误，请复检。',
  'image continuity audit failed':'图像连续性检查未通过。',
  'distributed clock synchronization audit failed':'分布式录制时间同步检查未通过。',
 }[reason]||reason));
 return [...new Set([...details,...reasons])];
}
</script>
<template>
<p v-if="online && (error||(!route.includes('/')&&tasksError))" class="global-error" role="alert">{{error?t('记录暂时无法更新，连接恢复后自动重试。'):tasksError}}</p>
<p v-if="!online" class="issue warning">{{t('设备离线，已加载的记录仍可查看；重连后自动更新。')}}</p>
<p role="status">{{notice}}</p>
<p v-if="loading" class="empty">{{t('正在读取记录')}}</p>
<section v-else-if="!route.includes('/')" class="card"><div class="section-title task-record-heading"><div><h2>{{t('任务记录')}}</h2><span>{{tasks.length}} {{t('个任务')}}</span></div><button ref="createButton" id="create-task" type="button" class="primary compact" :disabled="busy||!online" @click="emit('create-task')">{{t('新建任务')}}</button></div>
<ul class="record-list" role="list"><li v-for="task in tasks" :key="task.task_id" class="task-record-row"><a :href="`#history/${task.task_id}`" :data-history-route="`history/${task.task_id}`" class="route-link task-record-link" @click="emit('navigate',`history/${task.task_id}`,$event)"><strong>{{task.name}}</strong><span>{{task.stats?.recorded_takes||0}} Take ›</span></a><button type="button" class="secondary compact task-delete" :disabled="busy||!online||deleting" :aria-label="t('删除任务“{0}”',task.name)" @click="confirmDelete(task)">{{t('删除')}}</button></li></ul><p v-if="!tasks.length" class="empty">{{t('暂无记录')}}</p></section>
<section v-else-if="recordSet" class="card"><div class="section-title records-heading"><h2 ref="recordsHeading" tabindex="-1">{{recordSet.task.name}}</h2></div>
<p v-if="!selectedTakes.length" class="empty">{{t(route.split('/')[2]?'找不到这条录制记录':'暂无记录')}}</p>
<ul class="record-list take-list" role="list" :key="route.split('/')[2]?'detail':'list'" :aria-busy="renderLimit<selectedTakes.length"><li v-for="take in renderedTakes" :key="take.take_id" v-memo="[take,status,language,online,busy,deleting,route,recordSet.task]"><article class="recording-row">
<div><strong>Take {{String(take.take_id).padStart(4,'0')}}</strong><p><small>{{date(take.recording_timing?.started_at_epoch_s||take.start_epoch_s||take.reserved_at_epoch_s)}}</small></p></div><div class="meta"><span :class="take.state==='complete'?'state-complete':''">{{takeState(take.state)}}</span><p><small>{{takeDuration(take,status)}}</small></p></div>
<p v-if="!['recording','reserved','starting','finalizing'].includes(take.state)" :class="{'issue warning':['suspect','fail'].includes(take.quick_qc?.state)}">{{t('质量检查')}}：{{quality(take)}}</p>
<div class="recording-row-actions"><a :href="`#${route}/${take.take_id}`" v-if="!route.split('/')[2]" :aria-label="`${t(take.recovery?'查看恢复数据':'查看详情')} · Take ${take.take_id}`" :data-history-route="`${route}/${take.take_id}`" class="route-link secondary compact" @click="emit('navigate',`${route}/${take.take_id}`,$event)">{{t(take.recovery?'查看恢复数据':'查看详情')}}</a><button type="button" class="secondary compact task-delete take-delete" :disabled="busy||!online||deleting||['reserved','starting','recording','finalizing'].includes(take.state)" :aria-label="t('删除录制 Take {0}',take.take_id)" @click="confirmDelete(recordSet.task,take)">{{t('删除')}}</button></div>
<template v-if="route.split('/')[2]">
<section v-if="take.recovery" class="recovery-details" :aria-label="t('恢复数据')"><strong>{{t('已恢复可读取数据')}}</strong><p>{{t('恢复数据时长')}}：{{duration(take.recovery.recording_timing.duration_s)}} · {{t('{0} 条消息',take.recovery.message_count.toLocaleString())}}</p><p>{{t('仅包含中断前已写入且可读取的数据，原始文件仍保留。本条仍需复检。')}}</p><details><summary>{{t('数据位置')}}</summary><p>{{t('恢复位置')}}：{{take.recovery.bag_path}}</p><p>{{t('原始文件位置')}}：{{take.recovery.original_path}}</p></details><div class="recovery-downloads"><a :href="online?recoveryUrl(take,{kind:'bundle'}):undefined" :download="take.recovery.bag_name+'.zip'" :aria-disabled="!online" @click="!online&&$event.preventDefault()">{{t('下载恢复数据包（ZIP）')}}</a></div><p class="settings-note">{{t('数据包按 Take 命名，包含录制数据、回放元数据和恢复说明。解压后即可使用 ROS 工具读取。')}}</p><details><summary>{{t('单独下载文件')}}</summary><div class="recovery-downloads"><a v-for="file in take.recovery.files" :key="file.kind" :href="online?recoveryUrl(take,file):undefined" :download="file.name" :aria-disabled="!online" @click="!online&&$event.preventDefault()">{{t('下载')}} {{fileLabel(file.kind)}} · {{fileSize(file.size_bytes)}}</a></div><p class="settings-note">{{t('使用 ROS 工具回放时，请将录制数据和回放元数据保存到同一文件夹。')}}</p></details></section>
<p>{{t('任务说明')}}：{{recordSet.task.instruction}}</p><p v-if="!take.recovery">{{t(take.state==='finalization_failed'?'预期保存位置':'数据位置')}}：{{take.bag_path||'—'}}</p><p v-for="reason in qualityReasons(take)" :key="reason" class="issue warning">{{reason}}</p><p v-if="['complete','saved'].includes(take.state)">{{t('已保存表示文件已写入，质量结论以检查和复检结果为准。')}}</p><p>{{t('校准和轨迹数据以本条录制内容为准。')}}</p></template>
</article></li></ul><p v-if="renderLimit<selectedTakes.length" class="empty" role="status">{{t('正在读取记录')}}</p></section>
<section v-if="transfers?.enabled" class="card"><h2>{{t('回传与合包')}}</h2><ul class="record-list" role="list"><li v-for="item in transfers.recordings" :key="item.session_id"><article class="recording-row"><strong>{{item.task_name}} · Take {{item.take_id}}</strong><span>{{item.phase}}</span><p v-if="item.error" class="issue error">{{item.error}}</p><button v-if="['saved','error'].includes(item.phase)" class="secondary" :disabled="busy||!online||transfers.busy" :aria-label="`${t('回传并合包')} · ${item.task_name} · Take ${item.take_id}`" @click="merge(item.session_id)">{{t('回传并合包')}}</button></article></li></ul><p v-if="!transfers.recordings.length" class="empty">{{t('暂无待回传记录')}}</p></section>
<p class="settings-note">{{t('浏览记录不会切换当前采集任务。原始数据保存在 Jetson，可在桌面端回放。')}}</p>
<dialog ref="deleteDialog" id="delete-task-dialog" aria-labelledby="delete-task-title" aria-describedby="delete-task-description" @close="closeDelete">
<h2 id="delete-task-title">{{t(deleteTarget?.take?'删除录制':'删除任务')}}</h2>
<p id="delete-task-description">{{deleteTarget?.take?t('将删除任务“{0}”中的 Take {1} 及其录制数据，删除后无法恢复。',deleteTarget.task.name,deleteTarget.take.take_id):t('将删除任务“{0}”及其 {1} 条录制数据，删除后无法恢复。',deleteTarget?.task.name,deleteTarget?.task.stats?.recorded_takes||0)}}</p>
<p v-if="deleteError" class="issue error" role="alert">{{actionError||deleteError}}</p>
<div class="dialog-actions"><button type="button" class="secondary" autofocus @click="deleteDialog.close()">{{t(deleting?'关闭':'取消')}}</button><button type="button" class="danger" :disabled="deleting||busy||!online" @click="removeSelected">{{t(deleting?'正在删除':'确认删除')}}</button></div>
</dialog>
</template>
