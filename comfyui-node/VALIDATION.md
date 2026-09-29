# Validation — 2026-09-20 / ComfyUI 0.36.0

在 Linux（aarch64）/ NVIDIA GB10 环境下，确认了通过 `setup_env.py` 向现有环境的安装导入以及实际生成。
未改动 ComfyUI 本体、venv 的依赖包和全局 Python。对原始模型、Gate 及既有转换缓存全部进行了 SHA-256 校验比对，缓存通过同一驱动器上的硬链接复用。未进行模型下载或重新转换。

- ComfyUI: **0.36.0**, commit `7a0b5eede3f9721c8faab290689893f36edc6d66`
- Python 3.13.13 / PyTorch 2.14.0+cu130 / comfy-kitchen 0.2.34 / comfy-aimdo 0.5.5
- 全新安装、native INT4 GPU→CPU→GPU 检查、50 个 FC1 shard＋Gate＋原始权重的 SHA-256 校验比对均成功。
- 运行了 1 次 SayakaBenchmark 的 016 workflow。与现有标准用例相同：参考图像 3 张、英文台词 prompt、1024×1792、124 frames、24 fps、seed 43、4 steps、res_multistep/simple、Sigma Shift 12/3、ChunkFFN 4、FastVAE batch 2。应用了 FC1 W4A4＋Streaming VSA keep 5%。
- 未修改 SayakaBenchmark 的脚本，只读取使用其现有的 prepare / capture_reproduction / execution_status。不使用标准 runner 的 0.5 秒轮询，仅在外部执行时每隔 60 秒以及完成通知时进行监控。结果和视频也保存到 benchmark 文件夹之外。这不是运行标准 runner 本身的记录。
- 启动条件与标准 master 运行相同：Dynamic VRAM、`--disable-pinned-memory`、`--cache-none`、`--use-ck-attention`。本体、内核、节点的计算代码均无需修改。

| 指标 | 结果 |
|---|---:|
| 生成时间（execution_start → execution_success，含模型加载） | **209.233秒** |
| ComfyUI 控制台的 Prompt 时间 | 209.49秒 |
| GPU 使用量最大观测值（整卡，60 秒间隔＋完成时） | 11,479 MiB |
| 实际 ComfyUI 进程的 Peak RSS（自启动起） | 11,610.7 MiB |
| FC1 / Gate | 50/50层，runtime weight conversion 0秒 |
| Dynamic VRAM / Streaming VSA | 有效，已确认 sparse producer 日志 |
| Video / Audio | 1024×1792、124 frames、24 fps、有音频 |
| FFmpeg 全量解码 | 成功 |
| 中断 / 重新运行 | 无 / 无 |

| 生成进度 | GPU使用量 MiB | 进程RSS MiB | 系统可用RAM MiB | 系统disk read MiB/s |
|---:|---:|---:|---:|---:|
| 60秒 | 11,477 | 8,569.2 | 31,367.1 | 491.82 |
| 120秒 | 11,479 | 8,603.8 | 30,569.7 | 26.73 |
| 180秒 | 7,562 | 8,687.6 | 30,223.0 | 45.21 |
| 完成时（约209.5秒） | 5,234 | 5,720.8 | 33,283.3 | 0.35 |

GPU 值为 60 秒采样中的最大值，并非瞬时峰值。RAM 值为 OS 记录的进程生命周期峰值。disk 值为系统整体自上次观测以来的平均值，并非仅测量 pagefile 访问。初始模型加载完成后磁盘负载下降，可用 RAM 维持在约 29.5 GiB 以上。未观测到异常的长时间化或因内存不足导致的持续停滞。这并非"完全没有发生交换（swap）"的主张。

这是单次、含首次加载的验证，并非与其他方式的速度对比。视频和音频的主观质量未做评估。依赖节点使用了现有的，因此在这台 PC 上未执行通过 `--install-dependencies` 全新获取／pip 路径。由于复用了旧的转换缓存，未在 0.36.0 上进行 50 层的全新转换。

