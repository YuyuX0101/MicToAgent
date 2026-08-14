## 改了什么

<!-- 一两句话，为什么改。 -->

## 检查

- [ ] 本机 `.\.venv\Scripts\python.exe -m pytest tests/ -q` 通过
- [ ] **没有**给语音提交加超时重发（重发 = 目标会话收到重复消息）
- [ ] 没把 `logs/`、`voice-bridge/config.json`、`.env`、识别原文塞进提交
- [ ] 改语音链路或适配器前读过 `voice-core.py` 文件头 / `voice-bridge/README.md`
