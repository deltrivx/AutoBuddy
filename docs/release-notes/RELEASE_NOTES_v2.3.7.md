## 智能路由页「上游智能体」图标化

本版把智能路由页左侧「来源」区从纯文字改造为**官方 logo 图标 + 名字**的并列展示。

### 改了什么

- **上游智能体图标化**：OpenClaw / DeltrivX-App / wechat-bot / curl 等每个上游调用方，现在以「图标 + 名字」横向排列，去掉了原先多余的「上游智能体」小标签。
- **内置高清图标**：新增 `gateway/icons/`（21 个官方/通用 logo）。图标取自 [xushier/HD-Icons](https://github.com/xushier/HD-Icons) 高清仓库——OpenClaw、微信、Telegram、ChatGPT、Claude、Gemini、DeepSeek、通义、豆包、Kimi、Grok、飞书、Discord、Slack 等均有收录；未收录的非主流智能体（如 iOS-App、未知客户端）统一回退到通用 App 图标，不再显示裂图。
- **后端图标端点**：`web_proxy.py` 新增 `GET /icons/{name}`，安全返回 `gateway/icons/` 内的图标（拒绝 `../` 路径穿越与隐藏文件），缺失时回退 `generic-app.svg`。DeltrivX-App 复用产品 `icon.png` 作为品牌标识。

### 影响面

- 仅影响智能路由页来源区视觉呈现，不改变路由逻辑与账号匹配行为。
- 图标随镜像构建打包进 `/app/gateway/icons/`，运行时不依赖外网拉取。
