补全官方桌面端请求特征 —— `X-Domain` 改纯域名、`Content-Type` 补 charset、新增 `X-Client-*` 三件套。

## 一、背景

v1.0.22 首次引入桌面端请求特征模拟，但存在三处与官方不一致。本版逐一修正，全部依据仓库内一手样本（`vendor/workbuddy_daily.py`）与官方引擎二进制实证，无臆测。

## 二、三处修正

### 1. `X-Domain`：完整 URL → 纯域名

| | 值 |
| :--- | :--- |
| v1.0.22（误） | `https://copilot.tencent.com` |
| 官方（对） | `copilot.tencent.com` |
| 依据 | `vendor/workbuddy_daily.py:1212` — `"X-Domain": "copilot.tencent.com"` |

`X-Domain` 是**渠道标识**（无 scheme、无路径），与 `Origin` / `Referer` 分工不同——后两者仍用带 scheme 的完整源。网关转发的是 `/v2/chat/completions`，属 chat 域，故按纯域名口径发。

### 2. `Content-Type`：补 `charset=UTF-8`

| | 值 |
| :--- | :--- |
| v1.0.22 | `application/json` |
| 官方 | `application/json;charset=UTF-8` |
| 依据 | `vendor/workbuddy_daily.py:3142` |

### 3. 新增 `X-Client-*` 三件套

| 头 | 值 | 依据 |
| :--- | :--- | :--- |
| `X-Client-Platform` | `desktop` | 引擎二进制 strings 命中该头名；取值按 vendor 小程序范式推导 |
| `X-Client-Product` | `workbuddy-desktop` | 桌面指纹 envelope 的 `extName` 正是此值（`vendor/workbuddy_daily.py::desktop_fingerprint`） |
| `X-Client-Version` | 账号画像的 `ideVersion` | 与 UA 同源 |

vendor 小程序口径的同族头范式（`workbuddy_daily.py:2505-2513`）：

```
X-Client-Platform: miniprogram / mp-weixin
X-Client-Product:  workbuddy-mp
X-Client-Version:  2.4.0
```

⚠️ **诚实声明**：平台名 `desktop` 属**范式推导**——官方二进制只证明了 `X-Client-Platform` 这个头存在，未露出其取值。已开放环境变量 `AB_DESKTOP_CLIENT_PLATFORM` 以便按实况覆盖。

## 三、新增 `_domain_host()` 辅助函数

从 `CN_BASE_URL` 抠纯域名，自动剥 scheme / 路径 / 查询串 / 端口。环境变量改域时自动跟随，不写死。

单元测试 9/9 通过：

| 输入 | 输出 |
| :--- | :--- |
| `https://copilot.tencent.com/v2` | `copilot.tencent.com` |
| `https://copilot.tencent.com` | `copilot.tencent.com` |
| `copilot.tencent.com` | `copilot.tencent.com` |
| `https://www.codebuddy.ai/v2` | `www.codebuddy.ai` |
| `http://127.0.0.1:18091/v1` | `127.0.0.1` |
| `https://host.example.com:8443/v2` | `host.example.com` |
| `https://h/v2?x=1` | `h` |
| `""` / `None` | `""` |

含 IPv6 误伤防护：仅在端口段确为数字时才剥离。

## 四、最终发出的桌面端头族

```
Authorization: Bearer <token>
Content-Type: application/json;charset=UTF-8
Accept: application/json, text/plain, */*
User-Agent: WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1
X-Domain: copilot.tencent.com
X-Product: SaaS
X-Request-ID: <sessionId><毫秒尾段>
X-User-Id: <账号 uid>
X-Client-Platform: desktop
X-Client-Product: workbuddy-desktop
X-Client-Version: 5.5.6
Origin: https://copilot.tencent.com
Referer: https://copilot.tencent.com/
```

请求体补 `conversationId`（按 machineId + 会话内容稳定派生，同账号同会话稳定、跨账号不同）。

三条设计红线不变：画像按账号隔离（不共用 machineId/osVersion）；`AB_DESKTOP_FIDELITY=0` 可一键回退四头形态；`X-User-Id` / `machineId` 全部取自该账号自己的真实画像，不借用不编造。

## 五、测试

- 单元测试：`_domain_host` 9/9 通过。
- 仓库测试门禁：60 个 `_test_*.py`，**59 通过 / 1 失败**。
- 唯一失败 `_test_command_routing.py` 已用**父提交 v1.0.24 原版 `notify.py` 做基线对照**：同样失败，证实为预先存在的红，与本版改动无关（本版未触碰 `notify.py`）。该测试只在本机 LLM 环境下复现（助手接走未知命令），CI 无此环境。

## 六、升级说明

常规升级，无破坏性变更、无配置迁移、无数据格式变化。新增可用环境变量（均有默认值）：

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `AB_DESKTOP_CLIENT_PLATFORM` | `desktop` | `X-Client-Platform` 取值，可按实况覆盖 |
| `AB_DESKTOP_FIDELITY` | `1` | 置 `0` 一键回退四头形态 |

Unraid 用户更新镜像并重建容器即可，模板无需改动。
