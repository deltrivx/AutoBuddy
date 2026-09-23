FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# 安装 Node.js 20, Python 3 及依赖
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    python3 \
    python3-pip \
    socat \
    jq \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 workbuddy-switch（官方底层服务 + WebUI）。
#
# 版本必须锁定，不能用 @latest：
# patch/patch_binary.py 的锚点依赖官方前端的具体结构，上游发版会重排 minify 变量名
# 甚至改动 DOM（0.1.36 -> 0.1.40 就发生过一次），锚点一旦失配，构建会失败（见下方补丁
# 步骤），此时需要人工复核并更新锚点，不能盲跟 latest。
#
# 升级流程：
#   npm pack workbuddy-switch-linux-x64@<新版本> && tar xzf *.tgz
#   python3 patch/patch_binary.py package/bin/wb-switch-linux-x64   # 本地先验证锚点
#   改下面这行版本号 -> 构建
ARG WB_SWITCH_VERSION=0.1.40
RUN npm i -g "workbuddy-switch@${WB_SWITCH_VERSION}"

# 安装 API 网关依赖 (FastAPI + Uvicorn + HTTPX) 与 Playwright 驱动。
#
# **只装驱动，不装浏览器**。Chromium 体积大（解包后 100MB+）且随上游频繁发版，
# 打进镜像会让每次构建都多背一份、推拉都变慢。改为运行时从 UI 手动下载到挂载目录
# （挂载点见下方 PLAYWRIGHT_BROWSERS_PATH），一次下载多容器复用、升级不重建。
#
# 注意必须是 `playwright` 而不是 `playwright[chromium]`：后者会在 pip 阶段
# 就把浏览器拉下来，正是要避免的。浏览器改由 UI 触发 `playwright install chromium`。
RUN pip3 install --no-cache-dir --break-system-packages \
    fastapi \
    uvicorn \
    httpx \
    playwright

# Chromium 的运行时系统库（headless 下的共享库依赖）。
# 这些是 apt 包、体积可控，且不随 Playwright 版本变化，留在镜像里比运行时再装可靠 ——
# 运行时补装需要 apt 源与外网，NAS 上常常不具备。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2t64 libpango-1.0-0 libpangocairo-1.0-0 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY gateway/ /app/gateway/
COPY entrypoint.sh /entrypoint.sh
COPY patch/ /app/patch/
COPY icon.png /app/icon.png
RUN chmod +x /entrypoint.sh

# 给 wb-switch 二进制打补丁。
#
# 目标：只保留容器里真正有意义的能力（账号自动签到 + OpenAI 兼容 API 网关），
# 把依赖宿主桌面程序的功能从 JSX 里物理移除：
#   - 账号卡片头部/底部的 WorkBuddy / CodeBuddy IDE / CodeBuddy CLI 三个图标与「当前账号」徽章
#   - 账号页右上角桌面程序运行状态图标、导入本机账号按钮
#   - 设置页的权限检测 / 启动设置 / 自动更新 / 限额监听 / CodeBuddy CLI 自动轮换
#   - Token 统计页的数据来源 Tab 与冗余标题
# 同时把硬编码的 http://127.0.0.1:57890 换成本页 origin，避免跨域。
#
# 两条纪律（v0.3.11 教训）：
#   1. 绝不用 `|| true` / `2>/dev/null` 吞掉失败。历史上正是这样让补丁整体失效，
#      但镜像构建仍然 success，线上三个桌面图标复活而无人察觉。
#   2. patch_binary.py 在任一锚点失配时会打印明细并以非零码退出；配合 `set -e`
#      让构建直接失败，逼迫人工复核。
RUN set -eux; \
    BINS=$(find /usr/lib/node_modules/workbuddy-switch -type f -name 'wb-switch-linux-x64'); \
    [ -n "$BINS" ] || { echo 'ERROR: 未找到 wb-switch 平台二进制'; exit 1; }; \
    for b in $BINS; do python3 /app/patch/patch_binary.py "$b"; done

ENV HOME=/data
ENV WB_DATA_DIR=/data/.wb-switch
ENV PORT=18090
ENV API_PORT=18091

# 浏览器安装目录指向挂载卷（Unraid 模板里的「浏览器缓存目录」映射到这里）。
# 镜像内不再打包 Chromium，改由 UI 触发 `playwright install chromium` 下载到此处；
# 宿主机持久化后，容器重建/升级都不必重下。
ENV PLAYWRIGHT_BROWSERS_PATH=/data/.wb-switch/browsers

EXPOSE 18090 18091

ENTRYPOINT ["/entrypoint.sh"]
