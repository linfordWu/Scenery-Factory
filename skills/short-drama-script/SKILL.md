---
name: short-drama-script
description: 约束 short-video-factory"创建剧本"步骤的结构化提示词规范。覆盖剧本/角色/场景/道具/分镜/剪辑六层的 JSON 输出契约、H3 渲染提示词硬规则、以及"不符合常理"故障的写作侧预防清单。当用户为短视频工厂编写或审核编剧/导演 agent 的提示词、手工撰写 ShotSpec、或排查生成视频违背常理(多余人物、物体复制、静图伪视频、台词被读出标点等)时使用。
---

# Short Drama Script Skill:剧本阶段的结构化提示词规范

适用位置:short-video-factory 的两条规划链路——手动/一键成片的 `_run_plan()` 三步 LLM 规划(编剧拆场景 → 角色/地点登记 → 导演逐场景拆镜)与对话出片的 `agents/planner.py`(编剧+选角一次 → 导演一次拆完全片);两条链路共用 `_materialize_plan()` 物化与 `_compose_prompt()` 渲染提示词组装(`apps/api/app.py`)。

**为什么约束必须前置**:本流水线的 repair 回路只换 seed 重渲染,不会重写 prompt 文本(`workers/engine.py:306-318`);density/情绪词检查只告警不拦截。提示词质量必须一次到位。

## 一、总原则

1. **LLM 输出是不可信的草稿,代码钳制是底线**。时长钳制 [3,8] 秒、画幅锁项目值、总时长归一化都在代码里(`_materialize_plan()` 与 `domain/video_constraints.py` 的 `plan_limits()` / `normalize_durations()`)。写作侧的目标是让 LLM 输出落在这些钳制之内,不触发兜底。
2. **一个镜头一个主动作**。多动作挤进一镜是"不符合常理"画面的最大来源(人物顺移、物体瞬移)。
3. **凡是不进画面的信息不要写进画面描述**。角色内心活动只进 `felt_intent`,不进 `action`。
4. **否定约束用英文写**。中文否定会被 H3 反向 priming(写了"不要出现狗"反而出现狗)。
5. **单参考图限制**:渲染只取 `reference_assets` 第一张,优先级 = 首帧 > 定妆照 > 场景图(`adapter.py:193-196`)。不要把多锚点希望寄托在文字描述上。

## 二、六层结构化契约

### 1. 剧本 / 场景层(编剧输出)

Schema:`{"scenes":[{"title":str,"summary":str}]}`

- 每场约 5-8 秒;场景数 = ⌈目标时长 / 7⌉,从严控制,总时长不得超过目标。
- `summary` 只写"发生什么、结果是什么",不写镜头语言和情绪形容词。
- 每场必须有明确发生地(供地点登记);抽象空间("内心世界""回忆里")要落到具体物理场景。

### 2. 角色层(登记输出)

Schema:`{"characters":[{"name":str,"kind":"character|location","description":str}]}`

- 人物、动物、宠物、幻想生物一律 `kind=character`;`location` 只指固定场景/建筑/空间。地点清单不允许为空。
- **description 写成可复用的固定外观描述,全片逐字一致**。模板:
  `名字:年龄段+性别,发型发色,上衣,下装,标志性特征(至多1个)`
  示例:`艾米:20岁女孩,及肩黑发,米色毛衣,牛仔裤`
- 禁止:相对描述("和上一场一样的衣服")、情绪词("忧郁的眼神")、超过 2 句的长描述。
- 数量约束:主要角色 ≤ 4 个;单镜出场 ≤ 3 人(超过则身份漂移风险陡增)。

### 3. 场景 / 地点层

- description 写**空间布局 + 光线方向 + 关键陈设**,模板:
  `名字:空间类型,光源方向与色温,3件以内关键陈设,时段`
  示例:`厨房:清晨,左侧窗户冷色自然光,白色灶台+木质餐桌+挂钟`
- 同一场景跨镜头的描述逐字一致;不写在画面中看不到的背景故事。

### 4. 道具层(object_states)

每镜的关键物体逐项登记:

```json
{"name":"煎蛋盘","count":"仅一只","start_state":"在艾米手中","end_state":"在餐桌中央"}
```

