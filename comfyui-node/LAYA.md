# Laya: local decision engine for adaptive VSA / SLA

The adaptive nodes (`H3V2JevAdaptiveVSAPatch`, `H3JevNativeSLAPatch`) ask a decision
model for per-step / per-layer attention keep rates. By default those decisions now
come from a **local [Laya](https://github.com/NandhaKishorM/laya) checkpoint** —
a 322M non-autoregressive decision model that answers all 49–50 layer questions in a
few batched forward passes, instead of a remote API round trip per step.

| | Jev API | Laya (local) |
|---|---|---|
| decision latency, 1 question | ~250 ms p50 + network | ~35 ms (GPU) |
| decision latency, 50 questions | ~250 ms + network | one batched pass, no network |
| credentials / cost | `TYPESAFE_API_KEY`, per-token | none / free |
| offline | no | yes |

Questions, criteria and fallback behaviour are byte-identical between engines —
`laya_client.py` reuses the builders in `jev_client.py`. Set
`H3_DECISION_ENGINE=jev` to switch back to the TypeSafe API.

## Setup

1. Install the package into ComfyUI's Python: `pip install -r requirements-laya.txt`
2. Download the weights once (bundle directory with `rl_agent_config.json` at the root):
   - Hugging Face: `huggingface-cli download convaiinnovations/laya`
   - ModelScope: `modelscope download --model convaiinnovations/laya`
3. Point the node at them: `export LAYA_MODEL_DIR=/path/to/laya-bundle`

## Environment variables

| variable | default | purpose |
|---|---|---|
| `H3_DECISION_ENGINE` | `laya` | `laya` (local) or `jev` (TypeSafe API) |
| `LAYA_MODEL_DIR` | auto-detect | bundle dir; also tried: `~/models/laya/weights`, `./weights`, `ComfyUI/models/laya` |
| `LAYA_SUBFOLDER` | `multilingual` | checkpoint in the bundle (`""` = English root, `typed-decisions`) |
| `LAYA_DEVICE` | `cuda` if available | `cpu` saves ~0.7 GB VRAM at higher latency |
| `LAYA_MAX_LEN` | `8192` | token budget per question row (49-question policies carry multi-KB JSON states) |
| `LAYA_HEAD_MAX_LEN` | `512` | instructions+options token budget |
| `LAYA_BATCH` | `16` | questions per forward pass (shared-state mode); lower it if VRAM is tight |
| `LAYA_STATE_MODE` | `compact` | `compact`: per-question state with full own block + compact peers (~1k tokens/row, fast). `full`: byte-identical shared state for all 49 questions (~10k tokens/row, much slower, truncates trailing blocks) |

The checkpoint loads once at node-patch time (warmup), so the first sampler step
never blocks on model load. Decision quality is the same experimental,
uncalibrated heuristic as the Jev workflow — see `JEV_ADAPTIVE.md`.

Note on confidence: Jev reports the chosen option's probability as `confidence`,
and the controllers gate on it (`min_confidence`, default 0.5). Laya's native
`confidence` is entropy-normalised and systematically lower, so `laya_client`
reports the chosen option's probability to match Jev semantics; Laya's original
value is kept as `entropy_confidence` in each decision payload.

Laya is a 322M local model and is less decisive than the Jev API on this numeric
proxy task. If gate_v1 decisions keep falling back (`low_confidence` in the log),
lower the node's `min_confidence` to ~0.3, or accept the conservative fallback.
The layer policies (3 options per question) are less sensitive to this.
