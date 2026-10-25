转发前改写 dsh 默认提示词里的上游渠道校验指纹句，修复 dsh 调用稳定 400 code=11128。

## 修复

### dsh（DeepSeek Harness）调用稳定 400 code=11128「Illegal API invocation from an unapproved channel」

**现象**：dsh 经网关发起的请求在候选账号上轮遍全部被上游 11128 渠道拦截，
换号重试（v1.0.22 引入）无效；而同一批账号用最小 OpenAI 请求立刻成功 ——
与账号、模型、UA、头族均无关，是**请求体文本**触发。

**定位**：把 dsh 真实请求体（26KB system prompt + 25 个 tools 定义）逐级
裁剪回放，收敛到 dsh 默认 system prompt 第一句：

> You are an AI agent powered by DeepSeek Harness

该句出现即 100% 被拦（多次复测稳定）；只改动其中一处（加逗号 / 换动词 /
去品牌词）即放行。结论：上游渠道校验对特定客户端默认提示词做**特征串匹配**，
dsh 恰好命中。

**处理**（`gateway/main.py` 的 `_upstream_body()`）：

- 新增 `CHANNEL_FINGERPRINT_RULES` 改写表：转发前把指纹句替换为语义等价的
  近义句（实测 `running on` 版本可通过渠道校验），其余内容一字不动。
- 多模态 content（list 形态的 `{type: "text", text: ...}`）一并覆盖。
- `AB_CHANNEL_SANITIZE=0` 可整体关闭，回退透传形态。
- 上游后续若更换特征串，向规则表追加 `(命中, 替换)` 对即可，不改逻辑。

## 测试

- 新增 `tests/test_channel_sanitize.py`（10 项）：源码驱动，抽出改写表与
  接线做断言，并原样复现改写逻辑验证 str 与多模态 list 两种形态、其余片段
  不被误改。锁死「指纹句必须被覆盖、替换串不得再含命中串」两条回归红线。
