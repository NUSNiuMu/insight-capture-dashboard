const PREPARED_DISPLAY_FPS = 60;

export function createPreparedPosePayloadBuilder(manifest, livePosePayload = null) {
  const baseByName = new Map(
    (livePosePayload?.poses || []).map((pose) => [pose.name, pose])
  );
  const traceSequences = new Map();
  const trajectoryFrames = new Map();
  const poses = Array.isArray(manifest?.poses) ? manifest.poses : [];
  const frameCount = Math.max(1, Number(manifest?.frame_count || 1));

  for (const pose of poses) {
    trajectoryFrames.set(pose.name, deriveTrajectoryFrameIndices(pose));
  }

  return (framePosition, snapshot = false) => {
    const clampedFrame = Math.min(
      frameCount - 1,
      Math.max(0, Number(framePosition) || 0)
    );
    const traceFrame = Math.floor(clampedFrame + 1e-6);
    const payloadPoses = poses.map((pose) => {
      const base = baseByName.get(pose.name) || {};
      const trace = Array.isArray(pose.trajectory) ? pose.trajectory : [];
      const frames = trajectoryFrames.get(pose.name) || [];
      const targetSequence = upperBound(frames, traceFrame);
      const previousSequence = traceSequences.get(pose.role) || 0;
      const needsSnapshot = snapshot || targetSequence < previousSequence;
      const fromSequence = needsSnapshot ? 1 : previousSequence + 1;
      const points = needsSnapshot
        ? trace.slice(0, targetSequence)
        : trace.slice(previousSequence, targetSequence);
      traceSequences.set(pose.role, targetSequence);
      const interpolated = interpolatePreparedPose(pose, clampedFrame);
      return {
        ...base,
        name: pose.name,
        role: pose.role,
        visible: interpolated.visible,
        position: interpolated.position,
        quaternion_xyzw: interpolated.quaternion,
        avatar_model: pose.avatar_model,
        avatar_scale: pose.avatar_scale,
        avatar_rotation_deg_xyz: pose.avatar_rotation_deg_xyz,
        avatar_offset_xyz: pose.avatar_offset_xyz,
        asset_url: pose.avatar_model
          ? `/asset?path=${encodeURIComponent(pose.avatar_model)}`
          : null,
        trace_update: {
          mode: needsSnapshot ? "snapshot" : "delta",
          generation: 1,
          from_seq: fromSequence,
          to_seq: targetSequence,
          drop_before_seq: 1,
          points,
        },
      };
    });
    return {
      type: "pose_update",
      stick_figure_mode: Boolean(livePosePayload?.stick_figure_mode),
      display_fps_limit: PREPARED_DISPLAY_FPS,
      trace_capacity: Math.max(
        2,
        ...poses.map((pose) => Array.isArray(pose.trajectory) ? pose.trajectory.length : 0)
      ),
      trace_generation: 1,
      poses: payloadPoses,
    };
  };
}

export function deriveTrajectoryFrameIndices(pose) {
  const valid = Array.isArray(pose?.valid) ? pose.valid : [];
  const trajectoryCount = Array.isArray(pose?.trajectory) ? pose.trajectory.length : 0;
  const validFrames = [];
  valid.forEach((isValid, index) => {
    if (isValid) validFrames.push(index);
  });
  if (trajectoryCount <= 0 || validFrames.length <= 0) return [];
  if (trajectoryCount >= validFrames.length) {
    return validFrames.slice(0, trajectoryCount);
  }
  if (trajectoryCount === 1) return [validFrames[0]];
  // Prepared manifests cap long trails by evenly sampling valid pose frames,
  // but older caches do not store those source indices explicitly.
  return Array.from({ length: trajectoryCount }, (_, index) => {
    const selected = Math.round(index * (validFrames.length - 1) / (trajectoryCount - 1));
    return validFrames[selected];
  });
}

export function interpolatePreparedPose(pose, framePosition) {
  const positions = Array.isArray(pose?.positions) ? pose.positions : [];
  const quaternions = Array.isArray(pose?.quaternions_xyzw)
    ? pose.quaternions_xyzw
    : [];
  const lastIndex = Math.max(0, Math.min(positions.length, quaternions.length) - 1);
  const clamped = Math.min(lastIndex, Math.max(0, Number(framePosition) || 0));
  const lower = Math.floor(clamped);
  const upper = Math.min(lastIndex, lower + 1);
  const alpha = clamped - lower;
  const position = interpolateVector(
    positions[lower] || [0, 0, 0],
    positions[upper] || positions[lower] || [0, 0, 0],
    alpha
  );
  const quaternion = slerpQuaternion(
    quaternions[lower] || [0, 0, 0, 1],
    quaternions[upper] || quaternions[lower] || [0, 0, 0, 1],
    alpha
  );
  const visibilityIndex = Math.min(
    Math.max(0, Math.round(clamped)),
    Math.max(0, (pose?.valid?.length || 1) - 1)
  );
  return {
    position,
    quaternion,
    visible: Boolean(pose?.valid?.[visibilityIndex]),
  };
}

function upperBound(values, target) {
  let low = 0;
  let high = values.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (values[middle] <= target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function interpolateVector(left, right, alpha) {
  return [0, 1, 2].map((axis) => {
    const start = Number(left?.[axis] || 0);
    return start + (Number(right?.[axis] || 0) - start) * alpha;
  });
}

function slerpQuaternion(left, right, alpha) {
  const start = normalizeQuaternion(left);
  let end = normalizeQuaternion(right);
  let dot = start.reduce((sum, value, index) => sum + value * end[index], 0);
  if (dot < 0) {
    end = end.map((value) => -value);
    dot = -dot;
  }
  if (dot > 0.9995) {
    return normalizeQuaternion(start.map(
      (value, index) => value + alpha * (end[index] - value)
    ));
  }
  const theta = Math.acos(Math.min(1, Math.max(-1, dot)));
  const sinTheta = Math.sin(theta);
  const startWeight = Math.sin((1 - alpha) * theta) / sinTheta;
  const endWeight = Math.sin(alpha * theta) / sinTheta;
  return start.map(
    (value, index) => value * startWeight + end[index] * endWeight
  );
}

function normalizeQuaternion(value) {
  const result = [
    Number(value?.[0] || 0),
    Number(value?.[1] || 0),
    Number(value?.[2] || 0),
    Number(value?.[3] ?? 1),
  ];
  const length = Math.hypot(...result);
  if (length <= Number.EPSILON) return [0, 0, 0, 1];
  return result.map((component) => component / length);
}
