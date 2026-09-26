# AutoBuddy 网关模型列表（本地快照）

- 来源：`http://192.168.31.2:18091/v1/models`
- 快照时间：2026-09-26 16:04
- 网关模型总数：36（可用 26 / 不可用 10）

## 可用模型（26）

| 模型 | 思考档位 | 说明 |
|---|---|---|
| `balanced-model` | high |  |
| `deep-model` | low |  |
| `deepseek-chat` | max | 别名 -> deepseek-v3 |
| `deepseek-v3` | high |  |
| `deepseek-v3-0324` | medium |  |
| `deepseek-v4-flash` | max |  |
| `deepseek-v4-pro` | medium | 第一备用模型 |
| `deepseek-v4.1-flash` | high | 综合最快，推荐日常使用 |
| `default-model` | max（仅 max 稳定） |  |
| `fast-model` | max |  |
| `gemini-3.5-flash` | max |  |
| `glm-5.1` | high |  |
| `glm-5.2` | minimal | 仅输出思考、正文常空 |
| `glm-5.3` | medium | 低档反而变慢，medium 最快 1.42s |
| `glm-5.3-flash` | minimal | 深度思考模型，minimal 后 2.1s（原 39.5s） |
| `gpt-5.6-luna` | high |  |
| `gpt-5.6-terra` | max（仅 max 可用） |  |
| `hy3` | medium |  |
| `hy4` | max | 别名 -> hy3 |
| `hy4-preview-f` | minimal |  |
| `kimi` | low |  |
| `kimi-k2.5` | medium |  |
| `kimi-k2.6` | low |  |
| `kimi-k2.7` | high |  |
| `kimi-k3` | minimal |  |
| `minimax-m3` | high |  |

## 不可用模型（10，配置保留）

| 模型 | 原因 |
|---|---|
| `claude-opus-4.6` | 账号无权 (11102) |
| `claude-sonnet-4.6` | 账号无权 (11102) |
| `codewise-model-a9` | 服务不存在 |
| `gemini-3.1-pro` | 账号无权 (11102) |
| `gpt-4o` | 账号无权（实为 gpt-5.4） |
| `gpt-5.3-codex` | 账号无权 (11102) |
| `gpt-5.4` | 账号无权 (11102) |
| `gpt-5.5` | 账号无权 (11102) |
| `gpt-5.6-sol` | 账号无权 (11102) |
| `primary-model` | 服务不存在 |

## 思考档位策略

- 全局兜底 `thinkingDefault`: **medium**（原 max）
- 每个模型按实测「最快且正常出正文」的档位单独设置
- 可用档位：`minimal | low | medium | high | max`
