# SPDX-License-Identifier: GPL-3.0-only
"""One bounded Choice call in an existing SDK interpreter; never installs packages.

Question wording lives in plain-dict builders (keep_question, layer_questions,
block_questions, sla_questions) so the local Laya engine (laya_client.py) asks
byte-identical questions without importing the TypeSafe SDK.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

CRITERIA = {
    "5": "Most aggressive speed: strongly concentrated gate proxy, stable recent statistics, low uncertainty.",
    "7.5": "Moderate speed: concentrated gate proxy and small changes; some caution remains.",
    "10": "Balanced default: mixed or insufficient evidence without a clear diffuse/changing signal.",
    "15": "Conservative: diffuse gate proxy, noticeable change, or a detail-sensitive final step.",
    "20": "Most conservative available: very diffuse proxy together with large changes or high uncertainty about sparse coverage.",
}
INSTRUCTIONS = (
    "Select the attention keep percentage for the NEXT diffusion step from the five choices. "
    "Higher keeps more attention tiles, costs more compute, and reduces sparsification risk. "
    "Use current_step, next_step, total_steps, sigma, next_sigma, current_keep_percent and gate_stats. "
    "gate_stats is a small sample of absolute gate activations, NOT attention probabilities or measured quality. "
    "top10_mass near 0.1 is diffuse; larger values indicate concentration. relative_mean_change "
    "measures activation change, not motion. Favor safety for diffuse/changing observations. "
    "Final-step detail preservation can justify 15. Do not change values merely to demonstrate variation. "
    "This is an experimental heuristic, with no calibrated quality predictor."
)

V2_CRITERIA = {
    "1":"Maximum speed; both modalities stable across all depths, with no strong warning signal. This is an exploratory choice, not guaranteed safe.",
    "2":"Very sparse; mostly stable, with small localized uncertainty.",
    "3":"Speed-oriented balance; modest changes or no prior-step comparison yet, and no major anomaly.",
    "5":"Baseline coverage; moderate uncertainty or mixed signals across modalities or depths.",
    "7.5":"Extra coverage for noticeable changes in one modality or depth.",
    "10":"Conservative coverage for substantial changes across several measurements.",
    "15":"Strong precaution for large cross-step changes in both modalities or late-step instability.",
    "20":"Maximum available precaution for broad severe instability in the supplied observations.",
}
V2_INSTRUCTIONS = (
    "Choose the smallest NEXT-step attention keep percentage supported by observations, balancing compute and preservation. "
    "Inspect gate_stats.blocks for separate target video and audio samples at blocks 0,24,49. "
    "relative_l2_change compares signed activations at the same sampled positions against the preceding step. "
    "As a provisional uncalibrated heuristic, changes below .05 are small, .05-.2 moderate, above .2 substantial. "
    "Check the worst modality/depth, not only the average. A null change is unknown, not zero. "
    "Do not assume the gate activation concentration equals retained attention mass or perceptual quality. "
    "Starting at 5 already gave baseline coverage for scene formation. Prefer 3 or 5 if initial observations are ordinary "
    "but lack a previous-step comparison; consider 1 or 2 when subsequent observations are stable across both modalities. "
    "Do not force a high final-step keep solely due to step number, or force variation for demonstration. "
    "There is NO human quality label or known degradation threshold in this state. This is a hypothesis-driven policy test."
)

LAYER_CRITERIA = {
    "reduce": "Lower-than5 attention budget: low impact in both modalities relative to peers, acceptable exploratory risk.",
    "baseline": "Keep5: mixed or ordinary layer importance, conflicting observations or unclear risk.",
    "protect": "Above5: especially important to audio OR video relative to peers, extra coverage justified.",
}
BUDGET_CRITERIA = {
    "2.5": "Aggressive allocation; important effects limited to a few layers.",
    "3": "Balanced allocation; mixed ordinary importance and uncertainty.",
    "3.5": "Cautious allocation; unusually widespread relative risk.",
}
BLOCK_CRITERIA = {
    "execute": "Compute block to preserve transformations; high effect, stale or uncertain evidence.",
    "skip": "Identity bypass for a relatively low-effect, recently measured block; speed candidate.",
}
SLA_STEP_CRITERIA = {
    "1": "Aggressive: low impact in both modalities",
    "3": "Moderate reduction: relatively low effect or stable",
    "5": "Baseline: mixed/ordinary evidence",
    "10": "Protect: unusually high importance or risk",
}
SLA_INIT_BLOCK0_CRITERIA = {"5": "Moderate coverage", "10": "Highest allowed coverage"}
SLA_INIT_CRITERIA = {
    "1": "Strong reduction with compelling low-impact evidence",
    "3": "Moderate reduction with supporting evidence",
    "5": "Intermediate coverage",
    "10": "Highest allowed coverage for formation, text or uncertainty",
}


def _choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def keep_question(state):
    """Single keep-percent question for the gate_v1 and av_v2 policies."""
    v2 = state.get("gate_stats", {}).get("policy") == "av_v2"
    return {"keep": _choice(V2_INSTRUCTIONS if v2 else INSTRUCTIONS,
                            V2_CRITERIA if v2 else CRITERIA)}


def layer_questions(state):
    """Per-layer budget questions for layer_v4; layer_v5 adds the global budget question."""
    questions = {}
    for b in range(1, 50):
        questions[f"b{b:02d}"] = _choice(
            f"Select attention budget tier for layer {b}, NEXT step; read blocks['{b}'] including candidate_keep. This never skips the block. Judge audio and video separately. Reduce for relatively low residual ranks in BOTH modalities, especially interior layers. Baseline for mixed/medium effects. Protect for top-ranked effects in EITHER modality, or a large cross-step change compared with peer layers at similar depth. Gate statistics are secondary context, NOT retained attention mass. First measurement lacks temporal history, but relative ranks still support an exploratory reduce on low-effect layers. Do not interpret missing history as a reason to protect everything. Changes may reflect normal denoising, so compare peers and current keep. Human prefers fixed5 quality; avoid uniform extreme sparsity. Layer-specific percentages are provided in candidate_keep. Spend selectively; don't impose a quota or claim quality guarantee.",
            LAYER_CRITERIA)
    if state.get("policy") == "layer_v5":
        questions["budget"] = _choice(
            "Choose NEXT-step global mean attention keep budget. First step already preserved5 throughout. All blocks execute. Read both modalities across layers and budget_rule; per-layer answers represent importance priorities. Aim preserve fixed5 speech and identity while genuinely reducing attention work. 2.5 is aggressive,3 balanced,3.5 cautious. Normal denoising produces broad cross-step changes; compare layer peers rather than treating every change as damage. No quality guarantee. Choose3 for mixed ordinary observations,3.5 for unusually concentrated high-risk evidence in many layers,2.5 when effects concentrate in a few layers and most appear low risk.",
            BUDGET_CRITERIA)
    return questions


def block_questions(state):
    """Execute/skip questions for block_v3."""
    return {f"b{b:02d}": _choice(
        f"Choose execute or skip for transformer block {b} at next_step. Read blocks['{b}']. Smaller BOTH audio/video relative_residual_l2 and lower relative_rank suggest less change. These are uncalibrated proxies, not quality. Prefer skipping relatively low-effect blocks up to the max_skip_blocks budget when both modalities are modest compared with others. The budget is a cap, not a quota. Execute high-effect or uncertain blocks. A previously skipped block has stale measured_step: consider this uncertainty; execute when consecutive_skips reaches max_consecutive_skips, otherwise a previously low effect may justify a second skip. Host enforces these caps and always executes block0. Do not reuse cached output. Quality target is fixed5 4step. No guarantee; this is an experimental identity bypass.",
        BLOCK_CRITERIA) for b in range(1, 50)}


def sla_questions(state):
    """009jev native-SLA questions: first-step initialization or per-step keep percents."""
    if state.get("initialization"):
        return {str(b): _choice(
            f"Choose FIRST-step SLA keep for layer {b}. Read the full prompt, historical pilot block statistics and limitations. This pass establishes scene, identity and any details requested in the supplied prompt. Read historical source and human_feedback as supplied; do not assume a text-rendering requirement or invent a prior failure. The pilot used5%; its residuals do not establish causation or localize safe removal. Current-run residuals do not exist yet. Prefer broad coverage for potentially important transformations, reduce only with a specific defensible low-impact signal. Historical low residual does not establish safe removal. Block0 can only5 or10. Do not force differences from10 for demonstration.",
            SLA_INIT_BLOCK0_CRITERIA if b == 0 else SLA_INIT_CRITERIA) for b in range(50)}
    return {str(b): _choice(
        f"Choose NEXT-step SLA keep percent for layer {b}. Read blocks['{b}'] audio and video separately. Low residual ranks in BOTH support1 or3; mixed ordinary effects support5; high effects in either or unusually large cross-step drift support10. Compare peers; drift includes normal denoising. Preserve worst modality. Missing temporal history is not proof of danger. No quality guarantee; no forced change. Read host protection rules in state.",
        SLA_STEP_CRITERIA) for b in range(1, 50)}


def _to_sdk(questions):
    from typesafe_sdk import Choice
    return {k: Choice(instructions=q["instructions"], criteria=q["criteria"])
            for k, q in questions.items()}


def ask(state, python, timeout):
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        raise RuntimeError("missing_api_key")
    result = subprocess.run(
        [python or sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--worker"],
        input=json.dumps({"state": state, "timeout": timeout}), capture_output=True,
        text=True, encoding="utf-8", timeout=timeout + 3,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("sdk_worker_failed")
    answer = json.loads(result.stdout)
    if "error" in answer:
        raise RuntimeError(answer["error"])
    return answer


def worker():
    request = json.load(sys.stdin)
    try:
        from typesafe_sdk import RetryPolicy, TypeSafeClient
        policy = request["state"].get("policy")
        if policy in ("layer_v4", "layer_v5"):
            questions = _to_sdk(layer_questions(request["state"]))
            with TypeSafeClient(model="jev-1.13.0", retry=RetryPolicy(max_retries=0), timeout=request["timeout"], base_url="https://api.typesafe.ai") as client:
                response = client.system_one(state=request["state"], questions=questions)
            payload = {"decisions": {k: {"choice": v.choice, "confidence": v.confidence, "probabilities": v.probabilities} for k, v in response.choices.items() if k != "budget"}, "model": response.model, "usage": response.usage.model_dump(mode="json") if response.usage else None}
            if "budget" in response.choices:
                a = response.choices["budget"]
                payload["budget"] = {"choice": a.choice, "confidence": a.confidence, "probabilities": a.probabilities}
            print(json.dumps(payload))
            return
        if policy == "block_v3":
            questions = _to_sdk(block_questions(request["state"]))
            with TypeSafeClient(model="jev-1.13.0", retry=RetryPolicy(max_retries=0), timeout=request["timeout"], base_url="https://api.typesafe.ai") as client:
                response = client.system_one(state=request["state"], questions=questions)
            print(json.dumps({"decisions": {k: {"choice": v.choice, "confidence": v.confidence, "probabilities": v.probabilities} for k, v in response.choices.items()}, "model": response.model, "usage": response.usage.model_dump(mode="json") if response.usage else None}))
            return
        v2 = request["state"].get("gate_stats", {}).get("policy") == "av_v2"
        with TypeSafeClient(model="jev-1.13.0" if v2 else "jev-latest", retry=RetryPolicy(max_retries=0),
                            timeout=request["timeout"], base_url="https://api.typesafe.ai") as client:
            response = client.system_one(state=request["state"], questions=_to_sdk(keep_question(request["state"])))
        a = response.choices["keep"]
        usage = getattr(response, "usage", None)
        print(json.dumps({"choice": a.choice, "confidence": a.confidence,
                          "probabilities": a.probabilities, "model": response.model,
                          "usage": usage.model_dump(mode="json") if usage is not None else None}))
    except Exception as exc:
        # Never log exception text, request headers, environment, or credentials.
        print(json.dumps({"error": type(exc).__name__}))


if __name__ == "__main__":
    worker()
