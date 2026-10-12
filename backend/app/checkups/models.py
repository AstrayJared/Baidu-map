"""Versioned checkup wire contract.

Separate from the legacy ``/api/analyses`` and ``/api/v1/analysis/hybrid``
contracts. Nothing here is added to the generated demo contract, so the strict
POI evidence types keep their existing serialization byte-for-byte.
"""
import time
from typing import Literal
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from ..accessibility.grid import GRID_STEP_M, MAX_LEAF_CELLS, REFINED_STEP_M
from ..accessibility.service_graph import SEARCH_CUTOFF_M
from ..accessibility.zones import COMPOSITE_MIN_CATEGORIES, MIN_LABEL_AREA_M2
from .. import service_rules, catalog
from ..contracts import Issue, MajorCategory, Origin
from ..facilities import RULE as DISTANCE_RULE
from ..rules import DistanceRule

SCHEMA_VERSION = "checkup-v1"
RULE_VERSION = "walk-distance-1000-v1"

TaskStatus = Literal["queued", "running", "cancelling", "completed", "failed", "cancelled"]
BusinessStatus = Literal["complete", "partial", "insufficient"]
Stage = Literal["isochrone", "poi", "accessibility", "verification", "reporting", "ready"]

# Finite checkup budgets. Callers may lower these limits.
DEFAULT_POI_REQUESTS = 1200
DEFAULT_ROUTE_REQUESTS = 120
MAX_POI_REQUESTS = 1200
MAX_ROUTE_REQUESTS = 120
DETAIL_ROUTE_REQUESTS = 20

# The facility query domain is the computed boundary expanded by this margin in
# the metric plane; it is an engineering allowance, not evidence of a complete
# directory. Declared here so no request can widen it.
QUERY_PADDING_M = int(service_rules.QUERY_PADDING_M)

TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
#: 一次补查的终态。``partial`` 也是终态：它带回了部分页面，不会自己继续。
EXTENSION_TERMINAL: frozenset[str] = frozenset({"completed", "partial", "failed", "cancelled"})


