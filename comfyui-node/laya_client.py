# SPDX-License-Identifier: GPL-3.0-only
"""Local Laya decision engine: in-process replacement for the Jev API worker.

Asks byte-identical questions (builders live in jev_client.py) and returns the
same payload shapes, so every controller (gate_v1, av_v2, block_v3, layer_v4,
layer_v5) and the 009jev native-SLA node work unchanged. No API key, no network,
no per-token cost; one local forward pass per decision instead of a ~250 ms
remote round trip.

Environment:
    LAYA_MODEL_DIR     bundle directory with rl_agent_config.json (+ subfolders)
    LAYA_SUBFOLDER     checkpoint inside the bundle (default "multilingual";
                       "" selects the English root checkpoint)
    LAYA_DEVICE        "cuda" (default when available) or "cpu"
    LAYA_MAX_LEN       total token budget per question row (default 8192)
    LAYA_HEAD_MAX_LEN  instructions+options token budget (default 512)
    LAYA_BATCH         questions per forward pass, bounds VRAM (default 16)
"""
import logging
import os
from pathlib import Path
import threading
import time

try:
    from .jev_client import block_questions, keep_question, layer_questions, sla_questions
except ImportError:  # loaded as a top-level module (tests, standalone scripts)
    from jev_client import block_questions, keep_question, layer_questions, sla_questions

log = logging.getLogger(__name__)

_AGENT = None
_LOCK = threading.Lock()


