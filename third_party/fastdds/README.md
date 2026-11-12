# Owned Fast DDS runtime

These archives, patch and licenses are stored in this checkout. Builds never fetch
them from another repository or the network. No Ego project is consulted.

- Fast DDS 2.6.11: <https://github.com/eProsima/Fast-DDS/tree/v2.6.11>;
  Apache-2.0, original archive and license retained.
- Asio: `b84e6c16b2ea907dbad94206b7510d85aafc0b42`, the Fast DDS tag's
  submodule revision; <https://github.com/chriskohlhoff/asio/tree/b84e6c16b2ea907dbad94206b7510d85aafc0b42>;
  Boost Software License, original archive and license retained.
- `SHA256SUMS` fixes the exact inputs. `deploy/build_fastdds.sh` checks them,
  builds against the project's ROS Humble image dependencies and installs only
  the resulting library under `.native-build/fastdds/lib`.

## Fragment/GAP fix

Field diagnostics on 2026-11-12 traced a zero-length serialized Image to a
CacheChange returned to its pool by `StatefulReader::processGapMsg`, then still
visited through the non-keyed DataReader instance. The reset sequence, writer
GUID and source time were all zero. This happened both at subscription startup
and after a camera USB IPv4 interruption.

The mechanism matches <https://github.com/eProsima/Fast-DDS/issues/6567> and
<https://github.com/eProsima/Fast-DDS/pull/6571>. As checked on 2026-11-12, that PR
is **open, not merged**; its current head is
`fd3fea7598099c5a93fe12c098413bdf403fe33f`. This is a project-maintained fix for
2.6.11, not a claim of an upstream release or certification.

The patch removes the DDS instance reference before recycling a fragmented
sample covered by GAP. Its internal, non-virtual cleanup method retains the
existing no-callback GAP removal. Built-in histories keep the original path;
public class layouts, virtual tables, history limits and assembly timing are
unchanged. No empty-image filter is added to rosbag or to the original data.

This intentionally differs from the open PR's newer-version implementation:
an initial 2.6.11 port delaying instance insertion fixed the empty sample but
regressed KEEP_LAST depth 1 with out-of-order completion. That candidate was
rejected by the wire-level regression and was never deployed to the service.

`tests/runtime/dds/run_fragment_regression.sh` sends bounded, synthetic RTPS
traffic over localhost in domain 219 to test these cases. It uses the same
collection `DataReader::take` interface as ROS serialized subscriptions;
`take_next_sample` does not reproduce the field symptom on 2.6.11.

The container entrypoint selects this one runtime for the backend and inherited
ROS children. It does not replace `/opt/ros/humble` or another service's library.
A missing build artifact is an installation error, not a recording preflight
threshold. Customer image builds generate and ship the library; startup never
compiles or downloads it.
