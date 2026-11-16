<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue';
import { t } from './i18n.js';
import { roleLabel } from './useCapture.js';
const poses=ref([]), connected=ref(false), traces=new Map(); let socket,timer,alive=true;
const styles={head:{color:'#1d4ed8',dash:''},left_hand:{color:'#9a4800',dash:'8 4'},right_hand:{color:'#11754f',dash:'2 3'}};
function connect(){
 if(!alive||document.hidden)return;
 socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
 socket.onopen=()=>connected.value=true;
 socket.onmessage=event=>{const data=JSON.parse(event.data);if(data.type!=='pose_update')return;
   poses.value=data.poses.map(p=>{const u=p.trace_update,old=traces.get(p.name);
    const pts=!old||u.mode==='snapshot'||old.generation!==u.generation?u.points:[...old.points,...u.points];
    const record={generation:u.generation,points:pts.slice(-1500)};traces.set(p.name,record);return {...p,points:record.points};});};
 const current=socket;socket.onclose=()=>{if(socket!==current)return;connected.value=false;if(alive&&!document.hidden)timer=setTimeout(connect,2000);};
}
function visibility(){clearTimeout(timer);const old=socket;socket=null;old?.close();connected.value=false;if(!document.hidden)connect();}
const plotted=computed(()=>{
 const points=poses.value.filter(p=>p.visible).flatMap(p=>p.points);
 if(!points.length)return [];
 const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);const xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);
 const scale=280/Math.max(xmax-xmin,ymax-ymin,0.5),cx=(xmin+xmax)/2,cy=(ymin+ymax)/2;
 return poses.value.filter(p=>p.visible).map(p=>({...p,...(styles[p.role]||styles.head),line:p.points.map(q=>`${160+(q[0]-cx)*scale},${160-(q[1]-cy)*scale}`).join(' ')}));
});
onMounted(()=>{connect();document.addEventListener('visibilitychange',visibility);});
onBeforeUnmount(()=>{alive=false;clearTimeout(timer);socket?.close();document.removeEventListener('visibilitychange',visibility);});
</script>
<template><section class="card"><div class="section-title"><h2>{{t('轨迹')}}</h2><span>{{t('俯视图 · XY')}}</span></div>
<svg class="trajectory-map" viewBox="0 0 320 320" role="img" :aria-label="t('公共地图中的设备轨迹')"><defs><pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M 32 0 L 0 0 0 32" fill="none" stroke="#dce3ea"/></pattern></defs><rect width="320" height="320" fill="url(#grid)"/><polyline v-for="p in plotted" :key="p.name" :points="p.line" fill="none" :stroke="p.color" :stroke-dasharray="p.dash" stroke-width="2"/></svg>
<p v-if="!plotted.length" class="empty">{{t(connected?'等待公共地图定位':'正在连接轨迹')}}</p><div class="health-summary"><span v-for="p in plotted" :key="p.name" :style="{color:p.color,borderBottom:`2px ${p.role==='left_hand'?'dashed':p.role==='right_hand'?'dotted':'solid'} currentColor`}">{{roleLabel(p.role)}}</span></div>
<p class="section-description">{{t('仅显示已定位设备的公共坐标轨迹；校准完成不代表精度已验收。')}}</p></section></template>
