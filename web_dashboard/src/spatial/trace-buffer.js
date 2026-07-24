export function applyTraceUpdate(traceCaches, pose, capacity, mapPoint = (point) => point) {
  const update = pose.trace_update;
  if (!update) {
    const legacyPoints = Array.isArray(pose.trace) ? pose.trace : [];
    traceCaches.set(pose.role, {
      generation: 0,
      firstSeq: 1,
      lastSeq: legacyPoints.length,
      points: legacyPoints.slice(-capacity).map(mapPoint),
    });
    return true;
  }

  const generation = Number(update.generation);
  const fromSeq = Number(update.from_seq);
  const toSeq = Number(update.to_seq);
  const points = Array.isArray(update.points) ? update.points : [];
  const existing = traceCaches.get(pose.role);
  if (update.mode === "snapshot") {
    const keptPoints = points.slice(-capacity).map(mapPoint);
    traceCaches.set(pose.role, {
      generation,
      firstSeq: keptPoints.length > 0 ? toSeq - keptPoints.length + 1 : toSeq + 1,
      lastSeq: toSeq,
      points: keptPoints,
    });
    return true;
  }
  if (!existing || existing.generation !== generation) {
    traceCaches.delete(pose.role);
    return false;
  }
  if (toSeq <= existing.lastSeq) {
    return true;
  }
  if (fromSeq > existing.lastSeq + 1) {
    traceCaches.delete(pose.role);
    return false;
  }

  const overlap = Math.max(0, existing.lastSeq - fromSeq + 1);
  existing.points.push(...points.slice(overlap).map(mapPoint));
  existing.lastSeq = toSeq;
  const dropBeforeSeq = Number(update.drop_before_seq);
  const serverDropCount = Number.isFinite(dropBeforeSeq)
    ? Math.max(0, dropBeforeSeq - existing.firstSeq)
    : 0;
  const capacityDropCount = Math.max(0, existing.points.length - capacity);
  const dropCount = Math.min(
    existing.points.length,
    Math.max(serverDropCount, capacityDropCount)
  );
  if (dropCount > 0) {
    existing.points.splice(0, dropCount);
    existing.firstSeq += dropCount;
  }
  if (existing.points.length === 0) {
    existing.firstSeq = existing.lastSeq + 1;
  }
  return true;
}
