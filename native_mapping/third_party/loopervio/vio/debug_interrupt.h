// Copyright 2020 DeepMirror Inc. All rights reserved.
//
// A process-wide "the user asked to stop" flag for the debug / replay builds.
//
// The front-end debug windows step frame by frame on a blocking cv::waitKey(0),
// and there was no way out of such a session: the bindings were 'p'/'s'/'w' in
// the naive_vio window and 'q' meaning "hide this window" in the front-end one,
// while the host's own Quit button could not reach the data-feed loop at all.
// Both windows now set this flag, the host polls it to leave its feed loop, and
// every blocking wait polls in short slices so a stop requested from *another*
// thread (a GUI button, a signal) is honoured without needing a keypress in the
// OpenCV window first.
#pragma once

#include <atomic>

namespace dm {
namespace vio {

// Defined in one translation unit so it stays a single object even though the
// algorithm library and its host are separate shared objects.
std::atomic<bool>& DebugStopRequested();

inline void RequestDebugStop() { DebugStopRequested().store(true, std::memory_order_relaxed); }
inline bool DebugStopWasRequested() {
  return DebugStopRequested().load(std::memory_order_relaxed);
}

// cv::waitKey(delay_ms) with two differences: a stop request from any thread
// ends the wait, and delay_ms <= 0 means "wait indefinitely, but keep checking".
// 'q', 'Q' and ESC request the stop themselves and report as no key.
// Returns the key code, or -1 for "nothing pressed / the wait was cut short".
//
// Only ever called from FRONTEND_DEBUG_MODE code, which naive_vio.h gates on
// __x86_64__; on other targets this is a stub that returns -1 so no highgui
// dependency is added to the on-device build.
int DebugWaitKey(int delay_ms);

}  // namespace vio
}  // namespace dm
