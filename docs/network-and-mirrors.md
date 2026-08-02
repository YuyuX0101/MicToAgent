# 运行时联网点与国内外可达性说明

本文盘点 speech-to-kimi-code 运行时的全部联网点、本 fork 的处置方式，
以及国内网络环境下的加速途径。结论先行：**正常运行（启动+使用）不依赖任何外网**；
需要联网的只有首次下载依赖/模型那一次。

## 运行时联网点清单

| 联网点 | 原来的作用 | 本 fork 的处置 |
|---|---|---|
| funasr → `pypi.org` 版本检查 | 启动时检查 funasr 是否有新版（纯提醒，无功能作用；境外站、无超时，网慢时卡死启动） | fork 源码改动：`disable_update=True`（src/ STT/paraformer_handler.py）；想查更新用 `scripts/check-updates.py`（走阿里云/清华镜像） |
| modelscope 模型缓存校验 | paraformer 模型文件核对（"Downloading 13 files"日志） | 保留——modelscope 是国内站，正常 1 秒内完成 |
| HuggingFace Hub（Qwen3-TTS 模型加载） | 加载 TTS 模型时检查模型文件 | `HF_HUB_OFFLINE=1` 全跳过（启动器注入），直接用本地缓存 |
| transformers `is_base_mistral` → `huggingface.co` | tokenizer 构建时查模型元信息 | 补丁脚本容错：请求失败按 False 处理，不中断（docs/upstream-patches.md B 节） |
| silero VAD → GitHub（`torch.hub.load`） | VAD 模型下载/校验 | fork 源码改动：GitHub 不可达时回退本地 torch hub 缓存（src/ VAD/vad_handler.py） |
| nltk 数据 → GitHub raw | 分句用标注器数据 | fork 源码改动：修正数据路径检测，已存在则不重复下载（src/ s2s_pipeline.py） |
| LLM 请求 → `api.openai.com` | 上游默认的 LLM 端点 | 本 fork 指向本地适配器 `http://127.0.0.1:9100/v1`，不出本机 |

## 为什么启动时坚持全离线，而不是换国内镜像

- 镜像再快也是网络 I/O，且多一个故障点（镜像站抽风 = 启动卡住，funasr 的教训）；
- 这些"检查"的内容都是"有没有新版本/新文件"，而依赖与模型版本是锁定的，检查结果没有用途；
- 国内 HF 镜像（hf-mirror.com）并非官方服务，可达性无保障；Qwen 模型在 modelscope
  有国内源，但 TTS 加载库（faster_qwen3_tts）写死走 huggingface_hub，换源需改第三方库，
  维护成本大于收益。

## 首次下载的国内加速途径（一次性）

| 内容 | 推荐途径 |
|---|---|
| Python 依赖（PyPI） | `uv pip install -i https://mirrors.aliyun.com/pypi/simple/ ...`（或清华镜像） |
| paraformer 模型 | funasr 自动走 modelscope（国内），无需处理 |
| Qwen3-TTS 模型（HuggingFace） | 挂代理直连 huggingface.co；或先从 modelscope 下载后放到 HF 缓存目录 |
| silero VAD、nltk 数据（GitHub） | 挂代理直连；成功一次后 fork 改动保证以后不再联网 |

## 依赖版本更新检查

```bash
.venv\Scripts\python.exe scripts\check-updates.py
```

查询阿里云/清华 PyPI 镜像，列出核心依赖的当前版本与最新版本。
升级 transformers 后记得重跑 `scripts/apply-upstream-patches.py`
（见 docs/upstream-patches.md B 节）；升级其他依赖无需任何补丁操作。
