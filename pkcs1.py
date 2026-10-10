# -*- coding: utf-8 -*-
"""纯 Python 实现的 RSA / PKCS#1 v1.5 加密，零原生依赖。

为什么不用 pycryptodome 的 PKCS1_v1_5：
    `from Crypto.Cipher import PKCS1_v1_5` 会在导入时加载原生扩展
    `_pkcs1_decode.pyd`。在启用了应用程序控制策略（WDAC / 智能应用控制）的
    Windows 机器上，该 pyd 会被拦截：
        OSError: Cannot load native module ... error 0x11c7
        (WinError 4551 应用程序控制策略阻止了此文件)
    由于整个软件的可用性不能被单个原生扩展绑架，这里用纯整数运算自己实现。

RSA 加密只需要两件事：公钥的 (n, e)，以及把明文按 PKCS#1 v1.5 规则填充后
做大数模幂 c = m^e mod n。模幂用 Python 内建 pow(m, e, n)，2048 位下耗时约
0.2ms，完全够用，而且不会因为缺少 VC 运行库而失败。

关于 DER：正方教务的登录页在 JS 里是用 RSAUtils.getKeyPair(exponent, '', modulus)
构造公钥的，只用到原始 n / e，并不需要 DER。这里额外提供 encode/decode 是为了
方便做往返自测和与其他实现比对。
"""
from __future__ import annotations

import base64
import secrets
from typing import Tuple

# SubjectPublicKeyInfo 的固定前缀部分：
#   SEQUENCE { SEQUENCE { OID rsaEncryption(1.2.840.113549.1.1.1), NULL } BIT STRING { ... } }
_ALG_ID = bytes.fromhex("300d06092a864886f70d0101010500")


# --------------------------------------------------------------------------- #
# ASN.1 DER
# --------------------------------------------------------------------------- #
def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _der_int(value: int) -> bytes:
    if value == 0:
        body = b"\x00"
    else:
        body = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if body[0] & 0x80:          # 保证最高位为 0，避免被解析成负数
            body = b"\x00" + body
    return b"\x02" + _der_len(len(body)) + body


def encode_public_key(modulus: int, exponent: int) -> bytes:
    """编码成 SubjectPublicKeyInfo（和 pycryptodome 的 RSA.export_key 一致）。

    注意：所有长度都按实际字节数计算。RSA-1024 的模数 129 字节（含前导 0），
    BIT STRING 长度会进入长形式（0x81 0x8D），任何硬编码长度都会算错。
    """
    inner = _der_int(modulus) + _der_int(exponent)
    inner_seq = b"\x30" + _der_len(len(inner)) + inner

    # BIT STRING：首位 unused-bits 计数 0x00，其余为 DER 内容
    bit_string_body = b"\x00" + inner_seq
    bit_string = b"\x03" + _der_len(len(bit_string_body)) + bit_string_body

    body = _ALG_ID + bit_string
    return b"\x30" + _der_len(len(body)) + body


