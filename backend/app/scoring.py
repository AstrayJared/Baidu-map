"""§7.1 固定分母、覆盖区间分与可评估率。

这一层是纯算术：它只认识"某个类别在冻结评估域内各占多少面积"，不认识网格、路网或
百度核验是怎么来的。三条口径写在这里，而不是散在报告模板里：

* **分母固定且必须相加成立。** 评估域面积 A 由调用方给定并冻结，本模块不反过来用
  ``C + G + U`` 去凑 A —— 否则删掉未评估的格子就能把分数抬上去（§7.1）。
  ``C + G + U = A`` 必须成立；不成立是调用方的实现错误，直接报错，不静默归一化。
* **未知既不算覆盖也不算缺口。** 覆盖率因此是一个区间：下界只认 C，上界把未知也
  算作可能覆盖。全部类别都没有未知面积时区间自然退化为一个点，不另外产出一个"总分"。
* **部分类别不冒充总体。** 只有目录全部大类都参与并且都有空间支持时才有总体区间分；只分析
  一部分类别时总体分是"无法给出"，而不是把已分析类别重新加权成全部类别总分。

这些区间以当前目录与路网模型为前提，不是统计置信区间，也不包含目录本身遗漏的现实
设施；没有人口数据就不输出人口覆盖率，设施数量也不代表容量或政策准入（§7.1）。
"""
from collections.abc import Iterable
from dataclasses import dataclass

from .catalog import default_analysis_majors, majors
from .contracts import MajorCategory

MAJOR_CATEGORIES: tuple[MajorCategory, ...] = tuple(majors())
DEFAULT_SCORING_CATEGORIES: tuple[MajorCategory, ...] = tuple(default_analysis_majors())

# §7.1：三大类各占 1/3；只有三大类齐备时才有总体分。
CATEGORY_WEIGHT = 1.0 / len(DEFAULT_SCORING_CATEGORIES)


def category_weights(categories: Iterable[str]) -> dict[str, float]:
    """Return equal weights for exactly the categories in one analysis scope."""
    selected = tuple(dict.fromkeys(categories))
    if not selected:
        return {}
    weight = 1.0 / len(selected)
    return {category: weight for category in selected}

# §11.3 门槛 1：每类别 C＋G＋U 与评估域 A 的误差不超过 max(1 m², A×10⁻⁶)。
RELATIVE_AREA_TOLERANCE = 1e-6
ABSOLUTE_AREA_TOLERANCE_M2 = 1.0


class ScoringError(ValueError):
    """The areas handed in cannot be scored as a fixed-denominator partition."""


def area_tolerance(domain_area_m2: float) -> float:
    """The permitted gap between ``C + G + U`` and the frozen domain area."""
    return max(ABSOLUTE_AREA_TOLERANCE_M2, abs(domain_area_m2) * RELATIVE_AREA_TOLERANCE)


@dataclass(frozen=True)
class CategoryAreas:
    """一类的三态面积（平方米），互斥且覆盖该类的整个评估域。"""
    covered_m2: float = 0.0
    gap_m2: float = 0.0
    unknown_m2: float = 0.0

    def total_m2(self) -> float:
        return self.covered_m2 + self.gap_m2 + self.unknown_m2


@dataclass(frozen=True)
class CategoryScore:
    """一类的覆盖区间。``supported`` 为假时不给百分比（缺图不是 0 分）。"""
    category: str
    areas: CategoryAreas
    domain_area_m2: float
    supported: bool
    coverage_lower_pct: float | None
    coverage_upper_pct: float | None
    assessable_pct: float | None
    unknown_pct: float | None
    unavailable_reason: str | None = None

    @property
    def interval_width_pct(self) -> float | None:
        if self.coverage_lower_pct is None or self.coverage_upper_pct is None:
            return None
        return self.coverage_upper_pct - self.coverage_lower_pct


@dataclass(frozen=True)
class OverallScore:
    """全部类别的总体区间分：上下界分别按目录大类等权加权。"""
    coverage_lower_pct: float
    coverage_upper_pct: float
    assessable_pct: float
    unknown_pct: float
    categories: tuple[str, ...]
    weights: dict[str, float]


