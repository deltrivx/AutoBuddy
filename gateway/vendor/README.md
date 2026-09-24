# vendor/workbuddy_daily.py

来自上游开源项目 [L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)（MIT License），
单文件自包含签到脚本，以 vendor 方式内置，不做任何修改（便于跟随上游更新）。

由 `gateway/wb_daily.py` 以子进程方式调用：传入 `wb_refresh_tokens.json`（由 AutoBuddy
账号池的国内版账号 refresh_token 生成，兼容性已实测：Keycloak RT 可直接走其插件刷新接口，
且刷新后旧 RT 仍有效，只读共用无烧号风险），工作目录隔离，`--query --no-desktop` 运行。
