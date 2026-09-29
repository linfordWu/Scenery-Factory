---
name: learning-video
description: 把文档（docx/图片集）的示意图批量生成 1080P 动画解说片段并剪辑成学习视频。基于 MiniMax-H3 + laya 决策加速，每张图生成 5 秒"元素按语义顺序逐个浮现"的动画，配中文旁白，最后 ffmpeg 拼接。当用户要求"把文档/图片做成学习视频/解说视频/动画课件"时使用。
---

# Learning Video Skill：文档示意图 → 1080P 学习视频

## 前置条件

- ComfyUI 服务可用（默认 `http://localhost:8188`），已装本节点包（ComfyUI-MiniMax-H3-W4A4-VSA）
- MiniMax-H3 turbo8 模型 + qwen3vl 文本编码 + 视频/音频 VAE 就位（见仓库 DEPLOYMENT.zh.md）
- laya 权重在 `models/laya/`（默认决策引擎）；容器内有 ffmpeg

## 标准流程

1. **提取素材**：docx 用 `unzip`/`python zipfile` 读 `word/media/` 与 `document.xml` 段落结构；
   按章节主线选 6–10 张示意图（手绘/流程图类效果最好，截图类直接展示即可不必动画化）。
2. **写三要素**：每张图配 `旁白`（≤45 字，约 5 秒语速）与 `动效脚本`（见下），填入
   `scripts/gen_learning_video.py` 的 `CLIPS` 列表。
3. **放入参考图**：图片复制到 ComfyUI 的 `input/` 目录（如 `doc_01.png`）。
4. **先试一片**：生成 1 个片段并抽 3 帧（头/中/尾）质检，确认动效发生且文字可读，再批量。
5. **批量生成**：`python3 scripts/gen_learning_video.py 1 2 3 ...`（片段编号，断点可续；
   每片约 6–7 分钟，laya 加速中）。
6. **拼接**：`scripts/concat_learn.sh <输出名> <文件前缀>`（在容器内跑，ffmpeg 统一
   1920×1080、CRF 18、AAC）。
7. **（可选）去水印**：贴底边水印用 `scripts/dewatermark.sh`（crop+gblur+overlay，双角落）。

## 动效脚本写法（关键经验）

- **锁定镜头**：必须写 `Locked-off completely static camera: no zoom, no pan`——
  否则模型默认缓慢推镜，看起来像"图片放大"。
- **逐个浮现**：用明确的顺序词写清出现次序：
  `First the title text appears, then the arrows draw themselves segment by segment,
  then the machines appear one by one from left to right, finally ...`
- **不要写** `keep text exactly` / `no morphing`（会压死运动，产出"会呼吸的静图"）；
  改为 `text stays in place and readable; natural hand-drawn wobble is fine`。
- 已出现的元素给一点微动（lights blink, small motions），画面不死。
- 负面教训：约束越"保守"越像静图；v3（静态镜头+顺序浮现）在流程图类图片上文字质量最好。

## 提示词模板

见 `scripts/gen_learning_video.py` 中 `PROMPT_TMPL`；旁白用
`<d>[Chinese] ...</d>` 嵌入（H3 原生支持中文语音，女声旁白）。

## 质检清单

- 抽头/中/尾 3 帧：动效确实发生、文字无大面积漂移
- `ffprobe`：1920×1080、124 帧、有音频流
- 拼接后时长 = 片段数 × 5.17s；`learn_final*` 在 ComfyUI `output/video/` 下

## 文件

- `scripts/gen_learning_video.py` — 批量生成（自包含；环境变量 `COMFY_BASE`、`COMFY_INPUT_DIR`、`WORKFLOW_JSON` 可覆盖默认值）
- `scripts/concat_learn.sh` — 容器内 ffmpeg 拼接
- `scripts/dewatermark.sh` — 贴边水印局部模糊去除
- 工作流模板：仓库 `examples/learning_video_1080p.api.json`
- 详细文档：仓库 `docs/LEARNING_VIDEO.md`
