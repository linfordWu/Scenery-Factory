# SPDX-License-Identifier: GPL-3.0-only
"""Bounded, modality-separated gate observations at three depths."""
import math
import torch
from .adaptive import AdaptiveController

class AVController(AdaptiveController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.allowed = {str(v):float(v) for v in (1,2,3,5,7.5,10,15,20)}
        self.previous = {}
        self.observations = {}

    def begin(self, sigmas):
        self.sigmas = [float(v) for v in sigmas]
        self.total = len(self.sigmas)-1
        self.patch.topk_ratio = .05
        self.emit({"event":"begin", "total_steps":self.total, "keep_percent":5.0,
                   "max_requests":self.max_requests, "policy":"av_v2"})

    def observe_gate(self, gate, block_index, plan=None, layout=None):
        if block_index not in (0,24,49) or block_index in self.observations:
            return
        if plan is None or layout is None:
            return
        matrix=gate.detach().reshape(gate.shape[1],-1)
        narrow=matrix[:,::max(1,matrix.shape[1]//16)][:,:16]
        indices=plan.setdefault("jev_v2_indices",{})
        out={}
        for start,end,kind in layout.segments:
            if kind not in ("audio","video") or end<=start:
                continue
            key=(start,end,kind)
            if key not in indices:
                original=torch.linspace(start,end-1,min(64,end-start),device=gate.device).long()
                indices[key]=plan["inv"].index_select(0,original)
            sample=narrow.index_select(0,indices[key]).float().cpu().flatten()
            if not bool(sample.isfinite().all()): continue
            absolute=sample.abs(); mean=float(absolute.mean())
            previous=self.previous.get((block_index,kind))
            delta=None if previous is None else float((sample-previous).norm())/max(float(previous.norm()),1e-8)
            out[kind]={"samples":sample.numel(), "mean_abs":round(mean,7),
                       "std_abs":round(float(absolute.std(unbiased=False)),7),
                       "top10_mass":round(float(absolute.topk(max(1,math.ceil(sample.numel()*.1))).values.sum())/max(float(absolute.sum()),1e-12),6),
                       "relative_l2_change":None if delta is None else round(delta,6)}
            self.previous[(block_index,kind)]=sample
        self.observations[block_index]=out
        complete=all(b in self.observations and set(self.observations[b])=={"audio","video"} for b in (0,24,49))
        self.stats={"policy":"av_v2","complete":complete,
                    "blocks":{str(b):data for b,data in self.observations.items()},
                    "excluded":"text, reference images/audio, conditioning and padding",
                    "metric_kind":"gate activation proxy; not attention mass or quality scores"} if complete else None

    def completed_step(self, step):
        if step <= self.last_step:
            return
        super().completed_step(step)
        self.observations={}
