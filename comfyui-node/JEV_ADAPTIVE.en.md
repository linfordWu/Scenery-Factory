> Historical W4A4/VSA control. For the current009jev method, see [009JEV.en.md](009JEV.en.md).

# Experimental Jev Adaptive VSA

[简体中文](JEV_ADAPTIVE.md)

Branch: `exp/jev-adaptive-vsa`. This page documents **layer_v5, four steps**. This is a reproducible experiment, not a demonstrated acceleration release: fixed5 took212.20s, layer_v5 took219.15s. The under180s/quality target remains unmet. See [results](docs/experiments/README.md).

## Setup

Follow the base [README](README.md) for compatible ComfyUI, model weights, Gate weights, preconversion, KJNodes and FastVAE dependencies. Tested on Linux (aarch64) / NVIDIA GB10; other environments require validation. Weights and reference images are not bundled.

After this branch is published, clone it with `git clone --branch exp/jev-adaptive-vsa --single-branch https://github.com/sepiablue-ai/ComfyUI-MiniMax-H3-W4A4-VSA.git`. Place one copy under custom_nodes or use this branch's setup_env.py installer. The installer copies Jev modules, examples and docs, but does not install the optional SDK.

For adaptive mode only, create a separate SDK environment from the repository directory:

```bash
python3 -m venv .venv-jev
.venv-jev/bin/pip install -r requirements-jev.txt
```

Set node900's `sdk_python` to that environment's absolute python path (`.venv-jev/bin/python`). Blank means the ComfyUI interpreter, which will fail if the SDK is only in the separate environment. Fixed mode needs neither SDK nor API credentials. Do not install into global Python.

## Credentials and transmitted data

Set `TYPESAFE_API_KEY` in the environment of the process that launches ComfyUI. An already-running server will not inherit a later change. In the same shell that starts ComfyUI, read it without echoing and export it: `read -r -s -p 'TypeSafe API key: ' TYPESAFE_API_KEY; echo; export TYPESAFE_API_KEY` (see the [中文指南](JEV_ADAPTIVE.md#不把-api-密钥写入文件直接启动)). Never put a literal key in a workflow, script, log or commit. Clean up with `unset TYPESAFE_API_KEY` after use.

The worker does not log the key, HTTP headers or exception bodies. State sent to TypeSafe includes sampled aggregate audio/video activation statistics, sigma, layer indices, current keep ratios, and fixed experimental goals/character and speech feedback. It does not contain raw images/audio, model weights or the API key. Decision logs include statistics, responses and token usage.

## Run the paired API examples

- [Fixed5 / four steps](examples/fixed5_4step.api.json)
- [Jev layer_v5 / four steps](examples/jev_layer_v5_4step.api.json)

These are API graphs, not drag-and-drop GUI graphs. Supply three reference images in full-body, upper-body, face order. Change LoadImage nodes901–903 or provide input/jev_reference_01.png through03.png. Verify model/cache node127, VAEs119/120 and text encoder128 against your installation. Different weights or references are not an exact reproduction of the reported run. Asset rights are separate.

Submit once to an already-running dedicated ComfyUI server (Python, no extra dependencies):

```bash
# Point node900's sdk_python at the dedicated venv and submit once
python3 - <<'PY'
import json, urllib.request
with open('examples/jev_layer_v5_4step.api.json', encoding='utf-8') as f:
    graph = json.load(f)
graph['900']['inputs']['sdk_python'] = '/absolute/path/to/.venv-jev/bin/python'
req = urllib.request.Request(
    'http://127.0.0.1:8188/prompt',
    data=json.dumps({'prompt': graph}).encode('utf-8'),
    headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req).read().decode('utf-8'))
PY
```

Adjust the port. Retain prompt_id and wait for completion in ComfyUI. This example does not retry or poll; inspect history before resubmitting an uncertain request. Select an output folder with ComfyUI's `--output-directory`. Load the fixed example for the control. Keep references and prompt identical across both conditions.

Both examples use1024×1792,124frames,24fps,seed43,4steps,res_multistep/simple,sigma12/3. FastVAE is used; MotionCache output reuse is not enabled.

## Policy and limitations

layer_v5 executes all50 layers in every step. The first step uses5% throughout; subsequent steps protect blocks0 and1. Jev classifies49 layer priorities and chooses a mean attention budget of2.5/3/3.5%. Host code converts probabilities into priorities and allocates that budget. Layer answers are priorities, not49 independent binding percentage assignments.

Low/baseline/high keep values are2/5/7.5% for early layers,1/5/7.5% for middle layers,3/5/10% for the final five. Statistics separate target audio/video and exclude references/padding. Block residual size includes FFN effects; it is not measured attention sparsification error or perceptual quality. Cross-step changes also include ordinary denoising.

Use policy=layer_v5, mode=jev_adaptive, keep_percent=5, api_timeout=20, min_confidence=0.4, max_requests=3, producer_chunk=4096. The initial5% is part of this policy, not controlled by keep_percent. Maximum3 HTTP requests,50 questions per request; no final-step request. SDK0.7.0, modeljev-1.13.0, retries0.

Low budget confidence selects the cautious3.5% budget. API failure/invalid response selects all5% and stops further requests in that generation. Missing observations or exhausted request budget also selects all5%. Confidence is choice ambiguity, not a quality guarantee. Other step counts are rejected. Use res_multistep: the shared legacy unsupported-sampler path falls back to fixed10% and makes no API calls.

Legacy policies remain for investigation: gate_v1 starts/falls back10%; av_v2 starts5% and falls back10%; block_v3 can identity-skip blocks and falls back to all blocks at configured keep; layer_v4 selects each layer independently and falls back5%. block_v3 showed identity drift and is not the recommended starting point. Producer8192 did not demonstrate meaningful acceleration;16384 is untested.

Run `"$COMFY_PYTHON" -B test_adaptive.py` (or `python -B test_adaptive.py`) with the ComfyUI Python that has torch. No credentials, generation or paid API calls. Optional test_sdk_transport.py additionally needs SDK/httpx2 and torch in the same test environment; it uses mocked503/timeout responses and zero remote requests, and is not a normal installation requirement.

Existing GPL-3.0-only applies to code. Model weights, reference assets, generated media and service terms are separate.
