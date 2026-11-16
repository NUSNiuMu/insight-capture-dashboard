<script setup>
import { t } from "../i18n.js"

import { computed, ref, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { startWebRTC } from '../webrtc.js'
import { appActive } from '../lifecycle.js'
const props = defineProps({ preview: Object, online: Boolean, recording: Boolean, active: { type: Boolean, default: true } })
const emit = defineEmits(['notify', 'recovered'])
const selected = ref(''), video = ref(null), failed = ref(false), ready = ref(false)
const failure = ref('')
const panel = ref(null), inline = ref(null), overlay = ref(null), expanded = ref(false), hidden = ref(document.hidden)
const stage = ref(null), stageSize = ref({ width: 0, height: 0 })
const overview = computed(() => props.preview?.overview)
const previewPaused = computed(() => props.recording || props.preview?.paused)
const paused = computed(() => hidden.value || !appActive.value || !props.active)
const placeholder = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="1280"><rect width="100%25" height="100%25" fill="%23151b2c"/></svg>'
const image = ref(placeholder)
const sources = computed(() => props.preview?.sources || [])
const rtc = computed(() => props.preview?.transport === 'webrtc')
const isOverview = computed(() => rtc.value && selected.value === overview.value?.id)
const sourceSignature = computed(() => sources.value.map(s => `${s.id}:${s.topic}:${s.width}:${s.height}`).join("|"))
const fallbackSource = computed(() => sources.value.find(s => s.id === props.preview?.default_source) || sources.value[0])
const source = computed(() => sources.value.find(s => s.id === selected.value) || fallbackSource.value)
const aspect = computed(() => {
  const dimensions = isOverview.value ? overview.value : source.value
  return dimensions?.width && dimensions?.height ? `${dimensions.width} / ${dimensions.height}` : '1 / 1'
})
const pictureStyle = computed(() => {
  if (!expanded.value || !stageSize.value.width || !stageSize.value.height) return undefined
  const [width, height] = aspect.value.split('/').map(Number)
  return { width: `${Math.min(stageSize.value.width, stageSize.value.height * width / height)}px` }
})
function regionStyle(id) {
  const region = overview.value?.regions?.find(r => r.id === id)
  if (!region) return undefined
  return { position: 'absolute', left: `${region.x / overview.value.width * 100}%`,
    top: `${region.y / overview.value.height * 100}%`,
    width: `${region.width / overview.value.width * 100}%`, height: `${region.height / overview.value.height * 100}%` }
}
const fullscreenAvailable = Boolean(document.fullscreenEnabled)
let previousFocus, previousScroll, resizeObserver, returnToOverview = false
let rtcSession, retryTimer
let generation = 0, dead = false
try { selected.value = localStorage.getItem('insight-preview-source') || '' } catch { /* storage may be disabled */ }
watch([sources, () => overview.value?.id], ([list]) => {
  // Status arrives asynchronously; keep the saved selection until sources load.
  if (!list.length) return
  if (selected.value === overview.value?.id) return
  if (!list.some(s => s.id === selected.value)) selected.value =
    overview.value?.id || fallbackSource.value?.id || ''
}, { immediate: true })
function cleanup() {
  clearTimeout(retryTimer)
  generation++
  rtcSession?.close(); rtcSession = null
  image.value = placeholder
  ready.value = false
  if (video.value) {
    video.value.onerror = null
    video.value.pause(); video.value.removeAttribute('src'); video.value.load()
  }
}
function start() {
  cleanup()
  const id = selected.value, gen = generation
  if (!id || !props.online || previewPaused.value || paused.value) return
  failed.value = false; failure.value = ''
  try { localStorage.setItem('insight-preview-source', id) } catch { /* optional */ }
  if (!rtc.value) return
  const fallback = reason => {
    if (gen !== generation || dead || previewPaused.value || paused.value) return
    failed.value = true
    failure.value = reason
    cleanup()
    emit('notify', reason)
    retryTimer = setTimeout(() => {
      if (!dead && props.online && !previewPaused.value && !paused.value) start()
    }, 3000)
  }
  if (props.preview?.video_error) { fallback(props.preview.video_error); return }
  rtcSession = startWebRTC({ video: video.value, source: id, onFailure: fallback,
    onReady: () => { if (gen === generation) { ready.value = true; emit('recovered') } },
    // Stats remain internal for decoded-frame stall detection; the capture UI hides them.
    onStats: () => {},
  })
}
watch([() => selected.value, () => props.preview?.transport, () => props.online, () => props.preview?.video_error, previewPaused, paused, sourceSignature],
  () => {
  failed.value = false; failure.value = ''
  start()
}, { flush: 'post' })
function openPreview(id) {
  if (!expanded.value) returnToOverview = isOverview.value
  if (id) selected.value = id
  expanded.value = true
}
function showOverview() {
  if (selected.value !== overview.value.id) selected.value = overview.value.id
  else if (failed.value) { failed.value = false; failure.value = ''; start() }
}
function closePreview() {
  if (document.fullscreenElement === panel.value) document.exitFullscreen().catch(() => {})
  expanded.value = false
  if (returnToOverview && overview.value) selected.value = overview.value.id
  returnToOverview = false
}
async function fullscreen() {
  try {
    if (document.fullscreenElement === panel.value) await document.exitFullscreen()
    else await panel.value.requestFullscreen()
  } catch { emit('notify', '当前浏览器无法进入系统全屏，可继续使用放大预览') }
}
function overlayClosed() { if (!overlay.value.open) closePreview() }
function movePanel(target) {
  // The hosts are stable and empty except for this panel. Vue owns its content.
  if (typeof target.moveBefore === 'function') target.moveBefore(panel.value, null)
  else target.insertBefore(panel.value, null)
  // Traditional DOM moves can pause media in older browsers, including Safari.
  if (video.value?.srcObject && !previewPaused.value && !paused.value) video.value.play().catch(() => {})
}
watch(expanded, async value => {
  if (value) {
    previousFocus = document.activeElement
    previousScroll = { x: window.scrollX, y: window.scrollY }
    // Keep the inline slot in flow so opening cannot clamp the page's scroll.
    inline.value.style.height = `${inline.value.getBoundingClientRect().height}px`
    overlay.value.showModal()
    await nextTick()
    if (expanded.value && !dead) {
      movePanel(overlay.value)
      panel.value.querySelector('.preview-close')?.focus({ preventScroll: true })
    }
  } else {
    await nextTick()
    if (dead || expanded.value) return
    movePanel(inline.value)
    inline.value.style.height = ''
    overlay.value.close()
    // Repair focus only if moving DOM or replacing a selected tile lost the invoker.
    if (document.activeElement === document.body || !document.activeElement?.getClientRects().length) {
      const invoker = previousFocus?.isConnected ? previousFocus : panel.value.querySelector('#preview-overview, #preview-expand')
      invoker?.focus({ preventScroll: true })
    }
    if (props.active && previousScroll) window.scrollTo(previousScroll.x, previousScroll.y)
  }
})
watch(() => previewPaused.value, recording => { if (recording && expanded.value) closePreview() })
watch(() => props.active, active => { if (!active && expanded.value) closePreview() })
function visibility() { hidden.value = document.hidden }
onMounted(() => {
  document.addEventListener('visibilitychange', visibility)
  resizeObserver = new ResizeObserver(([entry]) => {
    stageSize.value = { width: entry.contentRect.width, height: entry.contentRect.height }
  })
  resizeObserver.observe(stage.value)
})
onBeforeUnmount(() => {
  dead = true; cleanup()
  document.removeEventListener('visibilitychange', visibility)
  resizeObserver?.disconnect()
  if (panel.value?.parentElement !== inline.value) movePanel(inline.value)
  overlay.value?.close()
})
</script>

<template>
  <div class="preview-host">
  <dialog ref="overlay" class="preview-dialog" :aria-label="t('相机预览')" @close="overlayClosed"></dialog>
  <div ref="inline" class="preview-inline">
  <section ref="panel" class="preview-card" :class="{ 'preview-expanded': expanded }"
    :aria-label="t(&quot;相机预览&quot;)" tabindex="-1">
    <div class="preview-controls">
    <div class="preview-toolbar">
    <div class="preview-head"><span><i></i>{{ t("相机预览") }}</span><small id="preview-topic">
      {{ !online ? t('设备离线') : previewPaused ? t('预览已暂停') : `${t(isOverview ? overview?.label || '三路总览' : source?.label || '等待相机')} · ${t(!ready ? '等待画面' : failed ? '等待重试' : '实时预览')}` }}
    </small><button v-show="!expanded" id="preview-expand" :aria-label="t(&quot;放大预览&quot;)" :disabled="!online || previewPaused" @click="openPreview()">↗</button></div>
    <div v-if="expanded" class="preview-actions">
      <button v-if="expanded && fullscreenAvailable" @click="fullscreen">{{ t("系统全屏") }}</button>
      <button v-if="expanded" class="preview-close" @click="closePreview">{{ t("关闭预览") }}</button>
    </div>
    </div>
    <small v-if="failure" id="preview-failure">{{ t('实时预览不可用：') }}{{ t(failure) }}
      <button class="secondary" @click="failed = false; failure = ''; start()">{{ t("重试") }}</button></small>
    <div id="preview-source" class="camera-switch"
      :style="{ '--preview-source-count': sources.length + (overview ? 1 : 0) }" role="group" :aria-label="t(&quot;选择预览相机&quot;)">
      <button v-if="overview" id="preview-overview" :class="{ active: isOverview }" :aria-pressed="isOverview" :disabled="!online" @click="showOverview">{{ t(overview.label || '三路总览') }}</button>
      <button v-for="item in sources" :id="`preview-source-${item.id}`" :key="item.id" type="button"
        :class="{ active: !isOverview && source?.id === item.id, unavailable: item.publisher_count === 0 && preview?.publishers_checked }"
        :aria-pressed="!isOverview && source?.id === item.id" :disabled="!online" @click="selected = item.id">{{ t(item.label) }}{{ t(item.publisher_count === 0 && preview?.publishers_checked ? ' · 未连接' : '') }}</button>
    </div>
    </div>
    <div ref="stage" class="preview-stage" :style="{ '--preview-aspect': aspect }">
      <div class="preview-picture" :style="pictureStyle">
        <img v-show="!rtc" id="preview-image" :src="image" :alt="t(&quot;相机视频预览&quot;)">
        <video v-show="rtc" id="preview-video" ref="video" muted autoplay playsinline :aria-label="t(&quot;H.264 WebRTC 相机视频预览&quot;)"></video>
        <div v-if="isOverview" class="preview-tiles">
          <button v-for="item in sources" :key="item.id" :disabled="!online || previewPaused" :style="regionStyle(item.id)"
            :aria-label="t(`放大${item.label}`)" @click="openPreview(item.id)">
            <span>{{ t(item.label) }}{{ t(item.publisher_count === 0 && preview?.publishers_checked ? ' · 未连接' : '') }}</span>
          </button>
        </div>
      </div>
    </div>
  </section>
  </div>
  </div>
</template>
