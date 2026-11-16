<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue';
import { t } from './i18n.js';
import { roleLabel } from './useCapture.js';
const poses=ref([]), connected=ref(false), traces=new Map(); let socket,timer,alive=true;
const styles={head:{color:'#1d4ed8',dash:''},left_hand:{color:'#9a4800',dash:'8 4'},right_hand:{color:'#11754f',dash:'2 3'}};
function updateTrace(old,u){
 let points,lastSeq;
 if(u.mode==='snapshot'){points=u.points;lastSeq=u.to_seq;}
 else{
  if(!old||old.generation!==u.generation||u.from_seq>old.lastSeq+1)return null;
  // A new viewer's snapshot can be newer than the shared broadcast cursor.
  const overlap=Math.max(0,old.lastSeq-u.from_seq+1);
  points=u.to_seq>old.lastSeq?[...old.points,...u.points.slice(overlap)]:old.points;
  lastSeq=Math.max(old.lastSeq,u.to_seq);
 }
 const firstSeq=lastSeq-points.length+1;
 const drop=Math.max(0,points.length-1500,u.drop_before_seq-firstSeq);
 return {generation:u.generation,lastSeq,points:points.slice(drop)};
}
function connect(){
 if(!alive||document.hidden)return;
 socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
 const current=socket;
 socket.onopen=()=>{if(alive&&socket===current)connected.value=true;};
 socket.onmessage=event=>{
  if(!alive||socket!==current)return;
  const data=JSON.parse(event.data);if(data.type!=='pose_update')return;
  const next=[];
  for(const p of data.poses){
   const record=updateTrace(traces.get(p.name),p.trace_update);
   if(!record){traces.clear();poses.value=[];connected.value=false;current.close();return;}
   traces.set(p.name,record);next.push({...p,points:record.points});
  }
  poses.value=next;
 };
 socket.onclose=()=>{if(socket!==current)return;connected.value=false;if(alive&&!document.hidden)timer=setTimeout(connect,2000);};
}
function visibility(){pointers.clear();clearTimeout(timer);const old=socket;socket=null;old?.close();connected.value=false;if(!document.hidden)connect();}
// Projection and orbit convention adapted by value from EgoHalo's owned UI;
// this project owns its implementation and has no runtime/build dependency on it.
const yaw=ref(-0.7),pitch=ref(0.65),zoom=ref(1),pan=ref([0,0]),panMode=ref(false),pointers=new Map();
const visible=computed(()=>poses.value.filter(p=>p.visible));
const bounds=computed(()=>{
 const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];let count=0;
 for(const pose of visible.value)for(const p of pose.points){count++;for(let i=0;i<3;i++){min[i]=Math.min(min[i],p[i]);max[i]=Math.max(max[i],p[i]);}}
 return count?{center:min.map((v,i)=>(v+max[i])/2),span:Math.max(0.5,Math.hypot(...max.map((v,i)=>v-min[i])))}:{center:[0,0,0],span:1};
});
function project(point,center=bounds.value.center,unit=250/bounds.value.span*zoom.value,origin=[160+pan.value[0],160+pan.value[1]]){
 const x=point[0]-center[0],y=point[1]-center[1],z=point[2]-center[2];
 const right=x*Math.cos(yaw.value)-y*Math.sin(yaw.value),depth=x*Math.sin(yaw.value)+y*Math.cos(yaw.value);
 return [origin[0]+right*unit,origin[1]-(z*Math.cos(pitch.value)-depth*Math.sin(pitch.value))*unit];
}
const plotted=computed(()=>visible.value.map(p=>({...p,...(styles[p.role]||styles.head),line:p.points.map(q=>project(q).join(',')).join(' '),tip:p.points.length?project(p.points.at(-1)):null})));
const axes=computed(()=>['X','Y','Z'].map((name,i)=>{const tip=[0,0,0];tip[i]=1;return {name,color:['#b91c1c','#11754f','#1d4ed8'][i],tip:project(tip,[0,0,0],27,[44,46])};}));
function fit(){zoom.value=1;pan.value=[0,0];}
function reset(){yaw.value=-0.7;pitch.value=0.65;fit();}
function scale(factor){zoom.value=Math.max(0.1,Math.min(30,zoom.value*factor));}
function pointerDown(e){if(e.pointerType==='mouse'&&![0,2].includes(e.button))return;e.currentTarget.setPointerCapture(e.pointerId);pointers.set(e.pointerId,[e.clientX,e.clientY]);}
function pointerMove(e){
 const old=pointers.get(e.pointerId);if(!old)return;
 const next=[e.clientX,e.clientY],dx=next[0]-old[0],dy=next[1]-old[1],unit=320/e.currentTarget.getBoundingClientRect().width;
 if(pointers.size===1){
  if(panMode.value||e.buttons===2||e.shiftKey)pan.value=[pan.value[0]+dx*unit,pan.value[1]+dy*unit];
  else{yaw.value+=dx*0.008;pitch.value=Math.max(-Math.PI/2,Math.min(Math.PI/2,pitch.value+dy*0.008));}
 }else{
  const other=[...pointers.entries()].find(([id])=>id!==e.pointerId)[1],before=Math.hypot(old[0]-other[0],old[1]-other[1]);
  if(before>1)scale(Math.hypot(next[0]-other[0],next[1]-other[1])/before);
  pan.value=[pan.value[0]+dx*unit/2,pan.value[1]+dy*unit/2];
 }
 pointers.set(e.pointerId,next);
}
function pointerEnd(e){pointers.delete(e.pointerId);}
function keydown(e){
 if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','+','=','-','Home'].includes(e.key))return;e.preventDefault();
 const dx=({'ArrowLeft':-1,'ArrowRight':1})[e.key]||0,dy=({'ArrowUp':-1,'ArrowDown':1})[e.key]||0;
 if(e.shiftKey)pan.value=[pan.value[0]+dx*12,pan.value[1]+dy*12];
 else{yaw.value+=dx*.15;pitch.value=Math.max(-Math.PI/2,Math.min(Math.PI/2,pitch.value+dy*.15));}
 if(['+','='].includes(e.key))scale(1.2);if(e.key==='-')scale(1/1.2);if(e.key==='Home')reset();
}
onMounted(()=>{connect();document.addEventListener('visibilitychange',visibility);});
onBeforeUnmount(()=>{pointers.clear();alive=false;clearTimeout(timer);socket?.close();document.removeEventListener('visibilitychange',visibility);});
</script>
<template><section class="card"><div class="section-title"><h2>{{t('实时 3D 轨迹')}}</h2><span>XYZ · {{t('米')}}</span></div>
<div class="trajectory-controls" role="group" :aria-label="t('轨迹视角')">
<button class="secondary compact" @click="reset">{{t('适应轨迹')}}</button>
<button class="secondary compact" :aria-pressed="panMode" @click="panMode=!panMode">{{t(panMode?'平移模式':'旋转模式')}}</button>
<button class="secondary compact" :aria-label="t('放大轨迹')" @click="scale(1.3)">＋</button><button class="secondary compact" :aria-label="t('缩小轨迹')" @click="scale(1/1.3)">−</button>
</div>
<svg class="trajectory-map trajectory-3d" viewBox="0 0 320 320" tabindex="0" role="img" :aria-label="t('3D 轨迹。方向键旋转，Shift 加方向键平移，加减键缩放，Home 复位。')"
 @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerEnd" @pointercancel="pointerEnd" @lostpointercapture="pointerEnd" @contextmenu.prevent @wheel.prevent="scale(Math.exp(-Math.max(-100,Math.min(100,$event.deltaY))*0.005))" @keydown="keydown">
