"""敏感配置加密层（Fernet 对称加密）。

职责：
1. 为 system_settings / providers 表中的敏感值提供加解密能力；
2. 密钥来源：环境变量 ``SETTINGS_ENCRYPTION_KEY``（Fernet key，base64），
   未设置时从 ``SECRET_KEY`` 经 HKDF-SHA256 派生（开发环境零配置）；
3. 密文统一带 ``enc:v1:`` 前缀，便于与历史明文区分（存量明文兼容读取）；
4. 解密失败不抛异常：返回 None 并记录错误（不因单条坏数据崩溃）。

使用约定：
- ``encrypt_value`` 仅用于写入路径；输出可直接存 DB（Text/JSONB 字符串值）；
- ``decrypt_value`` 用于读取密文；读取可能为明文的存量数据用
  ``decrypt_if_encrypted``（带前缀才尝试解密，否则原样返回）。
"""

from __future__ import annotations

import base64
import hashlib
import threading

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from loguru import logger

#: 密文前缀（版本化，便于未来轮换密钥 / 算法）
CIPHER_PREFIX = "enc:v1:"

#: HKDF 派生盐（固定公开值；安全性依赖主密钥而非盐）
_HKDF_SALT = b"btc-platform/settings-encryption/v1"
_HKDF_INFO = b"fernet-key-derivation"

_fernet: Fernet | None = None
_fernet_lock = threading.Lock()


def _derive_fernet() -> Fernet:
    """构建 Fernet 实例：优先 SETTINGS_ENCRYPTION_KEY，否则从 SECRET_KEY 派生。"""
    from app.core.config import settings

    explicit = getattr(settings, "settings_encryption_key", "") or ""
    if explicit:
        try:
            # 允许直接粘贴 Fernet key（base64 urlsafe 32 字节）
            Fernet(explicit.encode())
            return Fernet(explicit.encode())
        except Exception as e:  # noqa: BLE001 - 配置错误降级为派生
            logger.error(f"SETTINGS_ENCRYPTION_KEY 非法（{e}），回退 SECRET_KEY 派生")

    secret = (settings.secret_key or "change-me-in-production").encode("utf-8")
    kdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    derived = kdf.derive(secret)
    key = base64.urlsafe_b64encode(derived)
    return Fernet(key)


def get_fernet() -> Fernet:
    """获取全局 Fernet 单例（线程安全，惰性构建）。"""
    global _fernet
    if _fernet is None:
        with _fernet_lock:
            if _fernet is None:
                _fernet = _derive_fernet()
    return _fernet


def reset_fernet() -> None:
    """重置全局 Fernet 单例（密钥配置变更 / 测试用）。"""
    global _fernet
    with _fernet_lock:
        _fernet = None


def is_encrypted(value: str | None) -> bool:
    """判断字符串是否为本模块产出的密文（带版本前缀）。"""
    return bool(value) and str(value).startswith(CIPHER_PREFIX)


def encrypt_value(plain: str | None) -> str:
    """加密明文 -> ``enc:v1:<fernet-token>``。

    Args:
        plain: 明文字符串（None/空串返回空串）

    Returns:
        密文字符串
    """
    if not plain:
        return ""
    token = get_fernet().encrypt(str(plain).encode("utf-8")).decode("ascii")
    return f"{CIPHER_PREFIX}{token}"


def decrypt_value(cipher: str | None) -> str | None:
    """解密 ``enc:v1:`` 密文；失败返回 None 并记录错误（不崩溃）。"""
    if not cipher:
        return None
    raw = str(cipher)
    if not raw.startswith(CIPHER_PREFIX):
        logger.error("decrypt_value: 输入不是有效密文格式（缺少 enc:v1: 前缀）")
        return None
    token = raw[len(CIPHER_PREFIX):]
    try:
        return get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("decrypt_value: 解密失败（密钥不匹配或数据损坏），返回 None")
        return None
    except Exception as e:  # noqa: BLE001 - 任何解密异常都不允许上抛
        logger.error(f"decrypt_value: 解密异常: {e}")
        return None


def decrypt_if_encrypted(value: str | None) -> str | None:
    """兼容读取：带前缀的密文解密；否则视为存量明文原样返回。"""
    if not value:
        return value
    raw = str(value)
    if not raw.startswith(CIPHER_PREFIX):
        return raw
    return decrypt_value(raw)


def mask_secret(value: str | None) -> str:
    """敏感值掩码（已配置 -> ``***``，空 -> 空串）。"""
    return "***" if value else ""


def fingerprint(value: str | None) -> str:
    """不可逆指纹（审计日志记录敏感值变更时用于区分新旧值是否变化）。"""
    if not value:
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


__all__ = [
    "CIPHER_PREFIX",
    "decrypt_if_encrypted",
    "decrypt_value",
    "encrypt_value",
    "fingerprint",
    "get_fernet",
    "is_encrypted",
    "mask_secret",
    "reset_fernet",
]
