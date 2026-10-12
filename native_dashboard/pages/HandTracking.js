function handDistance(first, second) {
  const a = first.p || [];
  const b = second.p || [];
  const length = Math.min(3, a.length, b.length);
  if (!length) return Number.POSITIVE_INFINITY;
  let squared = 0;
  for (let index = 0; index < length; index += 1) squared += (Number(a[index]) - Number(b[index])) ** 2;
  return Math.sqrt(squared / length);
}

function updateTrackLabel(track, observed) {
  if (observed === track.label) {
    track.pendingLabel = "";
    track.pendingCount = 0;
  } else if (observed === track.pendingLabel) {
    track.pendingCount += 1;
  } else {
    track.pendingLabel = observed;
    track.pendingCount = 1;
  }
  if (track.pendingCount >= 8) {
    track.label = observed;
    track.pendingLabel = "";
    track.pendingCount = 0;
  }
  return track.label;
}

function stabilizeHandedness(records) {
  const tracks = [];
  let nextId = 0;
  return records.map((frame) => {
    const timestamp = Number(frame.t || 0);
    const hands = (frame.h || []).map((hand) => Object.assign({}, hand));
    for (let index = tracks.length - 1; index >= 0; index -= 1) {
      if (timestamp - tracks[index].time > 2000) tracks.splice(index, 1);
    }
    const assignments = new Map();
    const usedTracks = new Set();
    hands.forEach((hand, handIndex) => {
      if (hand.i === undefined || hand.i === null) return;
      const trackIndex = tracks.findIndex((track) => track.sourceId === hand.i);
      if (trackIndex >= 0) {
        assignments.set(handIndex, trackIndex);
        usedTracks.add(trackIndex);
      }
    });
    const candidates = [];
    hands.forEach((hand, handIndex) => {
      if (assignments.has(handIndex)) return;
      tracks.forEach((track, trackIndex) => {
        if (usedTracks.has(trackIndex) || track.sourceId !== null) return;
        const elapsed = Math.max(1, timestamp - track.time) / 1000;
        const gate = Math.min(0.35, Math.max(0.05, 4 * elapsed));
        const distance = handDistance(hand, track.hand);
        if (distance <= gate) {
          const labelPenalty = hand.c === track.label ? 0 : 0.06;
          candidates.push([distance + labelPenalty, handIndex, trackIndex]);
        }
      });
    });
    candidates.sort((a, b) => a[0] - b[0]);
    candidates.forEach(([, handIndex, trackIndex]) => {
      if (assignments.has(handIndex) || usedTracks.has(trackIndex)) return;
      assignments.set(handIndex, trackIndex);
      usedTracks.add(trackIndex);
    });
    hands.forEach((hand, handIndex) => {
      let track;
      if (assignments.has(handIndex)) {
        track = tracks[assignments.get(handIndex)];
      } else {
        track = {
          id: nextId,
          sourceId: hand.i === undefined || hand.i === null ? null : hand.i,
          label: hand.c,
          pendingLabel: "",
          pendingCount: 0,
        };
        nextId += 1;
        tracks.push(track);
      }
      hand.i = track.sourceId === null ? track.id : track.sourceId;
      hand.c = updateTrackLabel(track, hand.c);
      track.hand = hand;
      track.time = timestamp;
    });
    const labels = new Set();
    const uniqueHands = hands.filter((hand) => {
      if (labels.has(hand.c)) return false;
      labels.add(hand.c);
      return true;
    });
    return Object.assign({}, frame, {h: uniqueHands.slice(0, 2)});
  });
}

