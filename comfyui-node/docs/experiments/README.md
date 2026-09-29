# 逐层 Jev 验证 / Per-layer Jev experiment

2026-09-20 / Linux / NVIDIA GB10。所有条件均为 1024×1792、124f、24fps、4step、seed43，使用相同的 Ref2VA 参考、模型与 prompt。执行全部 200 层。初次运行时全层 5%，最终版还保护了后续入口处的 2 层。

| Condition | Mean keep % | Generation s | Later step mean s* |
|---|---:|---:|---:|
| Fixed5 |5|212.20|35.43|
| Fixed1 |1|200.71|32.68|
| run16 / layer_v4 |4.85|219.32|35.59|
| run17 / budget confidence fallback |5|222.64|36.32|
| run18 / layer_v5 |3.8675|219.15|35.22|

*Steps2–4 estimated from received progress timestamps, subtracting recorded decision durations. Includes observation overhead, not GPU kernel profiling. Single run per condition, historical controls; not statistically established speed differences. Startup of the server is excluded; model loading, generation and saving are included in generation time.

最终版将所需的非 sink 注意力预算削减了 22.65%，但总生成时间并未改善。API 判断耗时约 4.56 秒。keep 率并不等于总 FLOPs 或实际保留的 tile 数。sink、因同分而产生的额外选择、QKV、FFN、projection、routing 以及传输开销仍然存在。未达到 180 秒以内的目标。

The final policy reduced requested non-sink attention budget by22.65%, but did not improve total time. Keep ratio is not total FLOPs or measured retained tile count. The under180s goal remains unmet. API/decision time was about4.56s.

run17 used a low-confidence budget fallback to all5%; run18 changed that to cautious3.5%. Both failures and final results are retained in the summary. Current code corresponds to the final behavior, not the old run17 fallback. Later packaging changes do not constitute new generation tests.

在本次之前的比较中，用户对固定 5% 评价较好。针对本次 run16～18 尚未获得人工音质排名。最终版在 1/2.5/4 秒处的画面保持了丸子头和服装，但动作不同。音频 1～4kHz 能量比率为：固定 5% 是 9.344%，run18 是 9.495%。这并不能证明音质一致。

Human feedback preferred fixed5 over earlier block-skip trials. New outputs have no human audio-quality ranking. Sampled stills preserve the bun and clothing but differ in pose. Audio band-energy similarity is not proof of equal quality. Wavelet/envelope analysis has no clean reference or phoneme alignment.

[Measurement summary](layer_v5_measurements.json) / [Audio summary and method](audio_summary.json). Media, reference images, full logs, credentials and machine-specific paths are intentionally not distributed. For exact original media, local experiment evidence is retained by the author; repository examples use user-supplied reference images and therefore are trial templates, not bit-exact reproduction bundles.

本次验证通过 Jev 发起 9 次 HTTP 请求、447 个问题，输入 237982 tokens、输出 16586 tokens。按输入 $0.042/M、输出免费的假设估算约为 $0.009995，并非实际账单金额。SDK 0.7.0 / Jev 1.13.0。价格与服务规格可能发生变化。

Next evidence needed: measured attention runtime/retained tile counts and output sensitivity when changing keep on the same layer input. Existing residual/gate proxies do not establish causal sensitivity. No main-branch merge is implied by this experiment.
