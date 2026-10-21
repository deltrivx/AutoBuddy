# AutoBuddy Releases

[项目说明](README.md) | [更新日志](CHANGELOG.md)

本页是 **AutoBuddy 的版本索引**。GitHub Releases 侧栏按发布时间排序，本页按语义化版本号排序，
避免后补的旧版本让版本顺序看起来错乱。

**当前稳定版：** [v1.0.18](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.18)

> 本文件由 `scripts/gen_releases.py` 从 `CHANGELOG.md` 生成，修改请改 CHANGELOG 后重新生成。

## 版本索引

| Version | 日期 | 更新摘要 | 发布说明 |
|---|---|---|---|
| [v1.0.18](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.18) | 2026-10-21 | 安全修复——网关本体（18091）32 个零鉴权路由补齐登录门，与 WebUI 同一套会话判定；账号池落盘前留时间戳备份，注入不再不留痕 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.18.md) |
| [v1.0.17](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.17) | 2026-10-21 | — | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.17.md) |
| [v1.0.17](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.17) | 2026-10-21 | 修复账号卡片点「启用」时按钮跑到卡片底部（flex 折行点随按钮文案长度漂移），并补齐注入按钮 innerHTML 路径缺失的 type="button"（移动端整页刷新） | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.17.md) |
| [v1.0.16](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.16) | 2026-10-21 | 异常账号卡片登录提醒区域尺寸下调（不再比它替代的正常区域还高），模型区内框高度改为按同页正常卡实测对齐，只调尺寸不动结构 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.16.md) |
| [v1.0.15](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.15) | 2026-10-21 | 修复 v1.0.14 解除风控标记按钮假成功的缺陷——请求被 catch-all 路由转给官方后端返回 200，标记实际未被清除 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.15.md) |
| [v1.0.14](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.14) | 2026-10-21 | 账号卡片新增「被上游风控标记」徽章与推送通知，标记落在独立存储里、与本地停用彻底分开；只通知不自动停用，也不碰账号数据文件 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.14.md) |
| [v1.0.13](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.13) | 2026-10-21 | 补齐单账号风控预警推送（只通知不自动停用）与打卡事件时间戳抖动两项真缺口，均不改账号数据、不新增自动处置 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.13.md) |
| [v1.0.11](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.11) | 2026-10-21 | 版本号规范回归语义化三位制，撤销四段日期制试验；顺带修掉 GHCR 镜像标签带 v 前缀和 latest 重复产出两个 CI 缺陷 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.11.md) |
| [v1.0.10](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.10) | 2026-10-22 | 账号搜索框挪到档位切换行右端并对齐官方尺寸，每日任务签到/执行两个开关彻底拆分独立持久化，账号签到与凭据板块补背景框，设备指纹与 UA 改为动态池化隔离并覆盖新账号，关于页「说明」项移除 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.10.md) |
| [v1.0.9](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.9) | 2026-10-21 | 账号池白名单彻底收紧 + 负载均衡平摊 + 每日任务安全分级与防风控加固 + 国际版每日活跃接入 + 全量账号独立硬件指纹 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.9.md) |
| [v1.0.8](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.8) | 2026-10-20 | 安全修复——WebUI 全部 54 个管理端点补登录鉴权（此前 /api/import 等写操作未登录可直接调用，线上曾被写入注入测试账号） | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.8.md) |
| [v1.0.7](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.7) | 2026-10-20 | 401 卡片视觉精修：提示块对齐正常卡片官方区高度（112px 实测），顶部外框官方「需重新登录」badge 隐藏，按钮区/模型区灰化禁用 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.7.md) |
| [v1.0.6](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.6) | 2026-10-20 | 新增调用分流三档开关与 API 密钥编辑/重置统计/高危二次确认，401 账号卡片积分区原始错误页替换为「需要重新登录」提示（后端双来源合并判定） | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.6.md) |
| [v1.0.5](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.5) | 2026-10-20 | 修复账号卡片开关抖动 / 按钮跑位 / 移动端整页刷新三处交互缺陷，vendor 脚本升级 v3.14 支持 500 积分补偿领取 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.5.md) |
| [v1.0.4](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.4) | 2026-10-18 | 模型拉取支持国际版双通道并发与幂等合并，补齐国际版出站路由映射，国际独占模型定向放行，导出账号默认脱敏 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.4.md) |
| [v1.0.3](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.3) | 2026-10-18 | 版本仅含账号级停用策略的脏数据清理说明与版本号推进，无功能与逻辑变更（代码零改动） | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.3.md) |
| [v1.0.2](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.2) | 2026-10-18 | 修复移动端「账号管理」误跳数据总览（下拉面板丢失导航语义），并根治日报「昨日签到积分」「昨日调用」恒为 0（跨进程缓存失效 + 跨天聚合链抖动，改直连官方接口 + SQLite 精确计数兜底） | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.2.md) |
| [v1.0.1](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.1) | 2026-10-18 | 修复上游 401 英文错误页原样透传（改为中文「登录已过期」），并落地刷新令牌四项防复发改造（单飞锁、失败隔离、批量熔断告警、节奏限制），应对上游 session 批量重置 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.1.md) |
| [v1.0.0](https://github.com/deltrivx/autobuddy/releases/tag/v1.0.0) | 2026-10-17 | 正式版发布。六大页面样式完全统一、数据总览仪表盘、任务巡检与断点续跑、签到/任务调度拆分、智能助手双查能力，以及顶栏导航整体放大 | [详细说明](./docs/release-notes/RELEASE_NOTES_v1.0.0.md) |
| [v0.9.99](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.99) | 2026-10-17 | 修复 v0.9.94–v0.9.98 连续五个版本 CI 构建失败（源码契约测试未随代码同步，导致 test job 失败、镜像未推送） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.99.md) |
| [v0.9.98](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.98) | 2026-10-17 | 概览页「近 30 天积分消耗」趋势图改为固定 31 天窗口，缺失日期补 0，图表尺寸与位置不变 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.98.md) |
| [v0.9.97](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.97) | 2026-10-17 | 顶栏与导航继续整体放大（顶栏 72px、品牌图标 38px、导航 46px 高、移动端按钮 42px） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.97.md) |
| [v0.9.96](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.96) | 2026-10-17 | 顶栏与内容整体放大补齐品牌区，六页样式完全统一（宽度/左边缘/主标题/位置/顶栏六个维度），并修 logo 与主题左右散开 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.96.md) |
| [v0.9.95](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.95) | 2026-10-17 | 助手新增签到活动查询工具与「数字对不上必须双查」规则，并修掉第二轮 LLM 返回空正文导致答非所问的 bug | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.95.md) |
| [v0.9.94](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.94) | 2026-10-17 | 修从数据总览点官方页时页面不切换（pushState 不触发任何事件，视图应用从未运行） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.94.md) |
| [v0.9.93](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.93) | 2026-10-17 | 修带查询串打开时视图 hash 判定失效（每日任务退回官方页、高亮丢失），改用归一化后的视图 hash | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.93.md) |
| [v0.9.92](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.92) | 2026-10-17 | 数据总览改为默认主页（URL 无后缀）、账号管理改走 #/accounts 后缀，新增部署前自动中止任务的脚本，并取消密钥统计里的版本号提示 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.92.md) |
| [v0.9.91](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.91) | 2026-10-17 | 修密钥统计累计调用数口径错误（显示 92 而实际 45673），改为始终使用 api_keys 权威计数，并消除行内徽标与统计行的重复展示 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.91.md) |
| [v0.9.90](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.90) | 2026-10-17 | 修日报「昨日签到积分」恒为 0 与「昨日调用」翻倍，签到通知改为仅签到轮发送并加跨轮次去重，任务执行期改为增量落盘以支持中断后续跑，API 密钥行新增今日/累计调用与词元消耗 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.90.md) |
| [v0.9.89](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.89) | 2026-10-17 | 修复 v0.9.88 巡检完全失效的回归（重复 __init__ 覆盖 kind 参数）与顶栏选项卡键盘焦点不可见 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.89.md) |
| [v0.9.88](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.88) | 2026-10-17 | 每日任务新增巡检自动恢复与签到/任务调度拆分，数据总览改为 Tremor 式仪表盘并统一六页宽度，另修今日消耗口径、断点续跑与通知去重 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.88.md) |
| [v0.9.87](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.87) | 2026-10-16 | 修复巡检/日报通知文案重复与假数据、移动端参考百度做全屏适配（viewport 禁缩放+延伸到刘海区+惯性滚动） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.87.md) |
| [v0.9.86](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.86) | 2026-10-16 | 主界面重新设计为左右分离 + 背景磨砂样式（含移动端竖屏底部导航适配）、联网搜索引擎改为前端可自选配置（6 种引擎、密钥不再内置）、设置页账号资料置顶、主题入口挪到侧边栏左下角 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.86.md) |
| [v0.9.85](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.85) | 2026-10-15 | 修复设置页「智能助手」区块不显示、模块顺序偶发随机、侧边栏切换闪屏三个 UI 缺陷 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.85.md) |
| [v0.9.84](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.84) | 2026-10-15 | 任务上游 workbuddy_daily.py 升级至 v3.7（两种连签口径/任务过滤/Token 健壮性）+ 新增任务积分规则表，助手可回答「预计积分收益」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.84.md) |
| [v0.9.83](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.83) | 2026-10-15 | 企微链路根因修复（WECHAT_PROXY 纠正为 URL 重写）+ 回调改 MP 模式彻底解决 5 秒超时 + 智能助手补全至 30 只读工具/10 写操作 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.83.md) |
| [v0.9.82](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.82) | 2026-10-15 | 参考MP的wechatbot.py新增企业微信智能机器人WebSocket通道——出站长连接不校验来源IP，解决HTTP通道errcode=60020导致通知全阻塞 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.82.md) |
| [v0.9.81](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.81) | 2026-10-15 | 补齐企微渠道级代理（对齐MoviePilot的WECHAT_PROXY）+ 推送失败可读日志 + 助手人格化回复 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.81.md) |
| [v0.9.80](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.80) | 2026-10-15 | 修正统计页回归测试——兼容「线上已被替换」状态（修复生效后旧文案不存在，规则命中0属预期） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.80.md) |
| [v0.9.79](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.79) | 2026-10-15 | 修复统计页加载失败/卡死——签到接口上游劣化到5~9秒拖垮整页，改stale-while-revalidate；旧品牌名workbuddy-switch改AutoBuddy | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.79.md) |
| [v0.9.78](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.78) | 2026-10-15 | 修正助手provider切换语义——切换=选编辑目标（不落盘），只有保存才提交 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.78.md) |
| [v0.9.77](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.77) | 2026-10-14 | 修复助手配置页「点击OpenAI兼容接口直接卡死」——PUT保存时同步跑LLM探测实测102.1秒 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.77.md) |
| [v0.9.76](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.76) | 2026-10-14 | 修复「你可以做什么」收不到回复——企微5秒硬限制下LLM回复被丢弃，改为能力问句即时答+LLM延后推送 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.76.md) |
| [v0.9.75](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.75) | 2026-10-14 | 主动播报落地——任务失败推送智能分析（原因+建议），与既有通知分工不重叠 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.75.md) |
| [v0.9.74](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.74) | 2026-10-14 | 阶段6——助手主动智能（积分趋势预测+任务失败归因）；修掉运行中任务被误报为失败 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.74.md) |
| [v0.9.73](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.73) | 2026-10-14 | 阶段5——写操作两段式确认，写操作永不被自然语言直接触发 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.73.md) |
| [v0.9.72](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.72) | 2026-10-14 | 阶段4——助手只读工具 6→15，查询能力覆盖 AutoBuddy 五大功能域 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.72.md) |
| [v0.9.71](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.71) | 2026-10-14 | 修复「问一句就跑一轮任务」——疑问句被交互菜单的子串匹配拦截并触发写操作 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.71.md) |
| [v0.9.70](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.70) | 2026-10-14 | 修复设置页看不到智能助手区块——首次加载入口 run() 漏注入助手 section | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.70.md) |
| [v0.9.69](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.69) | 2026-10-14 | 设置页新增智能助手配置区——provider 注册表驱动，默认自带模型，也可配任意 OpenAI 兼容接口 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.69.md) |
| [v0.9.68](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.68) | 2026-10-14 | 修复智能助手 tool_echo 缺陷——hy3 第二轮把工具调用原文当正文回吐，用户看到乱码 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.68.md) |
| [v0.9.67](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.67) | 2026-10-14 | 交互加入智能助手——自然语言提问实时数据回答（参考 MoviePilot agent/llm 设计） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.67.md) |
| [v0.9.66](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.66) | 2026-10-14 | 修复 token 统计页 500——entrypoint 双向软链成环导致 SQLite 打不开数据库 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.66.md) |
| [v0.9.65](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.65) | 2026-10-14 | 修复配额仲裁边界缺陷——最优候选为0承接时任何有记录的账号都被判让位，粘性被架空 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.65.md) |
| [v0.9.64](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.64) | 2026-10-14 | 调度算法三项优化——配额账本时间衰减、sticky 记账+配额仲裁、解析上游 Retry-After | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.64.md) |
| [v0.9.63](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.63) | 2026-10-13 | 修复「交互菜单还是没变」根因——命令语义收拢为唯一实现，删除 web_proxy 重复内联命令链 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.63.md) |
| [v0.9.62](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.62) | 2026-10-13 | 修复通知「剩余积分>总积分」口径悖论；菜单按钮改为四项积分统计口径 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.62.md) |
| [v0.9.61](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.61) | 2026-10-13 | 修复「执行无记录」最后一条遗漏路径——进程被 SIGKILL/容器重建时任务不再无痕消失 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.61.md) |
| [v0.9.60](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.60) | 2026-10-13 | 修复积分调度被会话粘性架空——绑定账号余额掉队时解绑走配额，相关系数 0.032 → 有效 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.60.md) |
| [v0.9.59](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.59) | 2026-10-13 | 超时判定改为按账号（原为30分钟总时间，账号增多必被强杀） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.59.md) |
| [v0.9.58](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.58) | 2026-10-13 | 修复点击执行却无记录（超时/中止不落库）；调度算法改配额制按余额比例分摊；通知补齐 5 类场景 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.58.md) |
| [v0.9.57](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.57) | 2026-10-13 | 修复积分感知调度失效——调度器读的余额快照 9-23 起停更，改用实时快照 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.57.md) |
| [v0.9.56](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.56) | 2026-10-13 | 通知管理前端对齐 MoviePilot——渠道卡片网格 + 配置弹窗（每渠道独立启用/消息场景） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.56.md) |
| [v0.9.55](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.55) | 2026-10-13 | 修复企微菜单无回复——PKCS7 填充块误写 16，官方协议为 32 字节 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.55.md) |
| [v0.9.54](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.54) | 2026-10-13 | 修复 Telegram 交互命令路由——英文命令未归一化、accounts/models 缺分支、checkin 错跑全量任务 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.54.md) |
| [v0.9.53](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.53) | 2026-10-13 | 照 MoviePilot 补齐 Telegram 交互——命令菜单自动注册 + getUpdates 长轮询（不需要公网） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.53.md) |
| [v0.9.52](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.52) | 2026-10-13 | 新增 Telegram 通知通道（含渠道级独立代理）+ probe 自检端点；并更正此前「与 MoviePilot 对齐」的不准确表述 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.52.md) |
| [v0.9.51](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.51) | 2026-10-13 | 修复交互指令取不到值——/api/* 端点误打到网关(18091)，实际在 web_proxy(18090) | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.51.md) |
| [v0.9.50](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.50) | 2026-10-13 | 补齐 MoviePilot 同款核心交互——自动生成企微应用底部菜单（点按钮即交互），而非让用户手打命令 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.50.md) |
| [v0.9.49](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.49) | 2026-10-13 | 修复企微回调 echo 校验解密失败——AES 块大小误写 32 + openssl 与企微填充约定冲突（双重填充） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.49.md) |
| [v0.9.48](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.48) | 2026-10-13 | 修复回调字段重复注册——cb_token/cb_aes_key 被当成密钥，GET 回读成字典、前端无法回显 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.48.md) |
| [v0.9.47](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.47) | 2026-10-13 | 企业微信完整交互——内置回调端点（echo 校验+消息接收+指令自动回复），零辅助容器 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.47.md) |
| [v0.9.46](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.46) | 2026-10-13 | 设置页新增「通知管理」——多渠道通知框架（参照 MoviePilot），首渠道企业微信 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.46.md) |
| [v0.9.45](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.45) | 2026-10-13 | 接入 /v3/config 动态模型目录（官方新模型即时可见）+ 补全「手动执行任务」按钮 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.45.md) |
| [v0.9.44](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.44) | 2026-10-13 | 三项急迫修复——模型发现双通道全断、官方消耗图表停更（漏三个顶层字段）、积分感知调度 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.44.md) |
| [v0.9.43](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.43) | 2026-10-11 | 修复左侧 5 个标签切换时的闪屏与页面漂移 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.43.md) |
| [v0.9.42](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.42) | 2026-10-11 | 官方积分采集 9-24 起停更——账号明细（近7日/本月/按日图表）由网关实时数据整体接管 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.42.md) |
| [v0.9.41](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.41) | 2026-10-10 | 修复今日消耗在北京时间 0 点未重置（凌晨 1 点就显示昨日全天量级），并让 7 日/本月改为按日滚动求和自动刷新 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.41.md) |
| [v0.9.40](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.40) | 2026-10-09 | 修复「近7日/本月消耗比今日消耗还低」——累计值小于单日值，数学上不可能 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.40.md) |
| [v0.9.39](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.39) | 2026-10-09 | 补登记——任务结束后重试运行期被「前置未满足」拒掉的任务登记，解开成长任务完成度卡死 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.39.md) |
| [v0.9.38](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.38) | 2026-10-09 | 补领（等待异步计分落定后领奖）补上回归测试 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.38.md) |
| [v0.9.37](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.37) | 2026-10-09 | 请求级指标面板、成长任务等待异步计分落定后补领奖，并标注配置的热/冷边界 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.37.md) |
| [v0.9.36](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.36) | 2026-10-08 | 修复积分统计漏 15 个账号、token 统计只显示 2 天、429 盲目退避改为按上游重置墙钟冷却 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.36.md) |
| [v0.9.35](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.35) | 2026-10-08 | 渠道拦截（400 code=11128）让该账号退出轮询 30 分钟；出站 UA 对齐官方桌面客户端形态 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.35.md) |
| [v0.9.34](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.34) | 2026-10-07 | 移除全部 IP 白名单功能（含每密钥白名单与全局免密钥白名单），访问校验回归「密钥 + loopback」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.34.md) |
| [v0.9.33](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.33) | 2026-10-07 | 移除密钥列表头部的全局「累计调用」汇总，彻底修正调用量的归属 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.33.md) |
| [v0.9.32](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.32) | 2026-10-07 | 调用量与白名单下沉到每个 API 密钥，修正设置页的主次关系 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.32.md) |
| [v0.9.31](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.31) | 2026-10-06 | 参照 workbuddy2api 补齐四项治理能力——429/402 冷却、会话粘性、积分保底、成本折算 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.31.md) |
| [v0.9.30](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.30) | 2026-10-05 | 修复账号卡片读停更旧JSON导致新账号模型不显示、429响应体进日志、×N重复打印 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.30.md) |
| [v0.9.29](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.29) | 2026-10-02 | 用量明细迁到 SQLite（增量幂等写入 + 索引），聚合逻辑与输出契约一行未改 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.29.md) |
| [v0.9.28](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.28) | 2026-10-02 | 补上 v0.9.27 遗留的两处缺口——队列撑不住时不再静默丢弃、明细改为按时间保留 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.28.md) |
| [v0.9.27](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.27) | 2026-10-02 | 参照 sub2api/CLIProxyAPI 重构统计存储（攒批+原子写+分级保留）并让官方数据陈旧可见 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.27.md) |
| [v0.9.26](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.26) | 2026-10-02 | 修复工具调用透传丢失 tool_calls、日志重复计数改为行尾 ×N、词元统计只剩两天 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.26.md) |
| [v0.9.25](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.25) | 2026-09-30 | 一致性自检页补上「额度用尽」的显示档位，不再与「未检测」混淆 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.25.md) |
| [v0.9.24](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.24) | 2026-09-30 | 额度耗尽的账号由巡检自动停用，不再只提醒用户手动删除 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.24.md) |
| [v0.9.23](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.23) | 2026-09-30 | 修复新加账号永远不被调用、以及巡检探测账号池外账号 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.23.md) |
| [v0.9.22](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.22) | 2026-09-28 | 修复每日任务页四项数据框在深色主题下不变色 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.22.md) |
| [v0.9.21](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.21) | 2026-09-27 | 修正 v0.9.20 去重的两个缺陷：重复次数被悄悄吞掉、字典无上限 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.21.md) |
| [v0.9.20](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.20) | 2026-09-27 | access log 通用相邻去重，相同日志只出现一条 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.20.md) |
| [v0.9.19](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.19) | 2026-09-27 | 词元统计页剩余 Token 字段改走运行时替换，彻底清零 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.19.md) |
| [v0.9.18](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.18) | 2026-09-27 | 每日任务页积分四项独立成框、最近记录独立、积分自动更新、清理多余显示 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.18.md) |
| [v0.9.17](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.17) | 2026-09-27 | 热修 v0.9.16 —— 等长替换违反导致 JS 资源 500、前端整站白屏 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.17.md) |
| [v0.9.16](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.16) | 2026-09-27 | 修复侧边栏图标丢失回归、积分统计项补标题图标、Token 字段改为词元 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.16.md) |
| [v0.9.15](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.15) | 2026-09-27 | 修复刷新后需先点设置、侧边栏标题统一、每日任务页区块结构对齐官方 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.15.md) |
| [v0.9.14](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.14) | 2026-09-27 | 修正 v0.9.13 中被内联样式与选择器特异性抵消的三处对齐 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.14.md) |
| [v0.9.13](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.13) | 2026-09-27 | 每日任务页控件（按钮/输入框/下拉/表格）统一到官方规格 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.13.md) |
| [v0.9.12](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.12) | 2026-09-27 | 每日任务页整体对齐官方页面布局与样式、侧边栏收纳提示、浮窗四色区分 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.12.md) |
| [v0.9.11](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.11) | 2026-09-27 | 签到积分改从签到状态页取真实奖励、积分统计栏 1:1 复刻官方结构 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.11.md) |
| [v0.9.10](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.10) | 2026-09-27 | 积分统计栏对齐官方样式、浮窗数值着色、退出登录文字左对齐 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.10.md) |
| [v0.9.9](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.9) | 2026-09-27 | 新增每日任务积分统计（今日/签到/任务/总积分）与浮窗今日积分 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.9.md) |
| [v0.9.8](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.8) | 2026-09-27 | 移动端卡片图标同步显示、修复积分文案被误判为旅行图标、浮窗信息修正与对齐 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.8.md) |
| [v0.9.7](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.7) | 2026-09-27 | 修正认证凭据语义为「环境变量在场即覆盖、不在场则保留」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.7.md) |
| [v0.9.6](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.6) | 2026-09-27 | 修正每日任务完成度统计、修复移动端竖屏状态图标、修正认证凭据被环境变量覆盖 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.6.md) |
| [v0.9.5](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.5) | 2026-09-26 | 取消昵称统一显示名、头像预览独立放大、CHANGELOG 与 README 规范化 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.5.md) |
| [v0.9.4](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.4) | 2026-09-26 | README 重写为可照做的文档，后台日志彻底降噪，头像上传钮排版修正 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.4.md) |
| [v0.9.3](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.3) | 2026-09-26 | 移除「账号接入」（GitHub 自动注册）功能，修复误删的每日任务与 Buddy 徽章函数 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.3.md) |
| [v0.9.1](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.1) | 2026-09-26 | 修复能力矩阵重入死锁导致网关 502 / 挂起，统一 Buddy 徽章图标 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.1.md) |
| [v0.9.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.9.0) | 2026-09-26 | 模型失败即时学习并换号重试、正向能力矩阵、后台日志去重、Buddy 徽章统一图标 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.9.0.md) |
| [v0.8.2](https://github.com/deltrivx/autobuddy/releases/tag/v0.8.2) | 2026-09-26 | 账号并发分摊、头像全面圆形化、用户球悬浮展开、密码位数掩码 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.8.2.md) |
| [v0.8.1](https://github.com/deltrivx/autobuddy/releases/tag/v0.8.1) | 2026-09-26 | 品牌统一、浮窗积分信息、无 Buddy 虚线徽章、头像体系升级 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.8.1.md) |
| [v0.8.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.8.0) | 2026-09-26 | 右下角用户球与账号资料管理，会话收紧为 24 小时 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.8.0.md) |
| [v0.7.8](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.8) | 2026-09-25 | 彻底根除「幽灵账号」残留并保障所有账号按钮功能一致性 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.8.md) |
| [v0.7.7](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.7) | 2026-09-25 | 移除 401 审计落盘（恢复纯净镜像） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.7.md) |
| [v0.7.6](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.6) | 2026-09-25 | 新增 401 鉴权失败审计落盘（取证用） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.6.md) |
| [v0.7.5](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.5) | 2026-09-25 | 每日任务：签到与凭据合并为一行，不再重复列账号 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.5.md) |
| [v0.7.4](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.4) | 2026-09-25 | 设置页彻底移除自动签到功能 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.4.md) |
| [v0.7.3](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.3) | 2026-09-25 | 恢复自动签到历史记录与日志面板 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.3.md) |
| [v0.7.2](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.2) | 2026-09-25 | 彻底根除幽灵账号与模型策略残留 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.2.md) |
| [v0.7.1](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.1) | 2026-09-24 | 修复 v0.7.0 引入的注入 JS 语法错（每日任务页布局塔陷） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.1.md) |
| [v0.7.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.7.0) | 2026-09-24 | 统一账号来源：修好「按钮在、一点就报 account not found」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.7.0.md) |
| [v0.6.6](https://github.com/deltrivx/autobuddy/releases/tag/v0.6.6) | 2026-09-24 | 移除占位条目：账号池控件不再「硬加」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.6.6.md) |
| [v0.6.5](https://github.com/deltrivx/autobuddy/releases/tag/v0.6.5) | 2026-09-24 | 账号池控件改为「无论账号什么状态都完整渲染」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.6.5.md) |
| [v0.6.4](https://github.com/deltrivx/autobuddy/releases/tag/v0.6.4) | 2026-09-24 | 修复官方新加账号（如「一杯美式」）不出现在账号池控件里 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.6.4.md) |
| [v0.6.3](https://github.com/deltrivx/autobuddy/releases/tag/v0.6.3) | 2026-09-24 | 修复新账号卡片缺失账号池控件（启用/停用 · 设为首选 · 检测账号 · 调用次数） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.6.3.md) |
| [v0.6.2](https://github.com/deltrivx/autobuddy/releases/tag/v0.6.2) | 2026-09-24 | 修复 v0.6.1 引入的 500（缺 time 导入） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.6.2.md) |
| [v0.6.1](https://github.com/deltrivx/autobuddy/releases/tag/v0.6.1) | 2026-09-24 | 修复积分今日消耗恒为 0、进度闪烁、代理检测布局与轮询策略 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.6.1.md) |
| [v0.6.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.6.0) | 2026-09-24 | WorkBuddy 每日成长任务（侧边栏「每日任务」） | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.6.0.md) |
| [v0.5.12](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.12) | 2026-09-24 | 注册进度可视化、日志降噪与诊断能力补强 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.12.md) |
| [v0.5.11](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.11) | 2026-09-24 | GitHub 改为纯邮箱注册、邮箱参数 UI 可配、中止接口容错 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.11.md) |
| [v0.5.10](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.10) | 2026-09-24 | 修复临时邮箱创建判定导致注册流程无法启动 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.10.md) |
| [v0.5.9](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.9) | 2026-09-24 | 修复代理检测报错与任务进度丢失 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.9.md) |
| [v0.5.8](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.8) | 2026-09-24 | 账号接入页面全面优化与 GitHub 注册全自动化 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.8.md) |
| [v0.5.7](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.7) | 2026-09-24 | 浏览器底层升级为 Patchright 免检测驱动 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.7.md) |
| [v0.5.6](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.6) | 2026-09-24 | 登录安全增强与配置修正 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.6.md) |
| [v0.5.5](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.5) | 2026-09-24 | 代理架构优化与模板脱敏修复 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.5.md) |
| [v0.5.4](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.4) | 2026-09-24 | 自动打码与账号闭环接入 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.4.md) |
| [v0.5.3](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.3) | 2026-09-23 | 统计校准与修复 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.3.md) |
| [v0.5.2](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.2) | 2026-09-23 | 认证增强 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.2.md) |
| [v0.5.1](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.1) | 2026-09-23 | 认证与数据库 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.1.md) |
| [v0.5.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.5.0) | 2026-09-23 | 更名 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.5.0.md) |
| [v0.4.17](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.17) | 2026-09-23 | 新增 GitHub 账号自动化接入，浏览器改为运行时下载到挂载卷 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.17.md) |
| [v0.4.16](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.16) | 2026-09-21 | 修正账号被拦截的判定与措辞，重扫无效的说明移到设置页 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.16.md) |
| [v0.4.15](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.15) | 2026-09-21 | 修复设为首选清空账号白名单；新增模型一键恢复 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.15.md) |
| [v0.4.14](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.14) | 2026-09-20 | 异常账号的提示收成一句，详情从提示里移出 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.14.md) |
| [v0.4.13](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.13) | 2026-09-20 | 提示改说人话，风控账号纳入自动停用，恢复正常才自动放回 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.13.md) |
| [v0.4.12](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.12) | 2026-09-20 | 修正检测提示的配色与措辞：错误档改对类名、三档配色生效、窄屏不再溢出 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.12.md) |
| [v0.4.11](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.11) | 2026-09-20 | 统一账号有效性判定标准，修正内容审查被误判为凭据失效 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.11.md) |
| [v0.4.10](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.10) | 2026-09-20 | 修正「检测账号」的结论表述，说明凭据有效的判定依据 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.10.md) |
| [v0.4.9](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.9) | 2026-09-20 | 新增账号级停用与凭据检测；修复手机等窄屏下侧边栏与设置项被挤压 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.9.md) |
| [v0.4.8](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.8) | 2026-09-20 | 巡检落盘改为只叠加本轮判定，巡检期间点下的手动禁用不再被覆盖 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.8.md) |
| [v0.4.7](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.7) | 2026-09-20 | 账号卡片把「手动禁用」与「巡检禁用」分开计数，巡检也不再探测手动禁用的模型 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.7.md) |
| [v0.4.6](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.6) | 2026-09-20 | 账号卡片里注入的两块固定收尾，「积分明细」不再一会儿在上一会儿在下 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.6.md) |
| [v0.4.5](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.5) | 2026-09-19 | 缓存命中率改为采用上游实测用量，版本号合并为一套，设置页「关于」重新排版 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.5.md) |
| [v0.4.4](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.4) | 2026-09-19 | 巡检探测彻底对齐真实调用，并补上「探测被拒」「凭据失效」两道跳过规则 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.4.md) |
| [v0.4.3](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.3) | 2026-09-19 | 修复巡检误报导致的成批误禁，禁用项区分「手动 / 巡检」来源，设置页补上「关于」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.3.md) |
| [v0.4.2](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.2) | 2026-09-19 | 模型可用性自动巡检：调不通自动禁用，恢复自动启用 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.2.md) |
| [v0.4.1](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.1) | 2026-09-19 | 模型标签可点击禁用，精确到「账号 × 模型」 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.1.md) |
| [v0.4.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.4.0) | 2026-09-19 | 设置页新增 API 接入面板与访问密钥 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.4.0.md) |
| [v0.3.18](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.18) | 2026-09-19 | 修复账号调用次数随容器重建归零 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.18.md) |
| [v0.3.17](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.17) | 2026-09-19 | 清理数据卷里 CLI 时代遗留的痕迹 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.17.md) |
| [v0.3.16](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.16) | 2026-09-19 | 消除卡片上两个「调用次数」的口径混淆 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.16.md) |
| [v0.3.15](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.15) | 2026-09-19 | 更正 v0.3.14 的误判，并加改文案失效告警 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.15.md) |
| [v0.3.14](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.14) | 2026-09-19 | 修掉描述已移除产品的陈旧界面文案 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.14.md) |
| [v0.3.13](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.13) | 2026-09-19 | 修掉空状态里指向已删除按钮的误导文案 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.13.md) |
| [v0.3.12](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.12) | 2026-09-18 | 清掉最后两处 CLI 残留 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.12.md) |
| [v0.3.11](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.11) | 2026-09-18 | 彻底移除 CLI；修复补丁静默失效 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.11.md) |
| [v0.3.10](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.10) | 2026-09-18 | 补全容器内运行 CLI 的必要条件 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.10.md) |
| [v0.3.9](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.9) | 2026-09-18 | 让 CodeBuddy CLI 在容器里真正可用 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.9.md) |
| [v0.3.8](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.8) | 2026-09-18 | 全局巡检：修掉面板空数据与失真文案 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.8.md) |
| [v0.3.7](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.7) | 2026-09-18 | 新增并发分摊观测，并修掉一处并发隐患 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.7.md) |
| [v0.3.6](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.6) | 2026-09-18 | 补上卡片头部漏掉的桌面产品槽位 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.6.md) |
| [v0.3.5](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.5) | 2026-09-18 | 修复手动指定账号时状态不更新 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.5.md) |
| [v0.3.4](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.4) | 2026-09-18 | 卡片槽位物理切除 + 账号池并行调度 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.4.md) |
| [v0.3.3](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.3) | 2026-09-18 | 界面残留再清理，明细倒序与精简 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.3.md) |
| [v0.3.2](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.2) | 2026-09-18 | 卡片模型清单改为动态可用清单 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.2.md) |
| [v0.3.1](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.1) | 2026-09-18 | 请求明细从弹窗改为页面内平铺 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.1.md) |
| [v0.3.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.3.0) | 2026-09-18 | 界面源码级精简 + Token 统计引擎上线 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.3.0.md) |
| [v0.2.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.2.0) | 2026-09-18 | 容器化体验升级与全量模型网关支持 | [详细说明](./docs/release-notes/RELEASE_NOTES_v0.2.0.md) |
| [v0.1.2](https://github.com/deltrivx/autobuddy/releases/tag/v0.1.2) | 2026-09-18 | 图标全链路对齐与 Sub2API 对接 | — |
| [v0.1.0](https://github.com/deltrivx/autobuddy/releases/tag/v0.1.0) | 2026-09-18 | 初始版本发布 | — |
| [vX.Y.Z](https://github.com/deltrivx/autobuddy/releases/tag/vX.Y.Z) | YYYY-MM-DD | 一句话说清这个版本做了什么 | — |

## 镜像标签

镜像由 GitHub Actions **云端构建**并推送至 GHCR（不在本地构建上传）：

```bash
docker pull ghcr.io/deltrivx/autobuddy:latest   # 最新稳定版
docker pull ghcr.io/deltrivx/autobuddy:1.0.18    # 锁定版本
```

## 部署产物

每个 Release 均附带以下部署产物：

| 产物 | 说明 |
|---|---|
| `AutoBuddy.xml` | Unraid 容器模板（**请以模板方式创建容器，勿手工拼接 `docker run`**） |
| `docker-compose.yml` | Docker Compose 部署文件 |
| `icon.png` | 512×512 容器图标（Unraid 模板使用） |
| `SHA256SUMS` | 上述产物的校验值 |