@dataclass(frozen=True)
class OverallUnavailable:
    """为什么给不出总体分。缺一类或有一类没有空间支持时都不能加权。"""
    reason: str
    missing_categories: tuple[str, ...] = ()


def _pct(part_m2: float, whole_m2: float) -> float | None:
    if not whole_m2 > 0:
        return None
    return 100.0 * part_m2 / whole_m2


def category_score(category: str, areas: CategoryAreas, *, domain_area_m2: float,
                   spatial_support: bool = True,
                   unavailable_reason: str | None = None) -> CategoryScore:
    """一类的三态面积 → 覆盖区间。

    ``domain_area_m2`` 是冻结的评估域面积；``C + G + U`` 与它不符即报错，容差见
    §11.3。没有空间支持（缺 OSM 图或格网无法建立）时按 §6.4 只报"无法确定"，
    不把未知面积当成 100% 未覆盖。
    """
    if not domain_area_m2 > 0:
        raise ScoringError("domain area must be positive")
    # A category without spatial support -- including one already downgraded because
    # its areas did not add up -- is reported as "cannot be determined" before the
    # identity is checked: its areas are not a claim, so they cannot fail one.
    if not spatial_support:
        return CategoryScore(category=category, areas=areas, domain_area_m2=domain_area_m2,
                             supported=False, coverage_lower_pct=None, coverage_upper_pct=None,
                             assessable_pct=None, unknown_pct=None,
                             unavailable_reason=unavailable_reason or "no_spatial_support")
    gap = abs(areas.total_m2() - domain_area_m2)
    if gap > area_tolerance(domain_area_m2):
        raise ScoringError(
            f"category {category}: C+G+U={areas.total_m2()} differs from the frozen domain"
            f" {domain_area_m2} by {gap} (tolerance {area_tolerance(domain_area_m2)})")
    return CategoryScore(
        category=category, areas=areas, domain_area_m2=domain_area_m2, supported=True,
        coverage_lower_pct=_pct(areas.covered_m2, domain_area_m2),
        coverage_upper_pct=_pct(areas.covered_m2 + areas.unknown_m2, domain_area_m2),
        assessable_pct=_pct(areas.covered_m2 + areas.gap_m2, domain_area_m2),
        unknown_pct=_pct(areas.unknown_m2, domain_area_m2))


def overall_score(scores: dict[str, CategoryScore],
                  expected_categories: Iterable[str] | None = None) -> OverallScore | OverallUnavailable:
    """Score the requested category scope, or explain why it is unavailable.

    The default scope is the core three categories. Callers that explicitly
    request an expanded taxonomy must pass that scope so unrequested categories
    are not treated as missing evidence.
    """
    categories = tuple(dict.fromkeys(
        DEFAULT_SCORING_CATEGORIES if expected_categories is None else expected_categories
    ))
    missing = tuple(category for category in categories if category not in scores)
    if missing:
        return OverallUnavailable(reason="categories_not_analysed", missing_categories=missing)
    unsupported = tuple(category for category in categories
                        if not scores[category].supported)
    if unsupported:
        return OverallUnavailable(reason="category_without_spatial_support",
                                  missing_categories=unsupported)
    weights = category_weights(categories)
    lower = sum(scores[category].coverage_lower_pct * weights[category]  # type: ignore[operator]
                for category in categories)
    upper = sum(scores[category].coverage_upper_pct * weights[category]  # type: ignore[operator]
                for category in categories)
    assessable = sum(scores[category].assessable_pct * weights[category]  # type: ignore[operator]
                     for category in categories)
    unknown = sum(scores[category].unknown_pct * weights[category]  # type: ignore[operator]
                  for category in categories)
    return OverallScore(coverage_lower_pct=lower, coverage_upper_pct=upper,
                        assessable_pct=assessable, unknown_pct=unknown,
                        categories=categories, weights=weights)


def interval_degenerates(score: CategoryScore) -> bool:
    """全部类别都没有未知面积时区间退化为一个点；此处用于报告文案判断。"""
    return score.supported and score.areas.unknown_m2 <= 0
