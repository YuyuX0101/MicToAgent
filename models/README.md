# models/ 模型文件说明

[中文](README.md) | [English](README.en.md)

模型不入库，需自行下载到仓库根 `models/`（可用 `.env` 的 `MICTOAGENT_MODELS` 改到别处）。
三个都要：VAD + SenseVoice 初稿 + qwen3-asr 精修。目录名必须和下面一致。

仓库根执行（不依赖 venv）：

```powershell
py -3 scripts\download_models.py
```

已存在的会跳过。GitHub Release 在国内若失败，用下面的链接手动下载，解压后目录名保持不变。

## ten-vad.onnx

- 来源：sherpa-onnx 项目发布的 **sherpa 版** TEN-VAD（在 TEN-framework 官方原版
  基础上添加了 `mean`/`inv_stddev`/`window` 等 metadata，官方原版不可用）：
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/ten-vad.onnx
  （fp32 版，约 324KB；不用 int8 版）
- 上游：https://github.com/TEN-framework/ten-vad
- 许可：**修改版 Apache 2.0**（TEN-VAD 在 Apache 2.0 基础上附加了限制条款，
  个人/非商用使用没问题；商用前请核对上游 LICENSE：
  https://github.com/TEN-framework/ten-vad/blob/main/LICENSE）
- 用途：voice-bridge/voice-core.py 的 VAD 模型，
  特征管线复刻 sherpa-onnx `csrc/ten-vad-model.cc`（pitch 特征置 0 的简化版）。

## sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/

qwen3-asr 0.6B int8 离线识别模型（sherpa-onnx 版），dual 接力模式的精修引擎
（`stt_engine="qwen3_asr"` 时为单引擎）。模型本身支持热词解码偏置，本项目
未启用（偏置词占 max_total_len=512 的 prompt 额度，词表增长曾致生成静默为空）。
来源：https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2
解压后目录名保持 `sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/`，放到 `models/` 下。

## sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/

SenseVoice-Small 离线识别模型（sherpa-onnx 版，中/英/日/韩/粤），
dual 接力模式的初稿引擎（快，RTF≈0.03）。生产用 `model.int8.onnx`
（约 234MB）+ `tokens.txt`；`model.onnx`（fp32 全精度）与 `test_wavs/`
已删除（用不到）。
来源：https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2
解压后目录名保持 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/`，放到 `models/` 下。
