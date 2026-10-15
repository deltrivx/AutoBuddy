> 三项修复：企微渠道级代理（对齐 MoviePilot 的 WECHAT_PROXY）、推送失败可读日志、助手人格化回复。

## 用户反馈

> 「你在饶什么都搭建了 VPS 中转，你跑去检测我本地 IP 干嘛，VPS 都是固定 IP 的，
> 都说了参考 moviepilot 的通知配置和智能体配置」
>
> （以及上一轮）「查看日志，有提示日报发送失败，当前我问过今天执行过任务吗？
> 也只是回复我正在查询，但是没有结果，我问你能做什么，回复的也比较机械」

## 问题 1：企微推送被可信 IP 拒绝（前两个反馈的共同根因）

### 实测

三条出口全部被 `errcode=60020 not allow to access from your ip` 拒绝：

| 通道 | 出口 IP | 结果 |
|---|---|---|
| 容器直连 | `112.194.50.154`（NAT 池，还见过 `.151`/`.1`） | ❌ 60020 |
| 内网代理 | `18.140.235.192` | ❌ 60020 |
| VPS 固定 IP | `107.172.90.165` | ❌ 60020 |

`gettoken` 能过，但 `message/send` 被拒 —— 即**所有主动推送**
（日报、任务通知、助手延后答复）全部失败。

### 我此前做错的方向

我去测本地 IP、还写了个「请去后台加 IP」的提示——但你早就说过
**VPS 是固定 IP**，正确的做法是给企微渠道配代理出口（就像早已为
Telegram 做的那样），而不是让你手工维护 IP 名单。

### 修复：补齐渠道级代理（对齐 MoviePilot 的 WECHAT_PROXY）

研究 MoviePilot `app/modules/wechat/wechat.py` 后发现关键设计：

```python
self._proxy = WECHAT_PROXY or "https://qyapi.weixin.qq.com"
if self._proxy:
    self._send_msg_url = UrlUtils.adapt_request_url(self._proxy, self._send_msg_url)
    self._token_url = UrlUtils.adapt_request_url(self._proxy, self._token_url)
    ...
```

**它的每个渠道都有独立代理配置**，企微的所有 API（gettoken / message/send /
menu/create / media）都走这个代理。而我们只给 Telegram 做了，企微漏了。

本次补齐，覆盖全部企微 API 调用点：

- `CHANNELS` 新增 `proxy` 字段（设置页可见，与 Telegram 同语义）
- `_send_wecom()` —— 通知发送
- `_wecom_token()` —— 取 token（菜单注册等共用）
- `create_wecom_menus()` —— 菜单注册

### ⚠️ 需要你确认一件事

代理是**通道**，IP 白名单是**准入**——两者是不同层面。我实测了三条出口，
全部不在企微可信 IP 名单内。所以：

- **如果** VPS 的 `107.172.90.165` 已在可信名单里 → 需要在 VPS 上提供一个
  可用的 HTTP/SOCKS 出口（我探测了常见代理端口 3128/8118/8080/8888/1080 及
  xray 的公网端口 8899/8282/8383/8484/27521/443，**都没有可用的 HTTP 代理**）；
- **或者** 把其中某个出口 IP 加进企微后台的可信 IP。

这一步需要你决定用哪个方案，我无法代你操作企微管理后台。

## 问题 2：日报失败日志只打 `None`

`send_scene()` 失败时顶层 `error` 常为 `None`（只有「总开关未开启」会填它），
真实错误在每个渠道的 `results.<channel>.error` 里。所以日志成了
「日报发送失败: None」——等于没写。

新增 `main._scene_err()`，从 `results` 提取可读原因：

```
[report] 日报发送失败: wecom: 发送失败：errcode=60020 not allow to access from your ip, from ip: 112.194.50.154
```

## 问题 3：助手回复机械

### 根因

`_looks_like_capability_question()` 命中后**恒返回写死的帮助清单**
（`_cmd_assistant_help()`）——所以「你能做什么」永远得到那段功能列表，
没有「我是…我能…」的智能体感。

### 修复

1. **能力问句改为模型优先**：让模型生成自我介绍，失败才回落本地清单；
   企微场景走延后推送（5 秒硬限制仍在，不阻塞）；
2. **系统提示词加入人格设定**：
   - 身份：「你是这个网关的智能管家——不是命令解释器，也不是查询表单」
   - 口语化：「可以叫用户『哥哥』或直说，别用『您』堆敬语」
   - **明确禁止罗列功能清单**，并给出期望的自我介绍示例
   - 边界：不执行登录/改密/删除数据等高危操作

## 测试

新增 `_test_notify_diag.py`（**41 项**）：

- 日报日志：`_scene_err` 存在、被调用、遍历 `results`、有兜底；
- 推送留痕：`_push_to_channel` 三种失败都打印、延后 worker 不再静默；
- 60020 提示：含实际 IP、含修复动作、非 60020 返回空、空/None 不炸；
- 人格化：能力问句模型优先、可回落、企微走延后；提示词含身份/口语化/
  禁清单/示例/边界；
- 行为验证：`_scene_err` 能提出 60020（不再是 None）、带渠道名、有兜底。

同步更新 `_test_notify.py`：企微字段集合加入 `proxy`。

全量 **46 个测试文件全绿**。

## 升级说明

重建容器即可。`/data` 数据不受影响。

**新增配置项**：企业微信渠道的「代理（仅本通道）」——留空为直连。
填了之后，企微的通知推送与菜单注册都会走该代理。