class CheckupModel(BaseModel):
    """Camel-case wire naming, like the rest of the project's HTTP contracts."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True,
                              alias_generator=to_camel)


class CategoryDirectoryEntry(TypedDict):
    id: str
    label: str
    order: int


class CheckupIsochrone(CheckupModel):
    # The supported tiers are per engine; the registry rejects the rest with 422.
    budget: int | None = Field(default=None, ge=1, le=800)


class CheckupFacilities(CheckupModel):
    categories: tuple[MajorCategory, ...] = tuple(catalog.default_analysis_majors())
    max_poi_requests: int = Field(default=DEFAULT_POI_REQUESTS, ge=1, le=MAX_POI_REQUESTS)
    max_route_requests: int = Field(default=DEFAULT_ROUTE_REQUESTS, ge=1, le=MAX_ROUTE_REQUESTS)

    @model_validator(mode="after")
    def unique_categories(self):
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("duplicate facility category")
        if not self.categories:
            raise ValueError("at least one facility category is required")
        return self


class CheckupRequest(CheckupModel):
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    engine: str
    center: Origin
    coordinate_system: Literal["bd09ll"] = "bd09ll"
    isochrone: CheckupIsochrone = Field(default_factory=CheckupIsochrone)
    facilities: CheckupFacilities = Field(default_factory=CheckupFacilities)

    @classmethod
    def from_stored(cls, values: dict):
        """Read a previously accepted 1600-cap request without widening new admission."""
        raw = values.get('facilities') or {}
        key = 'maxPoiRequests' if 'maxPoiRequests' in raw else 'max_poi_requests'
        limit = raw.get(key)
        if type(limit) is int and MAX_POI_REQUESTS < limit <= 1600:
            bounded = {**values, 'facilities': {**raw, key: MAX_POI_REQUESTS}}
            request = cls(**bounded)
            request.facilities = request.facilities.model_copy(update={'max_poi_requests': limit})
            return request
        return cls(**values)

    def fingerprint(self) -> dict:
        """Identity-free input digest; the request id names, it does not configure."""
        return self.model_dump(mode="json", exclude={"client_request_id"})


class ContinueCheckupRequest(CheckupModel):
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    base_revision: int = Field(ge=1)


class CheckupCompletion(CheckupModel):
    report_revision: int = 0
    round_number: int = 1
    round_poi_limit: int = 60
    round_poi_requests: int = 0
    cumulative_poi_requests: int = 0
    route_requests: int = 0
    route_remaining: int = 120
    query_complete_by_major: dict[str, bool] = Field(default_factory=dict)
    evaluated_categories: int = 0
    total_categories: int = 10
    evaluation_status: Literal['partial', 'complete', 'limited'] = 'partial'
    can_continue: bool = False
    restart_retrieval: bool = False
    stop_reason: str | None = None
    limitations: list[str] = Field(default_factory=list)


class SessionOpenRequest(CheckupModel):
    """开一个浏览会话，或回到已有的那一个（§5 B2 决策 2 的第一条期限）。

    两个标识分两处存，因为它们的寿命不一样：**会话**属于这个浏览器（同一浏览器的两个
    标签页共用一个），**标签页**属于一个标签页（刷新要接着用同一个，关掉就该消失）。
    客户端负责保管它们，服务端只回答"这个会话现在还算数吗"。
    """
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    #: 已有的会话标识。带上它就要求**续上同一个会话**：它已经到期时明确拒绝，
    #: 而不是悄悄开一个新的 —— 那会让"关闭浏览器即到期"这条规则形同虚设。
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    #: 已有的标签页标识（刷新时带上）：同一个标签页刷新不该被算成多开了一个标签页。
    tab_id: str | None = Field(default=None, min_length=1, max_length=100)


class SessionView(CheckupModel):
    """一个会话此刻的状态：租约还剩多久、有几个标签页还开着。

    ``expires_at`` 是**租约到期时刻**，不是"数据已经删了"：到期之后还有一段可判定的
    时间用来拒绝迟到的心跳，但明细的可用性由 ``RetentionView`` 单独回答。
    """
    session_id: str
    tab_id: str
    lease_seconds: float
    expires_at: float
    #: 这一次是接上了已有的会话，还是新开了一个。
    resumed: bool
    open_tabs: int = Field(default=1, ge=0)
    #: 这个会话里的任务数：界面据此说明"这些结果会跟这个会话一起到期"。
    tasks: int = Field(default=0, ge=0)


class RetainedCheckupView(CheckupModel):
    """明细到期之后仍然给得出的那一部分（§5 B2 决策 2）。

    ``summary`` 是定稿时冻结的白名单汇总：结论、分数区间、面积、计数、停止原因。它里面
    **没有**设施名称、UID、地址、坐标或几何 —— 到期之后还能看到什么，由这份白名单定义，
    而不是由"原文件里还剩什么"定义。
    """
    task_id: str
    revision: int = Field(ge=1)
    stage: Stage
    business_status: BusinessStatus
    result_hash: str
    summary: dict = Field(default_factory=dict)
    retention: "RetentionView"
    notes: list[str] = Field(default_factory=list)


class RetentionView(CheckupModel):
    """这份结果的明细还能不能提供、什么时候到期、为什么。

    ``details_available`` 为假时**不等于**"没有这个任务"：任务、评分与汇总都还在，
    缺的是含设施名称、UID 与坐标的那部分明细（`evidence/06` §2 决策 2）。
    """
    details_available: bool
    expires_at: float | None = None
    reason: Literal["session_closed", "superseded", "legacy", "cleared"] | None = None
    #: 这一版明细来自哪几个任务（含它自己）：复用别人的页面时，那个任务的期限也是它的期限。
    sources: list[str] = Field(default_factory=list)
    #: 到期时刻已经过了但文件还在（清理由后台巡检做）：界面据此区分"已经删了"与"即将删"。
    cleared: bool = False


class EngineRef(CheckupModel):
    engine_id: str
    engine_version: str
    label: str


class ScopeEvidence(CheckupModel):
    """What this revision's analysis covers, and what it had to leave out.

    ``coverage_supported`` says the spatial analysis group is *present* in this
    revision; whether that analysis could reach a conclusion is
    ``accessibility_status``, and whether a domain was frozen at all is
    ``assessment_domain_available``. A revision can carry the group and still
    report nothing: those are different facts, so they are different fields.
    """
    projection: str
    data_version: str
    coverage_supported: bool
    assessment_domain_available: bool = False
    excluded_area_m2: float | None = None
    model_support_available: bool = False
    notes: list[str] = Field(default_factory=list)


class TraceEvidence(CheckupModel):
    data_versions: dict
    rule_versions: dict
    isochrone_hash: str
    result_hash: str
    budgets: dict
    #: 数据修订后离线重算出的一版：从哪一版来、为什么重算。重算不发网络请求，
    #: ``budgets`` 仍是原任务花掉的额度。
    recomputed: dict | None = None
    #: §5 B2 决策 1 的重试发布的一版：从哪一版来、这一轮新增了多少真实调用、当时
    #: 共同完成了多少圈面。与 ``recomputed`` 分开：重算不花钱，重试花钱并带来新证据。
    retried: dict | None = None


class FacilityGroup(CheckupModel):
    """§4.4's two quality axes, reported side by side and never conflated.

    ``query_status`` is what the planned queries and their spatial range
    established; ``catalog_completeness`` is whether the real directory was
    independently verified, which a keyword search can never establish, so it is
    ``unverified`` here by construction and not by omission.

    ``counts_by_category`` is what the run *retrieved* inside the computed
    boundary, keyed by the request's major categories. A zero is an observed
    zero: it must be read with ``query_status``, never as "none exist" (§4.4).
    """
    query_status: Literal["completed", "partial", "failed", "cancelled"]
    catalog_completeness: Literal["unverified"] = "unverified"
    provider: str
    api_version: str
    data_source: Literal["baidu_place", "synthetic"]
    query_domain: dict
    data_obtained_at: float | None = None
    #: 首轮检索计划与缓存复用估算（§四）：初始页数、可复用页数、预计新增调用数与当时的
    #: 预算。它是一次估算、不是额度预留，也不承诺翻页与细分能在其中跑完。
    #: 旧修订没有这一项，读作"未记录"。
    initial_plan: dict | None = None
    counts_by_category: dict[str, int] = Field(default_factory=dict)
    facilities: list[dict] = Field(default_factory=list)
    #: Accepted facilities outside the boundary but inside the query range (§2.3):
    #: service sources for the assessment, never counted, never drawn as findings.
    nearby_facilities: list[dict] = Field(default_factory=list)
    review_candidates: list[dict] = Field(default_factory=list)
    excluded_candidates: list[dict] = Field(default_factory=list)
    quarantine: list[dict] = Field(default_factory=list)
    query_coverage: list[dict] = Field(default_factory=list)
    #: Per queried category, the query blocks (bd09ll polygons) whose keyword evidence
    #: did not finish. The assessment refuses a gap within service reach of them.
    #: ``None`` means not recorded (an older revision): the task-wide status applies.
    query_incomplete_regions: dict[str, list[dict]] | None = None
    #: §5 B2 决策 1：所有所选小类**共同**完成检索的圈面比例。它算的是各小类未完成区域
    #: 取并集后的残余，而不是各小类覆盖率的平均 —— 后者会让"某一类整块没查、其余全部查完"
    #: 读成 90%。旧修订没有这一项（键缺失或明确 ``null``），两者都表示"未记录"，都不是 0。
    query_area_coverage: dict | None = None
    #: §5 B2 决策 2：这一版明细出自哪几个任务 —— 来源标识 → 页数、其中新取的页数、最早的
    #: 取数时刻。复用别人的页面时，**那个任务的期限**也是这一版的期限，所以来源必须落在
    #: 报告里；只记「来自缓存」会让一份派生报告看起来像这些数据是它自己取回来的。
    #: 旧修订没有这一项，读作「未记录」：那时也没有任何跨任务复用（窗口默认未配置）。
    source_tasks: dict[str, dict] | None = None
    statistics: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    stop_reason: str | None = None


class FacilityExtensionRequest(CheckupModel):
    """按需补查：复用原体检已经算出的圈面，只再跑一次设施检索。

    扩展类别产出点位、数量和分类统计，**不**进入核心综合分，也不进入综合灰区：
    原体检的修订、评分和报告不因为一次补查而改变。这样"十类设施"是加出来的，
    不是把有业务依据的三类口径换掉。
    """
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    categories: tuple[MajorCategory, ...]
    #: 不给就按类别数推导一个够用的下限；给了但不够就明确拒绝，不静默少查。
    max_poi_requests: int | None = Field(default=None, ge=1, le=MAX_POI_REQUESTS)

    @model_validator(mode="after")
    def unique_categories(self):
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("duplicate facility category")
        if not self.categories:
            raise ValueError("at least one facility category is required")
        return self


class FacilityRetryRequest(CheckupModel):
    """§5 B2 决策 1：同一次体检的**重试**，用新预算接着把没查完的地段查下去。

    与补查的区别只有一条，但它是本质的一条：补查产出的是**并列**的一份结果，原体检的
    修订、评分和报告一个字都不变；重试产出的是**这一次体检自己的新修订**，因为运营者的
    要求是"普通体检必须查到完整结果" —— 那必须体现在这一份报告里，而不是旁边多一个文件。

    类别不在这里给：重试沿用原体检已经冻结的类别集合。允许改类别就等于允许悄悄换掉
    这次体检问的问题，"这次查完了没有"随之失去意义。
    """
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    #: 本轮的新预算。不给就用部署默认（240）；给了但不够首轮就明确拒绝，不静默少查。
    max_poi_requests: int | None = Field(default=None, ge=1, le=MAX_POI_REQUESTS)


class FacilityRetryView(CheckupModel):
    """一次重试的状态：它自己的预算、它到达的修订，以及它剩下的缺口。"""
    retry_id: str
    task_id: str
    base_revision: int = Field(ge=1)
    client_request_id: str
    status: Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
    stage: Literal["poi", "ready"] | None = None
    budget: dict = Field(default_factory=dict)
    requests: int = Field(default=0, ge=0)
    network_requests: int = Field(default=0, ge=0)
    facilities_status: str | None = None
    stop_reason: str | None = None
    initial_plan: dict | None = None
    error: str | None = None
    created_at: float
    finished_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in EXTENSION_TERMINAL


class FacilityExtensionView(CheckupModel):
    """一次补查的状态。它有自己的标识和预算，不占用原任务的设施额度。"""
    extension_id: str
    task_id: str
    base_revision: int = Field(ge=1)
    client_request_id: str
    status: Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
    stage: Literal["poi", "ready"] | None = None
    categories: list[MajorCategory]
    budget: dict = Field(default_factory=dict)
    requests: int = Field(default=0, ge=0)
    network_requests: int = Field(default=0, ge=0)
    facilities_status: str | None = None
    counts_by_category: dict[str, int] = Field(default_factory=dict)
    #: 这次补查停在哪里：``completed`` 之外的具名原因（额度、限流、本地上限、取消……）。
    #: 旧行没有记录时读作 null，不猜一个。
    stop_reason: str | None = None
    #: 执行前算出、随结果一起定稿的首轮估算：首轮页数、缓存可复用页数、预计新增调用数与
    #: 当时的预算。它是在派发第一页**之前**算的（不是提交时冻结的承诺），也不要求翻页与
    #: 细分跑在其中；旧行读作 null。
    initial_plan: dict | None = None
    error: str | None = None
    created_at: float
    finished_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in EXTENSION_TERMINAL


class FacilityExtensionDocument(CheckupModel):
    """一次补查的完整结果。

    ``group`` 为 null 只有一个意思：这次检索没有产出可用结果（具名原因在
    ``issues`` 里）。它绝不是"这片区域没有这类设施"，空清单与空目录是两件事。
    """
    extension_id: str
    task_id: str
    base_revision: int = Field(ge=1)
    categories: list[MajorCategory]
    status: Literal["completed", "partial", "failed", "cancelled"]
    facilities_status: str | None = None
    group: FacilityGroup | None = None
    requests: int = Field(default=0, ge=0)
    network_requests: int = Field(default=0, ge=0)
    budget: dict = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CategoryCoverage(CheckupModel):
    """一类在冻结评估域内的三态面积（§7.1）。

    面积只在这里出现一次：:class:`ScoreEvidence` 报的是由这些面积算出的百分比，
    不再复制一份面积。``C + G + U`` 与 ``domain_area_m2`` 必须相符，不符的类别按
    ``supported=False`` 报出 ``unavailable_reason``，绝不归一化后照常给分。
    """
    category: str
    supported: bool
    covered_m2: float = 0.0
    gap_m2: float = 0.0
    unknown_m2: float = 0.0
    domain_area_m2: float | None = None
    unavailable_reason: str | None = None
    #: 叶格统计：细分是否到顶、每态各多少格。``refined`` 为细分成 25 米的父格数。
    cells: dict = Field(default_factory=dict)
    entrances: dict = Field(default_factory=dict)
    #: 该类别的模型可达面（bd09ll 几何）：供 ``accessibility`` 图层绘制。它是模型
    #: 估计的展示面，面积结论一律来自网格。
    coverage: dict | None = None


class AccessibilityEvidence(CheckupModel):
    """§5–§6 的服务可达性结果：评估域、网格参数与逐类别的三态面积。

    ``domain_area_m2`` 是冻结的分母 A。``excluded_area_m2`` 是被数据集覆盖范围
    裁掉的面积 —— 那是"不在结论范围内"，不是"没有服务"。
    """
    status: Literal["complete", "partial", "failed"] = "failed"
    domain: dict | None = None
    domain_area_m2: float | None = None
    excluded_area_m2: float | None = None
    grid_step_m: float = GRID_STEP_M
    refined_step_m: float = REFINED_STEP_M
    max_leaf_cells: int = MAX_LEAF_CELLS
    search_cutoff_m: float = SEARCH_CUTOFF_M
    views: dict = Field(default_factory=dict)
    categories: list[CategoryCoverage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ServiceZone(CheckupModel):
    """一片灰区在冻结修订里的样子（§6.5）。

    ``zones`` 曾经是裸 dict，于是"这一片有多大、为什么是灰区、建议查什么"只存在于
    ``Zone.as_dict()`` 的写法里：写错了没有一处会报错，读的人也没有类型可依。把它写成
    模型之后，冻结出来的清单自带校验，前端契约也从同一份定义生成。

    两份几何必须来自同一个对象：``geometry`` 是**统计几何**（米制，面积以它为准），
    ``displayGeometry`` 是同一对象的 bd09ll 形态。展示简化改变统计几何是 §6.5 明令禁止的。
    """
    id: str
    index: int = 0
    categories: list[str] = Field(default_factory=list)
    kind: str
    area_m2: float
    parts: int = 0
    cell_ids: list[str] = Field(default_factory=list)
    label_visible: bool = False
    suspected: bool = True
    evidence_grade: str = "model"
    query_status: str = "complete"
    nearest_facility: str | None = None
    reason: str | None = None
    suggestion: str
    geometry: dict | None = None
    geometry_system: Literal["metric"] = "metric"
    display_geometry: dict | None = None


class ServiceGapsEvidence(CheckupModel):
    """§6.5 的灰区清单：连通分量、面积与逐区的解释。

    ``by_category_m2`` 是每类灰区的并集面积；``composite_area_m2`` 是"三大类至少
    两类为 gap"的并集面积。两者都不与逐区面积相加混用。
    """
    status: Literal["complete", "partial", "failed"] = "failed"
    zones: list[ServiceZone] = Field(default_factory=list)
    by_category_m2: dict[str, float] = Field(default_factory=dict)
    composite_area_m2: float = 0.0
    gap_area_m2: float = 0.0
    composite_min_categories: int = COMPOSITE_MIN_CATEGORIES
    min_label_area_m2: float = MIN_LABEL_AREA_M2
    obstacle_layer_available: bool = False
    notes: list[str] = Field(default_factory=list)


class HeatmapEvidence(CheckupModel):
    """设施密度热力的数据面：每格到最近设施模型距离（§5.4）。

    这是**模型估计**：它只回答"路网上走 1000 米能到哪里"，不是实测、不是设施数量、
    也不是人口覆盖。缺口格（搜索截止内没有模型路径）照样出点，``distanceM`` 为空，
    状态为 ``gap``：地图据此画出缺口，而不是把没有点的地方读成"未知"；也绝不把它画成
    一个"距离为零"的热点。其余没有模型路径的格不出点，它们的结论由未知面积表达。
    """
    metric: Literal["walking_route"] = "walking_route"
    estimated: Literal[True] = True
    step_m: float = GRID_STEP_M
    domain: dict | None = None
    categories: dict[str, list[dict]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class WaterDataEvidence(CheckupModel):
    """这一版的水体障碍到底是什么：数据来源、版本、复核过的范围与仍然冲突的地方。

    热力与灰区把水体当障碍用；底图上画的水面并不参与计算。两者不一致时，读者需要
    知道计算用的是哪一份、哪一片被独立证据核对过 —— 否则底图上的一片"水"下面画着
    覆盖，看起来就像"水面上有服务"。``conflict_area_m2`` 是评估域里来源互相矛盾、
    又没有证据裁决的面积：那里的格一律是"数据冲突／未知"，不是覆盖，也不是灰区。

    ``reviews`` 按复核文件原样带出（几何已转 bd09ll），范围之外的水系是 OSM 原样，
    ``unreviewed_area_m2`` 说的就是这一部分。
    """
    obstacle_layer_available: bool = False
    osm_data_version: str | None = None
    source_pbf_sha256: str | None = None
    reviews: list[dict] = Field(default_factory=list)
    rejected_reviews: list[str] = Field(default_factory=list)
    domain_area_m2: float | None = None
    reviewed_area_m2: float = 0.0
    unreviewed_area_m2: float | None = None
    conflict_area_m2: float = 0.0
    statements: list[str] = Field(default_factory=list)


class ScoreRow(CheckupModel):
    """一个类别的区间分。``supported`` 为假时百分比全是 null —— "没有空间支持"和
    "支持了但算出来是 0%"必须能分辨。"""
    category: str
    supported: bool | None = None
    coverage_lower_pct: float | None = None
    coverage_upper_pct: float | None = None
    assessable_pct: float | None = None
    unknown_pct: float | None = None
    interval_width_pct: float | None = None
    interval_degenerate: bool | None = None
    unavailable_reason: str | None = None


class OverallScore(CheckupModel):
    """:func:`overall_score` 的两种结果写在同一个模型里：给得出区间，或者给不出。

    ``available`` 是唯一的分支开关，``reason`` 给不出时说明是哪一类（类别不齐、缺空间
    支持）；其余字段在两个分支里都是可空的，界面因此不需要按分支换类型。
    """
    available: bool
    reason: str | None = None
    missing_categories: list[str] = Field(default_factory=list)
    coverage_lower_pct: float | None = None
    coverage_upper_pct: float | None = None
    assessable_pct: float | None = None
    unknown_pct: float | None = None
    categories: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)


class ScoreEvidence(CheckupModel):
    """§7.1 的覆盖区间分：固定分母、上下界与可评估率。

    只报百分比与总体分，不重复 :class:`AccessibilityEvidence` 里的面积。总体分在
    三大类不齐备或有一类没有空间支持时是"无法给出"，而不是把已分析的类别重新加权。
    """
    rule_version: str = RULE_VERSION
    domain_area_m2: float | None = None
    categories: list[ScoreRow] = Field(default_factory=list)
    overall: OverallScore | None = None
    formula: dict = Field(default_factory=dict)


class CoverageRow(ScoreRow):
    """报告里的一行：区间分加上它赖以计算的面积与格数。

    继承 :class:`ScoreRow` 不是省字，而是写明"报告的每一行就是一个类别的分数行，附上
    这些分数是从哪块面积算出来的"。合并成一个模型之后，报告里同类别的数字只可能出现
    一次 —— 面积与分数分栏写，读者就只能自己把两栏对起来。
    """
    covered_m2: float | None = None
    gap_m2: float | None = None
    unknown_m2: float | None = None
    cells: dict = Field(default_factory=dict)
    entrances: dict = Field(default_factory=dict)
    evidence_grade: str = "model"


class ReportGaps(CheckupModel):
    """报告的灰区栏目：既可以是完整清单，也可以是"这一阶段没跑"的具名缺席。

    缺席时 ``status`` 是 ``failed`` 且 ``reason`` 有话说，``zones`` 为空 —— 省略这一栏
    在读者看来就是"没有灰区问题"，这比一份带原因的缺席错得多。
    """
    status: Literal["complete", "partial", "failed"] = "failed"
    reason: str | None = None
    zones: list[ServiceZone] = Field(default_factory=list)
    by_category_m2: dict[str, float] = Field(default_factory=dict)
    composite_area_m2: float = 0.0
    gap_area_m2: float = 0.0
    min_label_area_m2: float = MIN_LABEL_AREA_M2
    composite_min_categories: int = COMPOSITE_MIN_CATEGORIES
    obstacle_layer_available: bool = False
    notes: list[str] = Field(default_factory=list)


class ReportVerification(CheckupModel):
    """报告的核验栏目。``available`` 与 ``status`` 是两件事：这个部署有没有核验服务
    （available），以及核验跑到什么程度（status）。把 ``not_integrated`` 读成
    ``available: True`` 会让"没有核验"看起来像"核验过、没发现问题"。"""
    available: bool
    status: str
    provider: str | None = None
    checked: int = 0
    failed: int = 0
    unresolved: int = 0
    route_returns: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    strict_confirmed: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    tolerance_estimated: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    no_usable_decision: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    #: 第一层：中心可达性（体检中心 → 设施）。
    facilities: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    #: 第二层：覆盖抽检（格子 → 模型最近候选设施）及其汇总与局部重算点。
    spot_checks: list[dict] = Field(default_factory=list)
    spot_check_summary: dict = Field(default_factory=dict)
    local_overrides: list[dict] = Field(default_factory=list)
    queries: dict = Field(default_factory=dict)
    reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class ReportQuality(CheckupModel):
    """证据质量栏：这份结论建立在什么之上，以及这一版缺了什么。"""
    schema_version: str = SCHEMA_VERSION
    rule_version: str = RULE_VERSION
    source_result_hash: str
    views: dict | None = None
    grid: dict = Field(default_factory=dict)
    excluded_area_m2: float | None = None
    obstacle_layer_available: bool | None = None
    heatmap_estimated: bool | None = None
    catalog_completeness: str = "unverified"
    query_status: str | None = None
    notes: list[str] = Field(default_factory=list)


class ReportEvidence(CheckupModel):
    """§7.2 报告快照：一份可离线复核的完整结论。

    ``limitations`` 是这个报告不能回答的问题（目录完整性、人口覆盖、设施容量、
    政策准入、与建设选址的关系）。它随报告一起冻结，不能由界面自行省略。
    """
    report_id: str
    generated_at: float
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    domain: dict | None = None
    domain_area_m2: float | None = None
    categories: list[CoverageRow] = Field(default_factory=list)
    category_directory_version: str | None = Field(default=None, json_schema_extra={'x-legacy-optional': True})
    category_directory: list[CategoryDirectoryEntry] = Field(default_factory=list, json_schema_extra={'x-legacy-optional': True})
    overall: OverallScore | None = None
    # 三栏都是必填：一份"漏了核验栏"的报告与一份"核验缺席"的报告必须长得不一样，
    # 前者是组装错误，后者要带着原因出现在读者面前。
    gaps: ReportGaps
    verification: ReportVerification
    evidence: ReportQuality
    #: 数据来源栏：水体障碍来自哪一份数据、哪一份复核、复核覆盖了多少。几何不在报告里，
    #: 在同一版修订的 ``water`` 栏。早于水系复核的报告没有这一栏。
    data_sources: dict | None = None
    limitations: list[str] = Field(default_factory=list)
    completion: CheckupCompletion | None = Field(default=None, json_schema_extra={'x-legacy-optional': True})


class VerificationEvidence(CheckupModel):
    """§6.3 的百度核验阶段：能核实的与核实不了的都留下名字。

    ``checked`` 是真实发起过路线核验的设施；``unresolved`` 是核验后仍然接入不了的
    入口；``conflicts`` 是"模型说覆盖、路线说走不通"这类分歧所在的那几格。三者都不是
    "目录已完整"的证据 —— 目录完整性永远是 ``unverified``。
    """
    status: Literal["complete", "partial", "failed", "not_integrated"] = "not_integrated"
    provider: str | None = None
    checked: int = 0
    failed: int = 0
    unresolved: int = 0
    route_returns: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    strict_confirmed: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    tolerance_estimated: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    no_usable_decision: int = Field(default=0, json_schema_extra={'x-legacy-optional': True})
    facilities: list[dict] = Field(default_factory=list)
    #: 覆盖抽检里与模型结论对不上的格子。它们交给局部重算：只在实测点所在的 25 米格
    #: 定论，同一父格里被推翻的模型结论降为未知，不整片改面积。
    conflicts: list[dict] = Field(default_factory=list)
    #: 覆盖抽检（格子 → 模型最近候选设施的路线）的逐点结果与汇总。
    spot_checks: list[dict] = Field(default_factory=list)
    spot_check_summary: dict = Field(default_factory=dict)
    #: 交给局部重算的实测点（冲突与实测补定）；离线重算时照原样再用一次。
    local_overrides: list[dict] = Field(default_factory=list)
    queries: dict = Field(default_factory=dict)
    reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class CheckupSnapshot(CheckupModel):
    """One immutable stage revision. Published content is never rewritten."""
    schema_version: Literal["checkup-v1"] = SCHEMA_VERSION
    task_id: str
    revision: int = Field(ge=1)
    generated_at: float
    center: Origin
    coordinate_system: Literal["bd09ll"] = "bd09ll"
    stage: Stage
    business_status: BusinessStatus
    engine: EngineRef
    isochrone: dict
    # The rule body keeps ``DistanceRule``'s own field names: it is the same
    # object the legacy contracts and the assessment use, and one rule must not
    # be described two ways. Everything else in this document is camelCase.
    rules: DistanceRule
    scope: ScopeEvidence
    trace: TraceEvidence
    # Absent is not the same as empty: null means the facility stage could not
    # establish a result at all (no key, no boundary), an empty group means it
    # ran and retrieved nothing. ``facilities_status`` describes the *query*
    # axis only — "complete" there never means the catalogue is verified.
    facilities: FacilityGroup | None = None
    facilities_status: Literal["not_integrated", "complete", "partial", "failed"]
    # The accessibility group is the same rule: null means the assessment could
    # not run (no OSM graph, no boundary, no facilities to attach), an object
    # with no usable category means it ran and could establish nothing.
    accessibility: AccessibilityEvidence | None = None
    accessibility_status: Literal["not_integrated", "complete", "partial", "failed"] = \
        "not_integrated"
    service_gaps: ServiceGapsEvidence | None = None
    heatmap: HeatmapEvidence | None = None
    scores: ScoreEvidence | None = None
    verification: VerificationEvidence | None = None
    report: ReportEvidence | None = None
    #: 水体障碍的来源与复核范围；早于水系复核的修订没有这一栏（null）。
    water: WaterDataEvidence | None = None
    warnings: list[Issue] = Field(default_factory=list)
    completion: CheckupCompletion | None = Field(default=None, json_schema_extra={'x-legacy-optional': True})


class TaskProgress(CheckupModel):
    """The worker's current step inside the stage, as it last wrote it.

    ``count`` is what has actually happened in this step (samples sent, pages
    requested, cells judged, facilities verified); ``limit`` is a bound the step
    will not pass (a budget, the candidate list), not a forecast of where it
    ends. There is deliberately no percentage: most steps stop early.
    """
    step: str
    label: str
    count: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=0)
    unit: str | None = None
    #: When this step began, on the server's wall clock.
    since: float


class CheckupTaskView(CheckupModel):
    task_id: str
    client_request_id: str
    engine: str
    status: TaskStatus
    business_status: BusinessStatus | None = None
    stage: Stage | None = None
    revision: int = Field(default=0, ge=0)
    budget: int
    requests: int = Field(default=0, ge=0)
    network_requests: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0, ge=0)
    created_at: float
    cancel_requested: bool = False
    error: str | None = None
    #: The server's wall clock when this view was built. Every other time here
    #: is on the same clock, so a client measures "how long ago" against this
    #: value plus its own elapsed time since the response, never its own clock.
    server_time: float
    started_at: float | None = None
    finished_at: float | None = None
    #: When the current stage began; None for a task stored before it was recorded.
    stage_started_at: float | None = None
    #: The last time the worker wrote anything about this task (a count, a step,
    #: a revision, a state change). A cancel request is not the worker's activity.
    last_activity_at: float | None = None
    progress: TaskProgress | None = None
    completion: CheckupCompletion | None = Field(default=None, json_schema_extra={'x-legacy-optional': True})
    #: §5 B2 决策 2：这份结果的明细还能不能提供、为什么、什么时候到期。它是**任务视图**上
    #: 的活字段（不像修订那样冻结），因为它描述的是"现在"，而到期时刻会随后来发生的体检
    #: 变化 —— 冻结进修订就等于把一个会变的答案写成常数。
    retention: RetentionView | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL


class CheckupLayer(CheckupModel):
    layer_id: str
    revision: int = Field(ge=1)
    geometry: dict | None
    display_geometry: dict | None = None
    # A layer is usually a shape. The report is a document, and it is served from
    # this endpoint too so every published group of a revision is addressable the
    # same way; it fills this field and leaves both geometry fields null, which is
    # how a client tells the two kinds apart.
    document: dict | None = None
    result_hash: str


class FacilityRoute(CheckupModel):
    """§3.3 点击设施路线：一条独立详情证据，不改变任何已发布的结论。

    ``revision`` 是要它回答的那一版，不是它产生的版本：详情证据不发布新修订，所以
    ``withinRule`` 说的是"这条路线怎么走"，不是"评分变成了什么"。要让新的证据改变
    结论，必须重新评估并生成新修订。
    """
    task_id: str
    revision: int = Field(ge=1)
    facility_id: str
    category: str
    major_category: MajorCategory | None = None
    origin: Origin
    destination: Origin
    straight_line_m: float | None = None
    #: 判定看的是 ``access_distance_m``：严格层就是路线距离，端点容差层是"路线距离＋
    #: 两端偏移"的估计。落在误差带里或没有可用证据层时为 None。
    within_rule: bool | None = None
    route_distance_m: float | None = None
    access_distance_m: float | None = None
    #: ``strict``（端点完全重合）、``endpoint_tolerance``（偏移都不超过 50 米）或 None。
    verification_layer: Literal["strict", "endpoint_tolerance"] | None = None
    duration_s: float | None = None
    observed_duration_s: float | None = None
    #: 严格 POI 证据这一层：端点不重合就是 pending；它是附加标志，不决定判定。
    poi_status: Literal["pending", "verified_reachable", "verified_unreachable"] = "pending"
    poi_reason: str | None = None
    entrance_status: str | None = Field(default=None, json_schema_extra={'x-legacy-optional': True})
    evidence_grade: Literal["verified", "model"] = "model"
    route_origin: Origin | None = None
    route_destination: Origin | None = None
    origin_offset_m: float | None = None
    destination_offset_m: float | None = None
    reason: str | None = None
    provider: str
    network: bool = False
    attempts: int = Field(default=0, ge=0)
    #: 详情桶的余额。它是这个任务的限额，不是账户余额（见 /api/v2/capabilities）。
    budget: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class CheckupCapabilities(CheckupModel):
    schema_version: str = SCHEMA_VERSION
    engines: list[dict]
    rules: dict
    data_versions: dict
    coverage: dict
    budgets: dict
    #: 设施检索的预检口径：本地处理步数上限、网络预算与它分开、首轮计划会报缓存复用。
    #: 旧后端不给这一项，界面据此说"这一版不报预检口径"，而不是显示一个 0。
    poi_planning: dict = Field(default_factory=dict)
    #: §3.4 的跨任务复用窗口：``freshnessSeconds`` 为 null 表示缓存只在同一个任务内复用，
    #: 所以重复体检同一片区域不会省下任何请求。这个开关要让用户看得见。
    cache: dict = Field(default_factory=dict)
    #: 设施目录的 v2 视图（展示组 / 大类 / 每类检索小类数 / 哪几类是核心口径）：
    #: 类别选择器和预算算术都从这里来，不在界面里另写一份分类表。
    facility_categories: dict = Field(default_factory=dict)
    # The application's own remaining allowance, never the account's.
    quota: dict
    #: Water reviews this deployment applies: ``[{reviewId, version, label, title, bbox}]``.
    water_reviews: list[dict] = Field(default_factory=list)
    category_directory_version: str = catalog.VERSION
    category_directory: list[CategoryDirectoryEntry] = Field(default_factory=catalog.major_directory)


def new_trace(*, isochrone_hash: str, result_hash: str, data_versions: dict,
              budgets: dict, extra_rule_versions: dict | None = None) -> TraceEvidence:
    """The trace of one revision. A stage names the rule versions it added."""
    return TraceEvidence(
        data_versions=data_versions,
        rule_versions={"distance": RULE_VERSION, "status_threshold_s": "900",
                       **(extra_rule_versions or {})},
        isochrone_hash=isochrone_hash, result_hash=result_hash, budgets=budgets)


def generated_at() -> float:
    return time.time()
