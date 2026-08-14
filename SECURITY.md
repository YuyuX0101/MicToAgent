# 安全策略

## 如何报告漏洞

请用 GitHub 仓库的 **Security Advisories**（仓库页 → Security），或私下联系仓库 Owner。不要开公开 issue。

不要在 issue、PR 或讨论区粘贴：

- `logs/` 内容（含语音识别原文）
- `voice-bridge/config.json`、`.env`、token、热词里的个人映射

## 已知的信任边界

- 适配器监听 `127.0.0.1:9100`，**没有鉴权**。`listen_host` 不要改成 `0.0.0.0`。
- 适配器请求本机 Kimi Code Web 时 **不带 Bearer**。推荐配套启动：`kimi web --dangerous-bypass-auth`。关掉鉴权后，任何能访问该 Web 端口的人都可以控制会话、文件系统和 shell。只应在本机、可信环境使用。
- 识别在本地进行，本项目不会把录音发到第三方云；日志仍会留下转写文本，请自行保管 `logs/`。
