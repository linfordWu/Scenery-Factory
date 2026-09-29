> 这是旧版 W4A4/VSA 控制的记录。当前的 009jev 请参阅 [009JEV.md](009JEV.md)。

# Jev Adaptive VSA 实验版

[English](JEV_ADAPTIVE.en.md)

开发分支 `exp/jev-adaptive-vsa`。这不是保证稳定版加速效果的发布版本。本页的验证对象是 **layer_v5 / 4step**。固定 5% 耗时 212.20 秒，而最新方式为 219.15 秒。"180 秒以内且质量相当于固定 5%" 的目标尚未达成。参见[实测与限制](docs/experiments/README.md)。

## 必要条件与安装

1. 请满足 [README](README.md) 中对应的 ComfyUI、comfy-kitchen、模型、Gate 及预转换步骤的要求。模型、参考图像和已转换的权重不随仓库附带。实验已在 Linux（aarch64）/ NVIDIA GB10、ComfyUI 0.36 系的指定环境中验证，不保证其他环境的兼容性。
2. 请将本分支放入 ComfyUI 的 `custom_nodes` 目录，或使用本分支的 `setup_env.py` 安装。请勿同时加载同一节点的另一份副本。安装程序会复制 Jev 模块、文档和示例，但不会自动安装 SDK。
3. 仅在试用 Jev 时才需要准备 TypeSafe 的账号/API 密钥和 SDK 专用 venv。固定模式不需要 SDK/API 密钥。

获取示例（在本分支发布到远程后可用）：

```bash
git clone --branch exp/jev-adaptive-vsa --single-branch https://github.com/sepiablue-ai/ComfyUI-MiniMax-H3-W4A4-VSA.git
```

在仓库内创建 SDK 专用环境的示例（验证时 SDK 使用的 Python 为 3.10）：

```bash
python3 -m venv .venv-jev
.venv-jev/bin/pip install -r requirements-jev.txt
```

无需将 SDK 混入 ComfyUI 的 Python 或全局 Python。请在工作流的 `sdk_python` 中指定该 venv 的 Python 可执行文件绝对路径（`.venv-jev/bin/python`）。留空会使用 ComfyUI 自身的 Python，因此如果将 SDK 安装在单独的环境中，请务必进行设置。

## 不把 API 密钥写入文件直接启动

API 密钥从**启动 ComfyUI 的进程的环境变量 `TYPESAFE_API_KEY`** 中读取。请勿将密钥写入工作流、脚本、提交或日志中。对已经启动的 ComfyUI 事后设置的环境变量不会生效。

在同一个 shell 中以隐藏方式输入，然后从该处执行平时的 ComfyUI 启动命令。密钥明文不会留在命令历史记录中。

```bash
read -r -s -p 'TypeSafe API key: ' TYPESAFE_API_KEY; echo
export TYPESAFE_API_KEY
# 从此 shell 执行平时的 ComfyUI 启动命令。
# 使用后清理：unset TYPESAFE_API_KEY
```

代码不会将密钥、HTTP 头部、SDK 异常正文输出到日志。`[Jev VSA]` 日志会输出特征量、选择结果和 usage。layer_v5 除了汇总后的音频/视频激活度、sigma、层编号、keep 率之外，还会向 TypeSafe 发送关于实验目的和固定的人物/语音质量的说明。state 中不包含原始图像、音频、模型权重和 API 密钥。

## 对比工作流

- [固定 5% / 4step](examples/fixed5_4step.api.json)
- [Jev layer_v5 / 4step](examples/jev_layer_v5_4step.api.json)

这些是 ComfyUI **API 格式**，与普通的 GUI 工作流不同。请各自准备 3 张参考图像，修改 LoadImage 节点 901〜903 的名称，或将图像放入 `ComfyUI/input/jev_reference_01.png`〜`03.png`。顺序为全身、上半身、脸部。两种条件共用同一人物的参考图像。图像的发布权请使用者自行确认。

请确认 node127 的模型与转换缓存、119/120 的 VAE、128 的 Text Encoder 与你本地的名称一致。更换模型后的结果请与文中刊载的实测复现区分开。外部节点使用 KJNodes 的 ChunkFFN 和 MotionCache-FastVAE 的 FastVAE。不使用 MotionCache 的输出复用。

