#!/usr/bin/env python3
"""learning-video skill: batch-generate 5s 1080P animated clips from reference images.

Self-contained. Configure via environment:
    COMFY_BASE    ComfyUI base URL (default http://localhost:8188)
    WORKFLOW_JSON workflow template (default <repo>/examples/learning_video_1080p.api.json)

Usage: python3 gen_learning_video.py [clip numbers...]   (default: all)
Fill in CLIPS below: (image in ComfyUI input dir, narration, motion script)
"""
import json
import os
import sys
import time
import urllib.request
import uuid

BASE = os.environ.get("COMFY_BASE", "http://localhost:8188")
WORKFLOW = os.environ.get(
    "WORKFLOW_JSON",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                 "examples", "learning_video_1080p.api.json"),
)

# (image, narration, motion) — one tuple per clip.
CLIPS = [
    ("doc_01.png",
     "飞书驱动的异构算力编排，让碎片化算力变成可等待、可调度、可自动执行的研发能力。",
     "The base scene stays still. First the title text appears, then the queue rail and task papers pop in one after another along the rail, then the MultiCA robot appears and waves, then the machines on the right appear one by one, and finally the phone in the boy's hand lights up with a message."),
    # ... add one tuple per image
]

PROMPT_TMPL = """subject_definitions:
<Picture 1> is a hand-drawn watercolor illustration slide with Chinese text.

summary:
[reference generation] the illustration builds itself up as a sequential reveal animation: text labels and elements appear one by one in semantic order, while a calm narrator reads one Mandarin Chinese line.

integrated_multimodal_description:
Hand-drawn watercolor illustration style, faithful to the reference image's layout, characters and Chinese text. Locked-off completely static camera: no zoom, no push-in, no pan, no shake. The frame edges stay exactly fixed for the whole shot.

[Shot 1]
Static camera. The scene from <Picture 1> assembles itself step by step as a reveal animation:
{motion}
Elements that are already revealed keep living subtly (lights blink, small character motions) while later elements appear.

During the shot, a warm, calm female narrator clearly says in Mandarin Chinese:
<d>[Chinese] {narration}</d>

Her voice is clear and steady. No other sounds.

overall_soundscape:
Only the narrator voice with a faint quiet room tone.

non_diegetic_music:
None."""


def api(path, payload=None):
    if payload is None:
        return json.load(urllib.request.urlopen(BASE + path, timeout=30))
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def build_graph(image, narration, motion, prefix, seed):
    g = json.load(open(WORKFLOW))
    prompt = PROMPT_TMPL.format(narration=narration, motion=motion)
    g["131"]["inputs"]["prompt"] = prompt
    g["901"]["inputs"]["image"] = image
    if "900" in g:
        g["900"]["inputs"]["prompt_context"] = prompt
    g["129"]["inputs"]["noise_seed"] = seed
    g["92"]["inputs"]["filename_prefix"] = prefix
    return g


def run(tag, graph):
    resp = api("/prompt", {"prompt": graph, "client_id": "learn-" + uuid.uuid4().hex[:6]})
    pid = resp["prompt_id"]
    print(f"[{tag}] queued {pid}", flush=True)
    t0 = time.time()
    while True:
        time.sleep(10)
        hist = api("/history/" + pid)
        if pid in hist:
            status = hist[pid].get("status", {})
            if status.get("status_str") == "error":
                print(f"[{tag}] ERROR: {json.dumps(status, ensure_ascii=False)[:1500]}", flush=True)
                return None
            if status.get("completed") or status.get("status_str") == "success":
                dt = time.time() - t0
                print(f"[{tag}] done in {dt:.1f}s", flush=True)
                return dt
        if time.time() - t0 > 7200:
            print(f"[{tag}] TIMEOUT", flush=True)
            return None


def main():
    if not CLIPS:
        sys.exit("CLIPS is empty — fill in (image, narration, motion) tuples first.")
    wanted = {int(a) for a in sys.argv[1:]} or set(range(1, len(CLIPS) + 1))
    results = {}
    for i, (image, narration, motion) in enumerate(CLIPS, 1):
        if i not in wanted:
            continue
        tag = f"clip{i:02d}"
        results[tag] = run(tag, build_graph(image, narration, motion,
                                            f"video/learn_{tag}", 4026 + i))
        print(json.dumps(results, indent=1), flush=True)
    print("ALL DONE")


if __name__ == "__main__":
    main()
