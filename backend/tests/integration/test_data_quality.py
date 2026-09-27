"""数据质量集成测试（无 DB / Redis：DataChecker + QualityScorer 纯内存 + stub Provider）。

覆盖：
- 范围检查：price<=0 拒绝（HIGH）、百分比上下限、OHLC/bid-ask 一致性（CRITICAL）；
- 异常值检测：Z-score > 3 检出（>5 为 HIGH）、样本不足跳过、IQR 辅助判定；
- 连续性检查：时间跳跃（2x~3x 期望间隔 MEDIUM、>3x HIGH）；
- 重复数据检测：相同业务键标记 LOW；
- 质量评分：五维权重 30/25/20/15/10、加权求和、四级告警、自定义权重归一化、
  compute_from_checks 原始指标换算、改进建议生成；
- 多源交叉验证（MarketService）：偏差 > 0.5% 标记 CONFLICT、偏差小 → VERIFIED、
  单源跳过验证、cross_validate=False 关闭验证；
- run_all_checks 综合报告：干净数据 OK / 空数据 ERROR / 混合问题降级。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.engines.quality.checker import DataChecker, RANGE_DEFINITIONS
from app.engines.quality.scorer import (
    DIMENSION_WEIGHTS,
    QualityDimension,
    QualityLevel,
    QualityScorer,
)
from app.providers.base.config import ProviderConfig
from app.providers.base.provider import BaseProvider
from app.providers.base.registry import ProviderRegistry
from app.providers.base.types import (
    FetchResult,
    HealthState,
    ProviderLifecycleStatus,
    ProviderMetadata,
    QualityStatus,
)
from app.providers.manager import ProviderManager
from app.services.market_service import PRICE_DEVIATION_THRESHOLD_PCT, MarketService


class PriceStubProvider(BaseProvider):
    """返回固定价格的内存 Provider（用于交叉验证）。"""

    def __init__(self, name: str, *, price: float, priority: int = 10):
        config = ProviderConfig(name=name, category="market", priority=priority)
        super().__init__(config)
        self._status = ProviderLifecycleStatus.READY
        self._health_state = HealthState.ONLINE
        self._price = price

    async def health_check(self) -> bool:
        return True

    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(name=self.name, category=self.category)

    def get_supported_data_types(self) -> list[str]:
        return ["current_price"]

    async def get_current_price(self, **kwargs) -> FetchResult:
        return FetchResult(
            success=True,
            data={"symbol": kwargs.get("symbol", "BTC/USDT"), "price": self._price},
            provider_name=self.name,
        )


# ===========================================================================
# 范围检查
# ===========================================================================
class TestRangeCheck:
    """RANGE_DEFINITIONS 物理约束检查。"""

    def setup_method(self) -> None:
        self.checker = DataChecker()

    def test_zero_price_rejected(self):
        result = self.checker.check_range([{"price": 0}])
        assert result.records_failed == 1
        issue = result.issues[0]
        assert issue.field_name == "price"
        assert issue.severity == "HIGH"
        assert "低于最小值" in issue.message

    def test_negative_price_rejected(self):
        result = self.checker.check_range([{"price": -5.5}])
        assert result.records_failed == 1
        assert result.issues[0].value == -5.5

    def test_valid_price_passes(self):
        result = self.checker.check_range([{"price": 108000.5}])
        assert result.records_failed == 0
        assert result.records_passed == 1
        assert result.score == 100.0

    def test_negative_volume_rejected_medium(self):
        result = self.checker.check_range([{"volume": -1}])
        assert result.issues[0].field_name == "volume"
        assert result.issues[0].severity == "MEDIUM"

    def test_fear_greed_out_of_range(self):
        result = self.checker.check_range([{"fear_greed": 150}])
        assert result.issues[0].field_name == "fear_greed"
        assert "超过最大值" in result.issues[0].message

    def test_confidence_out_of_range(self):
        result = self.checker.check_range([{"confidence": 1.5}])
        assert any(i.field_name == "confidence" for i in result.issues)

    def test_unbounded_fields_pass(self):
        """funding_rate / net_flow 可正可负。"""
        assert self.checker.check_range([{"funding_rate": -0.01}]).records_failed == 0
        assert self.checker.check_range([{"net_flow_usd": -1e6}]).records_failed == 0

    def test_ohlc_high_lower_than_low_critical(self):
        result = self.checker.check_range(
            [{"open": 100, "high": 90, "low": 110, "close": 105}]
        )
        critical = [i for i in result.issues if i.severity == "CRITICAL"]
        assert critical and "high/low" in critical[0].field_name

    def test_ohlc_consistent_passes(self):
        result = self.checker.check_range(
            [{"open": 100, "high": 120, "low": 95, "close": 110}]
        )
        assert result.records_failed == 0

    def test_bid_ask_inversion_rejected(self):
        result = self.checker.check_range([{"bid": 101, "ask": 100}])
        assert any(i.field_name == "bid/ask" for i in result.issues)

    def test_multiple_records_partial_pass(self):
        records = [{"price": 100}, {"price": -1}, {"price": 200}]
        result = self.checker.check_range(records)
        assert result.records_checked == 3
        assert result.records_passed == 2
        assert result.records_failed == 1
        assert result.pass_rate == pytest.approx(2 / 3)

    def test_range_definitions_price_lower_bound(self):
        """price 的下限定义是 0（必须严格大于 0，实现上 < 0 才拒绝，=0 视为低于最小值）。"""
        min_val, max_val = RANGE_DEFINITIONS["price"]
        assert min_val is not None and min_val == 0
        assert max_val is None


# ===========================================================================
# 异常值检测（Z-score + IQR）
# ===========================================================================
class TestOutlierDetection:
    def setup_method(self) -> None:
        self.checker = DataChecker()

    def test_zscore_outlier_detected(self):
        """20 个正常值 + 1 个极端值 -> Z-score 检出。"""
        records = [{"price": 100.0} for _ in range(20)] + [{"price": 400.0}]
        result = self.checker.check_outliers(records, "price")
        assert len(result.issues) >= 1
        outlier = result.issues[0]
        assert outlier.check_type == "OUTLIER"
        assert outlier.severity == "MEDIUM"  # 3 < z <= 5
        assert outlier.record_id == "20"

    def test_zscore_above_5_is_high(self):
        """99 个 0 + 1 个 1000 -> z=9.9 > 5 -> HIGH。"""
        records = [{"price": 0.0} for _ in range(99)] + [{"price": 1000.0}]
        result = self.checker.check_outliers(records, "price")
        assert len(result.issues) == 1
        assert result.issues[0].severity == "HIGH"

    def test_small_sample_skipped(self):
        """样本 < 5 无法做统计检测。"""
        records = [{"price": 100.0}, {"price": 999999.0}]
        result = self.checker.check_outliers(records, "price")
        assert len(result.issues) == 0
        assert result.score == 100.0

    def test_zero_variance_skipped(self):
        """全部相同值（std=0）不产生误报。"""
        records = [{"price": 100.0} for _ in range(10)]
        result = self.checker.check_outliers(records, "price")
        assert len(result.issues) == 0

    def test_iqr_outlier_detected(self):
        """IQR 方法：极端值超出 Q3 + 1.5*IQR。"""
        records = [{"price": float(v)} for v in range(1, 10)] + [{"price": 100.0}]
        result = self.checker.check_outliers(records, "price")
        iqr_issues = [i for i in result.issues if "IQR" in i.message]
        assert len(iqr_issues) >= 1

    def test_custom_zscore_threshold(self):
        checker = DataChecker(zscore_threshold=10.0)  # 阈值调高
        records = [{"price": 0.0} for _ in range(99)] + [{"price": 1000.0}]
        result = checker.check_outliers(records, "price")
        assert len(result.issues) == 0  # z=9.9 < 10 不再检出


# ===========================================================================
# 连续性检查
# ===========================================================================
class TestContinuityCheck:
    def setup_method(self) -> None:
        self.checker = DataChecker()

    def _ts(self, minutes: int) -> dict:
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        return {"observation_time": base + timedelta(minutes=minutes)}

    def test_continuous_series_passes(self):
        records = [self._ts(m) for m in (0, 60, 120, 180)]
        result = self.checker.check_continuity(records, 3600)
        assert result.records_failed == 0
        assert result.score == 100.0

    def test_gap_detected_medium(self):
        """4h 间隔（期望 1h，2x~3x 范围）-> MEDIUM 跳跃。"""
        records = [self._ts(m) for m in (0, 60, 120, 360)]
        result = self.checker.check_continuity(records, 3600)
        assert result.records_failed == 1
        assert result.issues[0].severity == "MEDIUM"

    def test_big_gap_high(self):
        """8h 间隔（> 3x）-> HIGH 跳跃。"""
        records = [self._ts(m) for m in (0, 60, 540)]
        result = self.checker.check_continuity(records, 3600)
        assert result.issues[0].severity == "HIGH"

    def test_unsorted_input_handled(self):
        """乱序输入自动排序后检查。"""
        records = [self._ts(m) for m in (120, 0, 60)]
        result = self.checker.check_continuity(records, 3600)
        assert result.records_failed == 0

    def test_insufficient_points_skipped(self):
        records = [self._ts(0)]
        result = self.checker.check_continuity(records, 3600)
        assert "不足" in result.message
        assert result.score == 100.0


# ===========================================================================
# 重复数据检测
# ===========================================================================
class TestDuplicateCheck:
    def setup_method(self) -> None:
        self.checker = DataChecker()

    def test_duplicate_detected(self):
        t = datetime(2026, 1, 1, tzinfo=UTC)
        records = [
            {"symbol": "BTC/USDT", "observation_time": t, "source_id": 1, "price": 100},
            {"symbol": "BTC/USDT", "observation_time": t, "source_id": 1, "price": 100},
        ]
        result = self.checker.check_duplicates(records, "price", "BTC/USDT")
        assert result.records_failed == 1
        assert result.issues[0].severity == "LOW"

    def test_distinct_records_pass(self):
        t = datetime(2026, 1, 1, tzinfo=UTC)
        records = [
            {"symbol": "BTC/USDT", "observation_time": t, "source_id": 1},
            {"symbol": "BTC/USDT", "observation_time": t, "source_id": 2},
        ]
        result = self.checker.check_duplicates(records, "price", "BTC/USDT")
        assert result.records_failed == 0


# ===========================================================================
# 质量评分（五维加权 30/25/20/15/10）
# ===========================================================================
class TestQualityScorerWeights:
    def test_default_weights_documented_values(self):
        assert DIMENSION_WEIGHTS[QualityDimension.COMPLETENESS] == 0.30
        assert DIMENSION_WEIGHTS[QualityDimension.ACCURACY] == 0.25
        assert DIMENSION_WEIGHTS[QualityDimension.CONSISTENCY] == 0.20
        assert DIMENSION_WEIGHTS[QualityDimension.TIMELINESS] == 0.15
        assert DIMENSION_WEIGHTS[QualityDimension.VALIDITY] == 0.10

    def test_weights_sum_to_one(self):
        assert sum(DIMENSION_WEIGHTS.values()) == pytest.approx(1.0)

    def test_scorer_uses_documented_weights(self):
        scorer = QualityScorer()
        weights = scorer.weights
        assert weights["completeness"] == pytest.approx(0.30)
        assert weights["accuracy"] == pytest.approx(0.25)
        assert weights["consistency"] == pytest.approx(0.20)
        assert weights["timeliness"] == pytest.approx(0.15)
        assert weights["validity"] == pytest.approx(0.10)

    def test_custom_weights_normalized(self):
        scorer = QualityScorer(custom_weights={"completeness": 0.6})
        # 0.6 / (0.6+0.25+0.2+0.15+0.1) = 0.6 / 1.3
        assert scorer.weights["completeness"] == pytest.approx(0.6 / 1.3)
        assert sum(scorer.weights.values()) == pytest.approx(1.0)

    def test_custom_weights_invalid_key_ignored(self):
        scorer = QualityScorer(custom_weights={"nonexistent_dim": 5.0})
        assert scorer.weights["completeness"] == pytest.approx(0.30)


class TestQualityScorerCompute:
    def setup_method(self) -> None:
        self.scorer = QualityScorer()

    def _score(self, **pcts) -> float:
        return self.scorer.compute_score("price", "BTC/USDT", **pcts)

    def test_all_perfect(self):
        score = self._score()
        assert score.total_score == pytest.approx(100.0)
        assert score.level == QualityLevel.EXCELLENT
        assert score.is_healthy is True
        assert score.needs_attention is False

    def test_weighted_sum_single_low_dimension(self):
        """completeness=50 其余 100 -> 100 - 0.30*50 = 85。"""
        score = self._score(completeness_pct=50.0)
        assert score.total_score == pytest.approx(85.0)
        dims = {d.dimension: d for d in score.dimensions}
        assert dims[QualityDimension.COMPLETENESS].weighted_score == pytest.approx(15.0)

    def test_level_boundaries(self):
        # 100 -> EXCELLENT
        assert self._score().level == QualityLevel.EXCELLENT
        # 0.3*80+25+20+15+10 = 94 -> EXCELLENT
        assert self._score(completeness_pct=80.0).level == QualityLevel.EXCELLENT
        # 0.3*50+25+20+15+10 = 85 -> GOOD
        assert self._score(completeness_pct=50.0).level == QualityLevel.GOOD
        # 全 60 -> WARNING
        all_60 = self._score(
            completeness_pct=60.0, accuracy_pct=60.0, consistency_pct=60.0,
            timeliness_pct=60.0, validity_pct=60.0,
        )
        assert all_60.total_score == pytest.approx(60.0)
        assert all_60.level == QualityLevel.WARNING
        # 全 50 -> CRITICAL（50 不 > 50）
        all_50 = self._score(
            completeness_pct=50.0, accuracy_pct=50.0, consistency_pct=50.0,
            timeliness_pct=50.0, validity_pct=50.0,
        )
        assert all_50.level == QualityLevel.CRITICAL

    def test_scores_clamped(self):
        """越界输入被 clamp 到 [0, 100]。"""
        score = self._score(completeness_pct=150.0, accuracy_pct=-20.0)
        dims = {d.dimension: d for d in score.dimensions}
        assert dims[QualityDimension.COMPLETENESS].score == 100.0
        assert dims[QualityDimension.ACCURACY].score == 0.0

    def test_recommendations_for_low_dimensions(self):
        score = self._score(consistency_pct=40.0, timeliness_pct=30.0)
        assert any("一致性" in r or "交叉验证" in r for r in score.recommendations)
        assert any("时效" in r or "同步" in r for r in score.recommendations)

    def test_critical_level_inserts_urgent_recommendation(self):
        score = self._score(
            completeness_pct=10.0, accuracy_pct=10.0, consistency_pct=10.0,
            timeliness_pct=10.0, validity_pct=10.0,
        )
        assert score.level == QualityLevel.CRITICAL
        assert score.recommendations
        assert "立即" in score.recommendations[0]

    def test_to_dict_serializable(self):
        score = self._score(completeness_pct=88.0)
        d = score.to_dict()
        assert d["data_type"] == "price"
        assert d["level"] == score.level.value
        assert len(d["dimensions"]) == 5


class TestComputeFromChecks:
    def setup_method(self) -> None:
        self.scorer = QualityScorer()

    def test_metric_conversion(self):
        score = self.scorer.compute_from_checks(
            data_type="candles",
            symbol="BTCUSDT",
            total_expected=100,
            total_found=95,           # completeness = 95
            outlier_count=5,          # accuracy = (1 - 5/95)*100 ≈ 94.74
            conflict_count=10,        # consistency = (1 - 10/95)*100 ≈ 89.47
            last_update_seconds_ago=60,
            expected_update_interval_seconds=60,  # timeliness = 100
            invalid_count=2,          # validity = (1 - 2/95)*100 ≈ 97.89
        )
        dims = {d.dimension: d.score for d in score.dimensions}
        assert dims[QualityDimension.COMPLETENESS] == pytest.approx(95.0)
        assert dims[QualityDimension.ACCURACY] == pytest.approx((1 - 5 / 95) * 100)
        assert dims[QualityDimension.CONSISTENCY] == pytest.approx((1 - 10 / 95) * 100)
        assert dims[QualityDimension.TIMELINESS] == pytest.approx(100.0)
        assert dims[QualityDimension.VALIDITY] == pytest.approx((1 - 2 / 95) * 100)

    def test_timeliness_degradation_curve(self):
        """更新延迟 1-2 倍线性下降、2-5 倍加速下降。"""
        common = dict(
            data_type="price", symbol="BTC/USDT", total_expected=100, total_found=100,
            outlier_count=0, conflict_count=0, invalid_count=0,
            expected_update_interval_seconds=60,
        )
        on_time = self.scorer.compute_from_checks(last_update_seconds_ago=60, **common)
        late_1_5x = self.scorer.compute_from_checks(last_update_seconds_ago=90, **common)
        late_3x = self.scorer.compute_from_checks(last_update_seconds_ago=180, **common)
        very_late = self.scorer.compute_from_checks(last_update_seconds_ago=600, **common)

        dims = lambda s: {  # noqa: E731
            d.dimension: d.score for d in s.dimensions
        }[QualityDimension.TIMELINESS]

        assert dims(on_time) == pytest.approx(100.0)
        assert 50.0 < dims(late_1_5x) < 100.0
        assert 5.0 < dims(late_3x) < dims(late_1_5x)
        assert dims(very_late) == pytest.approx(5.0)

    def test_timeliness_unknown_gives_mid_score(self):
        score = self.scorer.compute_from_checks(
            data_type="price", symbol="BTC/USDT", total_expected=100, total_found=100,
            outlier_count=0, conflict_count=0, invalid_count=0,
            last_update_seconds_ago=None, expected_update_interval_seconds=60,
        )
        dims = {d.dimension: d.score for d in score.dimensions}
        assert dims[QualityDimension.TIMELINESS] == pytest.approx(50.0)

    def test_zero_expected_completeness_zero(self):
        score = self.scorer.compute_from_checks(
            data_type="price", symbol="BTC/USDT", total_expected=0, total_found=0,
            outlier_count=0, conflict_count=0, invalid_count=0,
            last_update_seconds_ago=None, expected_update_interval_seconds=60,
        )
        dims = {d.dimension: d.score for d in score.dimensions}
        assert dims[QualityDimension.COMPLETENESS] == pytest.approx(0.0)


# ===========================================================================
# 多源交叉验证（偏差 > 0.5% -> CONFLICT）
# ===========================================================================
class TestCrossValidation:
    @pytest.fixture
    def registry(self):
        ProviderRegistry.reset_instance()
        reg = ProviderRegistry()
        yield reg
        ProviderRegistry.reset_instance()

    def _make_service(self, registry, prices: dict[str, float]) -> MarketService:
        for i, (name, price) in enumerate(prices.items()):
            registry.register(PriceStubProvider(name, price=price, priority=(i + 1) * 10))
        manager = ProviderManager(registry)
        return MarketService(manager)  # cache=None 禁用 Redis 缓存

    @pytest.mark.asyncio
    async def test_deviation_below_threshold_verified(self, registry):
        """两源偏差 ~0.2% < 0.5% -> VERIFIED。"""
        service = self._make_service(registry, {"primary": 100000.0, "backup": 100200.0})
        result = await service.get_current_price("BTCUSDT")

        assert result.success is True
        assert result.quality_status == QualityStatus.VERIFIED
        validation = result.metadata["cross_validation"]
        assert validation["sources"] >= 2
        assert validation["max_deviation_pct"] is not None
        assert validation["max_deviation_pct"] <= PRICE_DEVIATION_THRESHOLD_PCT

    @pytest.mark.asyncio
    async def test_deviation_above_threshold_conflict(self, registry):
        """两源偏差 ~0.8% > 0.5% -> CONFLICT。"""
        service = self._make_service(registry, {"primary": 100000.0, "backup": 100800.0})
        result = await service.get_current_price("BTCUSDT")

        assert result.success is True
        assert result.quality_status == QualityStatus.CONFLICT
        validation = result.metadata["cross_validation"]
        assert validation["max_deviation_pct"] > PRICE_DEVIATION_THRESHOLD_PCT

    @pytest.mark.asyncio
    async def test_single_source_skips_validation(self, registry):
        """可用源 < 2 -> 跳过验证，质量状态保持 Provider 原值。"""
        service = self._make_service(registry, {"primary": 100000.0})
        result = await service.get_current_price("BTCUSDT")

        assert result.success is True
        assert result.quality_status == QualityStatus.VERIFIED
        validation = result.metadata.get("cross_validation", {})
        assert validation.get("sources", 0) == 0
        assert validation.get("max_deviation_pct") is None

    @pytest.mark.asyncio
    async def test_cross_validate_disabled(self, registry):
        """cross_validate=False -> 不执行验证。"""
        service = self._make_service(registry, {"primary": 100000.0, "backup": 100800.0})
        result = await service.get_current_price("BTCUSDT", cross_validate=False)

        assert result.success is True
        assert "cross_validation" not in result.metadata

    @pytest.mark.asyncio
    async def test_validation_with_multiple_sources_takes_max_deviation(self, registry):
        """三源验证取最大偏差。"""
        service = self._make_service(
            registry, {"p1": 100000.0, "p2": 100100.0, "p3": 100900.0}
        )
        result = await service.get_current_price("BTCUSDT")

        validation = result.metadata["cross_validation"]
        assert validation["sources"] == 3
        # (100900-100000)/100450*100 ≈ 0.896% > 0.5% -> CONFLICT
        assert result.quality_status == QualityStatus.CONFLICT


# ===========================================================================
# run_all_checks 综合报告
# ===========================================================================
class TestRunAllChecks:
    def setup_method(self) -> None:
        self.checker = DataChecker()

    def test_clean_data_full_score(self):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        records = [
            {"price": 100000.0, "volume": 10.0, "observation_time": base + timedelta(hours=i)}
            for i in range(10)
        ]
        report = self.checker.run_all_checks(
            records, "price", "BTC/USDT", expected_interval_seconds=3600
        )
        assert report.overall_status == "OK"
        assert report.overall_score == pytest.approx(100.0)
        assert report.critical_issues == 0
        check_types = {c.check_type for c in report.checks}
        assert {"RANGE", "OUTLIER", "CONTINUITY", "DUPLICATE"} <= check_types

    def test_empty_records_error(self):
        report = self.checker.run_all_checks([], "price", "BTC/USDT")
        assert report.overall_status == "ERROR"
        assert report.overall_score == 0.0
        assert report.checks[0].check_type == "EMPTY"

    def test_mixed_issues_degrade_score(self):
        """负价格（HIGH）+ 大跳跃（HIGH）-> 综合分下降、状态降级。"""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        records = [
            {"price": 100000.0, "observation_time": base},
            {"price": -1.0, "observation_time": base + timedelta(hours=1)},  # 范围违规
            {"price": 100000.0, "observation_time": base + timedelta(hours=9)},  # 8h 跳跃
        ]
        report = self.checker.run_all_checks(
            records, "price", "BTC/USDT", expected_interval_seconds=3600
        )
        assert report.overall_score < 100.0
        assert report.overall_status in ("WARNING", "ERROR", "CRITICAL")
        assert report.high_issues >= 2

    def test_report_to_dict(self):
        report = self.checker.run_all_checks(
            [{"price": 100.0, "observation_time": datetime(2026, 1, 1, tzinfo=UTC)}],
            "price",
            "BTC/USDT",
        )
        d = report.to_dict()
        assert d["data_type"] == "price"
        assert d["symbol"] == "BTC/USDT"
        assert isinstance(d["checks"], list) and d["checks"]
