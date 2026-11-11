import { deviceWebSocketUrl } from './endpoint.js'

// Native browser WebRTC + go2rtc signalling; no player bundle or cloud service.
export function startWebRTC({ video, source, onFailure, onStats, onReady }) {
  let pc, socket, timeout, statsTimer, stopped = false, previous, lastFrame = performance.now()
  let remoteReady = false, candidates = [], chain = Promise.resolve(), offerSent = false
  const localCandidates = []
  const close = () => {
    if (stopped) return
    stopped = true
    clearTimeout(timeout); clearTimeout(statsTimer)
    if (socket) { socket.onclose = null; socket.onerror = null; socket.close() }
    if (pc) {
      pc.onconnectionstatechange = null; pc.onicecandidate = null; pc.ontrack = null
      pc.getReceivers().forEach(receiver => receiver.track?.stop())
      pc.close()
    }
    video.pause(); video.srcObject = null
  }
  const fail = error => {
    if (stopped) return
    close()
    onFailure(error?.message || String(error))
  }
  async function stats() {
    try {
      const report = await pc.getStats()
      if (stopped) return
      const transport = [...report.values()].find(row => row.type === 'transport' && row.selectedCandidatePairId)
      const pair = report.get(transport?.selectedCandidatePairId) || [...report.values()].find(row =>
        row.type === 'candidate-pair' && row.state === 'succeeded' && row.nominated)
      const protocol = report.get(pair?.localCandidateId)?.protocol
      for (const row of report.values()) {
        if (row.type !== 'inbound-rtp' || (row.kind || row.mediaType) !== 'video') continue
        const codec = report.get(row.codecId)?.mimeType || ''
        if (codec && !/^video\/h264$/i.test(codec)) throw new Error(`协商编码不是 H.264：${codec}`)
        const frames = row.framesDecoded || 0
        if (frames > (previous?.frames || 0)) {
          lastFrame = performance.now(); clearTimeout(timeout); onReady()
        }
        const seconds = previous ? (row.timestamp - previous.timestamp) / 1000 : 0
        if (seconds > 0) onStats({
          fps: Math.max(0, (frames - previous.frames) / seconds),
          mbps: Math.max(0, (row.bytesReceived - previous.bytes) * 8 / seconds / 1e6),
          codec: codec || 'video',
          // Interval average receiver dwell time, NOT camera-to-screen latency.
          bufferMs: row.jitterBufferEmittedCount > previous.emitted && Number.isFinite(previous.delay)
            ? Math.max(0, (row.jitterBufferDelay - previous.delay) * 1000 / (row.jitterBufferEmittedCount - previous.emitted)) : null,
          rttMs: Number.isFinite(pair?.currentRoundTripTime) ? pair.currentRoundTripTime * 1000 : null,
          protocol,
        })
        previous = { frames, bytes: row.bytesReceived, timestamp: row.timestamp,
          emitted: row.jitterBufferEmittedCount, delay: row.jitterBufferDelay }
      }
      if (previous?.frames > 0 && performance.now() - lastFrame > 5000) throw new Error('视频解码已停滞')
      statsTimer = setTimeout(stats, 1000)
    } catch (error) { fail(error) }
  }
  // Defer setup so callers receive the cleanup function even on immediate failure.
  queueMicrotask(() => {
    if (stopped) return
    try {
      if (typeof RTCPeerConnection === 'undefined' || typeof RTCRtpReceiver === 'undefined') {
        throw new Error('浏览器不支持 WebRTC')
      }
      const codecs = RTCRtpReceiver.getCapabilities?.('video')?.codecs.filter(c => /^video\/H264$/i.test(c.mimeType)) || []
      if (!codecs.length) throw new Error('此浏览器未提供 H.264 WebRTC 解码能力')
      pc = new RTCPeerConnection({ iceServers: [], bundlePolicy: 'max-bundle' })
      const transceiver = pc.addTransceiver('video', { direction: 'recvonly' })
      if (!transceiver.setCodecPreferences) throw new Error('浏览器不支持指定视频编码')
      transceiver.setCodecPreferences(codecs)
      // A best-effort browser hint; unsupported browsers keep their default.
      if (transceiver.receiver && 'jitterBufferTarget' in transceiver.receiver) {
        try { transceiver.receiver.jitterBufferTarget = 0 } catch { /* optional capability */ }
      }
      pc.ontrack = event => {
        if (stopped) return
        video.srcObject = new MediaStream([event.track])
        video.play().catch(error => { if (!stopped) fail(error) })
      }
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === 'failed') fail(new Error('WebRTC 连接失败'))
      }
      const send = (type, value) => socket.send(JSON.stringify({ type, value }))
      pc.onicecandidate = event => {
        if (!event.candidate || stopped) return
        const value = event.candidate.candidate
        if (offerSent) send('webrtc/candidate', value)
        else localCandidates.push(value)
      }
      socket = new WebSocket(deviceWebSocketUrl(`/api/preview/webrtc?source=${encodeURIComponent(source)}`))
      socket.onopen = async () => {
        try {
          const offer = await pc.createOffer()
          if (stopped) return
          await pc.setLocalDescription(offer)
          if (stopped) return
          send('webrtc/offer', pc.localDescription.sdp)
          offerSent = true
          for (const value of localCandidates) send('webrtc/candidate', value)
          localCandidates.length = 0
        } catch (error) { fail(error) }
      }
      socket.onmessage = event => {
        // Serialize answer/candidates: addIceCandidate must follow setRemoteDescription.
        chain = chain.then(async () => {
          if (stopped) return
          const msg = JSON.parse(event.data)
          if (msg.type === 'error') throw new Error(msg.value)
          if (msg.type === 'webrtc/answer') {
            if (!/ H264\/90000/i.test(msg.value)) throw new Error('服务端未协商 H.264')
            await pc.setRemoteDescription({ type: 'answer', sdp: msg.value })
            remoteReady = true
            for (const candidate of candidates) await pc.addIceCandidate(candidate)
            candidates = []
          } else if (msg.type === 'webrtc/candidate' && msg.value) {
            const candidate = { candidate: msg.value, sdpMid: '0', sdpMLineIndex: 0 }
            if (remoteReady) await pc.addIceCandidate(candidate)
            else candidates.push(candidate)
          }
        }).catch(fail)
      }
      socket.onerror = () => fail(new Error('WebRTC 信令连接失败'))
      socket.onclose = () => fail(new Error('WebRTC 信令已断开'))
      timeout = setTimeout(() => fail(new Error('10 秒内未解码出视频画面')), 10000)
      statsTimer = setTimeout(stats, 1000)
    } catch (error) { fail(error) }
  })
  return { close }
}
