"""category 服务场：把一个格判成 覆盖 / 灰区 / 未知，并给出细化的理由（§6.1–§6.3）。

这个模块只做一件事：**在两个视图上都想一遍，然后只在证据支持时才下结论。**

* 严格视图（明确允许）说"能走到" → 覆盖；乐观视图（可能允许）也说"走不到" → 才可能是
  灰区；两者结论不同 → 未知。未知不是"没有结论就随便取一个"，它是 §7.1 里
  "最高覆盖率 (C+U)/A" 的 U。
* **误差带是不确定，不是放宽。** 模型距离落在 1000±100 米之间时 ``distance_within``
  返回 None：这一格必须细化，细化后仍落在带内就是未知，绝不按 1100 米算覆盖。
* **没有入口证据的类别不给灰区。** 一个类别的入口全部未解决（或根本查不到）时，
  整个类别是未知：把"我们不知道设施在哪"写成"这里没有服务"是最严重的一类误判。
* **图断开与距离超出要分开。** 两者都表现为"没有模型路径"，但前者是数据不足（未知），
  后者是明确的"超过 1000 米"（可以进灰区）。判据是整城图的连通分量，不是子视图的。

面积结论不在这里：那是 §7.1 由 :mod:`app.scoring` 按叶格面积汇总的事。这里只保证
每一格的三态和它的理由是可复核的。
"""
from dataclasses import dataclass, field as dataclass_field
from typing import Callable, Iterable, Sequence

from shapely import prepare
from shapely.geometry import Point, box as shapely_box
from shapely.strtree import STRtree

from ..rules import category_service, distance_within
from .grid import COVERED, GAP, REFINE, UNKNOWN
from .service_graph import (ALLOWED, ENTRANCE_LIMIT_M, POSSIBLE, SEARCH_CUTOFF_M, Entrance, ServiceViews,
                            PointAttachments, entrance_seeds_with_owner, reverse_field_owners,
                            sample_distance, sample_nearest_facility)

#: 判定用的度量名：必须与规则里的 metric 一致，否则 ``distance_within`` 一律返回 None。
MEASURED_METRIC = "walking_route"

#: ``blind`` 是 :func:`category_service` 的返回值，网格用的是 ``gap``；只在这一处翻译。
_SERVICE_TO_VERDICT = {"covered": COVERED, "blind": GAP, "unknown": UNKNOWN}


@dataclass(frozen=True)
class VerifiedPoint:
    """覆盖抽检的一条实测结论（米制点）：它只替自己所在的那个 25 米格说话。

    ``verdict`` 是这一点上的实测结论（``covered`` 或 ``unknown``——单条路线走不通
    不能证明没有别的设施可用，所以不会是 ``gap``）；``contradicted`` 是它推翻的模型
    结论，同一 50 米父格里模型给出同样结论的兄弟格因此降为未知。
    """
    point: Point
    verdict: str
    contradicted: str | None = None


@dataclass
class SampleAssessment:
    """一个支持点的判定：两个视图各自的距离、接入成本，以及它为什么没有结论。"""
    point: Point
    value: bool | None
    reason: str | None
    allowed_m: float | None = None
    possible_m: float | None = None
    attachment_m: float | None = None
    conflict: bool = False
    nearest_facility: str | None = None


@dataclass
class CellAssessment:
    """一个格的判定与它的证据（§6.2：状态、最近已知设施、模型距离、入口接入）。"""
    status: str
    reason: str | None = None
    samples: list[SampleAssessment] = dataclass_field(default_factory=list)
    min_distance_m: float | None = None
    nearest_facility: str | None = None
    evidence_grade: str = "model"

    @property
    def has_legal_attachment(self) -> bool:
        return any(sample.attachment_m is not None for sample in self.samples)


