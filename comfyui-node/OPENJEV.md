# OpenJev decision engine (experimental)

Local generative decision engine running on a llama.cpp server, as an
alternative to the default local Laya engine. Same question builders and
payload contract as `jev_client`/`laya_client`, so all policies
(gate_v1, av_v2, block_v3, layer_v4, layer_v5) and the 009jev native-SLA node
work unchanged.

## Setup

1. Download a GGUF, e.g.
   `modelscope download --model prithivMLmods/APUS-OpenJev-v1-4B-GGUF APUS-OpenJev-v1-4B.Q6_K.gguf`
2. Serve it (host reachable from the ComfyUI container):

   ```
   llama-server -m APUS-OpenJev-v1-4B.Q6_K.gguf -ngl 99 --host 0.0.0.0 --port 8091 -c 40960 --jinja -fa on
   ```

3. Run ComfyUI with `H3_DECISION_ENGINE=openjev` (default is `laya`, `jev`
   selects the remote Jev API). The engine is read per decision call.

## Behaviour

OpenJev answers one decision question with a bare option key, so each question
is a separate chat completion. The shared state is placed first in the prompt
and `cache_prompt` is enabled, so llama-server's prefix cache absorbs the state
cost (~77% of tokens cached in practice, ~250 ms/question after the first).
Replies are parsed leniently (bare key, JSON, or prose); unparseable replies are
retried once, then the safe middle option (`5` where available) is used.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `OPENJEV_URL` | `http://172.19.0.1:8091` | llama-server base URL (docker bridge gateway) |
| `OPENJEV_TIMEOUT` | `60` | per-request seconds |
| `OPENJEV_MAX_TOKENS` | `16` | completion budget per question |
| `OPENJEV_CONFIDENCE` | `0.75` | confidence reported for parsed choices |
| `OPENJEV_THINK` | `0` | `1` enables the thinking pass |
| `OPENJEV_STATE_MODE` | `compact` | `full` sends unmodified state blocks |
| `OPENJEV_RETRIES` | `1` | extra attempts on unparseable replies |

Tests: `python3 test_openjev_client.py` (stub server, no GPU needed).