- `count` 必须写数量限定词("仅一只""两把"),不写"一些""几个"。
- start_state 与 end_state 必须互斥且分别成立——禁止"盘子既在手中又已在桌上"这类同帧混合态。
- 物体不得复制、悬浮、穿模、突然出现或消失;有拿取动作必须先有"在手边/被拿起"的起点。
- 只登记**关键**物体(推动剧情或被角色交互的);背景陈设不进 object_states。

### 5. 分镜层(导演输出,权威契约见 `app.py` `_materialize_plan()` 与 `agents/planner.py`)

```json
{"shots":[{
  "action":"本镜画面描述(只写可见内容)",
  "dialogue":"台词原文,无则空串",
  "duration_s":4到8的整数,
  "characters":["与登记清单逐字一致的名字"],
  "sequence_relation":"sequence_first | next_shot",
  "felt_intent":"角色内心意图(不进画面)",
  "narrative_beat":"本镜推动的剧情",
  "motion_contract":{
    "start_state":"动作开始状态",
    "primary_motion":"角色/关键道具的主运动",
    "secondary_motion":"环境/道具/光影的独立次运动",
    "end_state":"动作结束状态",
    "camera_motion":"运镜"
  },
  "beats":{
    "already_happened":["已演完不许重播的情节"],
    "this_clip_only":["本镜头独占情节"],
    "reserved_for_later":["后续镜头预留,不许提前泄露"]
  },
  "object_states":["见第4层"],
  "camera":{"shot":"景别","movement":"运镜方式"},
  "lighting_palette":"光线方向/色温/主色调(同场景内逐字一致)",
  "acceptance":{"required":["验收必须看到的"],"forbidden":["验收不得出现的"]}
}]}
```

硬性写作规则:
- **一镜一主动作**:`primary_motion` 只承担一个主动作(如"端盘落桌");猫、窗帘、蒸汽、窗外景色只做响应式微动,不得抢占主事件。
- `secondary_motion` 必填且与主运动独立(光影/道具/景深视差至少一项)——否则产出"会呼吸的静图"。
- **禁止把静态图的裁切/平移/缩放/Ken Burns 当视频**;相机运动不能是画面唯一变化。
- `dialogue` ≤ 45 字(约 5 秒语速);单镜至多一个说话人;说话人必须是 `characters` 中第一个非 location 角色。
- `beats` 三桶必须填满,防"已演情节重播"和"未来情节泄露"。
- `acceptance.forbidden` 至少包含:"多余人物""肢体畸形""物体凭空出现/消失""画面文字/水印"。

### 6. 剪辑 / 衔接层

- 场景内第一镜 `sequence_first`,后续 `next_shot`;`next_shot` 的开场必须能接续上一镜的 `observed_end_state`(评审写回的实际末态),续接描述以观测末态为准而非计划末态。
- 时长预算:单镜 4-8 秒;单场景所有镜头 `duration_s` 之和 ≤ 场景秒数 + 2;全片偏差 >15% 会被代码强制归一化(`domain/video_constraints.py` `normalize_durations()`),与其被缩不如规划时从严。
- 相邻镜头避免同景别同机位硬切;景别序列要有变化(远→中→近或反之)。

## 三、H3 渲染提示词硬规则(`_compose_prompt` 消费端)

渲染提示词采用导演级分段结构(`app.py` `_compose_prompt`,段间空行分隔):

```
Duration: {duration_s} seconds | Aspect ratio: {aspect} | Style: {style}

SCENE 角色锁定（逐字保持）: … | 场景锁定（逐字保持）: … | 参考图是唯一视觉锚点…

SHOT 开场接续上一镜实际末态: … | {action} | 本镜剧情节拍: … |
动作开始状态/主动作/环境/道具变化/动作结束状态: … |
本镜头中{speaker}用中文普通话清晰地说: <d>[Chinese] 台词</d> 声音清晰、稳定…

CAMERA shot=…, movement=…

LIGHTING & PALETTE 光线方向/色温/主色调(ShotSpec.lighting_palette,导演拆镜必填)

AVOID: 已演不重演 | 不要提前出现 | 关键物体约束… | 非焦点人物微动约束 |
次要元素微动约束 | 无台词时的英文声景否定 | 系统级视频约束 |
画面中不出现任何文字、字幕、水印、logo、标识
```

- **不单独设 AUDIO 段**:台词内嵌 SHOT 段;无台词镜头的声景否定并入 AVOID。
- CAMERA / LIGHTING & PALETTE 段在无内容时省略,其余段恒定存在。
- 写作侧**不要在 action 里重复 AVOID 段的系统约束**,浪费密度预算。

