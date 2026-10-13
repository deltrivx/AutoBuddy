> 企业微信双向交互：回调（接收消息服务器）+ 可信 IP 解锁路径 + 指令式交互。

## 新增：企业微信回调（完整交互功能）

用户需求：接通通知的同时，提供完整交互功能。

### 回调端点（挂网关公网入口）

- `GET /api/notify/wecom/callback` —— 企微后台「启用 API 接收」的验证
  （校验签名 → AES 解密 echostr → 明文回显）
- `POST /api/notify/wecom/callback` —— 收成员消息（加密 XML）→ 解析指令 →
  加密回复 XML（WXBizMsgCrypt 协议全实现：SHA1 签名 + AES-256-CBC +
  PKCS7 + receiveid 校验）
- 公网入口：`https://autobuddy.deltrivx.com`（Cloudflare tunnel + nginx →
  网关 18091；本次补了 nginx reload，域名已实测 200）

### 交互指令（首版）

成员在企业微信里对 AutoBuddy 应用发消息：
- `帮助` / `help` —— 指令清单
- `积分` / `余额` —— 账号池剩余积分汇总（读 credit 快照）
- `签到` / `任务` —— 异步启动一轮每日任务，完成回执
- `状态` —— 网关健康状态
- 其余文本 —— 引导语

### 配置（设置页 → 通知管理 → 企业微信）

新增三个字段：回调 Token、回调 EncodingAESKey（secret，均不回传明文）、
回调 URL（预填公网地址）。配齐后到企微后台「接收消息 → 设置API接收」填入
同值并启用——**启用 API 接收也是解锁「企业可信 IP」配置前置的方式之一**，
可信 IP 加入网关出网 IP 后，通知发送即全通。

### 其他

- Dockerfile 新增 `pycryptodome`（AES 加解密依赖）
- nginx `autobuddy.deltrivx.com` server 块此前已存在但缺 reload，公网 404
  已修复（`nginx -t` → reload → 实测 200）

## 测试

- `_test_notify.py` 扩至 25 项（字段注册断言含回调三件）
- 全量 28 个测试文件全绿

## 升级说明

重建容器（新镜像含 pycryptodome）。回调三要素在设置页填入后，需到企微后台
「应用管理 → AutoBuddy → 接收消息 → 设置API接收」填 URL/Token/EncodingAESKey
并保存（企微会立即回调验证 GET）。
