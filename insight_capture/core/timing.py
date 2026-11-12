"""Clock-domain sanity bounds, separate from NTP precision measurements."""

# Match the live image timestamp check. These allow ordinary acquisition and
# delivery latency; they are not synchronization-accuracy tolerances.
SOURCE_CLOCK_MIN_OFFSET_NS = -2_000_000_000
SOURCE_CLOCK_MAX_OFFSET_NS = 1_000_000_000


def source_clock_consistent(stamp_ns: int, received_ns: int) -> bool:
    return SOURCE_CLOCK_MIN_OFFSET_NS <= stamp_ns - received_ns <= SOURCE_CLOCK_MAX_OFFSET_NS
