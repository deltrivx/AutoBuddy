# AutoBuddy v0.7.6

> 新增 401 鉴权失败审计落盘，为「密钥正确却偶发 401」提供可定案的原始证据。

## 1. 为什么要加这个审计

排查「密钥正确却偶发 401」时，容器日志只有一行 `401 Unauthorized`，且受
`--log-opt max-file=1` 限制会被轮转冲掉。每次排查都只能推测，无法定案。

## 2. 已排除的假设（三轮压测，全部零 401）

| 场景 | 做法 | 结果 |
| --- | --- | --- |
| 纯并发 | 同密钥 12 路并发打 `/v1/chat/completions` | 401 = 0 |
| 中断后继续 | 发起后立刻断 socket 模拟 abort，再继续请求 | 401 = 0 |
| 跨进程文件竞态 | 18091 高频调用 + 18090 高频读密钥文件，同时监控 `api_keys.json` 的 keys 数量与内容长度 | 全程稳定无突变，401 = 0 |

## 3. 决定性形态

容器日志显示**同一源端口**（HTTP keep-alive 复用连接）先 401 后 200：

```
192.168.31.5:41392 - "POST /v1/chat/completions" 401 Unauthorized
192.168.31.5:41392 - "POST /v1/chat/completions" 200 OK
```

AutoBuddy 的鉴权是**纯函数式**的：读内存 `_STATE` → `compare_digest` → 返回。
它不依赖连接、不依赖时间、不依赖并发。同一个端口上连续两个请求，一个失败一个成功，
**在 AutoBuddy 侧逻辑上不可能成立** —— 问题指向请求带上来的 Authorization 头本身。

## 4. 本次改动（只加取证，不改鉴权行为）

- 401 时把请求原貌追加写入挂载卷下的 `auth_fail_audit.jsonl`（JSONL 格式，独立于 docker 日志，不被轮转清理）；
- 记录以下字段用于定案：

  | 字段 | 用途 |
  | --- | --- |
  | `auth_present` | 头到底在不在 —— `false` 即铁证「真丢了」 |
  | `auth_len` | Authorization 头**确切长度**，与正常值比对可判定截断 |
  | `auth_prefix` | 前 12 字符明文，用于确认是「哪一把」密钥 |
  | `auth_hex` | 前 120 字节十六进制，看不可见字符 |
  | `xkey_present` / `xkey_len` | 是否改用 x-api-key 路径 |
  | `user_agent` / `header_names` | 看清客户端与请求头全貌 |
  | `mode` / `detail` | 落在 `missing`（缺少）还是 `invalid`（无效）分支 |

- **不写明文密钥**（审计文件可能被拷走），长度 + 前缀足以定案；
- 落盘失败一律吞掉，绝不影响主流程。

## 5. 怎么用

复现后直接看：

```bash
docker exec AutoBuddy cat /data/.autobuddy/auth_fail_audit.jsonl
```

判定口径：

- `auth_present: false` → 请求真的没带 Authorization 头；
- `auth_len: 0` 或明显偏小 → 头存在但为空/被截断；
- `auth_prefix` 与当前密钥前缀不一致 → 带的是另一把密钥；
- `auth_prefix` 一致且 `auth_len` 正常 → 头部完好，需往更上游查。
