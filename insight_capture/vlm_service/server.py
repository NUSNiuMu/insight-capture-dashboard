"""Qwen2.5-VL segment-labelling service for aruco pre-annotation.

Run on the GPU machine (needs ~17 GB VRAM in bf16):
    python -m insight_capture.vlm_service.server --model /path/to/Qwen2.5-VL-7B-Instruct

GET  /health    -> {"status": "ok", "model": ..., "busy": bool}; only answers once the model is loaded
POST /v1/label  {"frames": [base64 JPEG...], "fps": float, "hands": "left|right|both",
                 "action_table": {...} | null}
             -> {"task_id", "subtask_id", "task", "subtask", "description", "flags", "raw"}
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

from .prompt import build_prompt, parse_answer

PATCH = 28  # Qwen2.5-VL vision patch size; frame sides must be multiples of it.


def prepare(frames, max_side):
    images = []
    for item in frames:
        image = cv2.imdecode(np.frombuffer(base64.b64decode(item), np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法解码的图像帧")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        h, w = image.shape[:2]
        scale = min(1.0, max_side / max(h, w))
        size = (
            max(PATCH, round(w * scale / PATCH) * PATCH),
            max(PATCH, round(h * scale / PATCH) * PATCH),
        )
        images.append(cv2.resize(image, size, interpolation=cv2.INTER_AREA))
    if len(images) % 2:  # temporal patch size is two frames
        images.append(images[-1])
    return np.stack(images)


class Model:
    def __init__(self, path, max_side):
        import torch
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

        self.torch = torch
        self.name = Path(path).name
        self.max_side = max_side
        self.processor = AutoProcessor.from_pretrained(path)
        self.model = (
            Qwen2_5_VLForConditionalGeneration.from_pretrained(
                path, torch_dtype=torch.bfloat16, attn_implementation="sdpa"
            )
            .to("cuda")
            .eval()
        )
        self.lock = threading.Lock()  # one GPU, one request at a time

    def label(self, request):
        frames = request.get("frames")
        if not isinstance(frames, list) or len(frames) < 2:
            raise ValueError("至少需要 2 帧图像")
        video = prepare(frames, self.max_side)
        fps = float(request.get("fps") or 2.0)
        table = request.get("action_table") or None
        prompt = build_prompt(len(frames) / fps, request.get("hands"), table)
        messages = [{"role": "user", "content": [{"type": "video"}, {"type": "text", "text": prompt}]}]
        text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        with self.lock:
            inputs = self.processor(
                text=[text], videos=[video], fps=fps, return_tensors="pt"
            ).to("cuda")
            with self.torch.inference_mode():
                output = self.model.generate(**inputs, max_new_tokens=320, do_sample=False)
        answer = self.processor.batch_decode(
            output[:, inputs.input_ids.shape[1]:], skip_special_tokens=True
        )[0].strip()
        return {**parse_answer(answer, table), "raw": answer}


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
            if self.path != "/v1/label":
                return self.reply(404, {"error": "not found"})
            started = time.monotonic()
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                result = model.label(request)
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
    parser.add_argument("--model", required=True, help="Qwen2.5-VL 权重目录")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--max-side", type=int, default=672, help="送入模型前帧的最长边（像素）")
    args = parser.parse_args()
    started = time.monotonic()
    model = Model(args.model, args.max_side)
    print(f"model loaded in {time.monotonic() - started:.1f}s", flush=True)
    serve(model, args.host, args.port)


if __name__ == "__main__":
    main()
