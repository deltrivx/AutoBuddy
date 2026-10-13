> 修复企微回调 echo 校验解密失败（bad pkcs7 padding）：AES 块大小误写 + openssl 重复填充。

## 缺陷（2026-10-13 实证定位）

企微后台保存「接收消息服务器配置」时报「openapi回调地址请求不通过」。
容器日志显示**企微的请求确实到达了**（真实 timestamp/nonce），但解密失败：

```
WARNING:root:[notify] wecom echo 校验失败: bad pkcs7 padding
GET /api/notify/wecom/callback/... HTTP/1.1" 403 Forbidden
```

两个叠加的根因：

1. **AES 块大小误写成 32**（AES 固定为 16 字节）。PKCS7 填充/去填充按错误的
   块大小计算，自然对不上。
2. **openssl 与企微的填充约定冲突**：`openssl enc` 默认会**加/剥自己的 PKCS7
   填充并带 salt 头**，与企微「无 salt + 标准 PKCS7」的格式不符；本模块解密后
   **又剥了一次**填充，于是报 bad pkcs7 padding。

## 修复

- AES 块大小常量 `_AES_BLOCK = 16`，填充/去填充统一按 16 计算；
- 去填充增加**整段一致性校验**（`data[-n:] == bytes([n]) * n`），避免企微明文
  末尾恰好是合法值时的误剥；
- openssl 调用加 **`-nosalt -nopad`**：salt 与填充全部由本模块按企微协议处理，
  杜绝双重填充。

## 测试

`_test_wecom_callback_crypto.py` 修正块大小断言并新增防回归（17 项全过）：

- 边界：整块时补满一块、单字节补 15、unpad 正确、**unpad 拒绝非法填充**；
- 完整回调链（加密 → 解密 → 构造回复 → 回复包再解回）此前即通过，本次一并加固。

全量 29 个测试文件全绿。

## 升级说明

重建容器后回到企微后台重新保存一次回调配置即可（链路与密钥均无需改动）。
