`officialUsage.status` 改为按账本接管情况判定，消除积分统计页「部分账号官方用量未同步」误报。

## 一、前两轮为什么没修好

| 版本 | 改了什么 | 结果 |
| :--- | :--- | :--- |
| v1.0.28 | 把 `ok`/`detailTruncated`/`error`/`errors` 从硬编码改为"如实反映" | 判定式写成 `_acc_has_detail = bool(_new_daily) and bool(_acc_models)`，只签到账号被误判 |
| v1.0.32 | 拆开上述 `and`，`ok` 只看官方 daily | 数据层修好了（实测 `errors=[]`、`ok` 全 true、`detailTruncated` 全 false），**但页面仍显示警告** |

**v1.0.32 之后的容器实测（塔灵）**：

| 层 | 数据 |
| :--- | :--- |
| API | `errors=[]`、4 账号 `ok` 全 `true`、`detailTruncated` 全 `false`、`requests` 600 条账本实时数据 |
| 页面 | **仍显示「部分账号官方用量未同步」** + 「已同步 4/4 个当前账号；失败账号的官方数值显示为—」 |

**这说明官方前端不是（或不仅是）读 `errors[]` / `ok` / `detailTruncated`。**

## 二、真凶：`status` 无条件硬编码

`gateway/web_proxy.py:10943`（全文仅此 1 处赋值）：

```python
ou["status"] = "partial"
```

**没有任何 `if`，不依赖任何变量。**

官方前端确实读它 —— 源码注释（10933 行）实证：

```javascript
function py(e){return e?.status==="complete"||e?.status==="partial"}
```

且「**部分**账号未同步」与 `status: "**partial**"` 字面对应。

### 这个硬编码的来源（v1.0.23）

注释写：

```
# 取 partial 而非 complete：账号明细与按日明细是实时重建的，
# 但「官方请求级原始用量（requests）」仍来自已停更的采集，
# 用 partial 如实表达「部分实时」，与 errors=[] 不矛盾。
```

**理由当时成立，但现在已被推翻** —— v1.0.32 把 `requests` 换成网关账本
（`_g_reqs = _gateway_recent_requests()`，10885 行），塔灵实测容器里为 **600 条账本实时数据**（`client: "gateway"`），`source` 也已是 `gateway-realtime`。**status 却没跟着改。**

## 三、修法

```python
# 改前（10943）
ou["status"] = "partial"

# 改后
ou["status"] = "complete" if _g_reqs else "partial"
```

| `_g_reqs` | `status` | 含义 |
| :--- | :--- | :--- |
| **非空**（当前实测 600 条） | **`complete`** | accounts/daily/models/requests/summary 全部实时重建 |

### 为什么用 `_g_reqs` 作条件

与 10914 行已有的 `if _g_reqs: ou["requests"] = _g_reqs` **同一条件源**，逻辑自洽：
- 账本有数据 → requests 被接管 → 整份数据实时 → `complete`
- 账本为空 → requests 仍保留上游透传旧值（可能是停更采集）→ 维持 `partial`，**如实表达不谎报**

### 安全性

`py()` 对 `complete` 与 `partial` 都返回 true，改成 `complete` **不会**触发「官方用量暂不可用」那个提示，只会消除「部分账号未同步」。

## 四、测试

新增 `tests/test_status.py`（9 项），采用**源码驱动**：直接从 `web_proxy.py` 抽出 status 赋值表达式，代入 `_g_reqs` 求值，断言真实产出。

| 场景 | `_g_reqs` | 断言 status |
| :--- | :--- | :--- |
| A | 600 条（塔灵实测实际值） | **`complete`** |
| B | `[]` | `partial` |
| C | 1 条 | `complete` |

**反向验证（关键）** —— 同一份测试在两个版本的代码上跑：

| 代码 | 结果 |
| :--- | :--- |
| **旧**（`ou["status"] = "partial"`） | **3 项 FAIL** |
| **新** | **9 项全过** |

旧代码失败项：
```
FAIL 场景A _g_reqs 有 600 条时 status 必须为 complete（got 'partial'）
FAIL 场景C _g_reqs 有 1 条时 status 为 complete（got 'partial'）
FAIL status 表达式引用了 _g_reqs（不是恒定值，expr='"partial"'）```

## 五、影响

| 项 | 说明 |
| :--- | :--- |
| 改动量 | 1 行赋值（+ 注释替换） |
| 其它字段 | 未动（`ok`/`detailTruncated`/`errors` 保持 v1.0.32 的状态 |
| 用户可见 | 积分统计页「部分账号官方用量未同步」应消失；其它统计数值不变 |

## 六、需复验

请在容器内**打开 `/credit-stats` 页面**（左侧菜单「积分统计」，**不是 `#/credits` 账号管理页）确认警告是否消失**：
- 警告是否消失**
- 是否出现新的提示**
- 账号数值是否正常**

若仍显示，则需重新抓取页面 API 响应与前端 DOM 分析其它字段。
