const CONNECTIONS = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16],
  [13, 17], [17, 18], [18, 19], [19, 20], [0, 17],
];

const COLORS = { L: "#79adc2", R: "#cf7f6f" };

function unpack(flat) {
  const points = [];
  for (let index = 0; index + 2 < flat.length; index += 3) {
    points.push([Number(flat[index]), Number(flat[index + 1]), Number(flat[index + 2])]);
  }
  return points;
}

function formatTime(milliseconds) {
  const total = Math.max(0, Number(milliseconds) || 0);
  const minutes = Math.floor(total / 60000);
  const seconds = Math.floor((total % 60000) / 1000);
  const millis = Math.floor(total % 1000);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

export function createHandPoseViewer({ canvas, empty, timeline, playButton, timeLabel, coordinateLabel }) {
  const context = canvas.getContext("2d");
  const state = {
    frames: [],
    method: "",
    index: 0,
    playing: false,
    yaw: -0.45,
    pitch: 0.25,
    zoom: 1,
    dragging: false,
    lastX: 0,
    lastY: 0,
    animation: 0,
    lastStep: 0,
    selected: null,
  };

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(rect.width * ratio));
    const height = Math.max(1, Math.round(rect.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  function rotate(point) {
    const [x, y, z] = point;
    const cy = Math.cos(state.yaw);
    const sy = Math.sin(state.yaw);
    const cp = Math.cos(state.pitch);
    const sp = Math.sin(state.pitch);
    const rx = x * cy - z * sy;
    const rz = x * sy + z * cy;
    return [rx, y * cp - rz * sp, y * sp + rz * cp];
  }

  function frameGeometry(frame) {
    const sourceHands = frame?.h || [];
    const hands = sourceHands.map((hand, index) => {
      const rawPoints = unpack(hand.p || []);
      const relativeOffset = state.method === "mediapipe" && sourceHands.length > 1
        ? (index - (sourceHands.length - 1) / 2) * 0.16
        : 0;
      return {
        ...hand,
        rawPoints,
        points: rawPoints.map(([x, y, z]) => [x + relativeOffset, y, z]),
      };
    });
    const all = hands.flatMap((hand) => hand.points);
    if (!all.length) return { hands, center: [0, 0, 0], extent: 1 };
    const center = [0, 1, 2].map((axis) => all.reduce((sum, point) => sum + point[axis], 0) / all.length);
    let extent = 0;
    all.forEach((point) => {
      extent = Math.max(extent, Math.abs(point[0] - center[0]), Math.abs(point[1] - center[1]), Math.abs(point[2] - center[2]));
    });
    return { hands, center, extent: Math.max(extent, 0.025) };
  }

  function project(point, center, extent, width, height) {
    const rotated = rotate(point.map((value, axis) => value - center[axis]));
    const scale = Math.min(width, height) * 0.34 * state.zoom / extent;
    return [width / 2 + rotated[0] * scale, height / 2 + rotated[1] * scale, rotated[2]];
  }

  function drawGrid(width, height) {
    context.strokeStyle = "rgba(207,196,170,.38)";
    context.lineWidth = 1;
    for (let index = -4; index <= 4; index += 1) {
      const offset = index * Math.min(width, height) / 10;
      context.beginPath();
      context.moveTo(width / 2 + offset, height * 0.18);
      context.lineTo(width / 2 + offset, height * 0.82);
      context.stroke();
      context.beginPath();
      context.moveTo(width * 0.18, height / 2 + offset);
      context.lineTo(width * 0.82, height / 2 + offset);
      context.stroke();
    }
  }

  function draw() {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = canvas.width / ratio;
    const height = canvas.height / ratio;
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#17130c";
    context.fillRect(0, 0, width, height);
    drawGrid(width, height);
    const frame = state.frames[state.index];
    const geometry = frameGeometry(frame);
    const projected = [];
    geometry.hands.forEach((hand, handIndex) => {
      const points = hand.points.map((point) => project(point, geometry.center, geometry.extent, width, height));
      const color = COLORS[hand.c] || "#ffb020";
      context.strokeStyle = color;
      context.lineWidth = 2;
      CONNECTIONS.forEach(([start, end]) => {
        if (!points[start] || !points[end]) return;
        context.beginPath();
        context.moveTo(points[start][0], points[start][1]);
        context.lineTo(points[end][0], points[end][1]);
        context.stroke();
      });
      points.forEach((point, pointIndex) => {
        context.beginPath();
        context.fillStyle = pointIndex === 0 ? "#ffb020" : color;
        context.arc(point[0], point[1], pointIndex === 0 ? 5 : 3.2, 0, Math.PI * 2);
        context.fill();
        projected.push({ x: point[0], y: point[1], handIndex, pointIndex, source: hand.rawPoints[pointIndex], side: hand.c });
      });
    });
    state.projected = projected;
    timeline.value = String(state.index);
    timeLabel.textContent = formatTime(frame?.t || 0);
  }

  function setFrames(records, options = {}) {
    state.frames = Array.isArray(records) ? records.filter((frame) => Array.isArray(frame?.h) && frame.h.length) : [];
    state.method = options.method || "";
    state.index = 0;
    state.playing = false;
    playButton.textContent = "Play";
    playButton.disabled = state.frames.length < 2;
    timeline.disabled = state.frames.length < 2;
    timeline.max = String(Math.max(0, state.frames.length - 1));
    timeline.value = "0";
    empty.hidden = state.frames.length > 0;
    canvas.hidden = state.frames.length === 0;
    coordinateLabel.textContent = state.frames.length ? `${state.frames.length.toLocaleString()} detected frames` : "No landmark selected";
    draw();
  }

  function tick(timestamp) {
    if (!state.playing) return;
    const currentTime = Number(state.frames[state.index]?.t || 0);
    const nextTime = Number(state.frames[(state.index + 1) % state.frames.length]?.t || currentTime + 33);
    const interval = Math.min(250, Math.max(16, nextTime > currentTime ? nextTime - currentTime : 33));
    if (!state.lastStep || timestamp - state.lastStep >= interval) {
      state.index = (state.index + 1) % state.frames.length;
      state.lastStep = timestamp;
      draw();
    }
    state.animation = requestAnimationFrame(tick);
  }

  playButton.addEventListener("click", () => {
    if (state.frames.length < 2) return;
    state.playing = !state.playing;
    playButton.textContent = state.playing ? "Pause" : "Play";
    state.lastStep = 0;
    cancelAnimationFrame(state.animation);
    if (state.playing) state.animation = requestAnimationFrame(tick);
  });
  timeline.addEventListener("input", () => {
    state.index = Number(timeline.value);
    draw();
  });
  canvas.addEventListener("pointerdown", (event) => {
    state.dragging = true;
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!state.dragging) return;
    state.yaw += (event.clientX - state.lastX) * 0.008;
    state.pitch = Math.max(-1.4, Math.min(1.4, state.pitch + (event.clientY - state.lastY) * 0.008));
    state.lastX = event.clientX;
    state.lastY = event.clientY;
    draw();
  });
  canvas.addEventListener("pointerup", () => { state.dragging = false; });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    state.zoom = Math.max(0.35, Math.min(4, state.zoom * Math.exp(-event.deltaY * 0.001)));
    draw();
  }, { passive: false });
  canvas.addEventListener("click", (event) => {
    if (!state.projected?.length) return;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const nearest = state.projected.reduce((best, item) => {
      const distance = Math.hypot(item.x - x, item.y - y);
      return !best || distance < best.distance ? { ...item, distance } : best;
    }, null);
    if (!nearest || nearest.distance > 18) return;
    const [px, py, pz] = nearest.source;
    coordinateLabel.textContent = `${nearest.side} hand · landmark ${nearest.pointIndex} · x ${px.toFixed(4)} · y ${py.toFixed(4)} · z ${pz.toFixed(4)}`;
  });
  new ResizeObserver(resize).observe(canvas);
  window.addEventListener("resize", resize);
  resize();
  setFrames([]);

  return { setFrames };
}
