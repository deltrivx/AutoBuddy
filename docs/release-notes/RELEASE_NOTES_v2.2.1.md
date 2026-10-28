修复容器内独立启动 gateway/sanitize.py 时包导入路径问题。

## 修复

- **双模导入兼容**：`gateway/sanitize.py` 与 `gateway/main.py` 增加单独运行与包导入的 `try ... except ImportError` 容错，确保在 Docker 容器以 `python3 /app/gateway/main.py` 独立工作路径启动时正常解析 `paths` 与 `sanitize` 模块。
