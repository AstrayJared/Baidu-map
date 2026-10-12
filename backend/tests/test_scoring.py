"""§7.1 固定分母、覆盖区间分与可评估率；§11.3 门槛 1 的面积一致性。"""
import math

import pytest

from app.scoring import (CATEGORY_WEIGHT, MAJOR_CATEGORIES, CategoryAreas, CategoryScore, OverallScore,
                         OverallUnavailable, ScoringError, area_tolerance, category_score,
                         interval_degenerates, overall_score)


def score(category: str, *, covered: float, gap: float, unknown: float,
          domain: float = 100.0, spatial_support: bool = True) -> CategoryScore:
    return category_score(category, CategoryAreas(covered, gap, unknown),
                          domain_area_m2=domain, spatial_support=spatial_support)


def full_scores(overrides):
    return {category: overrides.get(category, score(category, covered=100, gap=0, unknown=0))
            for category in MAJOR_CATEGORIES}


def test_spec_example_is_reported_as_an_interval_and_never_as_full_coverage():
    """方案 §7.1 的算例：A=100, C=50, G=0, U=50 → 50—100，可评估率 50%。"""
    item = score("shopping", covered=50, gap=0, unknown=50)
    assert item.supported is True
    assert item.coverage_lower_pct == 50.0
    assert item.coverage_upper_pct == 100.0
    assert item.assessable_pct == 50.0
    assert item.unknown_pct == 50.0
    # 未知面积既不算覆盖也不算缺口：下界不包含它，可评估率也不包含它。
    assert item.coverage_lower_pct != item.coverage_upper_pct
    assert not interval_degenerates(item)


def test_unknown_area_never_becomes_coverage_or_gap():
    item = score("medical", covered=30, gap=20, unknown=50)
    assert item.coverage_lower_pct == 30.0
    assert item.coverage_upper_pct == 80.0
    assert item.assessable_pct == 50.0
    assert item.interval_width_pct == 50.0


def test_interval_collapses_to_a_point_when_no_area_is_unknown():
    item = score("education", covered=70, gap=30, unknown=0)
    assert item.coverage_lower_pct == item.coverage_upper_pct == 70.0
    assert item.assessable_pct == 100.0
    assert interval_degenerates(item)


def test_no_midpoint_or_single_total_is_ever_produced():
    """§7.1：不显示人为取中点的单一总分。"""
    fields = set(CategoryScore.__dataclass_fields__)
    assert not {name for name in fields if "mid" in name or "average" in name}
    item = score("shopping", covered=50, gap=0, unknown=50)
    assert not any(value == 75.0 for name, value in vars(item).items()
                   if name.endswith("_pct") and isinstance(value, float))


def test_domain_area_must_be_the_declared_denominator_and_the_identity_must_hold():
    """分母是冻结的评估域，不能用 C＋G＋U 反过来凑；不符即报错，不静默归一化。"""
    with pytest.raises(ScoringError):
        score("shopping", covered=50, gap=0, unknown=48, domain=100.0)
    with pytest.raises(ScoringError):
        score("shopping", covered=0, gap=0, unknown=0, domain=0.0)
    # 同一个 C，未知面积被悄悄删掉会破坏恒等式，因此不能借此抬高分数。
    with pytest.raises(ScoringError):
        score("shopping", covered=50, gap=0, unknown=0, domain=100.0)


def test_area_tolerance_follows_the_acceptance_threshold():
    """§11.3 门槛 1：误差不超过 max(1 m², A×10⁻⁶)。"""
    assert area_tolerance(100.0) == 1.0
    assert area_tolerance(1_000_000.0) == 1.0
    assert area_tolerance(1e9) == pytest.approx(1000.0)
    domain = 1_000_000.0
    # 容差内接受（含正好等于容差的边界），容差外拒绝。
    inside = score("shopping", covered=500_000.0, gap=0.0, unknown=499_999.5, domain=domain)
    assert inside.coverage_lower_pct == pytest.approx(50.0)
    edge = score("shopping", covered=500_000.0, gap=0.0, unknown=499_999.0, domain=domain)
    assert edge.coverage_upper_pct == pytest.approx(100.0 * 999_999.0 / domain)
    with pytest.raises(ScoringError):
        score("shopping", covered=500_000.0, gap=0.0, unknown=499_998.0, domain=domain)