def _read_tlv(buf: bytes, offset: int) -> Tuple[int, bytes, int]:
    """读取一个 TLV，返回 (tag, value, 下一个偏移)。"""
    tag = buf[offset]
    offset += 1
    length = buf[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0:
            raise ValueError("不支持不定长 DER 编码")
        length = int.from_bytes(buf[offset:offset + count], "big")
        offset += count
    return tag, buf[offset:offset + length], offset + length


def decode_public_key(der: bytes) -> Tuple[int, int]:
    """从 SubjectPublicKeyInfo 中解出 (modulus, exponent)。

    按 ASN.1 结构逐层解析，不依赖任何硬编码偏移，任意密钥长度都能处理。
    """
    tag, outer, _ = _read_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("不是预期的 SubjectPublicKeyInfo 结构（外层不是 SEQUENCE）")

    pos = 0
    tag, alg, pos = _read_tlv(outer, pos)
    if tag != 0x30:
        raise ValueError("缺少 AlgorithmIdentifier")
    tag, bits, pos = _read_tlv(outer, pos)
    if tag != 0x03:
        raise ValueError("缺少 BIT STRING")
    if not bits or bits[0] != 0x00:
        raise ValueError("BIT STRING 含未使用位，不是标准 RSA 公钥")

    tag, inner, _ = _read_tlv(bits, 1)  # 跳过 unused-bits 计数
    if tag != 0x30:
        raise ValueError("BIT STRING 内不是 SEQUENCE")

    pos = 0
    tag, mod_bytes, pos = _read_tlv(inner, pos)
    if tag != 0x02:
        raise ValueError("缺少 modulus INTEGER")
    tag, exp_bytes, pos = _read_tlv(inner, pos)
    if tag != 0x02:
        raise ValueError("缺少 exponent INTEGER")
    return int.from_bytes(mod_bytes, "big"), int.from_bytes(exp_bytes, "big")


# --------------------------------------------------------------------------- #
# Base64 与大整数互转（正方公钥字段格式）
# --------------------------------------------------------------------------- #
def b64_to_int(value: str) -> int:
    """把 base64 编码的大端字节串转成整数。

    正方 `/xtgl/login_getPublicKey.html` 返回的 modulus 就是这种格式：
        int.from_bytes(base64.b64decode(modulus), 'big')
    """
    if value is None:
        raise ValueError("公钥字段为空")
    raw = str(value).strip()
    if not raw:
        raise ValueError("公钥字段为空")
    # 有的学校魔改成直接下发十六进制
    if len(raw) >= 8 and _is_hex(raw):
        try:
            return int(raw, 16)
        except ValueError:
            pass
    padded = raw + "=" * (-len(raw) % 4)
    try:
        data = base64.b64decode(padded, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"公钥字段不是合法的 base64：{exc}") from exc
    if not data:
        raise ValueError("公钥字段解码后为空")
    return int.from_bytes(data, byteorder="big")


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
        return True
    except ValueError:
        return False


def int_to_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return base64.b64encode(value.to_bytes(length, "big")).decode("ascii")


# --------------------------------------------------------------------------- #
# PKCS#1 v1.5 加密
# --------------------------------------------------------------------------- #
def pkcs1_v15_encrypt(public_key_der_pem_or_key, plaintext: bytes) -> bytes:
    """用 RSA 公钥做 PKCS#1 v1.5 加密，返回密文字节。

    参数接受：
      - 整数 (n, e) 的元组
      - 已构造的密钥对象（具备 n / e 属性）
      - encode_public_key 产出的 DER 字节
    """
    if isinstance(public_key_der_pem_or_key, tuple):
        n, e = public_key_der_pem_or_key
    elif isinstance(public_key_der_pem_or_key, (bytes, bytearray)):
        n, e = decode_public_key(bytes(public_key_der_pem_or_key))
    else:
        n, e = int(public_key_der_pem_or_key.n), int(public_key_der_pem_or_key.e)

    k = (n.bit_length() + 7) // 8
    if len(plaintext) > k - 11:
        raise ValueError(
            f"明文过长：{len(plaintext)} 字节，该 {k * 8} 位密钥最多 {k - 11} 字节"
        )

    # EB = 0x00 || 0x02 || PS(>=8字节非零随机) || 0x00 || M
    ps_len = k - len(plaintext) - 3
    padding = b""
    while len(padding) < ps_len:
        chunk = secrets.token_bytes(ps_len - len(padding))
        padding += chunk.replace(b"\x00", b"")  # 填充字节必须非零
    padding = padding[:ps_len]

    eb = b"\x00\x02" + padding + b"\x00" + plaintext
    m = int.from_bytes(eb, "big")
    if m >= n:
        raise ValueError("填充后的整数不小于模数，无法加密")
    c = pow(m, e, n)
    return c.to_bytes(k, "big")


def rsa_encrypt_password(password: str, modulus_b64: str, exponent_b64: str) -> str:
    """复刻正方 login.js：RSA/ECB/PKCS1Padding 加密密码后 base64 输出。

    对应 JS：
        var key = RSAUtils.getKeyPair(exponent, '', modulus);
        var mm  = RSAUtils.encryptedString(key, password);   // base64
    """
    n = b64_to_int(modulus_b64)
    e = b64_to_int(exponent_b64) if exponent_b64 else 65537
    if e <= 1:
        e = 65537
    if n <= 0:
        raise ValueError("公钥 modulus 非法")

    try:
        message = password.encode("utf-8")
    except UnicodeEncodeError:  # pragma: no cover
        message = password.encode("latin1")

    cipher = pkcs1_v15_encrypt((n, e), message)
    return base64.b64encode(cipher).decode("ascii")


# --------------------------------------------------------------------------- #
# 纯 Python PKCS#1 v1.5 解密（仅用于离线自测与互操作校验）
# --------------------------------------------------------------------------- #
def pkcs1_v15_decrypt(n: int, d: int, ciphertext: bytes) -> bytes:
    """用私钥指数 d 解密，返回去掉填充后的明文。测试用。"""
    k = (n.bit_length() + 7) // 8
    if len(ciphertext) != k:
        raise ValueError("密文长度与模数不匹配")
    c = int.from_bytes(ciphertext, "big")
    m = pow(c, d, n)
    eb = m.to_bytes(k, "big")
    if eb[0] != 0x00 or eb[1] != 0x02:
        raise ValueError("PKCS#1 v1.5 填充头非法")
    sep = eb.find(b"\x00", 2)
    if sep < 0:
        raise ValueError("PKCS#1 v1.5 填充缺少分隔符")
    if sep - 2 < 8:
        raise ValueError("PKCS#1 v1.5 填充过短")
    return eb[sep + 1:]
