> 企业微信完整交互：内置回调端点（echo 校验 + 消息接收 + 指令自动回复），零辅助容器。

## 背景

用户需求：接通企业微信通知的同时，给 AutoBuddy 提供完整交互功能；且**必须内置**，
其他用户自行搭建好公网（FRP/任意反代）和企微后台配置即可用，不部署辅助容器。

## 实现

### ① 企微回调协议（gateway/notify.py）

完整实现企微「接收消息服务器」协议（对齐 MoviePilot 的交互思路）：

- AES-256-CBC 加解密（key = Base64(EncodingAESKey + "=")，32 字节）；
  容器内无 pycryptodome/cryptography，用 **openssl CLI** 实现（已实测往返一致），
  回调是低频路径（成员发消息才触发），subprocess 开销可接受；
- 明文结构 = 16 随机字节 + 4 字节网络序消息长度 + XML + corpid，PKCS7(32) 填充；
- 签名 = SHA1(sorted(token, timestamp, nonce, 密文))；
- echo 校验（企微保存配置时的 GET）+ 消息解密 + 加密回复。

### ② 回调端点（web_proxy.py）

`GET/POST /api/notify/wecom/callback/<path>`——**不设登录鉴权**：调用方是企微
服务器，鉴权靠消息体签名（密钥只有双方知道），比 cookie 更强。路径带随机段，
经 FRP（VPS → 本机 18090）公网可达即可。

### ③ 内置交互指令（零辅助容器）

被动回复全部在 5 秒内完成（不触发企微重推），长时间任务只回"已启动"：

| 指令 | 功能 |
|---|---|
| `help` / `帮助` / `?` | 指令菜单 |
| `status` | 网关与账号池状态 |
| `credits` | 积分与今日/近7天/本月消耗 |
| `accounts` | 账号余额清单（前 15 个） |
| `models` | 模型目录规模 |
| `daily` | 最近一轮每日任务状态 |
| `run` | 启动一轮每日任务 |
| `checkin` | 全部账号签到 |
| `notify on/off` | 开关通知推送 |

### ④ 渠道注册表扩展

企业微信渠道新增 `cb_token`（回调 Token）与 `cb_aes_key`（EncodingAESKey）
两个字段，设置页表单自动渲染（注册表驱动，前端零改动）。

## 测试

新增 `_test_wecom_callback_crypto.py`（15 项）：AES key 解码、加解密往返、
签名（含顺序无关性）、echo 校验（正确通过/错误拒绝）、完整回调链
（解密消息 → 构造回复 → 回复包再解回）、长文本多块往返、PKCS7 边界。
全量 29 个测试文件全绿；Python 语法 + 内嵌 JS `node --check` 通过。

## 升级说明

重建容器即可。启用交互三步：
1. 设置页「通知管理 → 企业微信」填入 CorPID/Secret/AgentId + 回调 Token/AESKey（企微后台「接收消息」页生成）；
2. 把 `http://<公网地址>/api/notify/wecom/callback/<随机段>` 填到企微后台接收消息服务器 URL（公网可用 FRP，如 VPS IP:端口 → 本机 18090）；
3. 保存配置（企微会发 GET 校验，通过即接入成功）。之后在企业微信里给应用发消息即可交互。
