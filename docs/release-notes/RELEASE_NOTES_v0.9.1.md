# AutoBuddy v0.9.1

> 修复能力矩阵重入死锁导致网关接口假死、移除无 Buddy 徽章外围虚线框。

## 1. 修复网关调用死锁

在 v0.9.0 引入能力矩阵时，`record_capability()` 在加锁内部直接调用了 `_load_capability()`，而锁原先为非可重入锁 `threading.Lock()`。在流式请求成功完成并调用 `record_capability()` 时，同一线程重入尝试获取锁导致永久阻塞。这会导致后续所有模型调用请求、选号及网关 `/health` 接口挂起超时，WebUI 出现 502 Bad Gateway。

v0.9.1 将 `_CAPABILITY_LOCK` 升级为 `threading.RLock()`，彻底消除了这一死锁隐患。

## 2. Buddy 徽章视觉统一

应用户要求，移除 `.wb-nobuddy-badge` 外围多余的虚线圆角边框（`border: none !important`），仅保留内部官方起飞飞机图标的虚线绘制与灰色配色，使无 Buddy、未旅行与旅行中三态在卡片右上角的视觉呈现更加自然统一。

## 升级

```bash
docker pull ghcr.io/deltrivx/autobuddy:0.9.1
```
