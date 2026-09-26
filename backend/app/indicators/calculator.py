"""指标调度计算器（IndicatorCalculator）。

职责
----
1. **调度计算**：按指标名从注册中心取实例并计算，屏蔽具体指标差异；
2. **批量计算**：一次对多个指标求值，单个失败不影响整体；
3. **结果缓存**：以 Redis 作为二级缓存（L2），按「指标名 + 参数 + 数据指纹」为键；
4. **结果持久化**：将计算结果写入 ``indicator_values`` 表（PostgreSQL UPSERT）。

设计要点
--------
- **同步核心 + 异步外壳**：纯计算（:meth:`calculate_indicator` /
  :meth:`calculate_batch`）为同步方法，仅依赖 polars/numpy，可脱离事件循环与
  外部服务独立测试；Redis 缓存与数据库持久化通过异步方法
  （:meth:`acalculate_indicator` / :meth:`acalculate_batch`）叠加，且对
  ``redis`` / ``sqlalchemy`` / ``loguru`` 缺失或连接失败均**优雅降级**（不抛错）。
- **数据指纹**：缓存键包含输入 DataFrame 的 IPC 字节 SHA1，保证「数据变则缓存失效」。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import polars as pl

from .base import TIME_COLUMN, IndicatorResult
from .registry import registry as _default_registry
from .utils.math_helpers import expanding_percentile_rank

# ---- 可选依赖：loguru（缺失时退回标准 logging）----
try:  # pragma: no cover - 取决于运行环境
    from loguru import logger as _logger
except Exception:  # pragma: no cover
    import logging

    _logger = logging.getLogger("app.indicators.calculator")

__all__ = ["IndicatorCalculator", "calculator"]


# --------------------------------------------------------------------------- #
# 序列化辅助
# --------------------------------------------------------------------------- #
def _jsonable(obj: Any) -> Any:
    """将 metadata 中的非 JSON 原生类型（numpy 标量、datetime 等）转为可序列化对象。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    # numpy 标量 / 数组
    if hasattr(obj, "item"):
        try:
            return _jsonable(obj.item())
        except Exception:
            pass
    if hasattr(obj, "tolist"):
        try:
            return _jsonable(obj.tolist())
        except Exception:
            pass
    return str(obj)


def _finite_decimal(val: Any) -> Decimal | None:
    """将数值安全转为 ``Decimal``；None / NaN / Inf 返回 ``None``。"""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return Decimal(str(f))


def _df_to_ipc(df: pl.DataFrame) -> bytes:
    """DataFrame → Arrow IPC 字节（无损，含时间列）。"""
    buf = io.BytesIO()
    df.write_ipc(buf)
    return buf.getvalue()


def _ipc_to_df(raw: bytes) -> pl.DataFrame:
    """Arrow IPC 字节 → DataFrame。"""
    return pl.read_ipc(io.BytesIO(raw))


