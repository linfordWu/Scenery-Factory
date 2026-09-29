# SPDX-License-Identifier: GPL-3.0-only
"""Per-sampler-step controller, with no network calls during block execution."""
import json
import logging
import math
import os
import time

log = logging.getLogger(__name__)


def ask(state, sdk_python, timeout):
    """Decision engine dispatch: local Laya by default; H3_DECISION_ENGINE=jev|openjev switches."""
    engine = os.environ.get("H3_DECISION_ENGINE", "laya").strip().lower()
    if engine == "jev":
        from .jev_client import ask as jev_ask
        return jev_ask(state, sdk_python, timeout)
    if engine == "openjev":
        from .openjev_client import ask as openjev_ask
        return openjev_ask(state, sdk_python, timeout)
    from .laya_client import ask as laya_ask
    return laya_ask(state, sdk_python, timeout)
ALLOWED = {"5": 5.0, "7.5": 7.5, "10": 10.0, "15": 15.0, "20": 20.0}

class AdaptiveController:
    def __init__(self, patch, sdk_python="", timeout=8.0, min_confidence=0.5,
                 max_requests=3, chooser=None):
        self.patch = patch
        self.allowed = ALLOWED
        self.sdk_python = sdk_python
        self.timeout = timeout
        self.min_confidence = min_confidence
        self.max_requests = max_requests
        self.chooser = chooser or (lambda state: ask(state, sdk_python, timeout))
        self.events = []
        self.requests = 0
        self.stats = None
        self.previous_mean = None
        self.disabled = False
        self.last_step = -1

    def emit(self, event):
        self.events.append(event)
        log.info("[Jev VSA] %s", json.dumps(event, allow_nan=False))

    def begin(self, sigmas):
        self.sigmas = [float(v) for v in sigmas]
        self.total = len(self.sigmas) - 1
        self.patch.topk_ratio = 0.10
        self.emit({"event": "begin", "total_steps": self.total, "keep_percent": 10.0,
                   "max_requests": self.max_requests, "policy": "gate-proxy-v1"})

    def observe_gate(self, gate, block_index, plan=None, layout=None):
        if self.stats is not None:
            return
        # At most 64 rows x 16 channels of the first eligible block, once per step.
        # Includes conditioning/padding: a deliberately cheap activation proxy.
        sample = gate.detach().reshape(gate.shape[1], -1)
        sample = sample[::max(1, sample.shape[0] // 64)][:64,
                        ::max(1, sample.shape[1] // 16)][:, :16].float().cpu().abs().flatten()
        if not bool(sample.isfinite().all()):
            return
        mean = float(sample.mean())
        mass = float(sample.sum())
        top = float(sample.topk(max(1, math.ceil(sample.numel() * .1))).values.sum())
        self.stats = {"block": block_index, "samples": sample.numel(),
                      "mean_abs": round(mean, 6), "std_abs": round(float(sample.std(unbiased=False)), 6),
                      "top10_mass": round(top / max(mass, 1e-12), 6),
                      "relative_mean_change": None if self.previous_mean is None else
                      round(abs(mean-self.previous_mean) / max(self.previous_mean, 1e-8), 6)}
        self.previous_mean = mean

    def completed_step(self, step):
        if step <= self.last_step:
            return
        self.last_step = step
        current = self.patch.topk_ratio * 100
        event = {"event": "step", "step": step+1, "total_steps": self.total,
                 "sigma": self.sigmas[step], "applied_keep_percent": current,
                 "gate_stats": self.stats}
        if step >= self.total-1:
            event.update(reason="last_step_no_request", next_keep_percent=None)
            self.emit(event)
            return
        state = {"current_step": step+1, "next_step": step+2, "total_steps": self.total,
                 "sigma": self.sigmas[step], "next_sigma": self.sigmas[step+1],
                 "current_keep_percent": current, "gate_stats": self.stats}
        selected, reason = 10.0, "fallback"
        started = time.perf_counter()
        if self.disabled:
            reason = "api_circuit_open"
        elif self.requests >= self.max_requests:
            reason = "request_budget_exhausted"
        elif self.stats is None:
            reason = "no_gate_statistics"
        else:
            self.requests += 1
            try:
                answer = self.chooser(state)
                choice, confidence = answer["choice"], float(answer["confidence"])
                if choice not in self.allowed or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    reason = "invalid_response"
                elif confidence < self.min_confidence:
                    reason = "low_confidence"
                else:
                    selected, reason = self.allowed[choice], "jev"
                event["answer"] = json.loads(json.dumps({k: answer.get(k) for k in ("choice", "confidence", "probabilities", "usage", "model")}, allow_nan=False))
            except Exception as exc:
                selected = 10.0
                reason = "api_error:" + type(exc).__name__
                self.disabled = True  # No repeated failed requests in the same generation.
        self.patch.topk_ratio = selected / 100
        event.update(next_keep_percent=selected, reason=reason, request_count=self.requests,
                     decision_seconds=round(time.perf_counter()-started, 6), state=state)
        self.stats = None
        self.emit(event)


def sampler_wrapper(patch, settings):
    def sample(executor, model_wrap, sigmas, extra_args, callback, noise,
               latent_image=None, denoise_mask=None, disable_pbar=False):
        sampler_name = getattr(getattr(executor.class_obj, "sampler_function", None), "__name__", "")
        if sampler_name != "sample_res_multistep":
            patch.topk_ratio = 0.10
            log.warning('[Jev VSA] unsupported sampler %s: fixed 10%%, zero API calls', sampler_name)
            return executor(model_wrap, sigmas, extra_args, callback, noise,
                            latent_image, denoise_mask, disable_pbar)
        settings_local = dict(settings)
        policy = settings_local.pop("policy", "gate_v1")
        if policy == "layer_v5":
            from .layer_adaptive import BudgetLayerController
            controller = BudgetLayerController(patch, **settings_local)
        elif policy == "layer_v4":
            from .layer_adaptive import LayerController
            controller = LayerController(patch, **settings_local)
        elif policy == "block_v3":
            from .block_adaptive import BlockController
            controller = BlockController(patch, **settings_local)
        elif policy == "av_v2":
            from .av_adaptive import AVController
            controller = AVController(patch, **settings_local)
        else:
            controller = AdaptiveController(patch, **settings_local)
        controller.begin(sigmas.detach().cpu().tolist())
        patch.adaptive_controller = controller
        def step_done(i, denoised, x, total):
            # The callback is one sampler step boundary, not a block or model call.
            # Set ratio for the following step; do not spend a request on the final step.
            controller.completed_step(i)
            if callback is not None:
                callback(i, denoised, x, total)
        try:
            return executor(model_wrap, sigmas, extra_args, step_done, noise,
                            latent_image, denoise_mask, disable_pbar)
        finally:
            patch.adaptive_controller = None
            controller.emit({"event": "end", "requests": controller.requests})
    return sample
