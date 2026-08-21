"""建图线程使用的时间戳限频、VIO 插值缓存和双目近时同步。

图像与 VIO 来自不同 ROS 话题，回调到达顺序不等于采样顺序。这里所有选择都基于
消息自身的纳秒时间戳；缓存遇到时间倒退会清空，避免设备重启后把新会话样本与旧
时间轴插值到一起。
"""

from __future__ import annotations

import bisect
import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Generic, Optional, Tuple, TypeVar

from .geometry import PoseSample, interpolate_pose


T = TypeVar("T")


def select_timestamp(
    stamp_ns: int, next_sample_ns: int, target_hz: float
) -> tuple[bool, int]:
    """按稳定的时间戳截止线限频，而不是按回调到达的墙钟时间限频。"""

    frequency = float(target_hz)
    if not math.isfinite(frequency) or frequency <= 0.0:
        raise ValueError("target_hz must be positive and finite")
    stamp_ns = int(stamp_ns)
    next_sample_ns = int(next_sample_ns)
    period_ns = max(1, int(round(1_000_000_000 / frequency)))
    if next_sample_ns > 0 and stamp_ns < next_sample_ns:
        return False, next_sample_ns
    if next_sample_ns <= 0 or stamp_ns - next_sample_ns >= period_ns:
        next_sample_ns = stamp_ns
    return True, next_sample_ns + period_ns


class PoseBuffer:
    """保留近期 VIO，并在图像时间戳处插值得到同步位姿。"""

    def __init__(
        self,
        *,
        max_samples: int = 1000,
        max_bracket_gap_ns: int = 50_000_000,
    ) -> None:
        self._max_samples = max(2, int(max_samples))
        self._max_bracket_gap_ns = max(1, int(max_bracket_gap_ns))
        self._samples: Deque[PoseSample] = deque(maxlen=self._max_samples)
        self._lock = threading.Lock()

    def append(self, sample: PoseSample) -> bool:
        """Append an increasing sample; clear the buffer after a clock rollback."""

        with self._lock:
            if self._samples and sample.stamp_ns == self._samples[-1].stamp_ns:
                return False
            # 时间倒退意味着相机时间轴或 VIO 会话已重置，旧缓存不可继续插值。
            reset = bool(self._samples and sample.stamp_ns < self._samples[-1].stamp_ns)
            if reset:
                self._samples.clear()
            self._samples.append(sample)
            return reset

    def lookup(self, stamp_ns: int) -> Optional[PoseSample]:
        """Return an interpolated pose, or ``None`` when no tight bracket exists."""

        stamp_ns = int(stamp_ns)
        with self._lock:
            samples = tuple(self._samples)
        if not samples:
            return None
        stamps = [sample.stamp_ns for sample in samples]
        index = bisect.bisect_left(stamps, stamp_ns)
        if index < len(samples) and samples[index].stamp_ns == stamp_ns:
            return samples[index]
        # 不外推到缓存范围之外；缺少包围样本时由调用方等待后续 VIO。
        if index == 0 or index == len(samples):
            return None
        first, second = samples[index - 1], samples[index]
        if (
            stamp_ns - first.stamp_ns > self._max_bracket_gap_ns
            or second.stamp_ns - stamp_ns > self._max_bracket_gap_ns
        ):
            return None
        return interpolate_pose(first, second, stamp_ns)

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()


@dataclass(frozen=True)
class StereoPair(Generic[T]):
    """A synchronized left/right payload pair."""

    left_stamp_ns: int
    left: T
    right_stamp_ns: int
    right: T

    @property
    def stamp_ns(self) -> int:
        return (self.left_stamp_ns + self.right_stamp_ns) // 2


class StereoPairSynchronizer(Generic[T]):
    """以最近时间戳配对左右目，同时限制回调中的缓存和搜索工作量。"""

    def __init__(
        self,
        *,
        tolerance_ns: int = 20_000_000,
        queue_size: int = 8,
    ) -> None:
        self._tolerance_ns = max(0, int(tolerance_ns))
        self._left: Deque[Tuple[int, T]] = deque(maxlen=max(2, int(queue_size)))
        self._right: Deque[Tuple[int, T]] = deque(maxlen=max(2, int(queue_size)))
        self._lock = threading.Lock()

    def push_left(self, stamp_ns: int, payload: T) -> Optional[StereoPair[T]]:
        return self._push(self._left, self._right, int(stamp_ns), payload, True)

    def push_right(self, stamp_ns: int, payload: T) -> Optional[StereoPair[T]]:
        return self._push(self._right, self._left, int(stamp_ns), payload, False)

    def _push(
        self,
        own: Deque[Tuple[int, T]],
        other: Deque[Tuple[int, T]],
        stamp_ns: int,
        payload: T,
        is_left: bool,
    ) -> Optional[StereoPair[T]]:
        with self._lock:
            own.append((stamp_ns, payload))
            if not other:
                return None
            # 小队列内精确选择最近时间戳，避免按到达顺序错误配对抖动帧。
            other_index = min(
                range(len(other)), key=lambda index: abs(other[index][0] - stamp_ns)
            )
            other_stamp, other_payload = other[other_index]
            if abs(other_stamp - stamp_ns) > self._tolerance_ns:
                cutoff = stamp_ns - self._tolerance_ns
                while other and other[0][0] < cutoff:
                    other.popleft()
                return None
            del other[other_index]
            own.pop()
            if is_left:
                return StereoPair(stamp_ns, payload, other_stamp, other_payload)
            return StereoPair(other_stamp, other_payload, stamp_ns, payload)

    def clear(self) -> None:
        with self._lock:
            self._left.clear()
            self._right.clear()