<g v-for="p in plotted" :key="p.name"><polyline :points="p.line" fill="none" :stroke="p.color" :stroke-dasharray="p.dash" stroke-width="2"/><circle v-if="p.tip" :cx="p.tip[0]" :cy="p.tip[1]" r="3.5" :fill="p.color"/></g>
<g v-for="axis in axes" :key="axis.name" :stroke="axis.color"><line x1="44" y1="46" :x2="axis.tip[0]" :y2="axis.tip[1]" stroke-width="1.5"/><text :x="axis.tip[0]+3" :y="axis.tip[1]-3" :fill="axis.color" stroke="none" font-size="11">{{axis.name}}</text></g>
</svg>
<p v-if="!plotted.length || !plotted.some(p=>p.points.length)" class="empty">{{t(connected?'等待公共地图定位':'正在连接轨迹')}}</p>
<div class="health-summary"><span v-for="p in plotted" :key="p.name" :style="{color:p.color,borderBottom:`2px ${p.role==='left_hand'?'dashed':p.role==='right_hand'?'dotted':'solid'} currentColor`}">{{roleLabel(p.role)}}</span></div>
<p class="section-description">{{t('单指旋转，双指缩放和平移；切换平移模式可单指移动。')}}</p>
<p class="section-description">{{t('仅显示已定位设备的公共坐标轨迹；校准完成不代表精度已验收。')}}</p></section></template>
