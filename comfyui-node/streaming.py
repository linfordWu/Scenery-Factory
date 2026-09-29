# SPDX-License-Identifier: GPL-3.0-only
"""Streaming INT8 gate with immutable per-plan padding decisions.

Adapted from ComfyUI H3 sparse attention and the evaluated StreamingGate path.
No profiler, global monkey patch, runtime quantizer, or experimental import.
"""
import comfy.model_management
import comfy_kitchen as ck
from comfy_extras.nodes_sparse_attention import (
    SparseAttnPatch as CoreSparseAttnPatch, PRODUCER_CHUNK, h3_eligible,
)


class SparseAttnPatch(CoreSparseAttnPatch):
    def vsa_plan(self, layout, device):
        plan = super().vsa_plan(layout, device)
        if "h3_v2_all_valid" not in plan:
            # One D2H per new plan. Preserve the original mask multiplication.
            src = plan["src"].detach().cpu().tolist()
            plan["h3_v2_all_valid"] = tuple(
                all(v >= 0 for v in src[i:i + getattr(self, "producer_chunk", PRODUCER_CHUNK)])
                for i in range(0, len(src), getattr(self, "producer_chunk", PRODUCER_CHUNK))
            )
        return plan


def streaming_attention(attn, x, rope_freqs, transformer_options, patch, block_index, gate):
    heads, head_dim = attn.heads, attn.head_dim
    qw = comfy.model_management.cast_to(attn.q_norm.weight, device=x.device)
    kw = comfy.model_management.cast_to(attn.k_norm.weight, device=x.device)
    gate_gpu = gate[0].to(device=x.device, non_blocking=True)
    scale_gpu = gate[1].to(device=x.device, non_blocking=True)
    plan = patch.vsa_plan(transformer_options["minimax_h3_layout"], x.device)
    producer_chunk = getattr(patch, "producer_chunk", PRODUCER_CHUNK)
    n, freqs = plan["n"], patch.vsa_rope_freqs(rope_freqs, plan)
    sink = (0, plan["n_prefix"])
    extra = {
        "tail": False, "block_len": plan["block_len"],
        "coarse_gate": x.new_empty(n, heads * head_dim).view(1, n, heads, head_dim),
    }

    def chunks():
        for i in range(0, n, producer_chunk):
            idx = plan["src"][i:i + producer_chunk]
            chunk_len = idx.shape[0]
            mask = idx >= 0
            all_valid = plan["h3_v2_all_valid"][i // producer_chunk]
            xc = x[idx.clamp_min(0)]
            if not all_valid:
                xc = xc * mask.unsqueeze(1).to(x.dtype)
            extra["coarse_gate"].view(n, heads * head_dim)[i:i + chunk_len] = ck.int8_linear(
                xc, gate_gpu, scale_gpu, out_dtype=x.dtype
            )
            yield attn.qkv_proj(xc)

    key = (block_index, n, tuple(transformer_options.get("uuids", ())))
    pooled = patch.pooled.get(key)
    out, kmean, vscale = ck.sol_attn_chunked(
        chunks, n, heads, freqs, (qw, kw),
        kmean=None if pooled is None else pooled[0],
        vscale=None if pooled is None else pooled[1],
        tau=patch.tau, topk_ratio=patch.topk_ratio, token_aug=patch.extra_tokens,
        sink_blocks=list(sink), sink_q=list(sink), rope_eps=attn.q_norm.eps, **extra,
    )
    controller = getattr(patch, "adaptive_controller", None)
    if controller is not None:
        controller.observe_gate(extra["coarse_gate"], block_index, plan, transformer_options["minimax_h3_layout"])
    patch.pooled[key] = (kmean, vscale)
    del gate_gpu, scale_gpu
    patch.log_once(("producer", n),
                   f"H3 v2 sparse producer: {x.shape[0]} tokens, {n} padded rows, INT8 streaming gate")
    out = out.view(n, heads * head_dim)
    out = out[plan["inv"]]
    return attn.out_proj(out)


def make_block_patch(block, index, patch, gate):
    def attention(h, rope_freqs=None, transformer_options=None):
        return streaming_attention(block.attn, h, rope_freqs, transformer_options, patch, index, gate)

    def block_patch(args, extra):
        controller=getattr(patch,"adaptive_controller",None)
        block_control=controller is not None and hasattr(controller,"should_skip")
        if block_control and controller.should_skip(index):
            return {"img":args["img"]}
        before=controller.sample(args["img"],args["layout"]) if block_control else None
        if not h3_eligible(block.attn, args["img"], args["rope_freqs"], args["transformer_options"], patch, index):
            output=extra["original_block"](args)
        else:
            output=extra["original_block"]({**args,"attention":attention})
        if block_control:controller.observe_block(index,before,controller.sample(output["img"],args["layout"]))
        return output

    return block_patch
