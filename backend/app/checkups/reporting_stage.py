"""§7.2 报告阶段：把这一版已经冻结的结论组装成一份可离线复核的报告。

这一层不重新计算任何东西。它读的是同一份修订里已经写着的证据对象，把它们摊平成报告的
栏目 —— 报告里出现的每个数字都必须能在同一份修订的其它栏目里找到来源。重新算一遍意味着
报告与快照可能给出不同的面积和分数，而核对时最难发现的就是这种"两边都说得通"的偏差。

三条口径：

* **限制写进报告本身，不写在界面文案里。** ``LIMITATIONS`` 随报告一起冻结。目录完整性、
  人口覆盖、设施容量、政策准入、与建设选址的关系，都是这份报告不能回答的问题；界面
  可以不显示，但不能让报告本身像是回答过了。
* **缺的阶段带着名字出现。** 核验没跑、可达性没跑、分数给不出，都写成带
  ``available``/``status``/``reason`` 的栏目，而不是省略该栏目。省略在读者看来就是
  "没有这个问题"。
* **报告 ID 可追溯。** 它由任务 ID 与修订号组成，因此任何一份报告文件都能直接对回磁盘上
  的那一版修订，不需要额外的索引。
"""
from .. import catalog
from ..scoring import DEFAULT_SCORING_CATEGORIES
from .models import (RULE_VERSION, SCHEMA_VERSION, CoverageRow, ReportEvidence, ReportGaps,
                     ReportQuality, ReportVerification)
from .. import catalog
from .verification_stage import evidence_counts

#: 这份报告不能回答的问题。逐条都是"读者最容易从这里读出来的过度结论"。
LIMITATIONS = (
    "目录完整性未经独立核实：查询状态只说明计划中的请求与空间范围跑完了，"
    "不代表现实中的设施都已被收录。",
    "没有人口数据，本报告不输出人口覆盖率，也不判断居民是否被服务。",
    "设施数量不代表服务容量、排队时间或政策准入（户籍、学籍、定点资格等）。",
    "本报告是现状评估，不构成建设、选址或调整结论，也不包含规划建议。",
    "覆盖与灰区由 OSM 步行路网模型计算，不是实测步行时间；路网数据缺失处不插值，"
    "而是标为未知。",
    "灰区的含义是「模型判定在 1000 米步行距离内没有该类设施」，"
    "不等于「确定没有该类设施」。",
    "15 分钟成圈阈值与服务标准 1000 米是两个口径，不能互相换算。",
    "热力面是模型距离的展示，不是实测、不是设施数量，也不代表人口覆盖。",
)


def _merge_categories(accessibility: dict | None, scores: dict | None,
                      requested_categories=None, query_status=None) -> list[CoverageRow]:
    """把面积证据与分数行按类别合并：一份报告里同类别的数字只出现一次。"""
    areas = {item["category"]: item for item in (accessibility or {}).get("categories", [])}
    rows = []
    for item in (scores or {}).get("categories", []):
        category = item["category"]
        evidence = areas.get(category, {})
        rows.append({**item, "coveredM2": evidence.get("coveredM2"),
                     "gapM2": evidence.get("gapM2"), "unknownM2": evidence.get("unknownM2"),
                     "cells": evidence.get("cells", {}),
                     "entrances": evidence.get("entrances", {}),
                     "evidenceGrade": "model"})
    # 没有分数行的类别（空间支持缺失时仍会出现在面积证据里）也要留在报告里。
    known = {row["category"] for row in rows}
    for category, evidence in areas.items():
        if category in known:
            continue
        rows.append({"category": category, "supported": evidence.get("supported"),
                     "coverageLowerPct": None, "coverageUpperPct": None,
                     "assessablePct": None, "unknownPct": None,
                     "unavailableReason": evidence.get("unavailableReason"),
                     "coveredM2": evidence.get("coveredM2"), "gapM2": evidence.get("gapM2"),
                     "unknownM2": evidence.get("unknownM2"), "cells": evidence.get("cells", {}),
                     "entrances": evidence.get("entrances", {}), "evidenceGrade": "model"})
    known = {row['category'] for row in rows}
    for category in requested_categories or ():
        if category not in known:
            rows.append({"category": category, "supported": False,
                         "unavailableReason": "query_incomplete" if query_status not in
                         ("complete", "completed") else "no_usable_evidence",
                         "evidenceGrade": "model"})
    return rows


def _verification_section(verification: dict | None) -> ReportVerification:
    """核验这一节：既能说出核验了什么，也能说出为什么没核验。

    ``not_integrated`` 是"这个部署没有路线服务"的具名拒绝，不是一次核验 —— 把它读成
    ``available: True`` 会让"没有核验"看起来像"核验过了、没发现问题"。拒绝的原因和说明
    仍然照发，读者要能看到缺席的名字。
    """
    if verification is None:
        return ReportVerification(available=False, status="not_integrated",
                                  reason="本次体检未进行现实核验，证据等级为模型推定。")
    if verification.get("status") == "not_integrated":
        return ReportVerification(**{**verification, "available": False})
    counts = evidence_counts(verification.get('facilities', []))
    return ReportVerification(**{**verification, **{
        'routeReturns': counts['route_returns'],
        'strictConfirmed': counts['strict_confirmed'],
        'toleranceEstimated': counts['tolerance_estimated'],
        'noUsableDecision': counts['no_usable_decision'],
    }, "available": True})