class ServiceField:
    """某一类别、某一任务的服务场：入口 → 两张视图的反向距离场 → 每格三态。"""

    def __init__(self, *, category: str, rule, store, views: ServiceViews,
                 entrances: Iterable[Entrance], query_complete: bool = True,
                 boundary=None, obstacle_intersects: Callable[[object], bool] | None = None,
                 data_conflict: Callable[[object], bool] | None = None,
                 verification_conflict: Callable[[object], bool] | None = None,
                 attachments: PointAttachments | None = None,
                 verified: Sequence[VerifiedPoint] = (),
                 query_incomplete=None, confirmed_empty: bool = False):
        self.category = category
        self.confirmed_empty = confirmed_empty
        self.verified = tuple(verified)
        # 检索没有查完的区域（米制）；给了就按格判断，代替整个任务一个开关。
        self.query_incomplete = query_incomplete
        if query_incomplete is not None:
            prepare(query_incomplete)
        self.rule = rule
        self.store = store
        self.attachments = attachments if attachments is not None else PointAttachments(store)
        if self.attachments.store is not store:
            raise ValueError('attachment_cache_store_mismatch')
        self.views = views
        self.query_complete = query_complete
        self.boundary = boundary
        self.obstacle_intersects = obstacle_intersects
        self.data_conflict = data_conflict
        self.verification_conflict = verification_conflict
        entrances = list(entrances)
        self.entrances = entrances
        self.seeds = entrance_seeds_with_owner(entrances)
        # 距离与归属来自同一次搜索，"最近已知设施"因此不会与最近距离互相矛盾。
        self.owned = reverse_field_owners(views, self.seeds)
        self.fields = {name: {node: cost for node, (cost, _) in field.items()}
                       for name, field in self.owned.items()}
        self.facility_of = {name: {node: owner for node, (_, owner) in field.items()}
                            for name, field in self.owned.items()}
        # 入口侧的分量：判断"没有模型路径"到底是图断开还是距离超出（§6.2）。
        self.entrance_components = {store.component.get(node) for node in self.seeds}
        self._unresolved = [item for item in entrances if item.attachment is None]
        self._unresolved_points = [item.point for item in self._unresolved if item.point is not None]
        self._unresolved_index = (STRtree(self._unresolved_points)
                                  if self._unresolved_points else None)
        self.counts = {"samples": 0, "graph_disconnected": 0, "views_conflict": 0,
                       "entrance_unresolved_nearby": 0, "query_incomplete_nearby": 0}

    # ---------------------------------------------------------------- 单点判定

    def _state(self, distance_m: float | None, attachment) -> tuple[bool | None, str | None]:
        """一个视图上的三态：True/False 是结论，None 是"这个视图上定不了"。"""
        if not self.seeds:
            return None, "no_valid_entrance"
        if distance_m is not None:
            value = distance_within(distance_m, MEASURED_METRIC, self.rule)
            return value, None if value is not None else "distance_in_tolerance_band"
        if self._same_component(attachment):
            # 同一分量却没有值 ⇒ 模型距离确实超过截止（>1100 米），这是明确的否。
            return False, "beyond_search_cutoff"
        return None, "graph_disconnected"

    def _same_component(self, attachment) -> bool:
        return any(self.store.component.get(node) in self.entrance_components
                   for node in attachment.seeds)

    def assess_sample(self, point: Point) -> SampleAssessment:
        attachment, reason = self.attachments.resolve((point.x, point.y))
        if attachment is None:
            return SampleAssessment(point=point, value=None, reason=reason)
        allowed_m = sample_distance(attachment, self.fields[ALLOWED])
        possible_m = sample_distance(attachment, self.fields[POSSIBLE])
        # 报告给用户的是**明确允许**的路径上的最近设施：乐观视图上的那家可能进不去。
        nearest = sample_nearest_facility(attachment, self.fields[ALLOWED],
                                         self.facility_of[ALLOWED])
        allowed, allowed_reason = self._state(allowed_m, attachment)
        possible, possible_reason = self._state(possible_m, attachment)
        value, value_reason, conflict = self._combine(allowed, allowed_reason, possible,
                                                      possible_reason)
        self.counts["samples"] += 1
        if allowed_reason == "graph_disconnected" or possible_reason == "graph_disconnected":
            self.counts["graph_disconnected"] += 1
        self.counts["views_conflict"] += int(conflict)
        return SampleAssessment(point=point, value=value, reason=value_reason, allowed_m=allowed_m,
                                possible_m=possible_m, attachment_m=attachment.distance_m,
                                conflict=conflict, nearest_facility=nearest)

    @staticmethod
    def _combine(allowed, allowed_reason, possible, possible_reason) -> tuple[bool | None, str | None, bool]:
        """两个视图合成一个结论（§5.2）。返回 ``(值, 理由, 两个视图是否不一致)``。"""
        if allowed is True:
            return True, None, False
        if possible is False:
            # 乐观视图都超过了 1100 米：那就是明确的不满足规则，与误差带无关。
            return False, "beyond_search_cutoff", False
        conflict = allowed is not True and possible is True
        if allowed is False:
            # 严格视图说走不到、乐观视图说走得到：结论取决于一处条件通行，只能是未知。
            return None, "views_disagree", True
        # 严格视图没有结论（误差带 / 图断开）：理由取严格视图那一侧的，它更具体。
        return None, allowed_reason or possible_reason or "views_disagree", conflict

    # ---------------------------------------------------------------- 格判定

    def assess(self, cell, samples: Sequence[Point]) -> CellAssessment:
        assessed = [self.assess_sample(point) for point in samples]
        assessment = CellAssessment(status=UNKNOWN, samples=assessed)
        distances = [sample.allowed_m for sample in assessed if sample.allowed_m is not None]
        if distances:
            assessment.min_distance_m = min(distances)
            # 最近已知设施取距离最小的那个支持点报出来的设施，两者来自同一次比较。
            assessment.nearest_facility = min(
                (sample for sample in assessed if sample.allowed_m is not None),
                key=lambda sample: sample.allowed_m).nearest_facility
        if not self.seeds:
            if (self.confirmed_empty and self.query_complete
                    and all(sample.attachment_m is not None for sample in assessed)
                    and assessed and not self._query_incomplete_near(cell)):
                assessment.status = GAP
                assessment.reason = "query_complete_no_facility"
                return assessment
            # 没有可用入口：整类别未知。不能把"设施位置不明"写成"没有服务"。
            assessment.reason = "no_valid_entrance"
            return assessment
        values = [sample.value for sample in assessed]
        if not assessment.has_legal_attachment:
            assessment.reason = (assessed[0].reason if assessed else None) or "no_legal_attachment"
            return assessment
        verdict = category_service(values, self.query_complete, self.rule)
        if verdict == "blind" and self._query_incomplete_near(cell):
            # 周边必要的检索分块没有查完：没查到不等于没有（§4.4），这一格不判缺口。
            assessment.reason = "query_incomplete_nearby"
            self.counts["query_incomplete_nearby"] += 1
            return assessment
        if verdict == "blind" and self._unresolved_nearby(cell):
            # 附近有入口未解决的同类设施：它可能就服务这一格（§5.3 的影响范围）。
            assessment.reason = "entrance_unresolved_nearby"
            self.counts["entrance_unresolved_nearby"] += 1
            return assessment
        assessment.status = _SERVICE_TO_VERDICT[verdict]
        assessment.reason = self._undecided_reason(assessed) if verdict == "unknown" else None
        return assessment

    @staticmethod
    def _undecided_reason(assessed: Sequence[SampleAssessment]) -> str | None:
        """按信息量排序取第一个出现的理由：具体的原因比笼统的"证据不足"更可复核。"""
        for reason in ("graph_disconnected", "views_disagree", "distance_in_tolerance_band"):
            if any(sample.reason == reason for sample in assessed):
                return reason
        return "no_conclusive_evidence"

    def _query_incomplete_near(self, cell) -> bool:
        """这一格的服务搜索范围内，有没有检索没有查完的分块。

        范围取搜索截止加入口接入上限：再远的设施怎么都服务不到这一格，它那里查没查完
        与这一格无关。
        """
        if self.query_incomplete is None:
            return False
        reach = SEARCH_CUTOFF_M + ENTRANCE_LIMIT_M
        return self.query_incomplete.intersects(cell.geometry().buffer(reach))

    def _unresolved_nearby(self, cell) -> bool:
        """这一格附近有没有未解决的同类入口：有就不许判灰区（§5.3 的影响范围）。

        用直线距离筛选是保守的：直线比步行路径短，所以"直线超过 1100 米"一定意味着
        "步行超过 1100 米"，不会漏掉真正可能有影响的入口（§6.3）。
        """
        if self._unresolved_index is None:
            return False
        reach = self.rule.threshold_m + self.rule.tolerance_m
        minx, miny, maxx, maxy = cell.geometry().bounds
        hits = self._unresolved_index.query(
            shapely_box(minx - reach, miny - reach, maxx + reach, maxy + reach))
        return any(cell.geometry().distance(self._unresolved_points[int(i)]) <= reach
                   for i in hits)

    def refine_reason(self, cell, samples: Sequence[Point], assessment: CellAssessment) -> str | None:
        """§6.1 的细化触发条件；``None`` 表示 50 米上的结论已经站得住。

        水系数据冲突排在最前：那一片是不是水本身没有定论，任何距离都站不住，细化到
        25 米仍然冲突的格就是"数据冲突／未知"，理由随格留下，地图据此区别于普通未知。
        """
        if self.data_conflict is not None and self.data_conflict(cell.geometry()):
            return "water_data_conflict"
        decided = {sample.value for sample in assessment.samples if sample.value is not None}
        if len(decided) > 1:
            return "support_points_disagree"
        if any(sample.reason == "distance_in_tolerance_band" for sample in assessment.samples):
            return "distance_in_tolerance_band"
        if self.boundary is not None and cell.geometry().intersects(self.boundary):
            return "boundary_intersection"
        if self.obstacle_intersects is not None and self.obstacle_intersects(cell.geometry()):
            return "obstacle_intersection"
        if self.verification_conflict is not None and self.verification_conflict(cell.geometry()):
            return "verification_conflict"
        return None

    def evaluate(self, cell, samples: Sequence[Point]) -> tuple[str, str | None]:
        """网格评估器：先看细化触发条件，再看结论（§6.1）。"""
        return self.decide(cell, samples, self.assess(cell, samples))

    def decide(self, cell, samples: Sequence[Point],
               assessment: CellAssessment) -> tuple[str, str | None]:
        """由已经算好的判定出结论，供调用方一次评估、两处使用（判定与证据）。

        顺序是有意的：圈边界或障碍穿过一个格时，即使这一格现在"看起来"是覆盖，
        它的面积也只是被评估域裁出来的一个近似 —— 边界两侧的分量必须分开算。
        因此几何触发条件先于结论判断，而"没有入口证据 / 没有合法接入"的格直接是
        未知：细化它不会带来任何新证据。
        """
        # 覆盖抽检的实测点先于模型：50 米格里有实测点就细化，实测点所在的 25 米格用实测
        # 证据定论（§6.3 的局部重算）。一条路线只替它自己的那一格说话。
        verified = self._verified_in(cell)
        if verified is not None:
            if cell.level == 0:
                return REFINE, "verification_evidence"
            return verified.verdict, ("verified_route" if verified.verdict == COVERED
                                      else "verification_conflict")
        verdict, reason = self._model_verdict(cell, samples, assessment)
        # 同一父格里被实测推翻的那个模型结论，在兄弟格上也站不住：降为未知。
        if cell.level == 1 and verdict in (COVERED, GAP) and self._contradicted_near(cell, verdict):
            return UNKNOWN, "verification_conflict"
        return verdict, reason

    def _model_verdict(self, cell, samples, assessment) -> tuple[str, str | None]:
        if (not self.seeds and not self.confirmed_empty) or not assessment.has_legal_attachment:
            return UNKNOWN, assessment.reason
        reason = self.refine_reason(cell, samples, assessment)
        if reason:
            return REFINE, reason
        if assessment.status in (COVERED, GAP):
            return assessment.status, assessment.reason
        return UNKNOWN, assessment.reason

    def _verified_in(self, cell) -> VerifiedPoint | None:
        return next((item for item in self.verified if cell.contains(item.point)), None)

    def _contradicted_near(self, cell, verdict: str) -> bool:
        parent = cell.parent()
        return any(item.contradicted == verdict and parent.contains(item.point)
                   for item in self.verified)

    def summary(self) -> dict:
        """写进任务证据的类别级统计：入口、查询完成度与各条款触发次数。"""
        return {"category": self.category, "entrances": len(self.entrances),
                "valid_entrances": sum(1 for item in self.entrances
                                       if item.attachment is not None),
                "unresolved_entrances": len(self._unresolved),
                "unresolved_without_location": sum(1 for item in self._unresolved
                                                   if item.point is None),
                "seed_nodes": len(self.seeds), "query_complete": self.query_complete,
                "query_status": "complete" if self.query_complete else "partial",
                **self.counts}
