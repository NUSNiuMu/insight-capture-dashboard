// Copyright 2020 DeepMirror Inc. All rights reserved.

#ifndef VIO_EXT_MUTEX_H_
#define VIO_EXT_MUTEX_H_

#include <shared_mutex>
#include <mutex>

// Enable thread safety attributes only with clang.
// The attributes can be safely erased when compiling with other compilers.
// http://clang.llvm.org/docs/ThreadSafetyAnalysis.html
#if defined(__clang__)
#define THREAD_ANNOTATION_ATTRIBUTE__(x) __attribute__((x))
#else
#define THREAD_ANNOTATION_ATTRIBUTE__(x)  // no-op
#endif

// GUARDED_BY()
//
// Documents if a shared field or global variable needs to be protected by a
// mutex. GUARDED_BY() allows the user to specify a particular mutex that
// should be held when accessing the annotated variable.
//
// Example:
//   Mutex mu;
//   int p1 GUARDED_BY(mu);
#define GUARDED_BY(x) THREAD_ANNOTATION_ATTRIBUTE__(guarded_by(x))

// PT_GUARDED_BY()
//
// Documents if the memory location pointed to by a pointer should be guarded
// by a mutex when dereferencing the pointer.
//
// Example:
//   Mutex mu;
//   int *p1 PT_GUARDED_BY(mu);
//
// Note that a pointer variable to a shared memory location could itself be a
// shared variable.
//
// Example:
//   // `q`, guarded by `mu1`, points to a shared memory location that is
//   // guarded by `mu2`:
//   int *q GUARDED_BY(mu1) PT_GUARDED_BY(mu2);
#define PT_GUARDED_BY(x) THREAD_ANNOTATION_ATTRIBUTE__(pt_guarded_by(x))

// ACQUIRED_AFTER() / ACQUIRED_BEFORE()
//
// Documents the acquisition order between locks that can be held
// simultaneously by a thread. For any two locks that need to be annotated
// to establish an acquisition order, only one of them needs the annotation.
// (i.e. You don't have to annotate both locks with both ACQUIRED_AFTER
// and ACQUIRED_BEFORE.)
//
// Example:
//   Mutex m1;
//   Mutex m2 ACQUIRED_AFTER(m1);
//
// Note: ACQUIRED_BEFORE() and ACQUIRED_AFTER() are currently unimplemented.
#define ACQUIRED_AFTER(...) THREAD_ANNOTATION_ATTRIBUTE__(acquired_after(__VA_ARGS__))
#define ACQUIRED_BEFORE(...) THREAD_ANNOTATION_ATTRIBUTE__(acquired_before(__VA_ARGS__))

// REQUIRES() / REQUIRES_SHARED()
//
// Documents a function that expects a mutex to be held prior to entry.
// The mutex is expected to be held both on entry to, and exit from, the
// function.
//
// Example:
//   Mutex mu1, mu2;
//   int a GUARDED_BY(mu1);
//   int b GUARDED_BY(mu2);
//
//   void foo() REQUIRES(mu1, mu2) { ... };
#define REQUIRES(...) THREAD_ANNOTATION_ATTRIBUTE__(requires_capability(__VA_ARGS__))
#define REQUIRES_SHARED(...) THREAD_ANNOTATION_ATTRIBUTE__(requires_shared_capability(__VA_ARGS__))

// ACQUIRE() / ACQUIRE_SHARED()
//
// Documents functions that acquire a lock / a shared (reader) lock in the body
// of a function, and do not release it.
#define ACQUIRE(...) THREAD_ANNOTATION_ATTRIBUTE__(acquire_capability(__VA_ARGS__))
#define ACQUIRE_SHARED(...) THREAD_ANNOTATION_ATTRIBUTE__(acquire_shared_capability(__VA_ARGS__))

// RELEASE() / REQUIRES_SHARED()
//
// Documents functions that expect a lock / a shared (reader) lock to be held
// on entry to the function, and release it in the body of the function.
#define RELEASE(...) THREAD_ANNOTATION_ATTRIBUTE__(release_capability(__VA_ARGS__))
#define RELEASE_SHARED(...) THREAD_ANNOTATION_ATTRIBUTE__(release_shared_capability(__VA_ARGS__))

// TRY_ACQUIRE() / TRY_ACQUIRE_SHARED()
//
// Documents functions that try to acquire a lock, and return success or failure
// (or a non-boolean value that can be interpreted as a boolean).
// The first argument should be `true` for functions that return `true` on
// success, or `false` for functions that return `false` on success. The second
// argument specifies the mutex that is locked on success. If unspecified, this
// mutex is assumed to be `this`.
#define TRY_ACQUIRE(...) THREAD_ANNOTATION_ATTRIBUTE__(try_acquire_capability(__VA_ARGS__))
#define TRY_ACQUIRE_SHARED(...) \
  THREAD_ANNOTATION_ATTRIBUTE__(try_acquire_shared_capability(__VA_ARGS__))

