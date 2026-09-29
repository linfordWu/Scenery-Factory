# SPDX-License-Identifier: GPL-3.0-only
"""Qwen-Image 2.1 text-to-image HTTP service (runs inside the comfyui container).

Loads the diffusers-format bundle once, serves POST /generate on 0.0.0.0:8601.
Uses model CPU offload so it can coexist with the H3 pipeline on one GB10.
"""
import base64
import io
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_DIR = os.environ.get("QWEN_IMAGE_DIR", "/opt/ComfyUI/models/qwen-image-2.1")
PORT = int(os.environ.get("QWEN_IMAGE_PORT", "8601"))

pipe = None


def get_pipe():
    global pipe
    if pipe is None:
        import torch
        from diffusers import QwenImage21Pipeline
        started = time.time()
        pipe = QwenImage21Pipeline.from_pretrained(MODEL_DIR, torch_dtype=torch.bfloat16)
        # Plenty of unified memory when vLLM is asleep/absent; module offload
        # avoids the sequential-offload cuda/cpu index_select mismatch.
        pipe.enable_model_cpu_offload()
        print(f"[qwen-image] loaded in {time.time()-started:.0f}s", flush=True)
    return pipe


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok","loaded":' + (b"true" if pipe else b"false") + b"}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip("/") != "/generate":
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            p = get_pipe()
            started = time.time()
            image = p(prompt=body["prompt"],
                      negative_prompt=body.get("negative_prompt", ""),
                      width=int(body.get("width", 1080)),
                      height=int(body.get("height", 1920)),
                      num_inference_steps=int(body.get("steps", 30)),
                      true_cfg_scale=float(body.get("cfg", 4.0)),
                      generator=None if body.get("seed") is None else
                      __import__("torch").Generator().manual_seed(int(body["seed"]))).images[0]
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            out = {"image_b64": base64.b64encode(buf.getvalue()).decode(),
                   "seconds": round(time.time() - started, 1)}
            data = json.dumps(out).encode()
            self.send_response(200)
        except Exception as exc:
            data = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"[qwen-image] serving on :{PORT}, model={MODEL_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