其余硬规则:

1. **台词必须走 `<d>[Chinese] 台词</d>` 语法**;写进 ShotSpec 前先做清洗:去引号、换行、句尾标点(`domain/prompt_text.py` `clean_dialogue()`)——否则模型会把"引号""逗号"读出来。
2. **无台词镜头的声景否定用英文**:
   `No voice, no speech, no dialogue, no narration, no murmuring, no whispering, no singing, in any language.`
3. **提示词密度 ≤ 3.0**(`quality/density.py`):台词每句 +1、每个角色 +1、非静态运镜 +0.5、`sequence_first` +2。超限就拆镜,不要堆词。
4. **裸情绪词黑名单**(出现在 action/felt_intent 会告警):
   `紧张/激动/悲伤/兴奋/愤怒/恐惧/绝望/焦虑/难过/开心/委屈/释然/戏剧性/epic/cinematic/dramatic/emotional/tense/sadly`
   情绪必须用**可见的身体语言**改写:"她很紧张" → "她双手绞着衣角,视线避开对方"。
5. `lighting_palette` 与场景描述、项目风格一致,同一场景内逐字一致。
6. 每镜不同 seed(默认 2026 起),防 ComfyUI 缓存命中。

## 四、"不符合常理"故障 → 写作侧预防对照表

| 故障现象 | 根因 | 写作侧预防 |
|---|---|---|
| 多余手指/肢体畸形 | 动作描述含糊、单镜动作过多 | 一镜一主动作;acceptance.forbidden 加"肢体畸形" |
| 多余人物 | characters 名单与 action 描述不一致 | action 中的人数 = characters 列表人数;forbidden 加"多余人物" |
| 物体复制成多份 | count 未限定 | object_states.count 写"仅一只" |
| 物体瞬移/悬浮 | start/end_state 混合或缺失 | 两态互斥、分别成立;有拿取先有起点 |
| 静图伪视频(会呼吸的静图) | 无独立次运动、约束过度保守 | secondary_motion 必填;不写 "keep exactly/no morphing" |
| 已演情节重播 | beats.already_happened 为空 | 三桶必填 |
| 未来情节泄露(结局提前出现) | reserved_for_later 缺失 | 三桶必填 |
| 角色换装/变脸 | description 跨镜不一致 | 全片逐字一致;以定妆照为锚 |
| 台词被读出标点/引号 | dialogue 未清洗 | 过 `_clean_dialogue` 规则 |
| 无台词镜头有杂音人声 | 中文否定反向 priming | 用英文否定声景段 |
| 相机推拉代替剧情运动 | camera_motion 是画面唯一变化 | primary_motion 与 camera 分离,各自必填 |

## 五、提交前自检清单

- [ ] 场景数 × 7 秒 ≈ 目标时长,无超时风险
- [ ] 角色/地点 description 全片逐字一致,无相对描述
- [ ] 每镜:1 个主动作、≤1 个说话人、台词 ≤45 字、duration_s ∈ [4,8]
- [ ] 每镜 motion_contract 五段齐全,secondary_motion 独立于主运动
- [ ] 每镜 lighting_palette 与场景/风格一致,同场景内逐字一致
- [ ] 每镜 beats 三桶非空;关键物体有 count 限定
- [ ] action/felt_intent 无裸情绪词;密度估算 ≤ 3.0
- [ ] dialogue 已去引号/换行/尾标点;无台词镜备英文否定声景
- [ ] acceptance.required/forbidden 覆盖对照表中的本片高风险项
- [ ] next_shot 镜头开场与上一镜 observed_end_state 兼容

## 相关文件

- 生产规划链路:`short-video-factory/apps/api/app.py`(`_run_plan`、`_materialize_plan`、`_compose_prompt`)
- 对话出片规划 agent:`short-video-factory/agents/planner.py`(对话 → 结构化分镜)与 `agents/assistant.py`(需求收集)
- 数据契约:`short-video-factory/domain/schemas/core.py`(ShotSpec)
- 系统约束与共享钳制:`short-video-factory/domain/video_constraints.py`
- 台词清洗:`short-video-factory/domain/prompt_text.py`
- 质量检查:`short-video-factory/quality/density.py`、`quality/checks.py`
- 修复映射:`short-video-factory/agents/repair.py`
