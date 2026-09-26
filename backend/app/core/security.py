"""安全工具：密码哈希、JWT 令牌签发/校验、可选认证依赖。

MVP 阶段不强制鉴权：:func:`get_current_user` 为「可选认证」依赖 ——
携带有效 Bearer Token 时返回用户，否则返回 ``None``（不抛 401），
供需要区分登录态的端点使用。

约定：
- access token 默认有效期 ``settings.access_token_expire_minutes``（24h）
- refresh token 固定 7 天，payload 带 ``type=refresh``
- 密码哈希使用 bcrypt（passlib）
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext
from sqlalchemy import select
from starlette.requests import Request

from app.core.config import settings
from app.core.database import get_db_session_ctx


def _patch_bcrypt_backend() -> None:
    """passlib 1.7.4 与 bcrypt>=4.1 的运行时兼容补丁。

    bcrypt>=4.1 移除了 ``__about__`` 且 ``hashpw`` 对超过 72 字节的密码抛出
    ValueError（历史语义为静默截断），导致 passlib 后端探测崩溃。
    此处恢复「静默截断到 72 字节」的历史语义并补回 ``__about__``，
    使 CryptContext(bcrypt) 正常工作；可安全重复调用。
    """
    try:
        import bcrypt as _bcrypt_mod
    except Exception:  # pragma: no cover - 未安装 bcrypt 时交由 passlib 报错
        return

    if getattr(_bcrypt_mod, "_passlib_compat_patched", False):
        return

    if not hasattr(_bcrypt_mod, "__about__"):
        try:
            _bcrypt_mod.__about__ = type(
                "_BcryptAbout",
                (),
                {"__version__": getattr(_bcrypt_mod, "__version__", "unknown")},
            )()
        except Exception:  # noqa: BLE001 - 仅用于消除版本探测告警
            pass

    orig_hashpw = _bcrypt_mod.hashpw

    def _hashpw_compat(secret, config):  # noqa: ANN001 - 保持 bcrypt 原签名
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        return orig_hashpw(secret[:72], config)

    _bcrypt_mod.hashpw = _hashpw_compat
    _bcrypt_mod._passlib_compat_patched = True  # type: ignore[attr-defined]


_patch_bcrypt_backend()

# bcrypt 单次最大 72 字节（超长部分由补丁静默截断，与历史语义一致）
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__truncate_error=False)

#: refresh token 固定有效期（天）
REFRESH_TOKEN_EXPIRE_DAYS = 7

__all__ = [
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_current_user",
    "hash_password",
    "verify_password",
]


# ---- 密码哈希 ----

def hash_password(password: str) -> str:
    """明文密码 -> bcrypt 哈希。"""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配（异常一律返回 False）。"""
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:  # noqa: BLE001 - 哈希格式非法等场景
        return False


# ---- JWT 签发 / 校验 ----

def _create_token(user_id: str | UUID, token_type: str, expires_delta: timedelta) -> str:
    """签发 JWT（内部通用实现）。"""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def create_access_token(user_id: str | UUID, expires_delta: timedelta | None = None) -> str:
    """签发 access token（默认有效期取 settings.access_token_expire_minutes）。"""
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    return _create_token(user_id, "access", expires_delta)


def create_refresh_token(user_id: str | UUID) -> str:
    """签发 refresh token（固定 7 天）。"""
    return _create_token(user_id, "refresh", timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))


def decode_token(token: str) -> dict[str, Any] | None:
    """解码并校验 JWT。

    Returns:
        payload 字典；过期、签名错误或格式非法返回 None（不抛异常）。
    """
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as e:
        logger.debug(f"JWT decode failed: {e}")
        return None
    except Exception as e:  # noqa: BLE001 - 任意解码异常均视为无效令牌
        logger.debug(f"JWT decode unexpected error: {e}")
        return None


# ---- FastAPI 依赖 ----

async def get_current_user(request: Request) -> Any:
    """可选认证依赖：有有效 access token 返回 User ORM 对象，否则返回 None。

    从 ``Authorization: Bearer <token>`` 头解析，数据库查询失败时静默降级为 None
    （MVP 不强制登录，鉴权失败不阻断匿名访问）。
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    if not token:
        return None

    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    sub = payload.get("sub")
    if not sub:
        return None

    try:
        user_uuid = UUID(str(sub))
    except (ValueError, TypeError):
        return None

    try:
        from app.models import User

        async with get_db_session_ctx() as session:
            row = await session.execute(select(User).where(User.id == user_uuid))
            user = row.scalar_one_or_none()
        if user is not None and not getattr(user, "is_active", True):
            return None
        return user
    except Exception as e:  # noqa: BLE001 - DB 不可用时降级为未认证
        logger.warning(f"get_current_user DB query failed: {e}")
        return None
