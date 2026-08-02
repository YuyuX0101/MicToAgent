# 上游修改记录

> **2026-08 起项目转为真正的 fork**：venv 以可编辑方式（`pip install -e .`）安装
> 本仓库 `src/`，基线为上游 v0.2.11。原"site-packages 补丁"中的 4 个
> speech_to_speech 包内补丁已成为 `src/` 里的正式源码改动（带
> `fork(speech-to-kimi-code)` 注释标记），**随仓库分发，不再丢失、无需重打**。
>
> 本文档保留两部分：A. fork 改动的存档记录（合并上游时对照用，流程见
> docs/upstream-tracking.md）；B. 仅剩的 1 个第三方库补丁（transformers，
> 仍打在 site-packages 上，重装依赖后需用脚本重打）。

---

## A. fork 改动存档（已在 src/ 中，无需任何操作）

### A1. paraformer MPS 调用加平台判断

- **文件**：`src/speech_to_speech/STT/paraformer_handler.py`（`process()`）
- **问题**：上游无条件调用 `torch.mps.empty_cache()`；`torch.mps` 只在 Apple
  Silicon 上存在，Windows/Linux 上调用抛 `AttributeError`，识别推理直接失败。
- **改动**：仅当 `torch.backends.mps.is_available()` 时才调用。

### A2. nltk 数据路径 tokenizers → taggers

- **文件**：`src/speech_to_speech/s2s_pipeline.py`（import 时执行）
- **问题**：上游用 `nltk.data.find("tokenizers/averaged_perceptron_tagger_eng")`
  检查数据，目录名写错（应为 `taggers/`），导致每次启动误判缺失并联网重下。
- **改动**：路径修正为 `taggers/averaged_perceptron_tagger_eng`。

### A3. silero VAD GitHub 直连失败回退本地缓存

- **文件**：`src/speech_to_speech/VAD/vad_handler.py`
- **问题**：`torch.hub.load("snakers4/silero-vad", ...)` 每次加载前无超时探测
  GitHub 默认分支，GitHub 不可达时挂死或抛异常，VAD 初始化失败、管线无法启动。
- **改动**：`try/except` 包裹，失败时以 `source="local"` 从 torch hub 本地缓存
  （`torch.hub.get_dir()/snakers4_silero-vad_master`）加载。

### A4. paraformer 跳过 funasr 启动联网更新检查

- **文件**：`src/speech_to_speech/STT/paraformer_handler.py`（`setup()`）
- **问题**：funasr `AutoModel` 默认请求 `pypi.org` 检查版本（境外站、无超时），
  网络差时卡死启动；该检查纯提醒、无功能作用。
- **改动**：`AutoModel(..., disable_update=True)`。按需查版本用
  `scripts/check-updates.py`（走国内镜像）。

> 上游 main 分支（2026-08 时点）以上 4 处问题**均未修复**，合并上游后仍需保留
> 我们的改动；若上游哪天修了，删除对应 fork 标记段即可。

---

## B. 第三方库补丁（仍在 site-packages，重装后需重打）

> **自动补丁脚本**：
> `.venv\Scripts\python.exe scripts\apply-upstream-patches.py`
> （`--check` 只检查不修改）。幂等，重复运行无副作用；锚点找不到说明上游版本
> 已变动，按本节手动核对。

### 补丁 B1：transformers `is_base_mistral` 加 try/except

- **文件路径**（相对 site-packages）：`transformers/tokenization_utils_base.py`
- **所属包 + 版本**：`transformers` 4.57.3
- **问题现象**：在 `chat_template` 相关逻辑中，函数 `is_base_mistral(model_id)`
  直接调用 `model_info(model_id)` 向 Hugging Face Hub 发起请求。离线、网络失败、
  或模型不可达时该调用抛异常，导致 tokenizer 构建 / 模板匹配流程报错中断。
- **修改原因**：给 `model_info(model_id)` 调用套上 `try/except`，请求失败时返回
  `False`（视为非 base mistral），保证在离线/网络异常环境下流程不中断。

```diff
@@ -2429,7 +2429,10 @@
         from huggingface_hub import model_info

         def is_base_mistral(model_id: str) -> bool:
-            model = model_info(model_id)
+            try:
+                model = model_info(model_id)
+            except Exception:
+                return False
             if model.tags is not None:
                 if re.search("base_model:.*mistralai", "".join(model.tags)):
                     return True
```

**重装后重打**：直接运行 `scripts/apply-upstream-patches.py` 即可（幂等）。

---

## 运行时补丁：音频入口门控（不改 site-packages，也不在 src/）

门控补丁在项目自己的启动入口 `speech-to-kimi-entry.py` 里对
`speech_to_speech.connections.local_audio_streamer.LocalAudioStreamer.run`
做**运行时猴子补丁**（fork 分叉版 run()，与上游唯一差异：麦克风帧进
`input_queue` 前检查 `voice-bridge/gate.state`，关门时改喂静音帧）。

- 状态文件由 `voice-bridge/mic-gate.pyw` 原子写入（"1"=开/"0"=关），音频回调按
  mtime 缓存读取；文件不存在一律视为关门。
- 放在入口文件而非 src/ 的原因：门控是"语音助手产品"的行为，不是管线库的行为，
  保持 src/ 与上游的 diff 最小，合并上游更轻松。升级 `speech_to_speech` 后应对照
  上游 `LocalAudioStreamer.run` 源码同步分叉版。
- 旧方案（适配器 `/gate` 丢弃文字 + 2 秒送达宽限）已废弃：适配器门控默认常开
  （`config.json` 的 `gate_default_open: true`），`/gate` 接口仅留作调试。

---

## 环境变更速查

| 操作 | 影响 | 恢复 |
|---|---|---|
| 重装/升级 transformers | 补丁 B1 丢失 | 重跑 `scripts/apply-upstream-patches.py` |
| 重装 speech-to-speech 依赖的其他包 | 无影响 | — |
| 删掉 venv 重建 | 全部丢失 | `pip install -e .` + 依赖 + 补丁脚本 |
| 合并上游新版 | fork 改动可能冲突 | 按 docs/upstream-tracking.md 流程 |
