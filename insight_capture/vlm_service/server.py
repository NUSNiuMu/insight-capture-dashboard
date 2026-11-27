"""Qwen vision-language pre-annotation service for the aruco workbench.

Works with Qwen2.5-VL (transformers 4.49+) and the natively multimodal
Qwen3.5 / 3.6 / 3.8 models (transformers 5.8+). Run it on the GPU machine:
    python -m insight_capture.vlm_service.server --model /path/to/weights
Qwen2.5-VL-7B needs ~17 GB VRAM in bf16, Qwen3.8-27B-FP8 ~30 GB.

GET  /health       -> {"status": "ok", "model": ..., "busy": bool}; only answers once the model is loaded
POST /v1/task      {"frames": [base64 JPEG...], "fps": float, "action_table": {...} | null}
                -> {"task_id", "task", "description", "flags", "raw"}
                   which task the whole recording performs
POST /v1/segments  {"frames": [...], "fps": float, "action_table": {...} | null,
                    "task_id": str, "task": str,
                    "views": [{"label": str, "frames": [...]}, ...] (optional)}
                -> {"segments": [{"start", "end", "subtask_id", "subtask", "flags"}], "flags", "raw"}
                   timed subtasks in one stretch of motion; times are seconds from its first frame.
                   With "views", every view is a separate, time-synchronised video of the
                   same span (head camera, wrist cameras...) and "frames" is ignored.
POST /v1/label     {"frames": [...], "fps": float, "spans": [[start, end], ...],
                    "action_table": {...} | null, "task_id": str, "task": str, "previous": str}
                -> {"labels": [{"subtask_id", "subtask", "confidence", "flags"}, ...], "raw"}
                   the subtask of each segment a person cut (spans: seconds from the first
                   frame, in order; boundaries stay as cut), seen together for the sequence
POST /v1/translate {"items": [{"task": str, "subtask": str}, ...], "action_table": {...} | null}
                -> {"items": [{"task", "subtask", "task_id", "subtask_id", "flags"}], "flags", "raw"}
                   Chinese labels typed by a person rewritten as English (text only, no frames)
POST /v1/ask       {"frames": [...], "fps": float, "question": str}
                -> {"answer": "yes" | "no" | "unknown", "raw"}
                   yes/no answer about the last frames (a robot's subtask post-condition check)
"""

import argparse
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

from .prompt import (
    build_label_prompt,
    build_segment_prompt,
    build_task_prompt,
    build_translate_prompt,
    parse_label_answer,
    parse_segments_answer,
    parse_task_answer,
    parse_translate_answer,
)


