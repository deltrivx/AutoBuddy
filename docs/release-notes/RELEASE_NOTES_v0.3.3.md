## 🔧 改进亮点

### 1. CodeBuddy IDE / CLI「当前账号」状态徽章彻底隐藏
- 这两个绿色对勾徽章检测的是**宿主机上的桌面客户端**（CodeBuddy IDE 桌面 IDE 与 CodeBuddy CLI 包装器），容器里根本不存在，因此状态恒为「未运行 / 未安装」。
- 从 **CSS 属性选择器** 直接命中隐藏（`[role="status"][aria-label="CodeBuddy IDE 当前账号"]` / `[role="status"][aria-label="CodeBuddy CLI 当前账号"]`），不再依赖 JS 时机，即使后渲染也会被 `display: none !important` 立即隐藏。
- 这两个徽章**只对当前激活的那一个账号**有意义（其他账号本来就不会显示），所以隐藏对其他账号无影响。

### 2. 请求明细按时间倒序展示
- `token_tracker.py` 现在按 `timestamp` 显式倒序，不再依赖云端流水的拼接顺序。
- 浏览器侧不需要改动，`fve` 表格组件已按入参顺序渲染。

### 3. 请求明细精简为最近 200 条
- 仅保留最近 200 条明细，避免 `/api/token-stats` 返回体过大。
- 前端默认每页 50 条保持不变（`Z1=50`）。

---

## 📦 升级方式

Unraid：拉取镜像并用容器模板重建。

```bash
docker pull ghcr.io/deltrivx/workbuddy-switch:latest
```

> 始终通过容器模板（Unraid Template）重建，不要手工拼接 `docker run`。