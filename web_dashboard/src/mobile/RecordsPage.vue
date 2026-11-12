<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue';
import { api, duration } from './useCapture.js';
import { t } from './i18n.js';
const props=defineProps({route:String,tasks:Array,busy:Boolean,online:Boolean,action:Function});
const emit=defineEmits(['navigate']);const recordSet=ref(null),error=ref(''),loading=ref(false);let generation=0;
watch(()=>props.route,async route=>{
 const taskId=route.split('/')[1];const gen=++generation;error.value='';
 if(!taskId){recordSet.value=null;return;}
 loading.value=true;
 try{const value=await api(`/api/tasks/${encodeURIComponent(taskId)}/recordings`);if(gen===generation)recordSet.value=value;}
 catch(e){if(gen===generation)error.value=e.message;}
 finally{if(gen===generation)loading.value=false;}
},{immediate:true});
onBeforeUnmount(()=>generation++);
const transfers=ref(null);let timer,alive=true;
async function refreshTransfers(){try{transfers.value=await api('/api/postprocessing/recordings');}catch{}if(alive)timer=setTimeout(refreshTransfers,5000);}
onMounted(refreshTransfers);onBeforeUnmount(()=>{alive=false;clearTimeout(timer);});
async function merge(session_id){if(await props.action('/api/postprocessing/merge',{session_id})){transfers.value=await api('/api/postprocessing/recordings');}}
const date=value=>value?new Date(value*1000).toLocaleString(): '—';
const takeState=s=>t({complete:'已保存',saved:'等待传输',finalization_failed:'保存异常',recording:'正在录制',finalizing:'正在保存'}[s]||s);
</script>
<template>
<p v-if="error" class="global-error" role="alert">{{error}}</p><p v-if="loading" class="empty">{{t('正在读取记录')}}</p>
<section v-else-if="!route.includes('/')" class="card"><div class="section-title"><h2>{{t('任务记录')}}</h2><span>{{tasks.length}} {{t('个任务')}}</span></div>
<button v-for="task in tasks" :key="task.task_id" class="task-record-link" @click="emit('navigate',`history/${task.task_id}`)"><strong>{{task.name}}</strong><span>{{task.stats?.recorded_takes||0}} Take ›</span></button><p v-if="!tasks.length" class="empty">{{t('暂无记录')}}</p></section>
<section v-else-if="recordSet" class="card"><div class="section-title"><h2>{{recordSet.task.name}}</h2></div>
<p v-if="!recordSet.takes.length" class="empty">{{t('暂无记录')}}</p>
<template v-for="take in recordSet.takes" :key="take.take_id">
<article v-if="!route.split('/')[2] || Number(route.split('/')[2])===take.take_id" class="recording-row">
<div><strong>Take {{String(take.take_id).padStart(4,'0')}}</strong><p><small>{{date(take.start_epoch_s||take.reserved_at_epoch_s)}}</small></p></div><div class="meta"><span :class="take.state==='complete'?'state-complete':''">{{takeState(take.state)}}</span><p><small>{{duration((take.end_epoch_s||take.start_epoch_s)-take.start_epoch_s)}}</small></p></div>
<button v-if="!route.split('/')[2]" class="secondary compact" @click="emit('navigate',`${route}/${take.take_id}`)">{{t('查看详情')}}</button>
<template v-else><p>{{t('任务说明')}}：{{recordSet.task.instruction}}</p><p>{{t('数据位置')}}：{{take.bag_path||'—'}}</p><p>{{t('质量检查')}}：{{take.quick_qc?.state||t('未检查')}}</p><p v-for="reason in take.quick_qc?.reasons" :key="reason" class="issue warning">{{reason}}</p><p>{{t('校准和轨迹数据以本条录制内容为准。')}}</p></template>
</article></template></section>
<section v-if="transfers?.enabled" class="card"><h2>{{t('回传与合包')}}</h2><article v-for="item in transfers.recordings" :key="item.session_id" class="recording-row"><strong>{{item.task_name}} · Take {{item.take_id}}</strong><span>{{item.phase}}</span><p v-if="item.error" class="issue error">{{item.error}}</p><button v-if="['saved','error'].includes(item.phase)" class="secondary" :disabled="busy||!online||transfers.busy" @click="merge(item.session_id)">{{t('回传并合包')}}</button></article><p v-if="!transfers.recordings.length" class="empty">{{t('暂无待回传记录')}}</p></section>
<p class="settings-note">{{t('浏览记录不会切换当前采集任务。原始数据保存在 Jetson，可在桌面端回放。')}}</p>
</template>