def _resolve_model_dir():
    candidates = []
    explicit = os.environ.get("LAYA_MODEL_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.home() / "models/laya/weights")
    candidates.append(Path("weights"))
    try:
        import folder_paths  # ComfyUI runtime
        candidates.append(Path(folder_paths.models_dir) / "laya")
    except Exception:
        pass
    for directory in candidates:
        if (directory / "rl_agent_config.json").is_file():
            return str(directory)
    raise RuntimeError(
        "laya_weights_not_found: no rl_agent_config.json in any candidate. "
        "Download convaiinnovations/laya (Hugging Face or ModelScope) and set LAYA_MODEL_DIR."
    )


def _build_agent():
    import laya
    import torch

    subfolder = os.environ.get("LAYA_SUBFOLDER", "multilingual").strip() or None
    device = os.environ.get("LAYA_DEVICE", "").strip().lower()
    if not device:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model_dir = _resolve_model_dir()
    started = time.perf_counter()
    agent = laya.load(model_dir, device=device, subfolder=subfolder)
    # 49-question policies repeat a multi-KB JSON state per row; the shipped 1024
    # token budget would truncate the trailing blocks. mmBERT supports 8192.
    agent.cfg["max_len"] = int(os.environ.get("LAYA_MAX_LEN", "8192"))
    agent.cfg["head_max_len"] = int(os.environ.get("LAYA_HEAD_MAX_LEN", "512"))
    log.info("[Laya VSA] checkpoint %s%s on %s ready in %.2fs (max_len=%s head=%s)",
             model_dir, "/%s" % subfolder if subfolder else "",
             agent.device, time.perf_counter() - started,
             agent.cfg["max_len"], agent.cfg["head_max_len"])
    return agent


def get_agent():
    global _AGENT
    if _AGENT is None:
        with _LOCK:
            if _AGENT is None:
                _AGENT = _build_agent()
    return _AGENT


def warmup():
    """Load the checkpoint at node-patch time so the first sampler step never waits."""
    get_agent()


def _predict(agent, state, questions):
    """One predict call per LAYA_BATCH questions; answers merged, token usage summed."""
    ids = list(questions)
    chunk = max(1, int(os.environ.get("LAYA_BATCH", "16")))
    answers, tokens = {}, 0
    for first in range(0, len(ids), chunk):
        part = {qid: questions[qid] for qid in ids[first:first + chunk]}
        result = agent.predict(state, part)
        answers.update(result["answers"])
        tokens += result.get("usage", {}).get("input_tokens", 0)
    return answers, tokens


def _decision(answer):
    # Jev's confidence is the chosen option's probability; laya's own "confidence" is
    # entropy-normalised and sits far below the controllers' min_confidence gates, so
    # report max-probability here and keep laya's value as entropy_confidence.
    probs = answer.get("probabilities") or {}
    top = probs.get(answer["choice"], answer.get("confidence", 0.0))
    return {"choice": answer["choice"], "confidence": round(float(top), 4),
            "probabilities": probs, "entropy_confidence": answer.get("confidence")}


_LAYER_LEGEND = ("peer_blocks values: a=[audio residual_relative_l2, audio residual_rank, "
                 "audio residual_cross_step_change], v=[same fields for video]; null = unknown")
_BLOCK_LEGEND = ("peer_blocks values: l2=[audio relative_residual_l2, video relative_residual_l2], "
                 "rank=relative_rank, skipped=was_skipped, streak=consecutive_skips, step=measured_step")


def _modal_compact(entry):
    row = {}
    for kind in ("audio", "video"):
        m = entry.get(kind)
        if isinstance(m, dict):
            row[kind[0]] = [m.get("residual_relative_l2", m.get("relative_residual_l2")),
                            m.get("residual_rank", m.get("rank")),
                            m.get("residual_cross_step_change", m.get("cross_step_change"))]
    return row


def _question_state(state, qid, policy):
    """Per-question view: full global context and the question's own block, compact peers.

    The 49-question policies repeat a ~10k-token JSON state per row; 49 x 8192 tokens
    costs ~20s on GPU while truncating the trailing blocks the questions ask about.
    Compact mode keeps blocks['<own>'] intact (what the instructions say to read) and
    reduces peer blocks to the comparison metrics, cutting each row to ~1k tokens.
    LAYA_STATE_MODE=full restores the original shared-state behaviour.
    """
    blocks = state.get("blocks")
    if (os.environ.get("LAYA_STATE_MODE", "compact").strip().lower() == "full"
            or not isinstance(blocks, dict) or not blocks):
        return state
    own = None if qid == "budget" else qid.lstrip("b")
    if own is not None and own not in blocks and own.isdigit():
        own = str(int(own))  # question ids are zero-padded ('b01'), state block keys are not ('1')
    view = {k: v for k, v in state.items() if k != "blocks"}
    if policy == "block_v3":
        view["peer_legend"] = _BLOCK_LEGEND
        view["peer_blocks"] = {
            b: {"l2": [e.get("relative_residual_l2", {}).get("audio"),
                       e.get("relative_residual_l2", {}).get("video")],
                "rank": e.get("relative_rank"), "skipped": e.get("was_skipped"),
                "streak": e.get("consecutive_skips"), "step": e.get("measured_step")}
            for b, e in blocks.items() if b != own}
    else:
        view["peer_legend"] = _LAYER_LEGEND
        view["peer_blocks"] = {b: _modal_compact(e) for b, e in blocks.items() if b != own}
    if own is not None and own in blocks:
        view["blocks"] = {own: blocks[own]}
        view["own_block"] = own
    return view


def _run_multi(state, questions, policy):
    """Multi-question policies: compact per-question states, or chunked shared state."""
    agent = get_agent()
    started = time.perf_counter()
    answers, tokens = {}, 0
    if os.environ.get("LAYA_STATE_MODE", "compact").strip().lower() != "full" \
            and isinstance(state.get("blocks"), dict) and state["blocks"]:
        for qid, q in questions.items():
            result = agent.predict(_question_state(state, qid, policy), {qid: q})
            answers.update(result["answers"])
            tokens += result.get("usage", {}).get("input_tokens", 0)
    else:
        answers, tokens = _predict(agent, state, questions)
    log.info("[Laya VSA] %d question(s), %d tokens, %.3fs local",
             len(questions), tokens, time.perf_counter() - started)
    return answers, {"input_tokens": tokens, "output_tokens": 0, "engine": "laya"}


def _run(state, questions):
    agent = get_agent()
    started = time.perf_counter()
    answers, tokens = _predict(agent, state, questions)
    log.info("[Laya VSA] %d question(s), %d tokens, %.3fs local",
             len(questions), tokens, time.perf_counter() - started)
    return answers, {"input_tokens": tokens, "output_tokens": 0, "engine": "laya"}


def ask(state, python="", timeout=8.0):
    """Drop-in for jev_client.ask: same payload contract, local inference.

    `python`/`timeout` only bounded the remote SDK worker; kept for signature
    compatibility and ignored here.
    """
    policy = state.get("policy")
    if policy in ("layer_v4", "layer_v5"):
        answers, usage = _run_multi(state, layer_questions(state), policy)
        payload = {"decisions": {k: _decision(v) for k, v in answers.items() if k != "budget"},
                   "model": "laya-local", "usage": usage}
        if "budget" in answers:
            payload["budget"] = _decision(answers["budget"])
        return payload
    if policy == "block_v3":
        answers, usage = _run_multi(state, block_questions(state), policy)
        return {"decisions": {k: _decision(v) for k, v in answers.items()},
                "model": "laya-local", "usage": usage}
    answers, usage = _run(state, keep_question(state))
    payload = _decision(answers["keep"])
    payload.update(model="laya-local", usage=usage)
    return payload


def native_ask(state):
    """009jev native-SLA contract: decisions keyed '0'..'49' (init) or '1'..'49' (step)."""
    answers, usage = _run_multi(state, sla_questions(state), "sla")
    return {"decisions": {k: _decision(v) for k, v in answers.items()},
            "model": "laya-local", "usage": usage}
