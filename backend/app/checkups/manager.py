"""Sequential checkup orchestration over the explicit engine registry.

One task runs at a time. The shared account-level gate already paces every
route attempt, and the first release deliberately does not add in-flight
concurrency before its own timing acceptance. A second request is queued
rather than rejected.

``quota`` is the application's one allocation entry. A stage takes its attempts
from a pool built here for the task, so nothing can spend outside the ledger and
a task's own pools stay separate from every other task's.

A run publishes one immutable revision per stage: the boundary first, then the
facility retrieval over it, then the service-accessibility assessment, the
verification and finally the report. Each revision is a complete document, so a
reader never has to join two files to see what was established, and the result
hash of the later revision covers every group it repeats.
"""
import asyncio
import logging
import json
import math
import time
from dataclasses import dataclass
from contextlib import AsyncExitStack
from types import SimpleNamespace
from uuid import uuid4

from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken

from ..accessibility.grid import AssessmentCancelled
from ..algorithms.osm_offline.lazy import resolve
from ..cache import KeyedCache
from ..catalog import major_of, poi_keys
from ..contracts import Issue, Origin
from ..engines import EngineContext, IsochroneAsk, IsochroneSnapshot, canonical_hash
from ..engines.protocol import EngineCancelled
from ..poi import plan as poi_plan
from ..poi.online import coarse_blocks
from ..poi.planner import RULES as POI_RULES
from ..quota import PLACE, attach_token
from ..request_control import RequestStopped, wait_with_cancel
from .accessibility_stage import AccessibilityOutcome, assess_accessibility
from .facilities import FacilityOutcome, collect_facilities, stale_for
from .facility_stage import query_domain
from .models import (DEFAULT_POI_REQUESTS, DETAIL_ROUTE_REQUESTS, DISTANCE_RULE,
                     EXTENSION_TERMINAL, MAX_POI_REQUESTS, QUERY_PADDING_M, RULE_VERSION, TERMINAL,
                     CheckupFacilities, CheckupRequest, CheckupSnapshot, CheckupTaskView, EngineRef,
                     FacilityExtensionDocument, FacilityExtensionRequest, FacilityExtensionView,
                     FacilityGroup, FacilityRetryRequest, FacilityRetryView, FacilityRoute,
                     ReportEvidence, RetainedCheckupView, RetentionView, ScopeEvidence,
                     SessionOpenRequest, SessionView, TaskProgress, new_trace)
from . import retention
from .places import declared_identity
from .progress import StepReporter, category_label
from .continuation import ContinuationMixin
from .reporting_stage import build_report
from .routes import DETAIL_POOL, ReplayRoutes, RoutesUnavailable, open_online as open_routes
from .store import (CheckupStore, ExtensionNotFound, RequestIdConflict, TaskNotFound,
                    extension_identity, extension_matches)
from .verification_stage import (VerificationOutcome, carried_over, judge_route, compatible_verification,
                                 refusal as verification_refusal, verify_facilities)

logger = logging.getLogger(__name__)

# Engines currently enforce their own internal deadline; this is the task-level
# bound every stage shares.
DEADLINE_SECONDS = 1800
# §5 B2 决策 2：会话租约。最后一个标签页离开后给这么多时间（刷新、短暂断网、多标签页
# 共享会话都不该被误判成"关掉了浏览器"），之后这个会话的数据就到期。
SESSION_LEASE_SECONDS = 300.0
#: 该次体检之后的第几次新体检结束就算它到期。失败与取消也计数；幂等重发、重试、补查
#: 都不新建任务行，因此不计数。
CHECKUPS_BEFORE_EXPIRY = 3
#: 后台巡检的间隔。到期是一个**事件**（最后一次心跳、第三次后续体检结束），没有请求会
#: 因为它的到来而发生 —— 所以必须有人定期去看，不能只在读取时判定。
MAINTENANCE_SECONDS = 60.0
RETENTION_NOTE = ("明细（设施名称、UID、地址与坐标）已按保留期到期不再提供；"
                  "这里的结论、分数与汇总是到期前定稿的那一份，未重算。")
ENGINE_UNAVAILABLE = {
    # 两个引擎都要百度端点证据，所以这句话不再点名 E8.2。
    "walking_ak_not_configured": "后端未配置百度步行服务 AK，需要百度端点证据的成圈无法运行。",
    # 离线合成模式连一个端点都不会问，所以它在成圈之前就必须被拒绝。
    "synthetic_mode_offline": "当前为离线合成模式，OSM＋百度的成圈需要百度端点核验，本地没有替身，无法运行。",
}
LATER_STAGES_NOTICE = "服务覆盖、灰区与报告阶段尚未接入，本修订只包含成圈结果与设施检索。"
#: The accessibility revision runs before verification, so its grey zones are
#: model-only *in that revision*; the next revision carries the route evidence.
VERIFICATION_NOTICE = "本修订尚未包含现实核验：这一版的灰区只有模型证据，核验阶段在其后发布。"
#: 修订文档里的空间分析组。摘要覆盖的正是这一组，不多不少。
ANALYSIS_FIELDS = ("accessibility", "service_gaps", "heatmap", "scores", "verification", "report")
#: Published with the analysis group and covered by its digest, but only when
#: present: revisions frozen before water reviews existed keep their identity.
OPTIONAL_ANALYSIS_FIELDS = ("water",)
#: 按任务保存的详情桶上限。它记在进程内：保护账户的是持久账本（每次尝试都先预约），
#: 重启或淘汰之后这个任务的详情额度从 20 重新计起，账本上的消耗不会被抹掉。任务记录的
#: 累计计数写的是整趟流水线的用量，点击详情不改写它 —— 让它和正在跑的阶段互相覆盖，
#: 只会把两边都算错。
#: 按需补查自带的三条口径，随结果一起冻结：它们是读这份结果时必须知道的事。
EXTENSION_NOTES = (
    "按需补查：复用原体检已经算出的圈面，只再跑一次设施检索，不重新成圈。",
    "扩展类别只做点位、数量和分类展示：不进入核心综合分，也不进入综合灰区，"
    "原体检的修订、评分与报告不因一次补查而改变。",
    "关键词检索查完不等于现实目录完整：这里的数量是本次检索到的记录，不是设施总量。",
)

DETAIL_BUCKETS = 256


class CheckupError(Exception):
    """Wire error carrying the v2 status and code."""

    def __init__(self, status_code: int, code: str, message: str):
        self.status_code, self.code, self.message = status_code, code, message
        super().__init__(code)


def resolve_budget(capabilities, requested: int | None) -> int:
    """Validate the tier against the named engine. No tier is shared, and the
    other algorithm is never entered as a substitute."""
    budget = capabilities.default_budget if requested is None else requested
    if budget not in capabilities.budgets:
        raise CheckupError(422, "checkup_unsupported_budget",
                           f"engine {capabilities.engine_id} supports budgets "
                           f"{list(capabilities.budgets)}")
    return budget


def business_status_for(quality: str, facilities_status: str | None = None,
                        analysis_status: str | None = None) -> str:
    """How much of the checkup this revision can stand behind.

    Never ``complete``: the catalogue is never independently verified (§4.4), so
    even a run where every stage succeeded is ``partial`` — what it retrieved is
    not the same claim as what exists. A boundary the engine itself called
    unusable is ``insufficient``, and so is a facility stage or an assessment
    that could not run at all: without either, the revision cannot answer the
    question the checkup exists for, whatever the boundary is worth. A group that
    merely has gaps in it is not by itself ``insufficient`` — that is ``partial``,
    and the stage that is incomplete says which one it is.
    """
    if quality == "insufficient":
        return "insufficient"
    if facilities_status == "failed":
        return "insufficient"
    if analysis_status == "failed":
        return "insufficient"
    return "partial"


def _analysis_document(analysis: dict | None) -> dict | None:
    """The analysis group in its published form; the digest covers exactly this.

    ``None`` for a group that was never established, so a revision that never ran
    the assessment is not identified as one that ran it and found nothing.
    """
    if not analysis or all(analysis.get(field) is None for field in ANALYSIS_FIELDS):
        return None
    return {field: (None if analysis.get(field) is None
                    else analysis[field].model_dump(mode="json", by_alias=True))
            for field in ANALYSIS_FIELDS + OPTIONAL_ANALYSIS_FIELDS
            if field in ANALYSIS_FIELDS or analysis.get(field) is not None}


def _data_versions(osm: str, engine: str, analysis: dict | None) -> dict:
    """Every dataset a revision was computed from. ``waterReviews`` is listed
    even when empty once the water evidence exists, so "no review applied here"
    and "computed before reviews existed" read differently."""
    versions = {"osm": osm, "engine": engine}
    water = (analysis or {}).get("water")
    if water is not None:
        versions["waterReviews"] = [review["label"] for review in water.reviews]
    return versions


def _analysis_status(analysis: dict | None) -> str:
    evidence = (analysis or {}).get("accessibility")
    return "not_integrated" if evidence is None else evidence.status


class _SpentBudget:
    """A finished task's pools as its last revision froze them: a recompute spends none."""

    def __init__(self, state: dict):
        self._state = state

    def state(self) -> dict:
        return json.loads(json.dumps(self._state))


def _point(value) -> Origin | None:
    """一个观测端点，按契约的对象形式返回；没有就是 None。"""
    if value is None:
        return None
    lng, lat = value
    return Origin(lng=lng, lat=lat)


def _service_sources(group) -> list[dict] | None:
    """The facilities that can serve the assessed places: the counted ones plus the
    accepted ones just outside the boundary (§2.3). Counts never read this list."""
    if group is None:
        return None
    return list(group.facilities) + list(getattr(group, "nearby_facilities", None) or [])


def _incomplete(group) -> dict | None:
    """The retrieval's unfinished query blocks; None when this revision never recorded them."""
    return None if group is None else getattr(group, "query_incomplete_regions", None)


def _assessment_progress(report):
    """Translate the assessment's own step callbacks into stored progress.

    The assessment runs in a worker thread; the store opens its own connection
    per write, so writing from there is safe. Cell ticks are throttled by the
    reporter; every step change is written at once.
    """
    def progress(step: str, *, major: str | None = None, index: int | None = None,
                 total: int | None = None, cells: int | None = None) -> None:
        if step == "category":
            report("category", key=major, count=cells, label=category_label(major, index, total),
                   throttle=bool(cells))
        else:
            report(step)
    return progress


@dataclass(frozen=True)
class _ExtensionBoundary:
    """What a new extension over one task would search, resolved on demand.

    ``revision`` is the revision the new request binds to; ``requested`` is the
    budget it will be given. Both are read only for a genuinely new request, so an
    already-accepted request id can be answered without the parent's newest revision
    being usable at all.
    """

    domain: object
    origin: tuple
    minors: tuple
    blocks: int
    requested: int
    revision: int