def _quality_notes(accessibility: dict | None, gaps: dict | None, heatmap: dict | None,
                   facilities: dict | None, source_result_hash: str) -> ReportQuality:
    """证据说明：结论建立在什么之上，以及这一版缺了什么。"""
    return ReportQuality(
        schema_version=SCHEMA_VERSION, rule_version=RULE_VERSION,
        source_result_hash=source_result_hash,
        views=(accessibility or {}).get("views"),
        grid={"stepM": (accessibility or {}).get("gridStepM"),
              "refinedStepM": (accessibility or {}).get("refinedStepM"),
              "maxLeafCells": (accessibility or {}).get("maxLeafCells"),
              "searchCutoffM": (accessibility or {}).get("searchCutoffM")},
        excluded_area_m2=(accessibility or {}).get("excludedAreaM2"),
        obstacle_layer_available=(gaps or {}).get("obstacleLayerAvailable"),
        heatmap_estimated=(heatmap or {}).get("estimated"),
        catalog_completeness=(facilities or {}).get("catalogCompleteness", "unverified"),
        query_status=(facilities or {}).get("queryStatus"),
        notes=sorted(set((accessibility or {}).get("notes", [])
                         + (gaps or {}).get("notes", [])
                         + (heatmap or {}).get("notes", [])
                         + (facilities or {}).get("warnings", []))),
    )


def _scope_limitations(scores: dict | None) -> tuple[str, ...]:
    """总体分的口径必须写在报告里，不能只存在于一个没人渲染的字段里。

    总体分固定按核心分析范围（三大类各 1/3）加权，所以有两种情况必须说清楚：
    报告列出了核心范围之外的类别时，读者要知道那个总分没有把它们算进去；这一次
    根本给不出总体分时，读者要知道缺的是哪几类，而不是把各类别区间自己脑补成一个总分。
    """
    overall = (scores or {}).get("overall")
    if not overall:
        return ()
    core = tuple(DEFAULT_SCORING_CATEGORIES)
    rows = (scores or {}).get("categories") or []
    if overall.get("available"):
        if not any(row.get("category") not in core for row in rows):
            return ()
        names = "、".join(catalog.major_label(category) for category in core)
        return (f"总体区间分只覆盖核心分析范围（{names}，各占 1/{len(core)}）："
                f"报告里列出的其他类别各自单独给出，不进入总体分。",)
    if overall.get("reason") == "categories_not_analysed":
        missing = "、".join(catalog.major_label(category)
                           for category in (overall.get("missingCategories") or []))
        return (f"本次没有给出总体区间分：{missing or '核心分析范围'}未参与评估。"
                f"总体分不能由已分析的类别重新加权得到，这里只列各类别自己的区间。",)
    return ()


def build_report(*, task_id: str, revision: int, source_result_hash: str, generated_at: float,
                 domain, domain_area_m2, accessibility, service_gaps, heatmap, scores,
                 facilities, verification=None, water=None, requested_categories=None) -> ReportEvidence:
    """组装报告。``revision`` 是**将要发布**的那一版：报告描述的是它自己所在的那一版。

    ``source_result_hash`` 是报告所汇总的那一版（可达性/核验阶段的那一版）的结果摘要，
    不是报告自己所在那一版的摘要 —— 报告是修订的一部分，而修订的摘要覆盖报告，把后者
    写进来就成了自己包含自己。钉住来源那一版，读者才能从报告直接回到它所描述的结论。
    """
    return ReportEvidence(
        report_id=f"{task_id}:{revision}", generated_at=generated_at,
        domain=domain, domain_area_m2=domain_area_m2,
        categories=_merge_categories(accessibility, scores, requested_categories,
                                     (facilities or {}).get('queryStatus')),
        category_directory_version=catalog.VERSION,
        category_directory=catalog.major_directory(requested_categories),
        overall=(scores or {}).get("overall"),
        gaps=ReportGaps(**service_gaps) if service_gaps else ReportGaps(
            status="failed", reason="服务覆盖阶段未完成，本报告不含灰区清单。"),
        verification=_verification_section(verification),
        evidence=_quality_notes(accessibility, service_gaps, heatmap, facilities,
                                source_result_hash),
        data_sources=_data_sources(water),
        limitations=list(LIMITATIONS) + list(_scope_limitations(scores)))


#: 报告数据来源栏从每份复核里带出的字段：足以复核，不含几何。
REVIEW_FIELDS = ("label", "title", "reviewedAt", "scope", "sources", "method", "limitations")


def _data_sources(water: dict | None) -> dict | None:
    """数据来源栏。水系证据缺席（早于复核的修订）时整栏为 null，而不是一栏空的"无复核"。"""
    if water is None:
        return None
    return {"water": {
        "obstacleLayerAvailable": water.get("obstacleLayerAvailable"),
        "osmDataVersion": water.get("osmDataVersion"),
        "sourcePbfSha256": water.get("sourcePbfSha256"),
        "reviews": [{**{k: review.get(k) for k in REVIEW_FIELDS},
                     "reaches": [{k: reach.get(k) for k in ("osmId", "name", "widthM", "osmWidthTag")}
                                 for reach in review.get("reaches", [])],
                     "crossings": len(review.get("crossings", [])),
                     "conflicts": [{k: item.get(k) for k in ("id", "status", "areaM2", "note")}
                                   for item in review.get("conflicts", [])],
                     "basemapMisdrawn": len(review.get("basemapMisdrawn", []))}
                    for review in water.get("reviews", [])],
        "rejectedReviews": water.get("rejectedReviews", []),
        "domainAreaM2": water.get("domainAreaM2"), "reviewedAreaM2": water.get("reviewedAreaM2"),
        "unreviewedAreaM2": water.get("unreviewedAreaM2"), "conflictAreaM2": water.get("conflictAreaM2"),
        "statements": water.get("statements", [])}}
