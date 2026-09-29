# Third-party notices

The distributed code is GPL-3.0-only; see LICENSE. Model weights are not included.

- **ComfyUI**, Comfy Org and contributors: [source](https://github.com/Comfy-Org/ComfyUI), GPL-3.0. This package adapts its MiniMax H3 sparse-attention block-patch / chunked-producer structure and imports its `SparseAttnPatch`, layout planner, eligibility checks, and override installation. Native model serialization, ModelPatcher, and offload remain provided by ComfyUI.
- **comfy-kitchen**, Comfy Org and contributors: [source](https://github.com/Comfy-Org/comfy-kitchen), Apache-2.0. Used as an external dependency for ConvRot W4A4 native serialization/quantization, CUDA INT4, INT8 Gate, and Sol-Attn. No kitchen source or binaries are bundled.
- **Kablex/ComfyUI-Ref2VA-VSA**: [source](https://github.com/Kablex/ComfyUI-Ref2VA-VSA), Apache-2.0. Prior Ref2VA-specific gate-transplant and prefix/video separation work informed this implementation. The Kablex custom node is not required at runtime.
- **FastVideo / FastH3 VSA**: [source](https://github.com/FastVideo/FastVideo). Origin of the VSA approach and learned gate architecture.
- **barelymining/ComfyUI-MiniMax-H3-FastVideo**: [source](https://github.com/barelymining/ComfyUI-MiniMax-H3-FastVideo). Distribution source for the extracted `fasth3_vsa_gate.safetensors`; its model terms are independent of this package's source-code license.
- **ComfyUI-KJNodes** and **ComfyUI-MiniMax-H3-MotionCache-FastVAE** are external workflow dependencies. Their code is not bundled; their respective licenses apply.

Changes in this distribution (2026-09-08): CPU-backed preconverted FC1 overlay loader; portable offline conversion/verification; preconverted INT8 Gate loading; block-wise Gate streaming; per-plan padding decision cache; standalone node IDs and workflows. Profilers, runtime FC1 conversion, experimental dispatch, and unadopted optimization variants are omitted.

The original diffusion, Gate, VAE, and text-encoder weights and their derived quantized copies remain subject to their own upstream terms, including applicable MiniMax H3 model terms. This repository does not grant rights to redistribute those weights.

009jev (experimental branch): imports native ComfyUI SLA and adds Jev keep-rate control. TypeSafe SDK is an optional external dependency under its own terms; it is not bundled. Its API service requires a user-provided account/key. The009jev path does not use W4A4 conversion or learned VSA gates.
