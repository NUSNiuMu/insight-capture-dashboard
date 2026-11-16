const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {buildSync}=require('esbuild');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'modal-scroll-'));
const file=path.join(dir,'guard.cjs');
buildSync({entryPoints:[path.resolve(__dirname,'../src/modal-scroll.js')],bundle:true,platform:'node',format:'cjs',outfile:file});
const {guardModalScroll}=require(file);fs.rmSync(dir,{recursive:true});
class Target extends EventTarget {
 constructor(){super();this.listeners=new Map()}
 addEventListener(type,fn,opts){super.addEventListener(type,fn,opts);if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn)}
 removeEventListener(type,fn,opts){super.removeEventListener(type,fn,opts);this.listeners.get(type)?.delete(fn)}
 count(){return [...this.listeners.values()].reduce((n,s)=>n+s.size,0)}
}
function fixture(){
 const doc=new Target();doc.activeElement=null;doc.getSelection=()=>null;
 doc.defaultView={getComputedStyle:n=>n.css};
 const node=(tag,parent=null)=>Object.assign(new Target(),{tagName:tag,parentElement:parent,
  scrollTop:0,scrollLeft:0,clientWidth:200,clientHeight:100,scrollWidth:200,scrollHeight:100,
  css:{overflowX:'visible',overflowY:'visible'},
  contains(value){for(let n=value;n;n=n.parentElement)if(n===this)return true;return false},
  closest(){for(let n=this;n;n=n.parentElement)if(['INPUT','TEXTAREA'].includes(n.tagName))return n;return null},
 });
 const modal=node('DIALOG');modal.ownerDocument=doc;modal.open=true;
 modal.getBoundingClientRect=()=>({left:20,right:320,top:100,bottom:400});
 modal.css={overflowX:'auto',overflowY:'auto'};
 const content=node('DIV',modal),release=guardModalScroll(modal);
 function event(type,target=content,touches=[{clientX:100,clientY:200}],cancelable=true){
  const e=new Event(type,{cancelable});Object.defineProperty(e,'target',{value:target});Object.defineProperty(e,'touches',{value:touches});doc.dispatchEvent(e);return e;
 }
 const start=(target=content,x=100,y=200)=>event('touchstart',target,[{clientX:x,clientY:y}]);
 const move=(target=content,x=100,y=150)=>event('touchmove',target,[{clientX:x,clientY:y}]);
 return {doc,modal,content,node,event,start,move,release};
}
test('backdrop pan cannot use a scrollable modal as an excuse to scroll the document',()=>{
 const f=fixture();f.modal.scrollHeight=600;f.start(f.modal,5,200);assert.equal(f.move(f.modal,5,150).defaultPrevented,true);f.release();
});
test('empty modal content cannot chain a touch pan to the document',()=>{
 const f=fixture();f.start();assert.equal(f.move().defaultPrevented,true);f.release();
});
test('native modal scroll is allowed until its boundary',()=>{
 const f=fixture();f.modal.scrollHeight=400;f.start();assert.equal(f.move().defaultPrevented,false);
 f.modal.scrollTop=300;assert.equal(f.move(f.content,100,100).defaultPrevented,true);f.release();
});
test('a textarea scrolls natively and does not chain out at its boundary',()=>{
 const f=fixture(),field=f.node('TEXTAREA',f.modal);field.css.overflowY='auto';field.scrollHeight=500;
 f.start(field);assert.equal(f.move(field).defaultPrevented,false);field.scrollTop=400;
 assert.equal(f.move(field,100,100).defaultPrevented,true);f.release();
});
test('a nested field can yield to remaining modal scroll without manually moving either',()=>{
 const f=fixture(),field=f.node('TEXTAREA',f.modal);field.css.overflowY='auto';field.scrollHeight=500;field.scrollTop=400;f.modal.scrollHeight=500;
 f.start(field);assert.equal(f.move(field).defaultPrevented,false);assert.equal(f.modal.scrollTop,0);assert.equal(field.scrollTop,400);f.release();
});
test('horizontal native input scrolling is preserved',()=>{
 const f=fixture(),field=f.node('INPUT',f.modal);field.scrollWidth=500;
 f.start(field);assert.equal(f.move(field,50,200).defaultPrevented,false);f.release();
});
test('active native text selection is not cancelled',()=>{
 const f=fixture(),field=f.node('INPUT',f.modal);field.selectionStart=0;field.selectionEnd=4;f.doc.activeElement=field;
 f.start(field);assert.equal(f.move(field).defaultPrevented,false);f.release();
});
test('a background text selection does not disable modal protection',()=>{
 const f=fixture();f.doc.getSelection=()=>({isCollapsed:false,anchorNode:f.node('P')});
 f.start();assert.equal(f.move().defaultPrevented,true);f.release();
});
test('selection inside the modal remains native',()=>{
 const f=fixture();f.doc.getSelection=()=>({isCollapsed:false,anchorNode:f.content});
 f.start();assert.equal(f.move().defaultPrevented,false);f.release();
});
test('pinch gestures and remaining fingers after pinch are not captured',()=>{
 const f=fixture();f.start();f.event('touchstart',f.content,[{clientX:50,clientY:200},{clientX:150,clientY:200}]);
 assert.equal(f.event('touchmove',f.content,[{clientX:40,clientY:200},{clientX:160,clientY:200}]).defaultPrevented,false);
 f.event('touchend',f.content,[{clientX:40,clientY:200}]);assert.equal(f.move().defaultPrevented,false);f.release();
});
test('native close and explicit unmount cleanup both remove every listener',()=>{
 const f=fixture();assert.equal(f.doc.count(),4);f.modal.open=false;f.modal.dispatchEvent(new Event('close'));
 assert.equal(f.doc.count(),0);assert.equal(f.modal.count(),0);f.release();assert.equal(f.doc.count(),0);
 const g=fixture();g.release();assert.equal(g.doc.count(),0);assert.equal(g.modal.count(),0);
});
test('a delayed close event does not remove a reopened modal guard',()=>{
 const f=fixture();f.modal.dispatchEvent(new Event('close'));assert.equal(f.doc.count(),4);
 f.start();assert.equal(f.move().defaultPrevented,true);f.release();
});
test('the guard ignores another modal and uncancelable events',()=>{
 const f=fixture(),other=f.node('DIALOG');f.start(other);assert.equal(f.move(other).defaultPrevented,false);
 f.start();assert.equal(f.event('touchmove',f.content,[{clientX:100,clientY:150}],false).defaultPrevented,false);f.release();
});

