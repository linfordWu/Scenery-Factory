# SPDX-License-Identifier: GPL-3.0-only
"""OpenJev decision engine: local llama-server (GGUF) replacement for the Jev API.

Asks the same questions (builders live in jev_client.py) and returns the same
payload shapes as laya_client, so every controller (gate_v1, av_v2, block_v3,
layer_v4, layer_v5) and the 009jev native-SLA node work unchanged. OpenJev is a
generative model fine-tuned to answer ONE decision question with a bare option
key, so each question is its own chat completion; the shared state is placed
first in the prompt so llama-server's prefix cache absorbs its cost.

Environment:
    OPENJEV_URL        llama-server base URL (default http://172.19.0.1:8091)
    OPENJEV_TIMEOUT    per-request seconds (default 60)
    OPENJEV_MAX_TOKENS completion budget per question (default 16)
    OPENJEV_CONFIDENCE confidence reported for parsed choices (default 0.75)
    OPENJEV_THINK      "1" keeps the model's thinking pass (default off)
    OPENJEV_STATE_MODE "compact" (default) shrinks state blocks to modal metrics
    OPENJEV_RETRIES    extra attempts when the reply is not a valid option (default 1)
"""
import json
import logging
import os
import re
import time
import urllib.request

try:
    from .jev_client import block_questions, keep_question, layer_questions, sla_questions
except ImportError:  # loaded as a top-level module (tests, standalone scripts)
    from jev_client import block_questions, keep_question, layer_questions, sla_questions

log = logging.getLogger(__name__)


def _url():
    return os.environ.get("OPENJEV_URL", "http://172.19.0.1:8091").rstrip("/")


def _chat(prompt):
    body = {
        "messages": [
            {"role": "system", "content": "You are a decision engine. Reply with exactly one option key."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": int(os.environ.get("OPENJEV_MAX_TOKENS", "16")),
        "chat_template_kwargs": {"enable_thinking": os.environ.get("OPENJEV_THINK", "0") == "1"},
        "cache_prompt": True,
    }
    req = urllib.request.Request(
        _url() + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    timeout = float(os.environ.get("OPENJEV_TIMEOUT", "60"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    usage = data.get("usage") or {}
    return data["choices"][0]["message"]["content"], usage


def _modal_compact(entry):
    row = {}
    for kind in ("audio", "video"):
        m = entry.get(kind)
        if isinstance(m, dict):
            row[kind[0]] = [m.get("residual_relative_l2", m.get("relative_residual_l2")),
                            m.get("residual_rank", m.get("rank")),
                            m.get("residual_cross_step_change", m.get("cross_step_change"))]
    return row or entry


def _compact_state(state):
    if os.environ.get("OPENJEV_STATE_MODE", "compact").strip().lower() == "full":
        return state
    blocks = state.get("blocks")
    if not isinstance(blocks, dict) or not blocks:
        return state
    view = {k: v for k, v in state.items() if k != "blocks"}
    view["block_metric_legend"] = "a=[audio residual_relative_l2, rank, cross_step_change], v=[same for video]; null = unknown"
    view["blocks"] = {b: _modal_compact(e) for b, e in blocks.items()}
    return view


def _default_choice(criteria):
    keys = list(criteria)
    return "5" if "5" in criteria else keys[len(keys) // 2]


_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_choice(text, criteria):
    """Accept a bare option key, or one wrapped in prose/markdown/JSON."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text in criteria:
        return text
    match = _JSON_RE.search(text)
    if match:
        try:
            obj = json.loads(match.group(0))
            for value in obj.values():
                if str(value).strip() in criteria:
                    return str(value).strip()
        except json.JSONDecodeError:
            pass
    for token in re.split(r"[^A-Za-z0-9_.]+", text):
        if token in criteria:
            return token
    raise ValueError(f"no valid option in reply: {text[:80]!r}")


def _run(state, questions):
    """One chat completion per question; state is a shared prompt prefix (server-cached)."""
    started = time.perf_counter()
    confidence = float(os.environ.get("OPENJEV_CONFIDENCE", "0.75"))
    attempts = 1 + int(os.environ.get("OPENJEV_RETRIES", "1"))
    prefix = "State:\n" + json.dumps(_compact_state(state), ensure_ascii=False) + "\n"
    answers = {}
    prompt_tokens = completion_tokens = cached_tokens = 0
    for qid, q in questions.items():
        prompt = (prefix + "Question: " + q["instructions"] +
                  "\nOptions: " + json.dumps(q["criteria"], ensure_ascii=False))
        choice = None
        for attempt in range(attempts):
            raw, usage = _chat(prompt if not attempt else
                               prompt + "\nReply with exactly one option key, nothing else.")
            prompt_tokens += usage.get("prompt_tokens", 0)
            completion_tokens += usage.get("completion_tokens", 0)
            cached_tokens += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            try:
                choice = _parse_choice(raw, q["criteria"])
                break
            except ValueError:
                log.warning("[OpenJev] %s unparseable (attempt %d): %.120s", qid, attempt + 1, raw)
        if choice is None:
            choice = _default_choice(q["criteria"])
        answers[qid] = {"choice": choice, "confidence": confidence,
                        "probabilities": {choice: confidence}}
    log.info("[OpenJev] %d question(s), %d+%d tokens (%d cached), %.1fs local",
             len(questions), prompt_tokens, completion_tokens, cached_tokens,
             time.perf_counter() - started)
    return answers, {"input_tokens": prompt_tokens, "output_tokens": completion_tokens,
                     "cached_tokens": cached_tokens, "engine": "openjev"}


def ask(state, python="", timeout=60.0):
    """Drop-in for jev_client.ask / laya_client.ask: same payload contract.

    `python` is ignored (no SDK worker); per-request HTTP timeout comes from
    OPENJEV_TIMEOUT because generation latency differs from the laya/jev budgets.
    """
    policy = state.get("policy")
    if policy in ("layer_v4", "layer_v5"):
        answers, usage = _run(state, layer_questions(state))
        payload = {"decisions": {k: v for k, v in answers.items() if k != "budget"},
                   "model": "openjev-local", "usage": usage}
        if "budget" in answers:
            payload["budget"] = answers["budget"]
        return payload
    if policy == "block_v3":
        answers, usage = _run(state, block_questions(state))
        return {"decisions": answers, "model": "openjev-local", "usage": usage}
    answers, usage = _run(state, keep_question(state))
    payload = dict(answers["keep"])
    payload.update(model="openjev-local", usage=usage)
    return payload


def native_ask(state):
    """009jev native-SLA contract: decisions keyed '0'..'49' (init) or '1'..'49' (step)."""
    answers, usage = _run(state, sla_questions(state))
    return {"decisions": answers, "model": "openjev-local", "usage": usage}


def warmup():
    """Probe the server so node-patch time fails fast when it is down."""
    with urllib.request.urlopen(_url() + "/health", timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"openjev server unhealthy: {resp.status}")