def _data_signature(data: pl.DataFrame) -> str:
    """计算输入数据指纹（列名 + 行数 + 内容 SHA1）。"""
    h = hashlib.sha1()
    h.update(repr(data.columns).encode("utf-8"))
    h.update(str(data.height).encode("utf-8"))
    try:
        h.update(_df_to_ipc(data))
    except Exception:
        # 退化：仅用列名与行数
        pass
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# IndicatorCalculator
# --------------------------------------------------------------------------- #
class IndicatorCalculator:
    """指标调度计算器。

    Parameters
    ----------
    redis_url:
        Redis 连接 URL；默认取 ``settings.redis_url``（不可用时自动降级为无缓存）。
    cache_ttl:
        缓存过期时间（秒），默认 3600。
    enable_cache:
        是否启用 Redis 缓存（异步方法生效），默认 True。
    registry:
        指标注册中心，默认使用全局单例。

    Examples
    --------
    同步纯计算（无需任何外部服务）::

        calc = IndicatorCalculator()
        res = calc.calculate_indicator("tech.rsi", df, period=14)

    异步缓存 + 持久化::

        res = await calc.acalculate_indicator(
            "tech.rsi", df, period=14,
            persist=True, indicator_id=uuid, source_id=uuid, symbol="BTC",
        )
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        cache_ttl: int = 3600,
        enable_cache: bool = True,
        registry: Any = None,
    ) -> None:
        self._registry = registry if registry is not None else _default_registry
        self.cache_ttl = int(cache_ttl)
        self.enable_cache = bool(enable_cache)
        self._redis_url = redis_url
        self._redis: Any | None = None
        self._redis_disabled = False
        # 命中率统计（便于观测）
        self.stats: dict[str, int] = {"hit": 0, "miss": 0, "error": 0, "persisted_rows": 0}

    # ------------------------------------------------------------------ #
    # 同步核心计算
    # ------------------------------------------------------------------ #
    def calculate_indicator(self, name: str, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算单个指标（同步，纯计算，无缓存/无持久化）。

        Parameters
        ----------
        name:
            指标唯一标识名（如 ``tech.rsi``）。
        data:
            输入 DataFrame（含时间列与所需特征列）。
        **params:
            覆盖指标默认参数。

        Returns
        -------
        IndicatorResult

        Raises
        ------
        KeyError
            指标未注册。
        """
        indicator = self._registry.get_indicator(name)
        return indicator.calculate(data, **params)

    def calculate_batch(
        self,
        names: Iterable[str],
        data: pl.DataFrame,
        *,
        params: dict[str, dict[str, Any]] | None = None,
        stop_on_error: bool = False,
    ) -> dict[str, IndicatorResult]:
        """批量计算多个指标（同步）。

        Parameters
        ----------
        names:
            指标名序列。
        data:
            共享输入 DataFrame。
        params:
            可选的「指标名 → 参数」映射，为特定指标定制参数。
        stop_on_error:
            遇到错误是否中断（默认 False，跳过失败项并记录）。

        Returns
        -------
        dict[str, IndicatorResult]
            成功计算的指标结果（失败项在 ``stop_on_error=False`` 时被跳过）。
        """
        per_params = params or {}
        results: dict[str, IndicatorResult] = {}
        for name in names:
            try:
                results[name] = self.calculate_indicator(name, data, **per_params.get(name, {}))
            except Exception as exc:  # noqa: BLE001 - 批处理需容错
                self.stats["error"] += 1
                _logger.error("批量计算指标 {} 失败: {!r}", name, exc)
                if stop_on_error:
                    raise
        return results

    # ------------------------------------------------------------------ #
    # 异步：缓存 + 持久化
    # ------------------------------------------------------------------ #
    async def acalculate_indicator(
        self,
        name: str,
        data: pl.DataFrame,
        *,
        use_cache: bool = True,
        persist: bool = False,
        symbol: str = "BTC",
        indicator_id: Any | None = None,
        source_id: Any | None = None,
        **params: Any,
    ) -> IndicatorResult:
        """计算单个指标（异步，叠加 Redis 缓存与可选持久化）。

        Parameters
        ----------
        use_cache:
            是否读写 Redis 缓存。
        persist:
            是否写入 ``indicator_values`` 表（需提供 ``indicator_id`` 与 ``source_id``，
            否则尝试按 ``name`` 从 ``indicator_definitions`` 解析 ``indicator_id``）。
        symbol:
            资产符号，默认 ``BTC``。
        indicator_id / source_id:
            持久化所需的外键 UUID。
        **params:
            指标计算参数。
        """
        cache_key = self._cache_key(name, data, params)

        # 1) 读缓存
        if use_cache and self.enable_cache:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                self.stats["hit"] += 1
                return cached

        self.stats["miss"] += 1
        # 2) 计算（CPU 密集但 polars 释放 GIL，直接调用即可）
        result = self.calculate_indicator(name, data, **params)

        # 3) 写缓存
        if use_cache and self.enable_cache:
            await self._cache_set(cache_key, result)

        # 4) 持久化
        if persist:
            try:
                await self.persist_result(
                    result, symbol=symbol, indicator_id=indicator_id, source_id=source_id
                )
            except Exception as exc:  # noqa: BLE001 - 持久化失败不应阻断计算返回
                _logger.error("持久化指标 {} 失败: {!r}", name, exc)

        return result

    async def acalculate_batch(
        self,
        names: Iterable[str],
        data: pl.DataFrame,
        *,
        params: dict[str, dict[str, Any]] | None = None,
        use_cache: bool = True,
        persist: bool = False,
        symbol: str = "BTC",
        source_id: Any | None = None,
        indicator_ids: dict[str, Any] | None = None,
    ) -> dict[str, IndicatorResult]:
        """批量计算（异步，并发调度，支持缓存与持久化）。

        单个指标失败不影响其余指标；失败项从返回字典中省略。
        """
        per_params = params or {}
        ids = indicator_ids or {}
        name_list = list(names)

        async def _one(nm: str) -> tuple[str, IndicatorResult | None]:
            try:
                res = await self.acalculate_indicator(
                    nm,
                    data,
                    use_cache=use_cache,
                    persist=persist,
                    symbol=symbol,
                    indicator_id=ids.get(nm),
                    source_id=source_id,
                    **per_params.get(nm, {}),
                )
                return nm, res
            except Exception as exc:  # noqa: BLE001
                self.stats["error"] += 1
                _logger.error("异步批量计算指标 {} 失败: {!r}", nm, exc)
                return nm, None

        pairs = await asyncio.gather(*[_one(nm) for nm in name_list])
        return {nm: res for nm, res in pairs if res is not None}

    # ------------------------------------------------------------------ #
    # 缓存键
    # ------------------------------------------------------------------ #
    def _cache_key(self, name: str, data: pl.DataFrame, params: dict[str, Any]) -> str:
        """构造缓存键：``ind:{name}:{sha1(params + data_signature)}``。"""
        canonical = json.dumps(_jsonable(params), sort_keys=True, ensure_ascii=False)
        sig = hashlib.sha1()
        sig.update(canonical.encode("utf-8"))
        sig.update(_data_signature(data).encode("utf-8"))
        return f"ind:{name}:{sig.hexdigest()}"

    # ------------------------------------------------------------------ #
    # Redis 缓存（优雅降级）
    # ------------------------------------------------------------------ #
    async def _get_redis(self) -> Any | None:
        """惰性获取 Redis 异步客户端；不可用时返回 None 并永久禁用缓存。"""
        if self._redis is not None:
            return self._redis
        if self._redis_disabled:
            return None
        try:
            import redis.asyncio as aioredis  # type: ignore

            url = self._redis_url
            if url is None:
                from app.core.config import settings  # 惰性导入，避免硬依赖

                url = settings.redis_url
            self._redis = aioredis.from_url(
                url,
                encoding="utf-8",
                decode_responses=False,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
                retry_on_timeout=False,
            )
            # 探活
            await self._redis.ping()
            return self._redis
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Redis 不可用，禁用指标缓存: {!r}", exc)
            self._redis = None
            self._redis_disabled = True
            return None

    async def _cache_get(self, key: str) -> IndicatorResult | None:
        client = await self._get_redis()
        if client is None:
            return None
        try:
            blob = await client.hgetall(key)
            if not blob:
                return None
            return self._deserialize(blob)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("读取指标缓存失败 key={}: {!r}", key, exc)
            return None

    async def _cache_set(self, key: str, result: IndicatorResult) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            blob = self._serialize(result)
            await client.hset(key, mapping=blob)
            await client.expire(key, self.cache_ttl)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("写入指标缓存失败 key={}: {!r}", key, exc)

    @staticmethod
    def _serialize(result: IndicatorResult) -> dict[str, bytes]:
        """IndicatorResult → Redis hash 字段（bytes）。"""
        meta = {
            "name": result.name,
            "metadata": _jsonable(result.metadata),
            "calculated_at": result.calculated_at.isoformat(),
        }
        return {
            b"df": _df_to_ipc(result.values),
            b"meta": json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        }

    @staticmethod
    def _deserialize(blob: dict[Any, Any]) -> IndicatorResult | None:
        """Redis hash 字段 → IndicatorResult。"""
        try:
            df_raw = blob.get(b"df") or blob.get("df")
            meta_raw = blob.get(b"meta") or blob.get("meta")
            if df_raw is None or meta_raw is None:
                return None
            meta = json.loads(meta_raw)
            return IndicatorResult(
                name=meta["name"],
                values=_ipc_to_df(df_raw),
                metadata=meta.get("metadata", {}),
                calculated_at=datetime.fromisoformat(meta["calculated_at"]),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("反序列化指标缓存失败: {!r}", exc)
            return None

    async def aclose(self) -> None:
        """关闭 Redis 连接。"""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                try:
                    await self._redis.close()  # 兼容旧版本 redis-py
                except Exception:
                    pass
            finally:
                self._redis = None

    # ------------------------------------------------------------------ #
    # 持久化：写入 indicator_values 表
    # ------------------------------------------------------------------ #
    async def persist_result(
        self,
        result: IndicatorResult,
        *,
        symbol: str = "BTC",
        indicator_id: Any | None = None,
        source_id: Any | None = None,
        quality_status: str = "VERIFIED",
    ) -> int:
        """将指标结果写入 ``indicator_values`` 表（PostgreSQL UPSERT）。

        Parameters
        ----------
        result:
            指标计算结果。
        symbol:
            资产符号。
        indicator_id:
            指标定义 UUID；缺省时按 ``result.name`` 从 ``indicator_definitions.code`` 解析。
        source_id:
            数据来源 Provider UUID（必填，无法解析）。
        quality_status:
            数据质量状态，默认 ``VERIFIED``。

        Returns
        -------
        int
            实际写入/更新的行数；无法持久化时返回 0。
        """
        if source_id is None:
            _logger.warning("persist_result 缺少 source_id，跳过持久化 ({})", result.name)
            return 0

        values = result.values
        if values.height == 0 or TIME_COLUMN not in values.columns:
            return 0

        # 时间列必须为真实 datetime 才能落库
        time_dtype = values.schema.get(TIME_COLUMN)
        if not (time_dtype == pl.Datetime or isinstance(time_dtype, pl.Datetime)):
            _logger.warning(
                "指标 {} 时间列非 datetime（{}），跳过持久化", result.name, time_dtype
            )
            return 0

        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from app.core.database import get_db_session_ctx
            from app.models.indicator import IndicatorDefinition, IndicatorValue
        except Exception as exc:  # noqa: BLE001 - 无 DB 依赖时降级
            _logger.warning("数据库依赖不可用，跳过持久化: {!r}", exc)
            return 0

        # 解析 indicator_id
        async with get_db_session_ctx() as session:
            if indicator_id is None:
                from sqlalchemy import select

                stmt = select(IndicatorDefinition.id).where(
                    IndicatorDefinition.code == result.name
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    _logger.warning(
                        "indicator_definitions 中未找到 code={}，跳过持久化", result.name
                    )
                    return 0
                indicator_id = row

            rows = self._build_persist_rows(result, symbol=symbol)
            if not rows:
                return 0

            # 补齐外键与质量状态
            for r in rows:
                r["indicator_id"] = indicator_id
                r["source_id"] = source_id
                r["quality_status"] = quality_status
                r["params_used"] = _jsonable(result.metadata.get("params", {}))

            stmt = pg_insert(IndicatorValue).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["indicator_id", "symbol", "observation_time"],
                set_={
                    "value": stmt.excluded.value,
                    "normalized_value": stmt.excluded.normalized_value,
                    "percentile": stmt.excluded.percentile,
                    "fetch_time": stmt.excluded.fetch_time,
                    "params_used": stmt.excluded.params_used,
                    "metadata_": stmt.excluded.metadata_,
                    "quality_status": stmt.excluded.quality_status,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)

        self.stats["persisted_rows"] += len(rows)
        _logger.info("持久化指标 {} 共 {} 行", result.name, len(rows))
        return len(rows)

    def _build_persist_rows(
        self, result: IndicatorResult, *, symbol: str
    ) -> list[dict[str, Any]]:
        """由 IndicatorResult 构造 indicator_values 行记录。

        - ``value``：主数值列；
        - ``percentile``：主列的扩展（Point-in-Time）百分位；
        - ``normalized_value``：percentile / 100（0-1 标准化）；
        - ``metadata``：附带整行其余数值列的快照。
        """
        values = result.values
        primary = result.primary_column
        if primary not in values.columns:
            return []

        times = values[TIME_COLUMN].to_list()
        primary_series = values[primary].cast(pl.Float64, strict=False)
        primary_arr = primary_series.to_list()

        # 扩展百分位（expanding_percentile_rank 已返回 0~100）
        try:
            pct_series = expanding_percentile_rank(primary_series, min_samples=2)
            pct_arr = pct_series.to_list()
        except Exception:  # noqa: BLE001
            pct_arr = [None] * len(primary_arr)

        extra_cols = [c for c in result.value_columns if c != primary]
        extra_map = {c: values[c].cast(pl.Float64, strict=False).to_list() for c in extra_cols}

        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for i, ts in enumerate(times):
            val = primary_arr[i]
            val_dec = _finite_decimal(val)
            if val_dec is None:
                continue  # null / NaN 值不落库
            pct_dec = _finite_decimal(pct_arr[i])
            norm_dec = (
                _finite_decimal(pct_arr[i] / 100.0) if pct_arr[i] is not None else None
            )
            row_meta: dict[str, Any] = {"category": result.metadata.get("category")}
            for c in extra_cols:
                cv = extra_map[c][i]
                if cv is not None and not (isinstance(cv, float) and cv != cv):
                    row_meta[c] = cv
            rows.append(
                {
                    "observation_time": ts,
                    "symbol": symbol,
                    "value": val_dec,
                    "normalized_value": norm_dec,
                    "percentile": pct_dec,
                    "fetch_time": now,
                    "updated_at": now,
                    "metadata_": _jsonable(row_meta),
                }
            )
        return rows


#: 全局计算器单例
calculator = IndicatorCalculator()