def test_a_category_without_spatial_support_reports_no_percentage_at_all():
    """§6.4：缺图时面积覆盖显示无法确定，不能显示成 0% 覆盖或 100% 未知。"""
    item = score("shopping", covered=0, gap=0, unknown=400.0, domain=400.0,
                 spatial_support=False)
    assert item.supported is False
    assert item.coverage_lower_pct is None and item.coverage_upper_pct is None
    assert item.assessable_pct is None and item.unknown_pct is None
    assert item.unavailable_reason == "no_spatial_support"


def test_overall_score_weights_all_categories_equally():
    scores = full_scores({"shopping": score("shopping", covered=50, gap=0, unknown=50),
              "medical": score("medical", covered=80, gap=20, unknown=0),
              "education": score("education", covered=20, gap=80, unknown=0)})
    overall = overall_score(scores, expected_categories=MAJOR_CATEGORIES)
    assert isinstance(overall, OverallScore)
    assert CATEGORY_WEIGHT == pytest.approx(1 / 3)
    assert overall.coverage_lower_pct == pytest.approx((50 + 80 + 20 + 700) / 10)
    assert overall.coverage_upper_pct == pytest.approx((100 + 80 + 20 + 700) / 10)
    assert overall.assessable_pct == pytest.approx((50 + 100 + 100 + 700) / 10)
    assert overall.unknown_pct == pytest.approx(50 / 10)
    assert overall.weights == {category: pytest.approx(1 / 10) for category in MAJOR_CATEGORIES}


def test_partial_categories_are_not_renamed_into_a_three_category_total():
    """§7.1：仅分析部分大类时只显示类别区间，不给总体分。"""
    scores = {"shopping": score("shopping", covered=50, gap=0, unknown=50),
              "medical": score("medical", covered=80, gap=20, unknown=0)}
    overall = overall_score(scores, expected_categories=MAJOR_CATEGORIES)
    assert isinstance(overall, OverallUnavailable)
    assert overall.reason == "categories_not_analysed"
    assert overall.missing_categories == tuple(category for category in MAJOR_CATEGORIES
                                                if category not in scores)


def test_one_category_without_support_withholds_the_overall_interval():
    scores = full_scores({"shopping": score("shopping", covered=50, gap=0, unknown=50),
              "medical": score("medical", covered=80, gap=20, unknown=0),
              "education": score("education", covered=0, gap=0, unknown=100,
                                 spatial_support=False)})
    overall = overall_score(scores, expected_categories=MAJOR_CATEGORIES)
    assert isinstance(overall, OverallUnavailable)
    assert overall.reason == "category_without_spatial_support"
    assert overall.missing_categories == ("education",)


def test_all_ten_supported_degenerate_interval_equals_the_point_score():
    scores = full_scores({"shopping": score("shopping", covered=60, gap=40, unknown=0),
              "medical": score("medical", covered=90, gap=10, unknown=0),
              "education": score("education", covered=30, gap=70, unknown=0)})
    overall = overall_score(scores, expected_categories=MAJOR_CATEGORIES)
    assert isinstance(overall, OverallScore)
    assert math.isclose(overall.coverage_lower_pct, overall.coverage_upper_pct)
    assert math.isclose(overall.coverage_lower_pct, (60 + 90 + 30 + 700) / 10)


def test_explicit_analysis_scope_controls_the_denominator():
    scores = {
        "shopping": score("shopping", covered=60, gap=40, unknown=0),
        "medical": score("medical", covered=90, gap=10, unknown=0),
        "care": score("care", covered=20, gap=80, unknown=0),
        "transport": score("transport", covered=10, gap=90, unknown=0),
    }
    overall = overall_score(scores, expected_categories=("shopping", "medical", "care", "transport"))
    assert isinstance(overall, OverallScore)
    assert overall.categories == ("shopping", "medical", "care", "transport")
    assert overall.weights == {category: pytest.approx(0.25) for category in overall.categories}
    assert overall.coverage_lower_pct == pytest.approx(45.0)
