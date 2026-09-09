// Count compositor submissions, including frames skipped between JS callbacks.
export class PresentedFrameRate {
  constructor(windowMs = 1500) {
    this.windowMs = windowMs;
    this.samples = [];
  }

  record(frames, at) {
    if (!Number.isFinite(frames) || frames < 0 || !Number.isFinite(at)) return;
    const last = this.samples[this.samples.length - 1];
    if (last && (frames < last.frames || at < last.at)) this.samples = [];
    else if (last && (frames === last.frames || at === last.at)) return;
    this.samples.push({ frames, at });
    while (this.samples.length > 2 && this.samples[1].at < at - this.windowMs) {
      this.samples.shift();
    }
  }

  fps(now) {
    const first = this.samples[0];
    const last = this.samples[this.samples.length - 1];
    if (!first || first === last || now - last.at >= this.windowMs) return 0;
    return (last.frames - first.frames) * 1000 / (Math.max(now, last.at) - first.at);
  }
}