function viewportFixture() {
 const f=fixture();f.release();
 const values=new Map(),frames=new Map();let id=0,hadStyle=false;
 f.modal.style={getPropertyValue:k=>values.get(k)?.[0]||'',getPropertyPriority:k=>values.get(k)?.[1]||'',
  setProperty(k,v,p=''){values.set(k,[v,p]);hadStyle=true},removeProperty:k=>values.delete(k),get length(){return values.size}};
 f.modal.hasAttribute=()=>hadStyle;f.modal.removeAttribute=()=>{hadStyle=false};
 const viewport=Object.assign(new Target(),{width:390,height:800,offsetTop:0,offsetLeft:0});
 Object.assign(f.doc.defaultView,{visualViewport:viewport,innerWidth:390,innerHeight:800,
  requestAnimationFrame(fn){frames.set(++id,fn);return id},cancelAnimationFrame:id=>frames.delete(id)});
 return {...f,viewport,frames,flush(){for(const [id,fn] of frames){frames.delete(id);fn()}}};
}
test('keyboard and zoom viewport changes coalesce and remain scoped to the form',()=>{
 const f=viewportFixture(),release=guardModalScroll(f.modal,{fitViewport:true});
 assert.equal(f.viewport.count(),2);assert.equal(f.modal.style.getPropertyValue('--modal-viewport-height'),'800px');
 Object.assign(f.viewport,{height:370,width:320,offsetTop:150,offsetLeft:20});
 f.viewport.dispatchEvent(new Event('resize'));f.viewport.dispatchEvent(new Event('scroll'));assert.equal(f.frames.size,1);f.flush();
 assert.equal(f.modal.style.getPropertyValue('--modal-viewport-height'),'370px');
 assert.equal(f.modal.style.getPropertyValue('--modal-viewport-inset'),'150px 50px 280px 20px');
 f.viewport.dispatchEvent(new Event('resize'));release();assert.equal(f.frames.size,0);assert.equal(f.viewport.count(),0);
 assert.equal(f.modal.style.length,0);assert.equal(f.modal.hasAttribute('style'),false);
});
test('native close restores owned viewport properties and preserves unrelated edits',()=>{
 const f=viewportFixture();f.modal.style.setProperty('--modal-viewport-height','600px','important');
 guardModalScroll(f.modal,{fitViewport:true});f.modal.style.setProperty('color','red');
 f.modal.open=false;f.modal.dispatchEvent(new Event('close'));
 assert.equal(f.viewport.count(),0);assert.equal(f.doc.count(),0);
 assert.equal(f.modal.style.getPropertyValue('--modal-viewport-height'),'600px');
 assert.equal(f.modal.style.getPropertyPriority('--modal-viewport-height'),'important');
 assert.equal(f.modal.style.getPropertyValue('color'),'red');
});
test('hidden viewport and absent optional API do not disable native dialog controls',()=>{
 const f=viewportFixture(),release=guardModalScroll(f.modal,{fitViewport:true});
 f.viewport.height=0;f.viewport.dispatchEvent(new Event('resize'));f.flush();
 assert.equal(f.modal.style.getPropertyValue('--modal-viewport-height'),'800px');release();
 const g=fixture();g.release();const close=guardModalScroll(g.modal,{fitViewport:true});g.start();
 assert.equal(g.move().defaultPrevented,true);close();assert.equal(g.doc.count(),0);
});