English: Setup and one complete generation passed on ComfyUI 0.36.0 / Linux (aarch64) / NVIDIA GB10. Existing weights and a fully SHA-256-verified cache were reused without downloads or package changes. The 1024×1792, 124-frame, 24-fps, four-step run took **209.233 seconds**, including model loading, with audio and full FFmpeg decode validation. Sampled GPU usage reached 11,479 MiB; process lifetime peak RSS was 11,610.7 MiB. Monitoring was every 60 seconds and on completion. SayakaBenchmark helpers were imported read-only through an external execution wrapper; its standard runner was not invoked or modified. Results were saved outside its repository. Disk reads are system-wide, not a direct pagefile measurement. There was no interruption or retry. This is a single-run compatibility validation, not a speed or quality comparison. Fresh dependency installation and a new full 50-block conversion were not performed in this validation.

---

# Original validation — 2026-09-08

在 NVIDIA GB10 的现有 ComfyUI 环境中验证了分发版代码和转换工具。
评估用的测量/对比代码、权重、个人参考图像和视频均未包含在分发物中。

## 预先转换

- 使用分发物中的 `convert.py`，从现有 INT8 ConvRot 模型转换了全部 50 层 FC1。
- 从 block 0 开始依次验证：保存→重新加载→QuantizedTensor/layout 恢复→GPU 执行→CPU offload→再次 GPU 执行。所有层的输出与保存前完全一致。
- 确认 CUDA `cutlass_int4_dequant` 的执行及成功返回值。每层 3 次，共 150 次。
- Gate 也在 CPU 上对 50 层进行 INT8 转换并保存，保存前后的 tensor 全部比对。
- 新转换的 FC1/Gate 全部 tensor 与采用候选评估中使用的现有缓存完全一致。
- 确认了完成缓存的 SHA-256 校验、对应 API 及 native INT4 的轻量 `--check`。

## 使用分发代码完整生成 1 次

使用分发 API workflow，仅将 3 张参考图像和 prompt 与评估时的 PROMPT-A 对齐，并指定了缓存的位置。其余计算条件与分发 workflow 相同。
用于对比的 latent 保留处理仅添加在验证进程内部，未包含在分发 workflow 中。

| 指标 | 结果 |
|---|---:|
| 分辨率 / frames / fps | 832×1408 / 124 / 24 |
| steps / seed | 4 / 43 |
| FC1 应用 | 50/50层 |
| FC1 native INT4 | 800次（每层16次），全部调用成功 |
| FC1 backing data / scale | 保留在 CPU，Dynamic VRAM 有效 |
| runtime FC1 / Gate weight 转换 | 0 |
| FC1 shard 加载 | 0.0197秒 |
| manifest 检查＋模型构建/加载 | 0.1875秒 |
| Gate CPU load / pin | 1.221秒 |
| Sampler 实际壁钟时间 | 99.858秒 |
| step 1 / 2 / 3 / 4 | 32.067 / 22.496 / 22.601 / 22.673秒 |
| steady（step 2~4 平均） | **22.590秒/step** |
| 进度条修正显示 | 90秒 |
| Prompt Total | **149.704秒** |
| FastVAE batch 2 | 24.90秒 |
| 模型 staged CPU | 16,320MiB |
| 视频/音频 latent | 与现有 FC1 Preconverted 基准双向完全一致且均为有限值 |
| 媒体 | ffprobe 确认为 832×1408、124 frames、有音频，FFmpeg 全量解码成功 |

本次最终确认未进行 VRAM/RSS 的周期监控。在此之前的采用候选评估中，整卡 GPU 峰值为 11,560.90MiB，RSS 为 10,631.68MiB，但不作为最终分发代码的同时测量值处理。

## 对比结果的解读

既有记录中，Baseline INT8 的 Prompt 为 168.44 秒，runtime-convert FC1 W4A4 为 213.28 秒。
相对采用候选评估的 146.26 秒，本次为 149.70 秒，但 steady 从 22.56→22.59 秒，基本处于同一水平。
各值均为单次测量，包含 OS file cache、首次加载顺序、测量处理差异等因素。不将微小差异判定为确定性的改善或退化。

ComfyUI 在启用 Dynamic VRAM 时会修正进度条的初始时间显示。请勿将以往的 Sampler 108秒/93秒 显示值与本次的实际壁钟时间 99.858 秒直接比较。
加载采用 lazy mmap，0.1875 秒并不意味着所有权重的实际页面读取和 GPU 传输已完成。

完全一致是在使用相同的原始权重、缓存、输入、seed 和依赖实现的前提下，本对比得出的结果。FC1 W4A4 相对原始 INT8 模型本身是不可逆转换，并未主张与 INT8 完全一致。
主观的画质、角色还原度和音频优劣未做评估。
