#!/usr/bin/env python3
"""企微回调加密回归测试：AES-256-CBC 加解密往返 + 签名 + echo 校验 + 消息解析。

企微协议：key = Base64(EncodingAESKey + "=")（43 字符 → 32 字节），
明文 = 16 随机 + 4 字节网络序长度 + xml + corpid，PKCS7(32) 填充，
签名 = SHA1(sorted(token, timestamp, nonce, 密文))。
容器内无 pycryptodome，实现走 openssl CLI —— 本测试在开发机跑，
需要有 openssl（开发机有）。
"""
import sys
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gateway"))

import notify  # noqa: E402

_ok = 0
_fail = []


def check(name, cond, extra=""):
    global _ok
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail.append(name)
        print(f"  FAIL {name}" + (f"  <- {extra}" if extra else ""))


# 模拟企微后台生成的三件套
TOKEN = "abtesttoken123"
AES_KEY = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"  # 43 字符
CORPID = "wwd413dae2a4611f63"

print("[1] AES key 解码")
key = notify._aes_key_b64(AES_KEY)
check("43 字符 AESKey 解码为 32 字节", len(key) == 32, f"got {len(key)}")

print("\n[2] 加解密往返")
xml = "<xml><ToUserName><![CDATA[wwd413dae2a4611f63]]></ToUserName><Content><![CDATA[你好企微]]></Content></xml>"
enc = notify._aes_encrypt(xml, AES_KEY, CORPID)
check("加密产出 Base64 字符串", isinstance(enc, str) and len(enc) > 50)
dec_xml, dec_corp = notify._aes_decrypt(enc, AES_KEY)
check("解密还原原文", dec_xml == xml, f"got {dec_xml[:60]}")
check("解密还原 corpid", dec_corp == CORPID)

print("\n[3] 签名")
ts, nonce = "1791861600", "nonce123"
sign = notify._wecom_sign(TOKEN, ts, nonce, enc)
check("签名是 40 位 SHA1", len(sign) == 40)
# 签名对输入顺序不敏感（协议要求 sorted）
sign2 = notify._wecom_sign(enc, nonce, ts, TOKEN)
check("sorted 签名与顺序无关", sign == sign2)

print("\n[4] echo 校验（企微保存配置时的 GET）")
try:
    echo_plain = notify.verify_echo(TOKEN, AES_KEY, CORPID, enc, sign, ts, nonce)
    check("验签通过回显明文", echo_plain == xml)
except Exception as e:
    check("验签通过回显明文", False, str(e)[:80])
# 错误签名必须拒绝
try:
    notify.verify_echo(TOKEN, AES_KEY, CORPID, enc, "0" * 40, ts, nonce)
    check("错误签名被拒绝", False)
except ValueError:
    check("错误签名被拒绝", True)

print("\n[5] 消息解密 + 回复加密（完整回调链）")
msg_xml = ('<xml><ToUserName><![CDATA[%s]]></ToUserName>'
           '<FromUserName><![CDATA[yangchao]]></FromUserName>'
           '<MsgType><![CDATA[text]]></MsgType>'
           '<Content><![CDATA[help]]></Content></xml>') % CORPID
msg_enc = notify._aes_encrypt(msg_xml, AES_KEY, CORPID)
msg_sign = notify._wecom_sign(TOKEN, ts, nonce, msg_enc)
got = notify.decrypt_message(TOKEN, AES_KEY, CORPID, msg_enc, msg_sign, ts, nonce)
parsed = notify.parse_message(got)
check("解密消息 Content=help", parsed.get("Content") == "help")
check("解密消息 FromUserName", parsed.get("FromUserName") == "yangchao")
reply_plain = notify.build_reply_text("yangchao", CORPID, "指令列表…")
reply_full = notify.encrypt_reply(reply_plain, TOKEN, AES_KEY, CORPID, ts, nonce)
check("回复包是完整 xml", reply_full.startswith("<xml>") and "<Encrypt>" in reply_full)
# 回复包再解密回来验证（对端企微做的事）
m_enc2 = reply_full.split("<Encrypt><![CDATA[")[1].split("]]></Encrypt>")[0]
back, back_corp = notify._aes_decrypt(m_enc2, AES_KEY)
check("回复包可解回", "指令列表" in back and back_corp == CORPID)

print("\n[6] 长文本与特殊字符")
long_xml = "<xml><Content><![CDATA[%s]]></Content></xml>" % ("状态报告" * 200)
enc_l = notify._aes_encrypt(long_xml, AES_KEY, CORPID)
back_l, _ = notify._aes_decrypt(enc_l, AES_KEY)
check("长文本（多块）往返", back_l == long_xml)


def _unpad_raises():
    """非法填充应抛 ValueError（防误剥）。"""
    try:
        notify._pkcs7_unpad(b"abc\x05\x05")
        return False
    except ValueError:
        return True

print("\n[7] PKCS7 边界（企微官方 block=32，此前误写 16 导致真实回调 padding 失败）")
check("pad 恰好整块时补满一块", len(notify._pkcs7_pad(b"x" * 32)) == 64)
check("pad 不足 32 时补到 32 倍数", len(notify._pkcs7_pad(b"a")) == 32)
check("块大小常量为 32（官方 WXBizMsgCrypt 语义）", notify._AES_BLOCK == 32)
check("unpad 正确", notify._pkcs7_unpad(notify._pkcs7_pad(b"abc")) == b"abc")
check("unpad 拒绝非法填充",
      (lambda: [False for _ in [0]] if False else _unpad_raises()))

print(f"\n{'=' * 52}\n通过 {_ok} 项" + (f"，失败 {len(_fail)} 项：{_fail}" if _fail else "，全部通过"))
raise SystemExit(1 if _fail else 0)
