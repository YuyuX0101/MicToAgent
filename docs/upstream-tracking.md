# 跟随上游（huggingface/speech-to-speech）更新指南

本项目是 speech-to-speech 的**真正 fork**：基线为上游 **v0.2.11**（2026 年发布版），
fork 改动直接写在 `src/` 里（带 `fork(speech-to-kimi-code)` 注释标记），
venv 以可编辑方式安装本仓库。

上游仍在活跃开发（2026 年中约 80 commits/月），但方向是 HuggingFace 自家
Reachy Mini 机器人语音栈，与本项目的垂直方向（Kimi Code 语音连接）不同。
**策略：不紧密跟随，定期评估，选择性合并。**

## 值得关注的上游成果（按价值排序）

| 上游动态 | 对本项目的价值 | 状态（2026-08 时点） |
|---|---|---|
| Qwen3-TTS GGML 后端（llama.cpp） | **高**：无独显用户也能跑 TTS，大幅降低开源用户门槛 | 开发中（main 分支活跃），等成熟发版后再引入 |
| Qwen3-ASR 新 STT 后端（issue #337） | 中：可能替代 paraformer，识别质量/速度待观察 | 进行中 |
| NLTK 3.10 升级 | 低：main 已升级，但上游 nltk 路径 bug 未修（我们的修复仍需要） | 已进 main |
| WebSocket API / 机器人 UI 相关 | 无：本项目用 local 模式 + 自有桥接 | — |

## 多久看一次

建议**每 1-2 个月**评估一次，或当上游发布新版本（PyPI `speech-to-speech` 版本号
超过 0.2.11）时。快速检查：

```bash
# 看上游最近动态（api.github.com 本机可直连）
curl -s https://api.github.com/repos/huggingface/speech-to-speech | grep pushed_at
# 看 PyPI 是否有新版（或运行 scripts/check-updates.py）
```

## 合并上游更新的流程

```bash
# 1. 拉取上游（GitHub 主站在国内需挂代理）
git fetch upstream

# 2. 评估：看 v0.2.11..upstream/main 的提交，挑与本项目相关的
git log --oneline v0.2.11..upstream/main -- src/speech_to_speech/TTS src/speech_to_speech/STT

# 3. 选择性合并（整体 merge 或 cherry-pick 单个提交）
git cherry-pick <commit>        # 或 git merge upstream/main

# 4. 冲突重点：带 fork(speech-to-kimi-code) 注释的 4 处改动
#    - src/speech_to_speech/STT/paraformer_handler.py（2 处）
#    - src/speech_to_speech/s2s_pipeline.py（1 处）
#    - src/speech_to_speech/VAD/vad_handler.py（1 处）
#    合并后确认这些标记还在、逻辑仍成立（上游可能已自行修复，那时删我们的改动即可）

# 5. 依赖变动：pyproject.toml 若改了依赖版本，重装依赖并重跑
#    scripts/apply-upstream-patches.py（transformers 补丁，锚点可能失效需人工核对）

# 6. 回归测试：完整启动语音助手 + 说一句话走通 STT→LLM→TTS 全链路
```

## 判断"要不要跟"的原则

- **跟**：我们用到的组件（VAD / paraformer / Qwen3-TTS / local 模式）的 bug 修复、
  GGML 后端发版、安全修复
- **不跟**：WebSocket API 变动、机器人 UI、新 LLM 后端（我们用 chat-completions 直通
  本地适配器，不受影响）
- **犹豫就不跟**：fork 的意义就是按自己的节奏走，错过一个版本不会有任何损失
