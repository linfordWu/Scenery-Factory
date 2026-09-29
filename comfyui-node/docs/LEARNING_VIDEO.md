# 学习视频工作流文档：文档插图 → 1080P 动画解说片段

以「飞书驱动的异构算力编排」学习视频为例，记录从 docx 文档到成片的完整工作流。
成片规格：8 个 5 秒片段、1920×1080、24fps、中文旁白、ffmpeg 拼接共 41.4 秒。

## 1. 流程总览

```
docx 文档
  ├─ 提取插图（word/media/）与段落结构 → 按章节主线选图（8 张示意图）
  ├─ 每张图配一句旁白（≤45 字）+ 一段专属动效脚本
  ├─ 逐图生成 5 秒动画片段（H3 + 009jev/laya，1920×1080）
  ├─ 抽帧质检（动效幅度、文字保真、旁白音频）
  ├─ （可选）去水印：局部裁剪→高斯模糊→覆盖
  └─ ffmpeg concat 拼接（统一 1920×1080、CRF 18）
```

## 2. 工作流文件

`examples/learning_video_1080p.api.json` — 由 `009jev_cold_start.api.json` 改造，差异：

| 节点 | 参数 | 值 |
|---|---|---|
| 131 `MiniMaxH3ReferenceToVideo` | width × height × length | **1920 × 1080 × 124**（模板为 1024×1792） |
| 131 | ref_images | **仅 ref_image_0**（删掉 901 之外的 LoadImage 与两条连线） |
| 131 | prompt | 见第 3 节结构 |
| 900 `H3JevNativeSLAPatch` | prompt_context | 与 131 的 prompt 一致 |
| 129 `RandomNoise` | noise_seed | 每片段不同（防 ComfyUI 执行缓存命中） |
| 92 `SaveVideo` | filename_prefix | `video/learn3_clipXX` |

决策引擎走默认 laya（每片段 370–410 秒，基线约 530 秒）。

## 3. 提示词结构（v3：逐个浮现 + 静态镜头）

```
subject_definitions: <Picture 1> 是手绘水彩示意图
summary: 顺序浮现动画 + 中文旁白
integrated_multimodal_description:
  锁定镜头（no zoom/pan/shake），保持版式与文字
[Shot 1]:
  {motion}     ← 每张图的专属动效脚本（关键！）
  旁白: <d>[Chinese] {narration}</d>
overall_soundscape: 仅旁白
```

**动效脚本要点**（v3 教训总结）：

- v1 失败：写 "keep text exactly / no morphing / very slow push-in" → 变成"会呼吸的静图"
- v2：放开运动 + 逐图定制动作 → 真动画，但带推镜且文字漂移
- v3：**镜头锁定 + 元素按箭头/语义顺序逐个浮现** → 最适合流程图解类图片，文字质量最好
- 写法：`First the title appears, then the arrows draw themselves segment by segment,
  then the machines appear one by one from left to right`（明确顺序词：first / then / finally）

## 4. 批量生成脚本

`gen_learning_video.py`（参考实现位于 `/home/wlf/models/`，skill 版本见 `../../skills/learning-video/`）：

```python
CLIPS = [("doc_01.png", "旁白……", "动效脚本……"), ...]   # 每图三要素
python3 gen_learning_video.py 1 2 3   # 按编号生成指定片段，断点可续
```

要点：每片段不同种子；提交后轮询 `/history`；失败自动打印 ComfyUI 错误详情。

## 5. 剪辑与去水印

- 拼接：`concat_learn.sh learn_final.mp4 learn3_clip`（concat demuxer →
  `scale=1920:1080:lanczos` → libx264 CRF 18 + AAC 160k，音画同步）
- 去水印：生成图的"由AI生成"水印贴底边，`delogo` 滤镜不支持贴边区域，
  用 `crop → gblur(sigma=25) → overlay` 局部模糊覆盖，双角落一次完成

## 6. 质检清单

- [ ] 抽 3 帧（头/中/尾）确认动效确实发生（不是静图）
- [ ] 文字无大面积漂移（v3 方案最稳）
- [ ] ffprobe 确认 1920×1080、124 帧、含音频流
- [ ] 旁白与画面对应（片段 8 模型可能自动把旁白渲染成字幕，可接受）