向正在运行的专用 ComfyUI 提交一次的示例（Python，无额外依赖）：

```bash
# 将 node900 的 sdk_python 指向专用 venv，并向已启动的 ComfyUI 提交一次
python3 - <<'PY'
import json, urllib.request
with open('examples/jev_layer_v5_4step.api.json', encoding='utf-8') as f:
    graph = json.load(f)
graph['900']['inputs']['sdk_python'] = '/absolute/path/to/.venv-jev/bin/python'
req = urllib.request.Request(
    'http://127.0.0.1:8188/prompt',
    data=json.dumps({'prompt': graph}).encode('utf-8'),
    headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req).read().decode('utf-8'))
PY
```

端口请根据使用环境修改。请记下返回的 prompt_id，并在 ComfyUI 中确认完成。此示例不会重试或轮询。响应不明确时，请先查看历史记录再重新提交。生成物的保存位置通过启动时的 `--output-directory` 指定。

固定版只需更改读取的文件即可。节点 900 为 mode=fixed 时，不会有附加统计和 API 通信。两个版本均保持 1024×1792、124f、24fps、seed43、4step、res_multistep/simple、sigma12/3。参考图像和提示词的修改请在两个版本中保持一致。

## 最新策略 layer_v5

执行全部 50 层 × 4step。第一个 step 所有层均为 5%，之后也保护 block0 和 block1。Jev 选择层重要度的 3 个等级和整体预算 2.5/3/3.5%。根据概率生成优先级，并在宿主侧于预算内进行分配。

| 层 | 低 / 基准 / 高 keep 率 |
|---|---|
| 浅层 | 2 / 5 / 7.5% |
| 中间层 | 1 / 5 / 7.5% |
| 最后 5 层 | 3 / 5 / 10% |

使用块输入输出差、各模态的排名、与前一个 step 的差异以及门控统计。这些并不是测量注意力机制稀疏化时的误差或质量本身的数值。API 在 step 边界最多发起 3 个 HTTP 请求（各 50 问），最后一个 step 之后不再调用。SDK 模型为 jev-1.13.0，重试 0 次。

推荐的验证设置：policy=layer_v5、mode=jev_adaptive、keep_percent=5、api_timeout=20、min_confidence=0.4、max_requests=3、producer_chunk=4096。在 layer_v5 中，首次 5% 已内置在策略中，无法通过 keep_percent 更改。

若预算 confidence 低于阈值，则采用保守的 3.5% 预算。API 失败/返回无效回答时，将下一个 step 恢复为全部 5%，并在此后该生成过程中停止 API 调用。达到预算上限/观测不足时也为全部 5%。confidence 并非质量保证的概率。除 4step 以外会报错。未支持的 sampler 会因通用 wrapper 的旧规格而变为固定 10%、API 调用 0 次，因此请指定 res_multistep。

## 保留的旧策略

| policy | 首次及判断对象 | 失败时 |
|---|---|---|
| gate_v1 | 首次 10%，从最初的门控样本中选择整个 step | 10% |
| av_v2 | 首次 5%，从 3 层深度的音频/视频门控中选择整个 step | 10% |
| block_v3 | 按设置的 keep，选择对 block0 以外省略 identity | 执行全部层并维持 keep |
| layer_v4 | 首次 5%，独立选择各层的比例 | 全部层 5% |
| layer_v5 | 首次 5%，按层重要度和整体预算 | 参见上文 |

block_v3 是人物再现性被破坏的试作方案，并非此次的入口。producer_chunk=8192 未确认到明显的加速效果，16384 尚未验证。请使用默认的 4096。

## 离线测试

使用装有 ComfyUI 的 torch 的 Python 执行。不需要 API 密钥，不涉及 GPU 生成和付费 API 调用。

```bash
"$COMFY_PYTHON" -B test_adaptive.py
```

test_sdk_transport.py 需要一个能同时 import SDK、httpx2 和 torch 的测试环境。无需在标准的 SDK 专用 venv 中添加 torch，常规安装也不要求这个辅助测试。它使用模拟的 503/timeout，外部 API 调用为 0 次。

代码的许可证为现有的 GPL-3.0-only。模型、参考素材、生成物和外部服务的使用条款另计。