def prepare(frames, max_side, factor):
    """Decode frames to RGB, longest side <= max_side, sides multiples of `factor`
    (vision patch x spatial merge), even count (two-frame temporal patches)."""
    images = []
    for item in frames:
        image = cv2.imdecode(np.frombuffer(base64.b64decode(item), np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法解码的图像帧")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        h, w = image.shape[:2]
        scale = min(1.0, max_side / max(h, w))
        size = (
            max(factor, round(w * scale / factor) * factor),
            max(factor, round(h * scale / factor) * factor),
        )
        images.append(cv2.resize(image, size, interpolation=cv2.INTER_AREA))
    if len(images) % 2:
        images.append(images[-1])
    return np.stack(images)


class Model:
    def __init__(self, path, max_side):
        import torch
        from transformers import AutoConfig, AutoModelForImageTextToText, AutoProcessor

        self.torch = torch
        self.name = Path(path).name
        self.max_side = max_side
        self.family = AutoConfig.from_pretrained(path).model_type
        self.processor = AutoProcessor.from_pretrained(path)
        video = getattr(self.processor, "video_processor", None) or self.processor.image_processor
        self.factor = getattr(video, "patch_size", 14) * getattr(video, "merge_size", 2)
        try:  # quantized checkpoints load straight onto the GPU through accelerate
            import accelerate  # noqa: F401

            placement = {"device_map": "cuda"}
        except ImportError:
            placement = {}
        self.model = AutoModelForImageTextToText.from_pretrained(
            path, dtype=torch.bfloat16, attn_implementation="sdpa", **placement
        )
        if not placement:
            self.model = self.model.to("cuda")
        self.model.eval()
        self.lock = threading.Lock()  # one GPU, one request at a time

    def video_kwargs(self, videos, fps):
        if self.family == "qwen2_5_vl":
            return {"fps": [fps] * len(videos)}
        # Qwen3-style processors place timestamps from the clip's metadata;
        # the frames are already sampled, so tell it not to resample them.
        from transformers.video_utils import VideoMetadata

        return {
            "do_sample_frames": False,
            "video_metadata": [
                VideoMetadata(
                    total_num_frames=len(video),
                    fps=fps,
                    duration=len(video) / fps,
                    frames_indices=list(range(len(video))),
                )
                for video in videos
            ],
        }

    def warmup(self):
        """Full-size requests on synthetic frames, so kernels are compiled."""
        image = np.full((720, 1280, 3), 128, np.uint8)
        cv2.rectangle(image, (500, 300), (780, 520), (40, 90, 160), -1)
        frame = base64.b64encode(cv2.imencode(".jpg", image)[1].tobytes()).decode()
        self.task({"frames": [frame] * 8, "fps": 1.0})
        self.segments({"frames": [frame] * 16, "fps": 4.0, "task": "Move the box"})
        wrist = base64.b64encode(cv2.imencode(".jpg", image[:, 280:1000])[1].tobytes()).decode()
        self.segments({"fps": 4.0, "task": "Move the box", "views": [
            {"label": "头部相机", "frames": [frame] * 16}, {"label": "腕部相机", "frames": [wrist] * 16},
        ]})

    def generate(self, frames, fps, prompt, max_new_tokens, views=None):
        """`views`: [(label, frames), ...] sent as separate, synchronised videos,
        each introduced by its label; otherwise `frames` is the one video."""
        views = views or [("", frames)]
        content, videos = [], []
        for label, clip in views:
            if not isinstance(clip, list) or len(clip) < 2:
                raise ValueError(f"{label or '视频'}至少需要 2 帧图像")
            videos.append(prepare(clip, self.max_side, self.factor))
            if label:
                content.append({"type": "text", "text": f"{label}："})
            content.append({"type": "video"})
        messages = [{"role": "user", "content": content + [{"type": "text", "text": prompt}]}]
        # Thinking models answer straight away: the answers are JSON, and
        # reasoning would multiply the time per request.
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        with self.lock:
            inputs = self.processor(
                text=[text], videos=videos, return_tensors="pt", **self.video_kwargs(videos, fps)
            ).to("cuda")
            with self.torch.inference_mode():
                output = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        return self.processor.batch_decode(
            output[:, inputs.input_ids.shape[1]:], skip_special_tokens=True
        )[0].strip()

    def generate_text(self, prompt, max_new_tokens):
        """A text-only request (no frames)."""
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True, enable_thinking=False
        )
        with self.lock:
            inputs = self.processor(text=[text], return_tensors="pt").to("cuda")
            with self.torch.inference_mode():
                output = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        return self.processor.batch_decode(
            output[:, inputs.input_ids.shape[1]:], skip_special_tokens=True
        )[0].strip()

    def label(self, request):
        frames, fps = request.get("frames"), float(request.get("fps") or 4.0)
        table = request.get("action_table") or {}
        task = next((t for t in table.get("tasks", []) if t["id"] == request.get("task_id")), None)
        spans = [(float(a), float(b)) for a, b in request.get("spans") or []]
        if not spans:
            spans = [(0.0, len(frames or []) / fps)]
        prompt = build_label_prompt(spans, task, str(request.get("task") or ""), str(request.get("previous") or ""))
        answer = self.generate(frames, fps, prompt, 120 * len(spans) + 200)
        return {"labels": parse_label_answer(answer, len(spans), task), "raw": answer}

    def translate(self, request):
        items = request.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("items 须为非空列表")
        items = [{"task": str(i.get("task") or ""), "subtask": str(i.get("subtask") or "")} for i in items]
        table = request.get("action_table") or None
        answer = self.generate_text(build_translate_prompt(items, table), 160 * len(items) + 200)
        return {**parse_translate_answer(answer, items, table), "raw": answer}

    def task(self, request):
        frames, fps = request.get("frames"), float(request.get("fps") or 1.0)
        table = request.get("action_table") or None
        answer = self.generate(frames, fps, build_task_prompt(len(frames or []) / fps, table), 400)
        return {**parse_task_answer(answer, table), "raw": answer}

    def ask(self, request):
        frames, fps = request.get("frames"), float(request.get("fps") or 1.0)
        question = str(request.get("question") or "").strip()
        if not question:
            raise ValueError("question 不能为空")
        if isinstance(frames, list) and len(frames) == 1:
            frames = frames * 2  # a still frame is a two-frame clip
        prompt = (
            "These frames come from a robot's head camera; the last frame is the current state. "
            f"{question} Answer with exactly one word: yes or no."
        )
        answer = self.generate(frames, fps, prompt, 8)
        word = answer.strip().strip(".").lower()
        return {"answer": "yes" if word.startswith("yes") else "no" if word.startswith("no") else "unknown", "raw": answer}

    def segments(self, request):
        frames, fps = request.get("frames"), float(request.get("fps") or 4.0)
        table = request.get("action_table") or {}
        task = next((t for t in table.get("tasks", []) if t["id"] == request.get("task_id")), None)
        views = request.get("views") or None
        if views:
            views = [(str(v.get("label") or ""), v.get("frames")) for v in views]
            frames = views[0][1]
        duration = len(frames or []) / fps
        prompt = build_segment_prompt(
            duration, task, str(request.get("task") or ""), [label for label, _ in views or []]
        )
        answer = self.generate(frames, fps, prompt, 2048, views)
        return {**parse_segments_answer(answer, duration, task), "raw": answer}


def serve(model, host, port):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self.reply(404, {"error": "not found"})
            self.reply(200, {"status": "ok", "model": model.name, "busy": model.lock.locked()})

        def do_POST(self):
            handler = {
                "/v1/task": model.task, "/v1/segments": model.segments, "/v1/ask": model.ask,
                "/v1/label": model.label, "/v1/translate": model.translate,
            }.get(self.path)
            if handler is None:
                return self.reply(404, {"error": "not found"})
            started = time.monotonic()
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                result = handler(request)
            except (ValueError, KeyError, TypeError) as exc:
                return self.reply(400, {"error": str(exc)})
            except Exception as exc:  # keep serving after e.g. CUDA OOM
                return self.reply(500, {"error": f"{type(exc).__name__}: {exc}"})
            result["elapsed_s"] = round(time.monotonic() - started, 2)
            self.reply(200, result)

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}", flush=True)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"serving {model.name} on http://{host}:{port}", flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="Qwen 视觉模型权重目录（Qwen2.5-VL 或 Qwen3.5 / 3.6 / 3.8）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--max-side", type=int, default=448, help="送入模型前帧的最长边（像素）")
    parser.add_argument("--no-warmup", action="store_true", help="启动时不做预热推理")
    args = parser.parse_args()
    started = time.monotonic()
    model = Model(args.model, args.max_side)
    print(f"model loaded in {time.monotonic() - started:.1f}s", flush=True)
    if not args.no_warmup:
        # The first request compiles GPU kernels (about 1.5 min for FP8
        # Qwen3.8 on a 4090); pay that before /health answers, not on the
        # first recording.
        started = time.monotonic()
        model.warmup()
        print(f"warmed up in {time.monotonic() - started:.1f}s", flush=True)
    serve(model, args.host, args.port)


if __name__ == "__main__":
    main()