// NO_THREAD_SAFETY_ANALYSIS
//
// Documents functions that turn off thread safety checking.
// It provides an escape hatch for functions which are either
//   (1) deliberately thread-unsafe,
//   (2) thread-safe, but too complicated for the analysis to understand.
// Reasons for (2) will be described in the Known Limitations:
// https://clang.llvm.org/docs/ThreadSafetyAnalysis.html#limitations
#define NO_THREAD_SAFETY_ANALYSIS THREAD_ANNOTATION_ATTRIBUTE__(no_thread_safety_analysis)

// CAPABILITY
//
// Documents if a class/type is a lockable type (such as the `Mutex` class).
#define CAPABILITY(x) THREAD_ANNOTATION_ATTRIBUTE__(capability(x))

// SCOPED_CAPABILITY
//
// Documents if a class does RAII locking (such as the `MutexLock` class).
// The constructor should use `LOCK_FUNCTION()` to specify the mutex that is
// acquired, and the destructor should use `UNLOCK_FUNCTION()` with no
// arguments; the analysis will assume that the destructor unlocks whatever the
// constructor locked.
#define SCOPED_CAPABILITY THREAD_ANNOTATION_ATTRIBUTE__(scoped_lockable)

namespace dm {

// Mutex is a wrapper for std::shared_mutex so that we can annotate them with
// thread safety attributes and use the -Wthread-safety warning with clang.
// The standard library types cannot be used directly because they do not
// provided the required annotations.
// This type meets C++ named requirements SharedMutex:
// https://en.cppreference.com/w/cpp/named_req/SharedMutex
class CAPABILITY("mutex") Mutex {
 public:
  Mutex() = default;
  ~Mutex() = default;

  // Acquire/lock this mutex exclusively.
  // Only one thread can have exclusive access at any one time.
  // Write operations to guarded data require an exclusive lock.
  void lock() ACQUIRE() { mu_.lock(); }

  // Acquire/lock this mutex for read operations, which require only a shared
  // lock. This assumes a multiple-reader, single writer semantics.
  // Multiple threads may acquire the mutex simultaneously as readers,
  // but a writer must wait for all of them to release the mutex before it can
  // acquire it exclusively.
  void lock_shared() ACQUIRE_SHARED() { mu_.lock_shared(); }

  // Release/unlock an exclusive mutex.
  void unlock() RELEASE() { mu_.unlock(); }

  // Release/unlock a shared mutex.
  void unlock_shared() RELEASE_SHARED() { mu_.unlock_shared(); }

  // Try to acquire the mutex.
  // Returns true on success, and false on failure.
  bool try_lock() TRY_ACQUIRE(true) { return mu_.try_lock(); }

  // Try to acquire the mutex for read operations.
  // Returns true on success, and false on failure.
  bool try_lock_shared() TRY_ACQUIRE_SHARED(true) { return mu_.try_lock_shared(); }

  // For negative capabilities.
  const Mutex& operator!() const { return *this; }

 private:
  std::shared_mutex mu_;
};

// MutexLock is an RAII class that acquires a Mutex and lock it in its
// constructor, and releases it in its destructor.
class SCOPED_CAPABILITY MutexLock {
 public:
  // NOLINTNEXTLINE: No lint for non-const reference parameter.
  explicit MutexLock(Mutex& mu) ACQUIRE(mu) : lk_(mu) {}
  ~MutexLock() RELEASE() = default;

  // Returns the underlying lock. Could be used in std::condition_variable_any.wait().
  std::unique_lock<Mutex>* mutable_lock() { return &lk_; }

 private:
  std::unique_lock<Mutex> lk_;
};

// MutexLockShared is an RAII class that acquires a Mutex and shared lock it
// in its constructor, and releases it in its destructor.
class SCOPED_CAPABILITY MutexLockShared {
 public:
  // NOLINTNEXTLINE: No lint for non-const reference parameter.
  explicit MutexLockShared(Mutex& mu) ACQUIRE_SHARED(mu) : lk_(mu) {}
  // Note the reason why using RELEASE() instead of RELEASE_SHARED():
  // https://bugs.llvm.org/show_bug.cgi?id=33504
  ~MutexLockShared() RELEASE() = default;

  // Returns the underlying lock. Could be used in std::condition_variable_any.wait().
  std::shared_lock<Mutex>* mutable_lock() { return &lk_; }

 private:
  std::shared_lock<Mutex> lk_;
};

}  // namespace dm

#endif  // VIO_EXT_MUTEX_H_