class CheckupManager(ContinuationMixin):
    def __init__(self, settings, registry, store: CheckupStore, quota, place_factory=None,
                 route_factory=None, offline=None):
        self.settings, self.registry, self.store, self.quota = settings, registry, store, quota
        # §3.4: a page is reusable across tasks only inside the configured window,
        # and inside its own task when no window is configured. One cache for the
        # process, outside every quota pool, so a hit costs no attempt.
        self.cache = KeyedCache(freshness_seconds=settings.cache_freshness_seconds,
                                reused=self._source_still_retained)
        # The seam a deployment without a key, or an offline run, substitutes at.
        # ``route_factory`` is the same seam for the walking routes the
        # verification stage and the facility-detail endpoint ask for.
        self.place_factory = place_factory
        self.route_factory = route_factory
        # The walking graph the assessment runs on. It is not engine-specific: a
        # boundary computed by either algorithm is assessed against the same OSM
        # network, so the two engines differ in how the circle was drawn, not in
        # what "within 1000 m on foot" means.
        self.offline = offline
        # 详情桶按任务保存，点击设施的路线与核验阶段共享同一个任务的额度。
        self.detail_budgets: dict[str, object] = {}
        self.tokens: dict[str, CancelToken] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        # 按需补查有自己的队列和 worker：它不进主流程的修订链，所以一次补查失败或
        # 被取消都不会碰到已经发布的报告。它和主流程共用同一个 place 服务池，
        # 两者加起来仍然只有一个调度点、一本日账。
        self.extension_queue: asyncio.Queue[str] = asyncio.Queue()
        self.extension_worker: asyncio.Task | None = None
        self.extension_tokens: dict[str, CancelToken] = {}
        self.closing = False
        #: 后台巡检任务（见 ``start_maintenance``）：由部署的启动钩子启动，关闭时取消。
        self.maintenance: asyncio.Task | None = None

    # -- 会话与保留期（§5 B2 决策 2）------------------------------------------

    def open_session(self, payload: SessionOpenRequest) -> SessionView:
        """开一个会话或回到它；带上已有的会话标识时，**已到期就是一堵墙**。

        这条规则是整个期限的地基：迟到的心跳若能复活一个已到期的会话，那么"关闭浏览器
        （或三次后续体检）即到期"就只剩一个说法 —— 数据还在，而且随时可以被重新认领。
        """
        now = time.time()
        if payload.session_id is None:
            session_id = str(uuid4())
            row = self.store.start_session(session_id=session_id,
                                           tab_id=payload.tab_id or str(uuid4()), now=now)
            return self._session_view(row, tab_id=payload.tab_id or self._only_tab(session_id),
                                      resumed=False, now=now)
        row = self.store.session_row(payload.session_id)
        if row is None:
            # 服务端不认识这个标识（换了后端、清了库）：新开一个，而不是假装续上了。
            session_id = str(uuid4())
            row = self.store.start_session(session_id=session_id,
                                           tab_id=payload.tab_id or str(uuid4()), now=now)
            return self._session_view(row, tab_id=payload.tab_id or self._only_tab(session_id),
                                      resumed=False, now=now)
        if self._session_expired(row, now=now):
            self.store.expire_session(session_id=payload.session_id, now=now)
            raise CheckupError(
                409, "checkup_session_expired",
                "这个浏览会话已经到期（关闭浏览器或后续体检已超过期限），它的数据不再保留；"
                "请刷新页面开始一个新的会话。")
        tab_id = payload.tab_id or str(uuid4())
        self.store.touch_session(session_id=payload.session_id, tab_id=tab_id, now=now, reopen=True)
        self.store.open_tab(session_id=payload.session_id, tab_id=tab_id, now=now)
        return self._session_view(self.store.session_row(payload.session_id), tab_id=tab_id,
                                  resumed=True, now=now)

    def heartbeat(self, session_id: str, tab_id: str) -> SessionView:
        """标签页续租。已到期的会话不能被迟到的心跳复活。"""
        now = time.time()
        row = self.store.session_row(session_id)
        if row is None:
            raise CheckupError(404, "checkup_session_not_found", "这个浏览会话不存在或已被清理")
        if self._session_expired(row, now=now):
            self.store.expire_session(session_id=session_id, now=now)
            raise CheckupError(409, "checkup_session_expired",
                               "这个浏览会话已经到期，它的数据不再保留；请刷新页面开始新的会话。")
        if self.store.touch_session(session_id=session_id, tab_id=tab_id, now=now,
                                    reopen=False) is None:
            raise CheckupError(404, "checkup_session_not_found", "这个浏览会话不存在或已被清理")
        return self._session_view(self.store.session_row(session_id), tab_id=tab_id,
                                  resumed=True, now=now)

    def close_tab(self, session_id: str, tab_id: str) -> SessionView:
        """一个标签页离开。它是**记录**，不是判决：会话是否到期由最后离开的时刻加宽限决定。"""
        now = time.time()
        row = self.store.session_row(session_id)
        if row is None:
            raise CheckupError(404, "checkup_session_not_found", "这个浏览会话不存在或已被清理")
        self.store.close_tab(session_id=session_id, tab_id=tab_id, now=now)
        return self._session_view(self.store.session_row(session_id), tab_id=tab_id,
                                  resumed=True, now=now)

    def _only_tab(self, session_id: str) -> str:
        tabs = self.store.session_tabs(session_id)
        return tabs[0]["tab_id"] if tabs else ""

    def _session_view(self, row, *, tab_id: str, resumed: bool, now: float) -> SessionView:
        tabs = self.store.session_tabs(row["session_id"])
        return SessionView(
            session_id=row["session_id"], tab_id=tab_id, lease_seconds=SESSION_LEASE_SECONDS,
            expires_at=self._lease_end(row, tabs=tabs) + SESSION_LEASE_SECONDS,
            resumed=resumed,
            open_tabs=sum(1 for tab in tabs if tab["closed_at"] is None),
            tasks=len(self.store.session_ids_of_tasks(row["session_id"])))

    @staticmethod
    def _lease_end(row, *, tabs) -> float:
        """最后一个标签页**还在**的时刻：它离开的时刻，或它最后一次续租的时刻。"""
        instants = [tab["closed_at"] if tab["closed_at"] is not None else tab["last_seen_at"]
                    for tab in tabs]
        return max([row["created_at"], *instants])

    def _session_expired(self, row, *, now: float) -> bool:
        if row["expired_at"] is not None:
            return True
        return now >= self._lease_end(row, tabs=self.store.session_tabs(row["session_id"])) \
            + SESSION_LEASE_SECONDS

    def sweep_retention(self, *, now: float | None = None) -> dict:
        """把已经到期的明细删掉：先落墓碑，再删文件，最后丢掉内存里的那一份。

        顺序是刻意的。墓碑先写：它让两个并发的巡检者里只有一个真的去删，而"已经清理过"
        这件事在删到一半失败之后仍然成立、仍然可以重试。文件只删
        ``checkup_dir/tasks/<task_id>`` 这一个目录 —— 额度账本、OSM 数据、用户文件都不在
        这个根下，也一个都不碰。

        只处理**终态**任务：正在跑的体检当然也已经到期（会话关闭时它就被取消了），
        但它的明细是这一轮正在写的东西，删除要先等它停下来。
        """
        now = time.time() if now is None else now
        cleared, failed = [], []
        for record in self.store.terminal_tasks():
            if record.details_cleared_at is not None:
                continue
            if self.retention_of(record, now=now).details_available:
                continue
            if not self.store.mark_details_cleared(record.task_id, now=now):
                continue
            leftovers = retention.delete_details(self.store.root, record.task_id)
            self.cache.drop_task(record.task_id)
            self.detail_budgets.pop(record.task_id, None)
            (failed if leftovers else cleared).append(record.task_id)
        return {"cleared": cleared, "failed": failed}

    def start_maintenance(self, interval: float = MAINTENANCE_SECONDS) -> asyncio.Task:
        """定期做两件没有请求会来触发的事：判会话到期、清到期的明细。

        它必须由部署的启动钩子启动，而不是靠某次读取顺手做：到期恰恰发生在**没有人看**
        的时候（最后一个标签页关掉之后），只靠读取触发就等于"没人看就永远不清"。
        """
        async def loop():
            while not self.closing:
                try:
                    self.expire_due_sessions()
                    self.sweep_retention()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("checkup retention maintenance failed")
                await asyncio.sleep(interval)

        self.maintenance = asyncio.create_task(loop())
        return self.maintenance

    def retained(self, task_id: str, revision: int | None = None) -> RetainedCheckupView:
        """明细到期之后仍然可以给出的东西：结论、汇总与到期原因。

        它从**冻结的汇总**读，不回头去读那份可能已经删掉的修订文件：到期之后还能不能
        回答这个问题，不能取决于文件还在不在。
        """
        record = self.get(task_id)
        stored = self.store.revision_row(task_id, revision)
        if stored is None:
            if revision is not None:
                raise CheckupError(409, "checkup_revision_not_found",
                                   f"这次体检没有第 {revision} 版修订")
            raise CheckupError(409, "checkup_result_not_ready", "尚无可用结果快照")
        summary = self.store.summary(task_id, stored["revision"])
        if summary is None:
            raise CheckupError(409, "checkup_retained_summary_missing",
                               "这次体检的那一版没有留存汇总，只有任务层面的状态可用")
        return RetainedCheckupView(
            task_id=task_id, revision=stored["revision"], stage=stored["stage"],
            business_status=record.business_status or "partial", result_hash=stored["result_hash"],
            summary=summary, retention=self.retention_of(record),
            notes=[RETENTION_NOTE])

    def retention_of(self, record, *, now: float | None = None) -> RetentionView:
        """这份结果的明细还能不能提供（§5 B2 决策 2）。

        两条期限**任一先到即过期**：会话租约（最后标签页离开 + 宽限），以及"这次体检
        之后的第三次新体检结束"。另外两条是历史口径：早于本库保留期基线的记录视为已过期；
        已经清理过的记录按墓碑回答。

        `expires_at` 报的是**两个期限里更早的那个**：界面上"还能看到什么时候"这个问题
        只有一个答案。
        """
        now = time.time() if now is None else now
        if record.details_cleared_at is not None:
            return RetentionView(details_available=False, reason="cleared", cleared=True)
        if record.created_at < self.store.retention_baseline_at():
            # 运营者的口径：已经落盘、没有会话归属的历史报告视为已过期（明细清除，
            # 保留不含明细的汇总）。判据是"比本库开始记录保留期的时刻早"，所以一个
            # 新建的库（测试、临时部署）里不会有任务被当成历史数据。
            return RetentionView(details_available=False, reason="legacy")
        arms: list[tuple[str, float]] = []
        if record.session_id is not None:
            row = self.store.session_row(record.session_id)
            if row is None:
                arms.append(("session_closed", record.created_at))
            else:
                tabs = self.store.session_tabs(record.session_id)
                arms.append(("session_closed",
                             self._lease_end(row, tabs=tabs) + SESSION_LEASE_SECONDS))
        try:
            later = self.store.later_terminal_instants(record.task_id,
                                                       limit=CHECKUPS_BEFORE_EXPIRY)
        except TaskNotFound:
            # 手里这个记录在库里没有对应行（构造出来的记录、或者一行被清掉的库）：
            # 没有提交顺序可数，这一条期限就**不成立**，而不是让整个视图报错。
            later = []
        if len(later) == CHECKUPS_BEFORE_EXPIRY and later[-1] is not None:
            arms.append(("superseded", later[-1]))
        if not arms:
            # 没有人认领、也还没有第三次后续体检：没有期限可言，如实报"未记录"。
            return RetentionView(details_available=True)
        reason, expires_at = min(arms, key=lambda arm: arm[1])
        return RetentionView(details_available=now < expires_at, expires_at=expires_at,
                             reason=None if now < expires_at else reason)

    def expire_due_sessions(self, *, now: float | None = None) -> list[str]:
        """把"最后一个标签页离开已经超过宽限"的会话判成过期，并停掉它们正在跑的工作。

        停止是必须的，不是顺手做的：会话到期说的是"这个人不再被授权看这些数据了"，
        那么继续为它花额度、往它名下写新明细都违反了同一条规则。
        """
        now = time.time() if now is None else now
        expired = self.store.expire_sessions_without_live_tabs(now=now, grace=SESSION_LEASE_SECONDS)
        for session_id in expired:
            for task_id in self.store.session_ids_of_tasks(session_id):
                record = self.store.get(task_id)
                if record.status in ("queued", "running", "cancelling"):
                    self.cancel(task_id)
        return expired



    def _source_still_retained(self, task_id: str) -> bool:
        """跨任务复用的一票否决：来源任务的明细还在期限内吗（§5 B2 决策 2）。

        这是"复用不重置来源期限"唯一能被执行的时刻。放进缓存而不是每个调用方各判一次，
        是因为缓存是唯一知道"这一页是谁的"的地方；调用方只看得到页面内容，而内容不会说
        自己是谁取回来的。
        """
        try:
            return self.retention_of(self.store.get(task_id)).details_available
        except TaskNotFound:
            # 任务行都没了（被清理过的库）：没有期限可继承，也没有数据可复用。
            return False

    def interrupt_unfinished(self) -> int:
        """Sweep tasks this process did not finish, as a restart does.

        Called once when the server starts serving, never from a constructor:
        an interrupted task stops being "running" because a new deployment took
        over, and merely building the app object is not that.
        """
        return self.store.interrupt_unfinished()

    def start(self) -> None:
        """Start the single worker on first use, so no lifecycle hook is required."""
        if self.worker is None or self.worker.done():
            self.closing = False
            self.worker = asyncio.create_task(self._serve())
        if self.extension_worker is None or self.extension_worker.done():
            self.extension_worker = asyncio.create_task(self._serve_extensions())

    def submit(self, payload: CheckupRequest, *, session_id: str | None = None
               ) -> tuple[CheckupTaskView, bool]:
        engine = self.registry.get(payload.engine)
        capabilities = engine.capabilities()
        budget = resolve_budget(capabilities, payload.isochrone.budget)
        # A deployment that cannot run the engine at all says so before a task
        # exists, instead of admitting one that can only fail without a reason.
        reason = getattr(engine, "unavailable_reason", lambda: None)()
        if reason is not None:
            raise CheckupError(503, "checkup_engine_unavailable", ENGINE_UNAVAILABLE.get(reason, reason))
        try:
            record, created = self.store.create(
                task_id=str(uuid4()), client_request_id=payload.client_request_id,
                engine=payload.engine, fingerprint=canonical_hash(payload.fingerprint()),
                payload=payload.model_dump(mode="json"), budget=budget,
                # 会话归属是**这一次体检**的保留期限之一（§5 B2 决策 2）。没有它也能建
                # 任务（命令行、脚本、旧客户端），但那样只剩"三次后续体检"这一条期限。
                session_id=session_id)
        except RequestIdConflict:
            raise CheckupError(409, "checkup_request_id_conflict",
                               "该请求标识已用于不同参数的体检") from None
        if created:
            self.queue.put_nowait(record.task_id)
            self.start()
        return self.view(record), created

    # -- execution ---------------------------------------------------------

    async def _serve(self) -> None:
        # The condition, not just the cancellation, ends this loop. A stage may absorb
        # the CancelledError delivered while it was running — a planner reports a
        # cancelled run as an outcome, with the evidence it already had — and a worker
        # that only relied on the exception would then block here forever, holding up
        # shutdown on a queue nothing will fill again.
        while not self.closing:
            task_id = await self.queue.get()
            try:
                if not self.closing:
                    await self._run(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A task must reach a terminal state even when orchestration
                # itself fails; the reason is recorded, never the raw exception.
                # The traceback still goes to the log: without it, "orchestration
                # failed" is the only thing anyone can ever see, and the failing
                # line is unreachable by any test that goes through HTTP.
                logger.exception("checkup orchestration failed: %s", task_id)
                self._finish(task_id, status="failed", error="orchestration_failed")
            finally:
                self.queue.task_done()

    async def _run(self, task_id: str) -> None:
        if not self.store.claim(task_id):
            return
        record = self.store.get(task_id)
        payload = CheckupRequest.from_stored(record.payload)
        token = CancelToken()
        self.tokens[task_id] = token
        try:
            current = self.store.round(task_id)
            if current is not None and current['number'] > 1:
                await self._continue_stages(task_id, payload, record, token)
            else:
                await self._stages(task_id, payload, record, token)
        except (AssessmentCancelled, RequestStopped):
            self._finish(task_id, status='cancelled')
        finally:
            self.tokens.pop(task_id, None)

    async def _stages(self, task_id: str, payload: CheckupRequest, record, token) -> None:
        origin = normalize((payload.center.lng, payload.center.lat))
        # One budget for the whole task, decided at claim time: every pool is
        # fixed before the first attempt, so a stage can only spend its own
        # allowance and none of them can be widened by a later stage (§9.2).
        budget = self.quota.task_budget(
            isochrone=record.budget, poi=payload.facilities.max_poi_requests,
            route=payload.facilities.max_route_requests, detail=DETAIL_ROUTE_REQUESTS)
        # 这一趟的桶和点击详情用的是同一个对象，所以"一个任务 20 次详情"是共享的一个
        # 额度，而不是各记一本账。
        self._remember_budget(task_id, budget)
        # Every write of an in-stage step goes through this one reporter, so the
        # step's own clock survives the counter ticks that follow it.
        report = StepReporter(lambda **fields: self.store.update(task_id, **fields))
        # The 800 tier's boundary may take up to 20 minutes on its own; the later
        # stages keep their share of the task deadline.
        context = EngineContext(
            task_id=task_id, token=token,
            deadline=time.monotonic() + DEADLINE_SECONDS + (600 if record.budget > 400 else 0),
            artifact_dir=self.store.artifact_dir(task_id),
            # The engine names its own sub-stage; the counts are its attempts
            # against the tier and what of them actually went to the network.
            on_progress=lambda snapshot: None if token.cancelled else report(
                snapshot.stage, count=snapshot.requests, limit=snapshot.budget,
                requests=snapshot.requests, network_requests=snapshot.network_requests),
            # Its preparation counts edges or nothing: the task counters stay as they are.
            on_step=lambda *args, **kwargs: None if token.cancelled else report(*args, **kwargs))
        try:
            snapshot = await wait_with_cancel(self.registry.get(record.engine).compute(
                IsochroneAsk(origin=origin, budget=record.budget), context), token)
        except (EngineCancelled, RequestStopped):
            # The user's cancel stopped the engine: the task ends, the worker serves on.
            self._finish(task_id, status="cancelled")
            return
        except asyncio.CancelledError:
            # The asyncio task itself is being cancelled (shutdown): never swallowed.
            self._finish(task_id, status="cancelled")
            raise
        except Exception:
            self._finish(task_id, status="failed", error="engine_execution_failed")
            return
        # The boundary revision is frozen even when the run is then cancelled:
        # an orderly stop never discards a result that has been paid for.
        self._publish_isochrone(task_id, payload, snapshot, budget)
        self.store.begin_round(task_id, payload.client_request_id, 0,
                               self._round_identity(payload, snapshot), initial=True,
                               poi_limit=payload.facilities.max_poi_requests)
        self._bind_round(task_id, budget)
        if token.cancelled:
            self._finish(task_id, status="cancelled",
                         business_status=business_status_for(snapshot.quality, "not_integrated"))
            return
        # The stage lasts as long as its queries do. The task counters follow the
        # pool as each attempt is reserved -- the boundary's own network count
        # plus what this stage has sent so far -- and the publish below writes
        # the stage's final account over them.
        self.store.update(task_id, stage="poi")
        boundary_network = snapshot.network_requests
        outcome = await collect_facilities(
            payload, snapshot, settings=self.settings, context=context, quota=self.quota,
            budget=budget, cache=self.cache, places_factory=self.place_factory, store=self.store,
            progress=lambda sent, limit, label=None: report(
                "places", count=sent, limit=limit, label=label, requests=boundary_network + sent,
                network_requests=boundary_network + sent))
        business = self._publish_facilities(task_id, payload, snapshot, budget, outcome)
        if token.cancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        try:
            assessment = await self._publish_accessibility(task_id, payload, snapshot, budget, outcome,
                                                           report=report, token=token)
        except AssessmentCancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        if token.cancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        # §6.3 verification asks for real routes before the report is assembled,
        # so the report can say what was verified and what stayed model-only.
        verification, assessment = await self._publish_verification(
            task_id, payload, snapshot, budget, outcome, assessment, deadline=context.deadline,
            report=report, token=token)
        if token.cancelled:
            self._finish(task_id, status="cancelled", business_status=business)
            return
        # §7.2 reporting repeats the assessment rather than recomputing it.
        self.store.update(task_id, stage="reporting")
        report("report")
        # Assembling and writing the report takes a second or more; off the event
        # loop, so status polls are answered meanwhile.
        business = await wait_with_cancel(asyncio.to_thread(
            self._publish_reporting, task_id, payload, snapshot, budget,
            outcome, assessment, verification, token=token), token, keep_running=True)
        if token.cancelled:
            self._finish(task_id, status='cancelled', business_status=business)
            return
        self._finish(task_id, status="completed", business_status=business)
        self.store.update(task_id, stage="ready")

    # -- revisions ---------------------------------------------------------

    def _budgets(self, budget, snapshot) -> dict:
        """Every pool of the task and what each has spent so far.

        The isochrone entry reports what the engine counted **on the network**:
        both algorithms still pace on the legacy shared gate (§9.1) and stop on
        their own tier, so their attempts are not drawn from this counter. What
        the pool is for is bounding paid usage, and a run whose provider never
        leaves the process -- the synthetic transport, and tests -- spends none
        of it, however many boundary samples it computed locally.
        """
        state = budget.state()
        if getattr(budget, 'poi_prior', 0):
            state['poi']['spent'] += budget.poi_prior
            state['poi']['limit'] += budget.poi_prior
        state["isochrone"]["spent"] = snapshot.network_requests
        return state

    def _base(self, task_id: str, payload: CheckupRequest, snapshot, *, revision: int, stage: str,
              business: str, budgets: dict, result_hash: str, warnings: list,
              facilities: dict | None, facilities_status: str, analysis: dict | None = None,
              extra_rules: dict | None = None, recomputed: dict | None = None,
              retried: dict | None = None) -> dict:
        """The part of a revision that does not depend on which stage published it.

        ``analysis`` is the group of spatial objects this revision carries; the
        digest covers exactly the same objects, so a group cannot be published
        under a digest that would not change with it.

        Whatever is still outstanding is appended here rather than by each stage,
        so a revision cannot forget to say which part of the checkup it does not
        contain — that notice is the difference between "nothing is missing" and
        "nobody looked".

        ``recomputed`` and ``retried`` are kept apart because they are not the same
        fact: a recompute spends nothing and changes the data behind the same
        evidence, while a retry spends new requests and adds evidence. A reader who
        cannot tell them apart cannot tell whether a revision cost money.
        """
        analysis = analysis or {}
        document = _analysis_document(analysis)
        warnings = list(warnings) + self._pending_warnings(document, analysis)
        trace = new_trace(isochrone_hash=snapshot.isochrone_hash, result_hash=result_hash,
                          data_versions=_data_versions(self.settings.osm_data_version,
                                                       snapshot.engine_version, analysis),
                          budgets=budgets, extra_rule_versions=extra_rules)
        if recomputed is not None:
            trace = trace.model_copy(update={"recomputed": recomputed})
        if retried is not None:
            trace = trace.model_copy(update={"retried": retried})
        return dict(
            task_id=task_id, revision=revision, generated_at=time.time(), center=payload.center,
            stage=stage, business_status=business,
            engine=EngineRef(engine_id=snapshot.engine_id, engine_version=snapshot.engine_version,
                             label=self.registry.get(snapshot.engine_id).capabilities().label),
            rules=DISTANCE_RULE,
            scope=ScopeEvidence(
                projection=self.settings.osm_metric_crs,
                data_version=self.settings.osm_data_version,
                coverage_supported=document is not None,
                assessment_domain_available=bool(
                    (analysis.get("accessibility") is not None
                     and analysis["accessibility"].domain_area_m2 is not None)),
                excluded_area_m2=None if analysis.get("accessibility") is None
                    else analysis["accessibility"].excluded_area_m2,
                model_support_available=bool(
                    analysis.get("accessibility") is not None
                    and analysis["accessibility"].status != "failed"),
                notes=self._pending_notices(document, analysis)),
            trace=trace,
            facilities=facilities, facilities_status=facilities_status,
            accessibility_status=_analysis_status(analysis), warnings=warnings,
            **{field: analysis.get(field) for field in ANALYSIS_FIELDS + OPTIONAL_ANALYSIS_FIELDS})

    def _pending_notices(self, document: dict | None, analysis: dict | None) -> list:
        """What this revision does not contain, in the reader's words.

        The analysis group absent means the assessment never ran at all; present
        without verification means it ran and every gap in it is model-only. Both
        are limitations of *this* revision, so both belong in its scope notes.
        """
        if document is None:
            return [LATER_STAGES_NOTICE]
        verification = (analysis or {}).get("verification")
        # 具名拒绝（这个部署没有路线服务）仍然是一次缺席：它带着原因发布在自己的字段里，
        # 但这一版确实没有现实核验，所以那条提示照发。
        if verification is None or getattr(verification, "status", None) == "not_integrated":
            return [VERIFICATION_NOTICE]
        return []

    def _pending_warnings(self, document: dict | None, analysis: dict | None) -> list:
        return [Issue(code="STAGES_NOT_INTEGRATED", message=notice, scope="checkup",
                      severity="pending")
                for notice in self._pending_notices(document, analysis)]

    def _result_hash(self, isochrone: dict, facilities: dict | None, *, facilities_status: str,
                     analysis: dict | None = None) -> str:
        """The full result digest, never the boundary-only one.

        It covers the boundary, the rules, the facility stage and the whole
        analysis group, so a document can never be re-identified after a group it
        repeats is changed, and it is owned by the store as the revision's identity.

        The stage's status belongs in the digest, not only its group: a stage
        that could not run is null exactly as a stage that never ran is, and the
        two revisions would otherwise share one identity while saying different
        things about the facility retrieval.
        """
        return canonical_hash({"isochrone": isochrone,
                               "rules": DISTANCE_RULE.model_dump(mode="json"),
                               "facilitiesStatus": facilities_status,
                               "facilities": facilities,
                               "analysis": _analysis_document(analysis)})

    def _common_warnings(self, snapshot) -> list:
        return [Issue(code="ALGORITHM_WARNING", message=message, scope="isochrone")
                for message in snapshot.warnings]

    def _publish_isochrone(self, task_id: str, payload: CheckupRequest, snapshot, budget) -> None:
        revision = self.store.get(task_id).revision + 1
        # The embedded engine snapshot uses the same wire naming as the rest of
        # the document, so a stored revision round-trips without translation.
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        result_hash = self._result_hash(isochrone, None, facilities_status="not_integrated")
        warnings = self._common_warnings(snapshot)
        document = CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="isochrone",
                         business=business_status_for(snapshot.quality),
                         budgets=self._budgets(budget, snapshot), result_hash=result_hash,
                         warnings=warnings, facilities=None, facilities_status="not_integrated"))
        self._publish(task_id, "isochrone", document, revision, result_hash)
        # The engine consumed what the progress callback last reported; the
        # frozen snapshot is the authoritative count for the finished stage.
        # 两列都记网络尝试：本任务"已用多少次"说的是花了多少付费额度，而合成与离线路径
        # 一次也没发出去。成圈自己算过的边界采样数在 snapshot.statistics 里，不会丢。
        self.store.update(task_id, requests=snapshot.network_requests,
                          network_requests=snapshot.network_requests)

    def _publish_facilities(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                            outcome: FacilityOutcome) -> str:
        """Freeze the facility revision, whatever the stage established."""
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        facilities = (outcome.group.model_dump(mode="json", by_alias=True)
                      if outcome.group is not None else None)
        result_hash = self._result_hash(isochrone, facilities,
                                        facilities_status=outcome.status)
        business = business_status_for(snapshot.quality, outcome.status)
        warnings = self._common_warnings(snapshot) + list(outcome.issues)
        document = CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="poi",
                         business=business, budgets=self._budgets(budget, snapshot),
                         result_hash=result_hash, warnings=warnings, facilities=facilities,
                         facilities_status=outcome.status,
                         extra_rules={"classification": POI_RULES["version"]}))
        self._publish(task_id, "poi", document, revision, result_hash)
        # Cumulative across the stages: the boundary's attempts were counted
        # before this stage drew from the same budget object. 只累加网络尝试，
        # 与上一阶段的写法一致 —— 否则这一列会在本阶段发布时突然跳一大截。
        self.store.update(task_id, requests=snapshot.network_requests + outcome.requests,
                          network_requests=snapshot.network_requests + outcome.network_requests)
        return business

    def _analysis_objects(self, assessment: AccessibilityOutcome, report=None,
                          verification: VerificationOutcome | None = None) -> dict:
        """The analysis group's objects, keyed by their revision field names.

        The accessibility objects are repeated in every later revision rather
        than rebuilt, so the report and the verification describe exactly the
        assessment that was frozen — not a second computation of it.
        """
        return {"accessibility": assessment.accessibility, "service_gaps": assessment.service_gaps,
                "heatmap": assessment.heatmap, "scores": assessment.scores,
                "verification": None if verification is None else verification.evidence,
                "report": report, "water": assessment.water}

    async def _publish_accessibility(self, task_id: str, payload: CheckupRequest, snapshot,
                                     budget, outcome: FacilityOutcome, *,
                                     report=None, token=None) -> AccessibilityOutcome:
        """Run and freeze the accessibility assessment (§5–§7.1).

        The graph it needs is the deployment's OSM store, not the engine's: a
        boundary from either algorithm is assessed against the same walking
        network. A deployment without a graph publishes the same revision with a
        named refusal in it, never an empty 0% — a revision that says "not
        assessed" and one that says "nothing is covered" must not look alike.
        """
        self.store.update(task_id, stage="accessibility")
        group = (None if outcome.group is None
                 else outcome.group.model_dump(mode="json", by_alias=True))
        if report is not None and self.offline is not None:
            report("graph")
        resolved = await self._resolve_offline(report, token=token)
        inner = None if report is None else _assessment_progress(report)

        def progress(step, **detail):
            # Called for every step and every cell: the cancel checkpoint of the assessment.
            if token is not None and token.cancelled:
                raise AssessmentCancelled()
            if inner is not None:
                inner(step, **detail)
        assessment = await asyncio.to_thread(
            assess_accessibility, geometry=snapshot.geometry,
            unknown_region=snapshot.unknown_region,
            facilities=_service_sources(outcome.group),
            query_status=outcome.status, majors=tuple(payload.facilities.categories),
            store=None if resolved is None else resolved.store,
            coverage=None if resolved is None else resolved.coverage,
            version=self.settings.osm_data_version, settings=self.settings,
            progress=progress, incomplete=_incomplete(outcome.group))
        objects = self._analysis_objects(assessment)
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        result_hash = self._result_hash(isochrone, group, facilities_status=outcome.status,
                                        analysis=objects)
        self._publish(task_id, "accessibility", CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="accessibility",
                         business=business_status_for(snapshot.quality, outcome.status,
                                                      assessment.status),
                         budgets=self._budgets(budget, snapshot), result_hash=result_hash,
                         warnings=self._common_warnings(snapshot) + list(outcome.issues)
                         + list(assessment.issues),
                         facilities=group, facilities_status=outcome.status, analysis=objects,
                         extra_rules={"classification": POI_RULES["version"],
                                      "accessibility": RULE_VERSION})), revision,
            result_hash)
        return assessment

    async def _publish_verification(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                                    outcome: FacilityOutcome, assessment: AccessibilityOutcome,
                                    *, deadline: float, report=None, token=None, previous=None) -> VerificationOutcome:
        """Run and freeze the route verification (§6.3).

        A deployment without a walking-route service publishes the same revision
        with a named refusal in it: the grey zones then stand on model evidence
        alone, and the report says that in those words instead of leaving the
        section out. Nothing here rewrites the assessment — a route that
        disagrees with a cell flags it for refinement, and one route never
        re-verdicts a whole cell.
        """
        self.store.update(task_id, stage="verification")
        before = self.store.get(task_id)
        progress = None if report is None else (
            lambda done, candidates, attempts: report(
                "routes", count=done, limit=candidates, requests=before.requests + attempts,
                network_requests=before.network_requests + attempts))
        heatmap = (None if assessment.heatmap is None
                   else assessment.heatmap.model_dump(mode="json", by_alias=True))
        gaps = (None if assessment.service_gaps is None else assessment.service_gaps.zones)
        carried = compatible_verification(previous, _service_sources(outcome.group), assessment.entrances)
        async with AsyncExitStack() as stack:
            try:
                routes = (self.route_factory(self.settings) if self.route_factory is not None
                          else await open_routes(self.settings, stack))
            except RoutesUnavailable as exc:
                result = (VerificationOutcome(evidence=carried, status=carried.status,
                                               overrides=carried.local_overrides) if carried is not None
                          else verification_refusal(exc.reason))
            else:
                # The session takes the task's route bucket from the direction
                # pool, so verification, both engines and the click-detail route
                # all pass the one scheduling point of their service (§9.2).
                result = await verify_facilities(
                    facilities=_service_sources(outcome.group),
                    majors=tuple(payload.facilities.categories),
                    # 冻结的灰区是模型，而阶段读的是普通字典（和 facilities 一样）：
                    # 转换摆在边界上，阶段里就不会出现"模型还是字典"的两套读法。
                    zones=None if gaps is None else [zone.model_dump(mode="json", by_alias=True)
                                                     for zone in gaps],
                    heatmap=heatmap, entrances=assessment.entrances,
                    origin=normalize((payload.center.lng, payload.center.lat)),
                    # 没有圈面时核验没有对象，而且原因在成圈不在这里。
                    boundary_available=snapshot.geometry is not None,
                    session=ReplayRoutes(attach_token(routes.session(self.quota.direction, budget=budget,
                                                        deadline=deadline), token),
                                         store=self.store, task_id=task_id, budget=budget),
                    progress=progress, token=token, previous=carried)
        # §6.3 局部重算：覆盖抽检的冲突与实测补定只改它们所在的那几格，其余格照旧由
        # 模型判定。重算不发任何请求；没有路网或被取消时保留原评估。
        if result.overrides and assessment.accessibility is not None and not (token and token.cancelled):
            local = await self._assess(snapshot, outcome, payload, verified=result.overrides, token=token)
            if local is not None:
                assessment = local
        self._freeze_verification(task_id, payload, snapshot, budget, outcome, assessment, result)
        # 核验阶段发的是真实路线请求，所以它也计入任务自己的请求数 —— 任务视图说
        # "本次体检发出了多少请求"，少算这一阶段就等于少报了一百多次调用。运行中的
        # 计数已经随每家候选写过，这里按阶段开始时的底数写定终值，不会重复累加。
        self.store.update(task_id, requests=before.requests + result.network_requests,
                          network_requests=before.network_requests + result.network_requests)
        return result, assessment

    async def _assess(self, snapshot, outcome, payload, *, verified=None, token=None):
        """Re-run the assessment off the event loop; None without a graph or on cancel."""
        resolved = await self._resolve_offline(token=token)
        if resolved is None:
            return None

        def progress(step, **detail):
            if token is not None and token.cancelled:
                raise AssessmentCancelled()
        try:
            return await asyncio.to_thread(
                assess_accessibility, geometry=snapshot.geometry,
                unknown_region=snapshot.unknown_region, facilities=_service_sources(outcome.group),
                query_status=outcome.status, majors=tuple(payload.facilities.categories),
                store=resolved.store, coverage=resolved.coverage,
                version=self.settings.osm_data_version, settings=self.settings,
                progress=progress, verified=verified, incomplete=_incomplete(outcome.group))
        except AssessmentCancelled:
            return None

    def _freeze_verification(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                             outcome: FacilityOutcome, assessment: AccessibilityOutcome,
                             result: VerificationOutcome, *, recomputed: dict | None = None,
                             retried: dict | None = None) -> None:
        group = (None if outcome.group is None
                 else outcome.group.model_dump(mode="json", by_alias=True))
        objects = self._analysis_objects(assessment, verification=result)
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        result_hash = self._result_hash(isochrone, group, facilities_status=outcome.status,
                                        analysis=objects)
        self._publish(task_id, "verification", CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="verification",
                         business=business_status_for(snapshot.quality, outcome.status,
                                                      assessment.status),
                         budgets=self._budgets(budget, snapshot), result_hash=result_hash,
                         warnings=self._common_warnings(snapshot) + list(outcome.issues)
                         + list(assessment.issues) + list(result.issues),
                         facilities=group, facilities_status=outcome.status, analysis=objects,
                         extra_rules={"classification": POI_RULES["version"],
                                      "accessibility": RULE_VERSION},
                         recomputed=recomputed, retried=retried)), revision,
            result_hash)

    def _publish_reporting(self, task_id: str, payload: CheckupRequest, snapshot, budget,
                           outcome: FacilityOutcome, assessment: AccessibilityOutcome,
                           verification: VerificationOutcome | None = None, *,
                           recomputed: dict | None = None,
                           retried: dict | None = None, token=None) -> str:
        """Freeze the report revision: the same assessment, assembled for readers."""
        if token is not None and token.cancelled:
            raise AssessmentCancelled()
        revision = self.store.get(task_id).revision + 1
        isochrone = snapshot.model_dump(mode="json", by_alias=True)
        group = (None if outcome.group is None
                 else outcome.group.model_dump(mode="json", by_alias=True))
        dump = lambda item: None if item is None else item.model_dump(mode="json", by_alias=True)
        evidence = assessment.accessibility
        report = build_report(
            task_id=task_id, revision=revision,
            # 报告汇总的是它自己所在那一版之前的结论；写进自己那一版的摘要会成环。
            source_result_hash=self.store.revision(task_id)["result_hash"],
            generated_at=time.time(),
            domain=None if evidence is None else evidence.domain,
            domain_area_m2=None if evidence is None else evidence.domain_area_m2,
            accessibility=dump(evidence), service_gaps=dump(assessment.service_gaps),
            heatmap=dump(assessment.heatmap), scores=dump(assessment.scores),
            facilities=group,
            requested_categories=payload.facilities.categories,
            verification=None if verification is None or verification.evidence is None
                         else verification.evidence.model_dump(mode="json", by_alias=True),
            water=dump(assessment.water))
        objects = self._analysis_objects(assessment, report, verification)
        report.completion = self._completion(task_id, {'facilities': group, 'revision': revision,
            'isochrone': isochrone, 'report': report.model_dump(mode='json', by_alias=True)}, frozen=True)
        result_hash = self._result_hash(isochrone, group, facilities_status=outcome.status,
                                        analysis=objects)
        business = business_status_for(snapshot.quality, outcome.status, assessment.status)
        self._publish(task_id, "reporting", CheckupSnapshot(
            isochrone=isochrone,
            **self._base(task_id, payload, snapshot, revision=revision, stage="reporting",
                         business=business, budgets=self._budgets(budget, snapshot),
                         result_hash=result_hash,
                         warnings=self._common_warnings(snapshot) + list(outcome.issues)
                         + list(assessment.issues),
                         facilities=group, facilities_status=outcome.status, analysis=objects,
                         extra_rules={"classification": POI_RULES["version"],
                                      "accessibility": RULE_VERSION},
                         recomputed=recomputed, retried=retried)), revision,
            result_hash, token=token)
        return business

    # -- recompute after a data correction ---------------------------------

    async def recompute(self, task_id: str, *, reason: str) -> list[int]:
        """Republish a finished task from its paid evidence after a data correction.

        Nothing is asked again and the original budgets stand as spent:

        * the boundary is rebuilt from the engine's stored samples when the
          engine reads the corrected data (Hybrid replays its ledger against the
          current obstacle layer), and kept as frozen when it does not;
        * the facility retrieval is carried over, but only while the new
          boundary leaves every record on the side it was counted on -- anything
          else needs a new retrieval, which is a new task, not a recompute;
        * the assessment is computed afresh, the verification routes are carried
          over with their entrance layer re-read, and the report is rebuilt.

        It publishes the verification and reporting revisions a run would, both
        marked ``trace.recomputed``, so the report still summarizes the revision
        before it and a reader can tell which revisions predate the correction.
        """
        record = self.get(task_id)
        if record.status not in TERMINAL or task_id in self.tokens:
            raise CheckupError(409, "checkup_task_running", "任务仍在运行，不能重算")
        previous, _ = self.snapshot(task_id)
        if previous.stage != "reporting" or previous.facilities is None:
            raise CheckupError(409, "checkup_recompute_needs_report", "只有已出报告的任务可以重算")
        payload = CheckupRequest.from_stored(record.payload)
        origin = normalize((payload.center.lng, payload.center.lat))
        frozen = IsochroneSnapshot(**previous.isochrone)
        engine = self.registry.get(record.engine)
        ledger_name = getattr(engine, "ledger_name", None)
        if ledger_name is None:
            # The engine does not read the obstacle layer: its boundary stands.
            snapshot, boundary = frozen, "unchanged"
        else:
            ledger = json.loads((self.store.artifact_dir(task_id) / ledger_name)
                                .read_text(encoding="utf-8"))
            context = EngineContext(task_id=task_id, token=CancelToken(),
                                    deadline=time.monotonic() + DEADLINE_SECONDS)
            snapshot = await engine.compute(IsochroneAsk(origin=origin, budget=record.budget),
                                            context, replay=ledger)
            # The samples are the original run's paid attempts; the replay itself
            # sent none, which ``trace.recomputed`` says.
            snapshot = snapshot.model_copy(update={
                "network_requests": frozen.network_requests,
                "statistics": {**snapshot.statistics, "networkRequests": frozen.network_requests,
                               "replayedFromLedger": True}})
            boundary = "replayed_from_ledger"
        group = previous.facilities
        stale = stale_for(group, frozen.geometry, snapshot.geometry, origin)
        if stale is not None:
            raise CheckupError(409, "checkup_recompute_needs_retrieval",
                               f"新边界与原设施检索不再一致（{stale}），需要新任务重新检索")
        outcome = FacilityOutcome(group=group, status=previous.facilities_status,
                                  issues=[issue for issue in previous.warnings
                                          if issue.scope == "facilities"])
        resolved = await self._resolve_offline()
        # The spot checks' measured points are facts about their cells, independent of
        # the water data: the recompute applies them again, sending nothing.
        overrides = [] if previous.verification is None else list(previous.verification.local_overrides)
        assessment = await asyncio.to_thread(
            assess_accessibility, geometry=snapshot.geometry,
            unknown_region=snapshot.unknown_region, facilities=_service_sources(group),
            query_status=outcome.status, majors=tuple(payload.facilities.categories),
            store=None if resolved is None else resolved.store,
            coverage=None if resolved is None else resolved.coverage,
            version=self.settings.osm_data_version, settings=self.settings,
            verified=overrides or None, incomplete=_incomplete(group))
        verification = (None if previous.verification is None
                        else carried_over(previous.verification, entrances=assessment.entrances,
                                          revision=previous.revision))
        recomputed = {"fromRevision": previous.revision, "reason": reason, "networkRequests": 0,
                      "boundary": boundary, "carriedOver": ["facilities", "verificationRoutes"],
                      "previousIsochroneHash": frozen.isochrone_hash,
                      "previousResultHash": previous.trace.result_hash}
        budget = _SpentBudget(previous.trace.budgets)
        published = []
        if verification is not None:
            self._freeze_verification(task_id, payload, snapshot, budget, outcome, assessment,
                                      verification, recomputed=recomputed)
            published.append(self.store.get(task_id).revision)
        business = self._publish_reporting(task_id, payload, snapshot, budget, outcome,
                                           assessment, verification, recomputed=recomputed)
        published.append(self.store.get(task_id).revision)
        self.store.update(task_id, stage="ready", business_status=business)
        return published

    async def _resolve_offline(self, report=None, *, token=None):
        """Resolve the walking graph off the event loop; None when there is none.

        A load in progress is told to ``report`` step by step, its edges counted.
        """
        if self.offline is None:
            return None
        try:
            return await resolve(self.offline, None if report is None else (
                lambda step, done, total: report(step, count=done, limit=total)), token=token)
        except RequestStopped:
            raise AssessmentCancelled() from None
        except Exception:
            # A graph that fails to load is the same answer as a deployment that
            # has none: the assessment says why, and the rest of the revision
            # still reports what the other stages established.
            return None

    def _publish(self, task_id: str, stage: str, document: CheckupSnapshot, revision: int,
                 result_hash: str, *, token=None) -> None:
        # Published files use wire naming, so a stored revision round-trips back
        # into the response model without a translation step.
        document.completion = self._completion(task_id, document.model_dump(mode='json', by_alias=True), frozen=True)
        wire = document.model_dump(mode="json", by_alias=True)
        if token is not None and token.cancelled:
            raise AssessmentCancelled()
        published = self.store.publish(task_id, stage=stage, snapshot=wire,
                                       result_hash=result_hash,
                                       cancelled=None if token is None else lambda: token.cancelled,
                                       # §5 B2 决策 2：汇总与修订同一笔定稿 —— 明细删掉之后，
                                       # 它就是这份结论唯一还留下来的东西。
                                       summary=retention.summary_of(wire))
        if published != revision:
            raise RuntimeError("revision counter diverged")

    def _finish(self, task_id: str, *, status: str, business_status: str | None = None,
                error: str | None = None) -> None:
        """Close the task out without erasing what a revision already established."""
        fields = {"status": status, "finished_at": time.time()}
        if business_status is not None:
            fields["business_status"] = business_status
        if error is not None:
            fields["error"] = error
        self.store.update(task_id, **fields)

    # -- facility routes ----------------------------------------------------

    def _remember_budget(self, task_id: str, budget):
        """The task's own pools, kept for the click-detail route, oldest evicted."""
        if len(self.detail_budgets) >= DETAIL_BUCKETS and task_id not in self.detail_budgets:
            self.detail_budgets.pop(next(iter(self.detail_budgets)))
        self.detail_budgets[task_id] = budget
        return budget

    def detail_budget(self, task_id: str, record):
        """This task's detail pool, rebuilt for a task whose run is long gone."""
        budget = self.detail_budgets.get(task_id)
        if budget is None:
            budget = self.quota.task_budget(isochrone=record.budget, detail=DETAIL_ROUTE_REQUESTS)
            self._remember_budget(task_id, budget)
        return budget

    async def detail_route(self, task_id: str, facility_id: str) -> FacilityRoute:
        """§3.3 点击设施路线：一条独立详情证据，不改变任何已发布的结论。

        它走的是核验阶段同一条闸门 —— 同一个任务的详情桶、同一个 DIRECTION 服务池、
        同一个调度点 —— 所以点击既不能绕过配额，也不会把某次点击读成"评分变了"。
        想让新证据改变结论，必须重新评估并发布新修订。
        """
        snapshot, stored = self.snapshot(task_id)
        if snapshot.facilities is None:
            raise CheckupError(409, "checkup_facilities_not_ready", "设施结果尚未就绪")
        item = next((row for row in _service_sources(snapshot.facilities) if row["id"] == facility_id),
                    None)
        if item is None:
            raise CheckupError(404, "checkup_facility_not_found", "该设施不在本次体检结果中")
        record = self.get(task_id)
        payload = CheckupRequest.from_stored(record.payload)
        origin = normalize((payload.center.lng, payload.center.lat))
        destination = normalize((item["location"]["lng"], item["location"]["lat"]))
        budget = self.detail_budget(task_id, record)
        deadline = time.monotonic() + DEADLINE_SECONDS
        async with AsyncExitStack() as stack:
            try:
                routes = (self.route_factory(self.settings) if self.route_factory is not None
                          else await open_routes(self.settings, stack))
            except RoutesUnavailable:
                raise CheckupError(409, "checkup_route_unavailable",
                                   "本部署没有可用的步行路线服务") from None
            session = routes.session(self.quota.direction, budget=budget, deadline=deadline)
            observation = await session(facility_id, origin, destination, pool=DETAIL_POOL)
            provider, attempts, network = session.identity, session.attempts, bool(routes.network)
            stopped = session.stop_reason
        if observation is None:
            # 没有结论的原因是具名的：额度用完和"路走不通"是两件事。
            if stopped == "task_budget_exhausted":
                raise CheckupError(429, "checkup_detail_budget_exhausted",
                                   f"本任务的详情路线额度已用完（{DETAIL_ROUTE_REQUESTS} 次）")
            raise CheckupError(409, "checkup_route_unavailable",
                               f"本次没有取到路线结论（{stopped or 'unknown'}）")
        # 判定与核验阶段同一套（:func:`judge_route`）：严格层成立用路线距离，端点容差层
        # 用"路线距离＋两端偏移"的接入距离估计；两者都不成立时只留下返回的距离。
        prior = next((row for row in (snapshot.verification.facilities if snapshot.verification else [])
                      if row.get('facilityId') == item['id']), None)
        entrance_status = prior.get('entranceStatus') if prior else None
        judged = judge_route(observation, origin, destination, item["id"],
                             entrance_status=entrance_status)
        strict, returned = judged["strict"], judged["returned"]
        x, y = LocalProjection(origin).to_local(destination)
        return FacilityRoute(
            task_id=task_id, revision=stored["revision"], facility_id=item["id"],
            category=item["category"], major_category=major_of(item["category"]),
            origin=Origin(lng=origin[0], lat=origin[1]),
            destination=Origin(lng=destination[0], lat=destination[1]),
            straight_line_m=round(math.hypot(x, y), 3), within_rule=judged["within"],
            route_distance_m=None if returned is None else round(returned, 3),
            access_distance_m=None if judged["estimate"] is None else round(judged["estimate"], 3),
            verification_layer=judged["layer"],
            duration_s=observation.duration, observed_duration_s=observation.observed_duration,
            poi_status=strict.status, poi_reason=strict.reason,
            entrance_status=entrance_status,
            evidence_grade="verified" if judged["layer"] is not None else "model",
            route_origin=_point(observation.route_origin),
            route_destination=_point(observation.route_destination),
            origin_offset_m=observation.origin_offset_m,
            destination_offset_m=observation.destination_offset_m,
            reason=observation.reason, provider=provider, network=network, attempts=attempts,
            budget={"pool": DETAIL_POOL, "limit": budget.pools()[DETAIL_POOL],
                    "spent": budget.spent.get(DETAIL_POOL, 0),
                    "remaining": budget.remaining(DETAIL_POOL)},
            notes=["这条详情证据不改变已发布的评分：它只回答这一次点击，"
                   "要让新证据影响结论必须重新评估并生成新修订。",
                   "判定用的是返回的路线距离；直线距离只用来排序和保守筛选。"])

    # -- reads and cancellation -------------------------------------------

    def snapshot(self, task_id: str, revision: int | None = None):
        """The newest published revision, or a named not-ready refusal.

        An explicit revision that does not exist is refused **by name**: "no result
        yet" and "that revision is gone" are different answers, and a client that
        asked for a version to keep its own view consistent must be told which one
        it got rather than silently served something else.
        """
        self.get(task_id)
        stored = self.store.revision(task_id, revision)
        if stored is None:
            if revision is not None:
                raise CheckupError(409, "checkup_revision_not_found",
                                   f"这次体检没有第 {revision} 版修订（可能是另一版已发布，"
                                   f"或它已被清理）；请读取当前最新版本。")
            raise CheckupError(409, "checkup_result_not_ready", "尚无可用结果快照")
        # Re-validated from the stored mapping so a hand-edited file cannot widen
        # the contract; serialization always goes through the model.
        return CheckupSnapshot(**stored["snapshot"]), stored

    def view(self, record) -> CheckupTaskView:
        # Every stored time is the wall clock (``time.time()``); a running task
        # is measured against the same clock, never a monotonic one.
        now = time.time()
        if record.started_at is None:
            elapsed = 0.0
        else:
            end = record.finished_at if record.finished_at is not None else now
            elapsed = max(0.0, end - record.started_at)
        return CheckupTaskView(
            task_id=record.task_id, client_request_id=record.client_request_id,
            engine=record.engine, status=record.status,
            business_status=record.business_status, stage=record.stage,
            revision=record.revision, budget=record.budget, requests=record.requests,
            network_requests=record.network_requests, elapsed_seconds=elapsed,
            created_at=record.created_at, cancel_requested=record.cancel_requested,
            error=record.error, server_time=now, started_at=record.started_at,
            finished_at=record.finished_at, stage_started_at=record.stage_started_at,
            last_activity_at=record.activity_at,
            completion=self._completion(record.task_id),
            progress=None if record.progress is None else TaskProgress(**record.progress),
            retention=self.retention_of(record, now=now))

    def get(self, task_id: str):
        try:
            return self.store.get(task_id)
        except TaskNotFound:
            raise CheckupError(404, "checkup_task_not_found", "任务不存在或已过期") from None

    def by_request(self, client_request_id: str):
        try:
            return self.store.by_request(client_request_id)
        except TaskNotFound:
            raise CheckupError(404, "checkup_request_not_found",
                               "该请求标识没有可用任务") from None

    def cancel(self, task_id: str):
        record = self.get(task_id)
        if record.status in TERMINAL:
            return self.view(record), False
        # The request is the client's, not the worker's: it does not count as
        # server activity. A queued task is closed here, which is its last event.
        self.store.update(task_id, cancel_requested=True, activity=False)
        if record.status == "queued":
            self.store.update(task_id, status="cancelled", finished_at=time.time())
        else:
            self.store.update(task_id, status="cancelling", activity=False)
            token = self.tokens.get(task_id)
            if token is not None:
                token.cancel()
        return self.view(self.store.get(task_id)), True

    # -- 按需补查 ----------------------------------------------------------

    def submit_extension(self, task_id: str, payload: FacilityExtensionRequest) -> FacilityExtensionView:
        """为一次已完成的体检补查若干设施大类。

        这里的顺序就是这件事本身：先认领资源、再认请求，最后才算额度。

        * 同一个请求标识回来是**取回原来那次补查** —— 不重新排队、不发请求、不扣额度，
          哪怕今天已经没有余额，也不管父任务后来发过几版修订。
        * 同一个标识带不同参数是冲突（409），而且这个结论优先于任何余额判断：额度不足
          不该掩盖"你换了参数"。
        * 只有确实的新请求才做首轮计划与预算检查，且检查用的是**预计新增网络调用数**：
          完全命中缓存的补查在日余额为 0 时也能提交，因为那一次不会联网。
        """
        parent = self.get(task_id)
        categories = list(payload.categories)
        identity = extension_identity(categories=categories,
                                      max_poi_requests=payload.max_poi_requests)
        # 幂等先于预算，也先于"最新修订"。这一行存在就说明这次补查受理过：合法重试拿回
        # 原记录与原 baseRevision、与原圈面 —— 父任务后来发过几版修订、那一版的几何是否
        # 还可用，都不该让一次已经受理过的请求失败。参数不同则是冲突，同样与余额无关。
        existing = self.store.find_extension(task_id, payload.client_request_id)
        if existing is not None:
            if not extension_matches(
                    existing, identity=identity,
                    resolved_budget=lambda: self._extension_boundary(
                        task_id, payload).requested):
                raise CheckupError(409, "checkup_extension_request_id_conflict",
                                   "该请求标识已用于不同参数的补查")
            return self.extension_view(existing)
        # 新请求才需要当前可用的圈面与几何。
        boundary = self._extension_boundary(task_id, payload)
        domain, origin, minors = boundary.domain, boundary.origin, boundary.minors
        blocks, requested = boundary.blocks, boundary.requested
        # 先看首轮要什么、缓存能回答多少，再决定够不够。这一步与设施阶段共用同一条
        # 准入规则：全缓存放行、部分缓存按缺页放行、只有冷启动缺额才具名拒绝。检查本身
        # 不发请求、不预扣额度、不刷新缓存数据时间；额度仍由服务池在派发前最终裁定。
        provider, api_version = declared_identity(self.settings, self.place_factory)
        plan = poi_plan.initial_plan(domain, origin, minors, provider=provider,
                                     api_version=api_version)
        estimate = poi_plan.estimate(plan, self.cache, task_id=task_id,
                                     remaining_task_budget=requested,
                                     remaining_daily_budget=self.quota.remaining(PLACE))
        refusal = poi_plan.admission_refusal(estimate)
        if refusal == "daily_budget_exhausted":
            remaining_today = estimate.remaining_daily_budget
            raise CheckupError(
                429, "checkup_extension_daily_budget",
                f"今天的地点检索额度不足以完成这次补查：首轮 {estimate.initial_page_count} 页里"
                f"缓存可复用 {estimate.reusable_initial_page_count} 页，"
                f"还需新增 {estimate.estimated_new_initial_calls} 次网络调用，"
                f"本应用今天还剩 {remaining_today} 次。请减少类别，"
                f"或在次日（北京时间）额度恢复后再补查；"
                f"完全命中缓存的补查不受余额影响。")
        if refusal is not None:
            raise CheckupError(
                422, "checkup_extension_budget_too_small",
                f"补查 {len(categories)} 个设施大类首轮需要 {estimate.initial_page_count} 页"
                f"地点检索（{blocks} 个查询分块 × {len(minors)} 个检索小类，每类先取主关键词一次），"
                f"缓存可复用 {estimate.reusable_initial_page_count} 页，"
                f"还需新增 {estimate.estimated_new_initial_calls} 次网络调用，"
                f"但这次只给了 {requested} 次。请提高补查预算或减少类别；"
                f"翻页与细分还需要更多次数，首轮够用不代表一定查完。")
        try:
            record, created = self.store.create_extension(
                extension_id=str(uuid4()), task_id=task_id,
                client_request_id=payload.client_request_id, identity=identity,
                fingerprint=canonical_hash(identity), base_revision=boundary.revision,
                categories=categories, budget=requested)
        except RequestIdConflict:
            raise CheckupError(409, "checkup_extension_request_id_conflict",
                               "该请求标识已用于不同参数的补查") from None
        if created:
            self.extension_queue.put_nowait(record.extension_id)
            self.start()
        # 落败的一方（并发里先被别人建出来）只是取回那一行：不入队，也不重复跑一遍
        # 已经做过的预算判断。
        return self.extension_view(record)

    def _extension_boundary(self, task_id: str, payload: FacilityExtensionRequest):
        """The boundary a *new* extension over this task would search, and its budget.

        只在真正需要时调用：既回答一次已有记录的等价判断（那时需要的只是解析出来的
        预算），也供新请求构造首轮计划。圈面不可用时按既有规则拒绝，且这个拒绝不会
        波及"只是取回原记录"的重试。
        """
        stored = self.store.revision(task_id)
        if stored is None:
            raise CheckupError(409, "checkup_extension_boundary_unavailable",
                               "原体检还没有可用的圈面，暂时无法补查设施")
        isochrone = stored["snapshot"].get("isochrone") or {}
        geometry = isochrone.get("geometry")
        if geometry is None:
            raise CheckupError(409, "checkup_extension_boundary_unavailable",
                               "原体检的圈面没有可用几何，无法补查设施")
        parent = self.get(task_id)
        origin = normalize((parent.payload["center"]["lng"], parent.payload["center"]["lat"]))
        try:
            domain, _widened = query_domain(geometry, origin, QUERY_PADDING_M)
        except ValueError:
            raise CheckupError(409, "checkup_extension_boundary_unavailable",
                               "原体检的圈面无法转成查询范围，无法补查设施") from None
        minors = poi_keys(list(payload.categories))
        blocks = len(coarse_blocks(domain))
        requested = payload.max_poi_requests or min(MAX_POI_REQUESTS,
                                                    max(DEFAULT_POI_REQUESTS, blocks * len(minors)))
        return _ExtensionBoundary(domain=domain, origin=origin, minors=minors, blocks=blocks,
                                  requested=requested, revision=stored["revision"])

    def extensions(self, task_id: str) -> list[FacilityExtensionView]:
        self.get(task_id)
        return [self.extension_view(record) for record in self.store.extensions_of(task_id)]

    def extension(self, task_id: str, extension_id: str) -> FacilityExtensionView:
        return self.extension_view(self._extension_of(task_id, extension_id))

    def extension_document(self, task_id: str, extension_id: str) -> FacilityExtensionDocument:
        record = self._extension_of(task_id, extension_id)
        document = self.store.extension_document(extension_id)
        if document is None:
            raise CheckupError(409, "checkup_extension_result_not_ready", "补查结果尚未就绪")
        return FacilityExtensionDocument(**document)

    def cancel_extension(self, task_id: str, extension_id: str) -> FacilityExtensionView:
        """取消一次补查。原体检不受影响 —— 它从来没有等过这次补查。"""
        record = self._extension_of(task_id, extension_id)
        if record.status in EXTENSION_TERMINAL:
            return self.extension_view(record)
        if record.status == "queued":
            self.store.update_extension(extension_id, status="cancelled", finished_at=time.time())
        else:
            token = self.extension_tokens.get(extension_id)
            if token is not None:
                token.cancel()
        return self.extension_view(self.store.extension(extension_id))

    def _extension_of(self, task_id: str, extension_id: str):
        try:
            record = self.store.extension(extension_id)
        except ExtensionNotFound:
            raise CheckupError(404, "checkup_extension_not_found", "补查不存在或已过期") from None
        if record.task_id != task_id:
            # 一个任务读不到另一个任务的补查：这是一次越权的数据访问，不是一个空结果。
            raise CheckupError(404, "checkup_extension_not_found", "该补查不属于这个任务") from None
        return record

    def extension_view(self, record) -> FacilityExtensionView:
        spent = record.network_requests
        return FacilityExtensionView(
            extension_id=record.extension_id, task_id=record.task_id,
            base_revision=record.base_revision, client_request_id=record.client_request_id,
            status=record.status, stage=record.stage, categories=list(record.categories),
            budget={"limit": record.budget, "spent": spent,
                    "remaining": max(0, record.budget - spent)},
            requests=record.requests, network_requests=spent,
            facilities_status=record.facilities_status,
            counts_by_category=record.counts_by_category, error=record.error,
            stop_reason=record.stop_reason, initial_plan=record.initial_plan,
            created_at=record.created_at, finished_at=record.finished_at)

    # -- 同一次体检的重试（§5 B2 决策 1）------------------------------------

    def submit_retry(self, task_id: str, payload: FacilityRetryRequest) -> FacilityRetryView:
        """用新预算，接着把这次体检没查完的地段查下去。

        与补查共用同一张表、同一套幂等/冲突/队列/取消规则，只有定稿之后做的事不同：
        补查产出的是**并列**的一份结果，重试**为这次体检发布新修订** —— 运营者要的是
        "普通体检必须查到完整结果"，那必须体现在这一份报告里，而不是旁边多一个文件。

        **类别由原体检决定，不在这个请求里给。** 允许重试改类别，就等于允许悄悄换掉
        这次体检问的问题，"这次查完了没有"这句话随之失去意义。

        顺序与补查一致：先认领资源、再认请求、最后算额度。
        """
        parent = self.get(task_id)
        if parent.status not in TERMINAL or task_id in self.tokens:
            raise CheckupError(409, "checkup_task_running", "任务仍在运行，不能重试")
        request = CheckupRequest.from_stored(parent.payload)
        categories = list(request.facilities.categories)
        identity = extension_identity(categories=categories,
                                      max_poi_requests=payload.max_poi_requests)
        # 幂等先于"还没查完"：同一个标识回来必须取回原记录。一次成功的重试会把这次体检
        # 变成"已查完"，如果先判"无需重试"，那个标识的第二次请求就会拿到 409 —— 而它
        # 明明是同一次请求。这与补查的规则一致：先认请求，再判条件。
        existing = self.store.find_extension(task_id, payload.client_request_id)
        if existing is not None:
            if existing.kind != "retry" or not extension_matches(
                    existing, identity=identity,
                    resolved_budget=lambda: self._retry_budget(payload)):
                raise CheckupError(409, "checkup_retry_request_id_conflict",
                                   "该请求标识已用于另一次不同的请求")
            return self.retry_view(existing)
        stored = self.store.revision(task_id)
        if stored is None:
            raise CheckupError(409, "checkup_retry_boundary_unavailable",
                               "原体检还没有可用的修订，暂时无法重试")
        isochrone = (stored["snapshot"] or {}).get("isochrone") or {}
        # 「这次查完了」的证据在这份修订自己身上：查询未结束，或者共同完成面积没到线。
        group = (stored["snapshot"] or {}).get("facilities")
        if group is None:
            raise CheckupError(409, "checkup_retry_nothing_to_continue",
                               "这次体检没有可继续的设施检索结果")
        if group.get("queryStatus") == "completed":
            raise CheckupError(409, "checkup_retry_not_needed",
                               "这次体检的设施检索已经查完，无需重试")
        # 同一体检不允许并行发布两条重试修订：它们会各自读同一份基线，后一条会把前一条
        # 的结果盖掉，而两条都真的花了额度。串行队列本来就一次只跑一条，这里把这条
        # 不变量写成明面规则，而不是依赖"恰好只有一个 worker"。
        for other in self.store.extensions_of(task_id):
            if other.kind == "retry" and other.status in ("queued", "running"):
                raise CheckupError(409, "checkup_retry_in_progress",
                                   "这次体检已经有一次重试在进行中，请等它结束")
        geometry = isochrone.get("geometry")
        if geometry is None:
            raise CheckupError(409, "checkup_retry_boundary_unavailable",
                               "原体检的圈面没有可用几何，无法重试")
        origin = normalize((request.center.lng, request.center.lat))
        try:
            domain, _widened = query_domain(geometry, origin, QUERY_PADDING_M)
        except ValueError:
            raise CheckupError(409, "checkup_retry_boundary_unavailable",
                               "原体检的圈面无法转成查询范围，无法重试") from None
        minors = poi_keys(categories)
        requested = self._retry_budget(payload)
        # 与设施阶段、补查共用同一条准入规则：全缓存放行、部分缓存按缺页放行、只有冷启动
        # 缺额才具名拒绝。检查本身不发请求、不预扣额度；额度仍由服务池在派发前最终裁定。
        provider, api_version = declared_identity(self.settings, self.place_factory)
        plan = poi_plan.initial_plan(domain, origin, minors, provider=provider,
                                     api_version=api_version)
        estimate = poi_plan.estimate(plan, self.cache, task_id=task_id,
                                     remaining_task_budget=requested,
                                     remaining_daily_budget=self.quota.remaining(PLACE))
        refusal = poi_plan.admission_refusal(estimate)
        if refusal == "daily_budget_exhausted":
            raise CheckupError(
                429, "checkup_retry_daily_budget",
                f"今天的地点检索额度不足以继续这次重试：首轮 {estimate.initial_page_count} 页里"
                f"缓存可复用 {estimate.reusable_initial_page_count} 页，"
                f"还需新增 {estimate.estimated_new_initial_calls} 次网络调用，"
                f"本应用今天还剩 {estimate.remaining_daily_budget} 次。"
                f"请在次日（北京时间）额度恢复后重试；已取得的证据都保留在报告里。")
        if refusal is not None:
            raise CheckupError(
                422, "checkup_retry_budget_too_small",
                f"重试首轮需要 {estimate.initial_page_count} 页地点检索"
                f"（{estimate.blocks} 个查询分块 × {len(minors)} 个检索小类），"
                f"缓存可复用 {estimate.reusable_initial_page_count} 页，"
                f"还需新增 {estimate.estimated_new_initial_calls} 次网络调用，"
                f"但这次只给了 {requested} 次。请提高本轮的 maxPoiRequests；"
                f"翻页与细分还需要更多次数，首轮够用不代表一定查完。")
        try:
            record, created = self.store.create_extension(
                extension_id=str(uuid4()), task_id=task_id,
                client_request_id=payload.client_request_id, identity=identity,
                fingerprint=canonical_hash(identity), base_revision=stored["revision"],
                categories=categories, budget=requested, kind="retry")
        except RequestIdConflict:
            raise CheckupError(409, "checkup_retry_request_id_conflict",
                               "该请求标识已用于另一次不同的请求") from None
        if created:
            self.extension_queue.put_nowait(record.extension_id)
            self.start()
        return self.retry_view(record)

    @staticmethod
    def _retry_budget(payload: FacilityRetryRequest) -> int:
        """本轮预算：请求给了就用它，否则用部署默认值。每轮各有一份，互不占用。"""
        return payload.max_poi_requests or DEFAULT_POI_REQUESTS

    def retry_view(self, record) -> FacilityRetryView:
        """一次重试的状态。

        它**不**报"发布了第几版"：那一版是任务自己的最新修订，客户端照常读任务即可
        （重试结束 → 任务出现新修订）。在这里再存一份修订号，只会多出一份可能与任务
        不一致的副本。
        """
        return FacilityRetryView(
            retry_id=record.extension_id, task_id=record.task_id,
            base_revision=record.base_revision, client_request_id=record.client_request_id,
            status=record.status, stage=record.stage,
            budget={"limit": record.budget, "spent": record.requests,
                    "remaining": max(0, record.budget - record.requests)},
            requests=record.requests, network_requests=record.network_requests,
            facilities_status=record.facilities_status, stop_reason=record.stop_reason,
            initial_plan=record.initial_plan, error=record.error,
            created_at=record.created_at, finished_at=record.finished_at)

    def retries(self, task_id: str) -> list[FacilityRetryView]:
        self.get(task_id)
        return [self.retry_view(record) for record in self.store.extensions_of(task_id)
                if record.kind == "retry"]

    def retry(self, task_id: str, retry_id: str) -> FacilityRetryView:
        return self.retry_view(self._retry_of(task_id, retry_id))

    def _retry_of(self, task_id: str, retry_id: str):
        record = self._extension_of(task_id, retry_id)
        if record.kind != "retry":
            raise CheckupError(404, "checkup_retry_not_found", "该标识不是一次重试")
        return record

    def cancel_retry(self, task_id: str, retry_id: str) -> FacilityRetryView:
        record = self._retry_of(task_id, retry_id)
        if record.status not in ("queued", "running"):
            return self.retry_view(record)
        if record.status == "queued":
            self.store.update_extension(retry_id, status="cancelled", finished_at=time.time())
        else:
            token = self.extension_tokens.get(retry_id)
            if token is not None:
                token.cancel()
        return self.retry_view(self.store.extension(retry_id))

    async def _finish_retry(self, record, outcome: FacilityOutcome, token: CancelToken,
                            budget) -> None:
        """把这一轮的新证据发布成**这次体检自己的新修订**。

        圈面与类别都取自被冻结的那一版，所以重试不会移动评估范围，也不会换掉问题；
        变的是设施证据本身 —— 这正是"这次查完了没有"的答案所在。评分由同一套
        ``assess_accessibility`` 重算，权重一个字没改：差异只能来自证据。
        """
        task_id = record.task_id
        if outcome.group is None:
            self.store.finish_extension(record.extension_id, status="failed", document_path=None,
                                        error="no_facilities_retrieved",
                                        requests=outcome.requests,
                                        network_requests=outcome.network_requests)
            return
        previous, _ = self.snapshot(task_id)
        stored = self.store.revision(task_id, record.base_revision)
        isochrone = (stored or {}).get("snapshot", {}).get("isochrone") or {}
        frozen = IsochroneSnapshot(**isochrone)
        payload = CheckupRequest.from_stored(self.store.get(task_id).payload)
        resolved = await self._resolve_offline()
        overrides = ([] if previous.verification is None
                     else list(previous.verification.local_overrides))
        assessment = await asyncio.to_thread(
            assess_accessibility, geometry=frozen.geometry,
            unknown_region=frozen.unknown_region, facilities=_service_sources(outcome.group),
            query_status=outcome.status, majors=tuple(payload.facilities.categories),
            store=None if resolved is None else resolved.store,
            coverage=None if resolved is None else resolved.coverage,
            version=self.settings.osm_data_version, settings=self.settings,
            verified=overrides or None, incomplete=_incomplete(outcome.group))
        # 核验路线是上一轮真实问过路的证据，沿用它、并只重读它依赖的入口层：重试不是
        # "重新核验"，一条都不该重发（发了就要另算额度，而这次重试只有 POI 预算）。
        verification = (None if previous.verification is None
                        else carried_over(previous.verification,
                                          entrances=assessment.entrances,
                                          revision=previous.revision))
        retried = {"fromRevision": previous.revision,
                   "networkRequests": outcome.network_requests,
                   "processedPages": outcome.requests,
                   "coverageTarget": (outcome.group.query_area_coverage or {}).get("target"),
                   "areaCoverage": outcome.group.query_area_coverage,
                   "carriedOver": ["isochrone", "verificationRoutes"]}
        if verification is not None:
            self._freeze_verification(task_id, payload, frozen, budget, outcome, assessment,
                                      verification, retried=retried)
        business = self._publish_reporting(task_id, payload, frozen, budget, outcome, assessment,
                                           verification, retried=retried)
        self.store.update(task_id, stage="ready", business_status=business)
        self.store.finish_extension(
            record.extension_id, status=_extension_status(outcome, token), document_path=None,
            counts_by_category=outcome.group.counts_by_category,
            facilities_status=outcome.group.query_status, requests=outcome.requests,
            network_requests=outcome.network_requests,
            stop_reason=outcome.group.stop_reason, initial_plan=outcome.group.initial_plan)

    async def _serve_extensions(self) -> None:
        # Same reason as ``_serve``: the flag has to be able to end this loop, because
        # a cancelled run swallows the cancellation and returns its partial evidence.
        while not self.closing:
            extension_id = await self.extension_queue.get()
            try:
                if not self.closing:
                    await self._run_extension(extension_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 补查必须走到终态，哪怕编排本身出错；原因被记下，原始异常不外泄。
                self.store.finish_extension(extension_id, status="failed", document_path=None,
                                            error="orchestration_failed")
            finally:
                self.extension_queue.task_done()

    async def _run_extension(self, extension_id: str) -> None:
        if not self.store.claim_extension(extension_id):
            return
        record = self.store.extension(extension_id)
        parent = CheckupRequest.from_stored(self.store.get(record.task_id).payload)
        stored = self.store.revision(record.task_id, record.base_revision)
        if stored is None:
            self.store.finish_extension(extension_id, status="failed", document_path=None,
                                        error="boundary_missing")
            return
        isochrone = stored["snapshot"].get("isochrone") or {}
        # 只借用圈面的几何与质量：设施检索读到的就是这两样，别的都不该由补查决定。
        snapshot = SimpleNamespace(geometry=isochrone.get("geometry"),
                                   quality=isochrone.get("quality"))
        payload = parent.model_copy(update={
            "facilities": CheckupFacilities(categories=tuple(record.categories),
                                            max_poi_requests=record.budget,
                                            max_route_requests=1)})
        # 只有 POI 这一个池会被用到：补查不成圈，也不请求步行路线。
        budget = self.quota.task_budget(isochrone=0, poi=record.budget, route=0, detail=0)
        if record.kind == "retry":
            prior = self._completion(record.task_id)
            if prior is None:
                self.store.finish_extension(extension_id, status="failed", document_path=None,
                                            error="checkup_details_expired")
                return
            self.store.begin_round(record.task_id, record.client_request_id, record.base_revision,
                                   self._round_identity(parent, IsochroneSnapshot(**isochrone)),
                                   poi_before=prior.cumulative_poi_requests,
                                   route_before=prior.route_requests, poi_limit=record.budget,
                                   requeue=False)
            self._bind_round(record.task_id, budget)
        token = CancelToken()
        self.extension_tokens[extension_id] = token
        # 缓存用原任务的标识：核心三类已经取过的页面在这里算"同任务命中"，不再付费。
        context = EngineContext(task_id=record.task_id, token=token,
                                deadline=time.monotonic() + DEADLINE_SECONDS,
                                artifact_dir=self.store.artifact_dir(record.task_id))
        try:
            outcome = await collect_facilities(
                payload, snapshot, settings=self.settings, context=context, quota=self.quota,
                budget=budget, cache=self.cache, places_factory=self.place_factory,
                store=self.store if record.kind == "retry" else None,
                progress=lambda sent, limit, label=None: self.store.update_extension(
                    extension_id, network_requests=sent))
        finally:
            self.extension_tokens.pop(extension_id, None)
        if record.kind == "retry":
            # 重试与补查的分岔点就在这一行：补查写一份结果文档了事，重试要为这次体检
            # 发布新修订（见 ``_finish_retry``）。
            await self._finish_retry(record, outcome, token, budget)
            return
        document = FacilityExtensionDocument(
            extension_id=extension_id, task_id=record.task_id, base_revision=record.base_revision,
            categories=list(record.categories), status=_extension_status(outcome, token),
            facilities_status=(outcome.group.query_status if outcome.group is not None else None),
            group=outcome.group, requests=outcome.requests,
            network_requests=outcome.network_requests, budget=budget.state().get("poi", {}),
            issues=outcome.issues, notes=list(EXTENSION_NOTES))
        path = self.store.publish_extension_document(extension_id, record.task_id,
                                                     document.model_dump(mode="json", by_alias=True))
        self.store.finish_extension(
            extension_id, status=document.status, document_path=path,
            counts_by_category=(outcome.group.counts_by_category
                                if outcome.group is not None else {}),
            facilities_status=document.facilities_status, requests=outcome.requests,
            network_requests=outcome.network_requests,
            stop_reason=(None if outcome.group is None else outcome.group.stop_reason),
            initial_plan=(None if outcome.group is None else outcome.group.initial_plan),
            error=None if outcome.group is not None else "no_facilities_retrieved")

    async def close(self) -> None:
        self.closing = True
        if self.maintenance is not None and not self.maintenance.done():
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        for token in list(self.tokens.values()):
            token.cancel()
        for token in list(self.extension_tokens.values()):
            token.cancel()
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.extension_worker is not None and not self.extension_worker.done():
            self.extension_worker.cancel()
            await asyncio.gather(self.extension_worker, return_exceptions=True)
        self.store.close()


def _extension_status(outcome: FacilityOutcome, token: CancelToken) -> str:
    """检索器的 ``queryStatus`` → 补查自己的状态。被取消的补查仍然是"取消"。"""
    if token.cancelled:
        return "cancelled"
    return {"complete": "completed", "partial": "partial", "cancelled": "cancelled"}.get(
        outcome.status, "failed")
