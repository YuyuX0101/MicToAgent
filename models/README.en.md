# models/ — what to download

[中文](README.md) | [English](README.en.md)

Models are not in git. Download them into repo-root `models/` (or set `MICTOAGENT_MODELS` in `.env`). You need all three: VAD + SenseVoice draft + qwen3-asr refine. Directory names must match exactly.

From the repo root (no venv required):

```powershell
py -3 scripts\download_models.py
```

Existing files are skipped. If a GitHub Release download fails, use the links below and keep the extracted folder names unchanged.

## ten-vad.onnx

- Source: the **sherpa** build of TEN-VAD from sherpa-onnx (adds `mean` / `inv_stddev` / `window` metadata; the upstream TEN-framework file will not work):
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/ten-vad.onnx
  (fp32, ~324KB; do not use the int8 build)
- Upstream: https://github.com/TEN-framework/ten-vad
- License: **Apache 2.0 with extra terms**. Personal / non-commercial use is fine; check the upstream LICENSE before commercial use:
  https://github.com/TEN-framework/ten-vad/blob/main/LICENSE
- Used by `voice-bridge/voice-core.py`. Feature pipeline follows sherpa-onnx `csrc/ten-vad-model.cc` (pitch features zeroed).

## sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/

qwen3-asr 0.6B int8 (sherpa-onnx). Refine engine in `dual` mode; sole engine when `stt_engine="qwen3_asr"`. Supports hotword decode bias.
https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2  
Extract so the folder name stays `sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/` under `models/`.

## sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/

SenseVoice-Small (sherpa-onnx, zh/en/ja/ko/yue). Draft engine in `dual` mode (fast, RTF≈0.03). Production files: `model.int8.onnx` (~234MB) + `tokens.txt`. `model.onnx` (fp32) and `test_wavs/` are unused and may be deleted.
https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2  
Extract so the folder name stays `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/` under `models/`.
