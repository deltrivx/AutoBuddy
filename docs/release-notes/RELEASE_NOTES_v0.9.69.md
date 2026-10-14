> 设置页新增「智能助手」配置区：provider 注册表驱动，默认自带模型，也可配任意 OpenAI 兼容接口。

## 需求

用户要求「在设置页面添加智能助手模型配置，而不是你自己写，参考 MoviePilot 的相关配置，
默认自带模型提供，也可以配置其他模型，仔细思考如何实现」。

## 参考 MoviePilot 的 LLMProviderInfo

我把 MoviePilot `app/schemas/llm.py` 的 provider 模型搬了过来，并**按我们的场景裁剪**：

| MoviePilot | 本实现 |
|---|---|
| `base_url_presets`（预设地址） | ✅ 每个 provider 带预设标签，点一下填充 |
| `base_url_editable` | ✅ 允许直接改地址 |
| `requires_base_url` | ✅ 必填性控制占位文案 |
| `supports_api_key` + `api_key_label/hint` | ✅ 密钥字段，secret 语义与通知渠道一致 |
| `supports_model_refresh` | ✅ 「刷新列表」拉 `/v1/models`，datalist 候选 |
| `description` | ✅ 设置页说明 + provider 标签 tooltip |
| `LLMTestResult`（连通性测试） | ✅ 「测试连通性」真实问一句，返回耗时 + 回答预览 |

**没有照搬的两处**（场景不同）：
- OAuth 授权流（`PendingAuthSession` 等）——我们是自托管网关，密钥模式足够；
- 服务端工具能力探测（`ServerToolRegistry`）——那是给 OpenAI/Anthropic 官方端的能力，
  我们的工具调用由 assistant 自己实现。

## 默认自带 + 可扩展

注册表内置两个 provider：

1. **AutoBuddy 网关（自带）**——默认选项，复用本网关账号池与模型目录，**无需额外密钥**，开箱即用；
2. **OpenAI 兼容接口**——预设 OpenAI / DeepSeek / Moonshot / 硅基流动，也可手填任意兼容地址。

> 注册表驱动：以后加 provider 只改后端 `PROVIDERS` 一处，前端自动渲染。

## 实现要点

**配置层**（`assistant.py`）：
- 独立配置文件 `assistant_config.json`（与通知配置解耦，生命周期不同）；
- secret 语义：**不提交=保留、空串=清除、有值=覆盖**（与通知渠道一致）；
- `public_view()` 密钥只回 `{has_value}`，绝不回明文；
- `_runtime()` 解析出当前生效的 provider/base_url/api_key/model/systemPrompt；
- 系统提示词也可在页面自定义（留空用内置）。

**接口层**（`web_proxy.py`）：
- `GET/PUT /api/assistant`、`POST /api/assistant/test`、`POST /api/assistant/models`；
- 测试与保存分离：**测试用当前填写值**（不必先保存就能验证），保存也可顺带自检。

**UI 层**：设置页新增「智能助手」section（位于 API 接入、模型巡检之后，通知管理之前），
内联表单：总开关 → provider 标签选择 → 地址/密钥/模型/提示词 → 测试连通性 + 保存。

## 过程中修掉的一个真 bug

写配置层时用了 `threading.Lock`，而 `_save_config` 持有锁期间会调用 `_load_config()`
（读-改-写需基于最新值）——**普通 Lock 不可重入，直接自死锁**。

本地测试跑到 `_save_config` 整条命令挂住 3 分钟无输出，才暴露出来。
已改为 `threading.RLock()` 并加注释说明。这类 bug 若漏到生产，会让所有保存请求永久挂起。

## 测试

`_test_assistant.py` 新增 [6][7][8] 三组共 58 项（总数 78 → **136**）：

- **[6] 注册表契约**：key 唯一、必填字段齐全、preset 结构、secret provider 必须有 label/hint；
- **[7] 配置读写**（真实文件）：默认值、provider/base_url/api_key 贯通、
  **secret 三态语义**、`public_view` 永不回明文、enabled 开关贯通、systemPrompt 覆盖、
  落盘重读、尾斜杠归一化、显式模型优先；
- **[8] 接口契约**：4 个端点存在、GET 脱敏、保存合并式、测试返回
  `durationMs`/`replyPreview`、模型清单带 key、section 注入、刷新接线、DOM 顺序。

全量 **39 个测试文件全绿**；Python 语法 + 内嵌 JS `node --check` 均通过。

## 升级说明

重建容器即可。`/data` 数据不受影响；不配置时默认走「网关（自带）」，行为与之前一致。
