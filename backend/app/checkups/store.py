"""Durable checkup task metadata and the immutable revision index.

Metadata, idempotency keys and consumption counters live in SQLite; published
revisions are atomic JSON files, so a partially written revision can never
replace a frozen one. A restart marks unfinished tasks interrupted and keeps
every revision they already published; it never resumes paid requests.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from pathlib import Path
from uuid import uuid4

from ..persistence import atomic_dump
from .rounds import ROUND_SCHEMA, RoundStore
from . import retention

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    client_request_id TEXT NOT NULL UNIQUE,
    engine TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    business_status TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    budget INTEGER NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    network_requests INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    stage_started_at REAL,
    activity_at REAL,
    progress TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expired_at REAL
);
CREATE TABLE IF NOT EXISTS session_tabs (
    tab_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    opened_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS session_tabs_of_session ON session_tabs (session_id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    stage TEXT NOT NULL,
    created_at REAL NOT NULL,
    result_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (task_id, revision)
);
CREATE TABLE IF NOT EXISTS facility_extensions (
    extension_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    categories TEXT NOT NULL,
    budget INTEGER NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    requests INTEGER NOT NULL DEFAULT 0,
    network_requests INTEGER NOT NULL DEFAULT 0,
    document TEXT,
    counts_by_category TEXT,
    facilities_status TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    intent TEXT,
    stop_reason TEXT,
    initial_plan TEXT,
    UNIQUE (task_id, client_request_id)
);
"""
#: Columns added after the first release, with their declarations. A store
#: created before them is migrated in place; existing rows read them as NULL,
#: which the view reports as "not recorded" rather than inventing a time.
#:
#: ``session_id`` 是这一次体检属于哪个浏览会话（§5 B2 决策 2 的第一条期限）。
#: ``details_cleared_at`` 是清理过的**墓碑**：文件删掉之后再有人按这个任务读明细，
#: 要得到"已到期"而不是"没有这个任务"，而且在两个并发读取之间只能被判一次。
ADDED_COLUMNS = (("stage_started_at", "REAL"), ("activity_at", "REAL"), ("progress", "TEXT"),
                 ("session_id", "TEXT"), ("details_cleared_at", "REAL"))
#: 这一份库第一次被"带保留期的那一版"打开的时刻。它之前落盘的任务没有会话归属，
#: 也没有任何人替它们承担保留义务 —— 运营者的口径是这些历史报告**视为已过期**：
#: 明细不再提供，只留不含明细的汇总，实际删除交给清单与人工确认（`evidence/06` §2 决策 2）。
#: 记在库里而不是写死一个日期：判据是"这份数据比本库开始记录保留期的时刻早"，
#: 一个新建的库（测试、临时部署）里就不会有任务被判成历史数据。
RETENTION_BASELINE_KEY = "retention_baseline_at"
#: 补查表同理：早于它的库按原样读回，不编造缺的字段。``intent`` 记的是客户端当时声明的
#: 请求身份（类别与是否显式给了预算）；旧行没有它，按旧版的解析结果判定，见
#: :func:`extension_matches`。``stop_reason``/``initial_plan`` 是定稿时冻结的诊断：
#: 界面读状态就能看到停在哪、缓存帮了多少，不必再取结果文件。
EXTENSION_ADDED_COLUMNS = (("facilities_status", "TEXT"), ("intent", "TEXT"),
                           ("stop_reason", "TEXT"), ("initial_plan", "TEXT"),
                           ("kind", "TEXT"))
#: 这一行是哪一种工作。``extension``：按需补查，**不**改父任务的报告与修订。
#: ``retry``：§5 B2 决策 1 的重试，它**要**为同一次体检发布新修订。
#: 两者共用同一张表是有意的：幂等键、409 冲突、串行队列、取消与终态语义
#: 是同一套规则，各写一遍才是真正会分叉的地方。区别只在定稿之后做什么。
#: 旧行为 NULL，按 ``extension`` 读 —— 那正是它们被创建时的语义。
EXTENSION_KINDS = frozenset({"extension", "retry"})
#: What ``update`` may write. ``progress`` is the worker's in-stage step (a JSON
#: object); ``stage_started_at`` and ``activity_at`` are maintained here, never
#: passed in.
UPDATABLE = frozenset({"status", "stage", "business_status", "requests", "network_requests",
                       "started_at", "finished_at", "cancel_requested", "error", "progress"})
#: 与 ``ADDED_COLUMNS`` 同理，只是加在修订表上。``summary`` 是定稿时就冻结好的白名单汇总
#: （见 ``retention.summary_of``）：到期之后它是这份结论唯一还留下来的东西，所以它必须
#: 在**删掉明细文件之前**就已经存在，而不是删之前临时算一遍。
REVISION_ADDED_COLUMNS = (("summary", "TEXT"),)

#: 补查行可以改的字段。``document`` 是结果文件的相对路径，写入即意味着这次补查定稿。
EXTENSION_UPDATABLE = frozenset({"status", "stage", "requests", "network_requests",
                                 "document", "counts_by_category", "facilities_status",
                                 "error", "finished_at", "stop_reason", "initial_plan"})


class RequestIdConflict(Exception):
    """Same client request id, different inputs."""


class TaskNotFound(LookupError):
    """Unknown or pruned task id."""


class ExtensionNotFound(LookupError):
    """Unknown补查 id，或它不属于这个任务。"""


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    client_request_id: str
    engine: str
    fingerprint: str
    payload: dict
    status: str
    stage: str | None
    business_status: str | None
    revision: int
    budget: int
    requests: int
    network_requests: int
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    cancel_requested: bool
    error: str | None
    #: When the current stage began, on the same wall clock as ``started_at``.
    stage_started_at: float | None = None
    #: The last time the worker wrote anything about this task: a counter, a step,
    #: a revision or a state change. A client's cancel request is not activity.
    activity_at: float | None = None
    #: The worker's current in-stage step, as written; None before the first one.
    progress: dict | None = None
    #: 这一次体检属于哪个浏览会话（§5 B2 决策 2）。None 表示**没人认领**：它要么早于
    #: 会话机制（历史数据），要么由不带会话标识的调用方创建（脚本、命令行、旧客户端）。
    session_id: str | None = None
    #: 明细被清理掉的时刻（墓碑）。有它就意味着"这份结果还在，但含明细的文件已经不在了"，
    #: 那与"没有这个任务"是两种回答。
    details_cleared_at: float | None = None


def _record(row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"], client_request_id=row["client_request_id"], engine=row["engine"],
        fingerprint=row["fingerprint"], payload=json.loads(row["payload"]), status=row["status"],
        stage=row["stage"], business_status=row["business_status"], revision=row["revision"],
        budget=row["budget"], requests=row["requests"], network_requests=row["network_requests"],
        created_at=row["created_at"], updated_at=row["updated_at"], started_at=row["started_at"],
        finished_at=row["finished_at"], cancel_requested=bool(row["cancel_requested"]),
        error=row["error"], stage_started_at=row["stage_started_at"],
        activity_at=row["activity_at"],
        progress=None if row["progress"] is None else json.loads(row["progress"]),
        session_id=row["session_id"],
        details_cleared_at=row["details_cleared_at"])


@dataclass(frozen=True)
class ExtensionRecord:
    """一次按需补查的行：它自己的预算、状态和结果文件。"""
    extension_id: str
    task_id: str
    client_request_id: str
    fingerprint: str
    base_revision: int
    categories: list[str]
    budget: int
    status: str
    stage: str | None
    requests: int
    network_requests: int
    document_path: str | None
    counts_by_category: dict
    facilities_status: str | None
    error: str | None
    created_at: float
    updated_at: float
    finished_at: float | None
    #: 客户端当时声明的身份（``categories`` 与 ``maxPoiRequests``）。旧行没有它，
    #: 读作 None —— 那意味着"这一行只冻了当时解析出来的预算"。
    intent: dict | None = None
    #: 定稿时冻结的停止原因与首轮估算：它们是读这次补查时必须知道的事。
    stop_reason: str | None = None
    initial_plan: dict | None = None
    #: ``extension``（补查，不动父任务报告）或 ``retry``（重试，为父任务发布新修订）。
    #: 旧行读作 ``extension``。
    kind: str = "extension"

    @property
    def declared_max_poi_requests(self) -> int | None:
        """客户端显式给出的预算，或 None：它当时把预算留给了部署默认值。"""
        return None if self.intent is None else self.intent.get('maxPoiRequests')


def _extension_record(row) -> ExtensionRecord:
    return ExtensionRecord(
        extension_id=row["extension_id"], task_id=row["task_id"],
        client_request_id=row["client_request_id"], fingerprint=row["fingerprint"],
        base_revision=row["base_revision"], categories=json.loads(row["categories"]),
        budget=row["budget"], status=row["status"], stage=row["stage"],
        requests=row["requests"], network_requests=row["network_requests"],
        document_path=row["document"], error=row["error"], created_at=row["created_at"],
        updated_at=row["updated_at"], finished_at=row["finished_at"],
        facilities_status=row["facilities_status"],
        intent=(None if row["intent"] is None else json.loads(row["intent"])),
        stop_reason=row["stop_reason"],
        initial_plan=(None if row["initial_plan"] is None else json.loads(row["initial_plan"])),
        # 旧行没有这一列，读作 ``extension``：那正是它们被创建时的语义，"补查不改父任务报告"。
        kind=row["kind"] or "extension",
        counts_by_category=({} if not row["counts_by_category"]
                            else json.loads(row["counts_by_category"])))


def extension_identity(*, categories, max_poi_requests) -> dict:
    """What the client asked for, as an identity nothing else may widen.

    It is deliberately the *declared* request and not what the deployment resolved
    it to: a retry that leaves the budget out must stay the same request after the
    default, the boundary geometry or the day's balance has moved. Only the two
    things that change what is being asked for take part.
    """
    return {"categories": sorted(set(categories)), "maxPoiRequests": max_poi_requests}


def extension_matches(record: ExtensionRecord, *, identity: dict,
                      resolved_budget: int | Callable[[], int]) -> bool:
    """Whether ``identity`` is the request ``record`` was created from.

    The stored row keeps the client's own declaration, so a repeat matches on it
    directly. Two equivalences are preserved rather than invented:

    * A request that left the budget out resolves to the deployment default, so it
      is the same request as one that named exactly that number — in either
      direction, which is what the older release compared.
    * A row written before ``intent`` existed froze only its resolved budget, so it
      is judged the way it was created: by the number this request resolves to now.

    ``resolved_budget`` is therefore only ever consulted for those two cases, and
    never for two rows that both declared what they wanted. It may be a callable so a
    caller can pass something expensive — the deployment's current default, which needs
    the parent's boundary — without reading it to answer a question that does not use it.
    """
    def resolved() -> int:
        return resolved_budget() if callable(resolved_budget) else resolved_budget

    if sorted(set(identity["categories"])) != sorted(set(record.categories)):
        return False
    declared_now = identity["maxPoiRequests"]
    if record.intent is None:
        return resolved() == record.budget
    declared_then = record.declared_max_poi_requests
    if declared_now is None:
        # Omitted: it resolves to the deployment default, so it is the same request
        # as one that named exactly that number — and only that number. A row that
        # declared its own budget is not matched just because the omission happens
        # to resolve to the same figure it froze.
        return declared_then is None or resolved() == declared_then
    if declared_then is None:
        # The row left the budget out, so it froze the default it applied: naming
        # that number is the same request, naming another one is not.
        return declared_now == record.budget
    return declared_now == declared_then


class CheckupStore(RoundStore):
    def __init__(self, root: Path, *, read_only: bool = False):
        """``read_only`` 打开这份库**只做读取**：不建表、不迁移、不写墓碑。

        它存在的原因是清单工具：运营者要先看清单再决定删什么，那么"看一眼"就必须真的
        只是看一眼 —— 一份为了被审阅而跑的工具，不该顺手改动它正在回答的那份数据。
        """
        self.root = Path(root)
        self.read_only = read_only
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "checkups.sqlite3"
        self._keeper = None

    def close(self):
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    @contextmanager
    def _connection(self):
        if self.read_only:
            # URI 形式的只读连接：连 PRAGMA 与 journal 都不给写的机会。
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30,
                                         isolation_level=None)
        else:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if not self.read_only:
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> int:
        """The whole of a restart: create the schema, then sweep unfinished tasks.

        Returns the number of tasks marked interrupted.
        """
        self.create_schema()
        return self.interrupt_unfinished()

    def create_schema(self) -> None:
        """Create the tables and add any later column. Changes no task's state.

        Also stamps the retention baseline once, on the first run of a release
        that has retention at all. Doing it here -- and only once, by ``OR IGNORE``
        -- is what makes "this data predates the retention policy" a fact about
        the store rather than a hard-coded date: a store created today starts its
        own baseline today, so nothing inside it can be mistaken for history.
        """
        if self.read_only:
            return
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            # Keep WAL alive between short per-operation connections. Closing the
            # last connection otherwise checkpoints and removes WAL on every page.
            if self._keeper is None:
                self._keeper = sqlite3.connect(self.path, check_same_thread=False,
                                               isolation_level=None)
            connection.executescript(SCHEMA)
            connection.executescript(ROUND_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            round_columns = {row["name"] for row in connection.execute("PRAGMA table_info(checkup_rounds)")}
            if "poi_limit" not in round_columns:
                connection.execute("ALTER TABLE checkup_rounds ADD COLUMN poi_limit INTEGER NOT NULL DEFAULT 60")
                # Old continuation rounds always used 60. Initial rounds could
                # explicitly lower it; missing request fields meant 60 then.
                for row in connection.execute("SELECT r.task_id,t.payload FROM checkup_rounds r "
                                              "JOIN tasks t ON t.task_id=r.task_id WHERE r.number=1").fetchall():
                    facilities = json.loads(row["payload"]).get("facilities", {})
                    limit = facilities.get("max_poi_requests", facilities.get("maxPoiRequests", 60))
                    connection.execute("UPDATE checkup_rounds SET poi_limit=? WHERE task_id=? AND number=1",
                                       (limit, row["task_id"]))
            connection.execute("COMMIT")
            present = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, declaration in ADDED_COLUMNS:
                if name not in present:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            revision_columns = {row["name"] for row in
                                connection.execute("PRAGMA table_info(revisions)")}
            for name, declaration in REVISION_ADDED_COLUMNS:
                if name not in revision_columns:
                    connection.execute(f"ALTER TABLE revisions ADD COLUMN {name} {declaration}")
            extension_columns = {row["name"] for row in
                                 connection.execute("PRAGMA table_info(facility_extensions)")}
            for name, declaration in EXTENSION_ADDED_COLUMNS:
                if name not in extension_columns:
                    connection.execute(
                        f"ALTER TABLE facility_extensions ADD COLUMN {name} {declaration}")
            connection.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                               (RETENTION_BASELINE_KEY, repr(time.time())))

    def interrupt_unfinished(self) -> int:
        """Fail every queued/running/cancelling task as ``interrupted_by_restart``.

        Returns how many were marked. Cancelled or completed tasks are left
        untouched, and no paid request is ever replayed.

        This is a **serving** startup step, not an import-time one: importing the
        app object is not an event in the deployment's history. Anything that
        imports ``app.main`` for its own reasons -- ``tools/export_contract.py``
        builds the OpenAPI out of it -- would otherwise reach into the live store
        and kill whatever checkup is running at that moment.
        """
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='failed', stage=NULL, finished_at=?,"
                " error='interrupted_by_restart', updated_at=?"
                " WHERE status IN ('queued','running','cancelling')",
                (time.time(), time.time()))
            # 补查走同一条重启语义：没跑完的补查不会自己续上，也不会重放任何已付费的请求。
            # 它不动原任务，所以原体检的报告仍然可以照常查看。
            connection.execute(
                "UPDATE facility_extensions SET status='failed', stage=NULL, finished_at=?,"
                " error='interrupted_by_restart', updated_at=?"
                " WHERE status IN ('queued','running')", (time.time(), time.time()))
            return cursor.rowcount

    def artifact_dir(self, task_id: str) -> Path:
        directory = self.root / "tasks" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    # -- 会话与租约（§5 B2 决策 2 的第一条期限）--------------------------------
    #
    # 这几件事分成两层：**记下来**在这里，**判期限**在 manager（它才知道 5 分钟宽限
    # 这类产品口径）。store 只回答"最后一刻还在的时候是什么时候"，不去解释它意味着什么。

    def retention_baseline_at(self) -> float:
        """这份库开始记录保留期的时刻；缺失时按"此刻"记一次（老库的第一次迁移）。

        只读模式下不写这一行，而是返回无穷大：这份库还没有基线时，现有数据**全部**早于
        将要写下的那个时刻 —— 那正是清单要说的"这些都是历史数据"。清单的输出会同时说明
        这一点，免得有人把它当成"永远都算历史"。
        """
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?",
                                     (RETENTION_BASELINE_KEY,)).fetchone()
            if row is None:
                if self.read_only:
                    return float("inf")
                connection.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                                   (RETENTION_BASELINE_KEY, repr(time.time())))
                return time.time()
        return float(row["value"])

    def session_row(self, session_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id=?",
                                     (session_id,)).fetchone()
        return None if row is None else dict(row)

    def session_tabs(self, session_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM session_tabs WHERE session_id=? ORDER BY opened_at, tab_id",
                (session_id,)).fetchall()
        return [dict(row) for row in rows]

    def start_session(self, *, session_id: str, tab_id: str, now: float) -> dict:
        """开一个会话并登记它的第一个标签页。"""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO sessions (session_id, created_at, last_seen_at) VALUES (?,?,?)",
                (session_id, now, now))
            connection.execute(
                "INSERT INTO session_tabs (tab_id, session_id, opened_at, last_seen_at)"
                " VALUES (?,?,?,?)", (tab_id, session_id, now, now))
            connection.execute("COMMIT")
        return self.session_row(session_id)

    def touch_session(self, *, session_id: str, tab_id: str, now: float,
                      reopen: bool) -> dict | None:
        """续租；``reopen`` 为真时把一个已关闭的标签页重新登记（刷新后回到同一个标签页）。

        未知会话返回 None —— 调用方据此决定是"让它过期"还是"接不上就别接"，
        这里不替它判断。
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM sessions WHERE session_id=?",
                                     (session_id,)).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            connection.execute("UPDATE session_tabs SET last_seen_at=?,"
                               " closed_at=CASE WHEN ? THEN NULL ELSE closed_at END"
                               " WHERE tab_id=? AND session_id=?",
                               (now, int(reopen), tab_id, session_id))
            connection.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                               (now, session_id))
            connection.execute("COMMIT")
        return self.session_row(session_id)

    def open_tab(self, *, session_id: str, tab_id: str, now: float) -> bool:
        """把一个新标签页登记到已有会话里。返回它是否真的是新的。"""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO session_tabs (tab_id, session_id, opened_at, last_seen_at)"
                " VALUES (?,?,?,?)", (tab_id, session_id, now, now))
            if cursor.rowcount:
                connection.execute("UPDATE sessions SET last_seen_at=? WHERE session_id=?",
                                   (now, session_id))
            connection.execute("COMMIT")
            return cursor.rowcount == 1

    def close_tab(self, *, session_id: str, tab_id: str, now: float) -> bool:
        """标签页离开。返回它是否真的从"开着"变成了"关着"（重复关闭不是第二次事件）。"""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE session_tabs SET closed_at=?, last_seen_at=?"
                " WHERE tab_id=? AND session_id=? AND closed_at IS NULL",
                (now, now, tab_id, session_id))
            return cursor.rowcount == 1

    def expire_session(self, *, session_id: str, now: float) -> bool:
        """把会话判成已过期，只判一次。已过期是终态：迟到的心跳不能让它复活。"""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE sessions SET expired_at=? WHERE session_id=? AND expired_at IS NULL",
                (now, session_id))
            return cursor.rowcount == 1

    def expire_sessions_without_live_tabs(self, *, now: float, grace: float) -> list[str]:
        """把"最后一个标签页离开已经超过宽限期"的会话判成过期，返回它们的标识。

        判据只用到**已经写下来的时刻**：最后一个标签页的关闭时刻，或它最后一次续租的
        时刻 —— 后者覆盖"标签页被直接杀掉、来不及说再见"的情形。
        """
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT s.session_id AS session_id FROM sessions s"
                " WHERE s.expired_at IS NULL AND NOT EXISTS ("
                "   SELECT 1 FROM session_tabs t WHERE t.session_id = s.session_id"
                "     AND t.closed_at IS NULL AND t.last_seen_at > ?)"
                " AND COALESCE((SELECT MAX(COALESCE(t2.closed_at, t2.last_seen_at))"
                "               FROM session_tabs t2 WHERE t2.session_id = s.session_id),"
                "              s.created_at) <= ?",
                (now - grace, now - grace)).fetchall()
            ids = [row["session_id"] for row in rows]
            if ids:
                connection.executemany(
                    "UPDATE sessions SET expired_at=? WHERE session_id=? AND expired_at IS NULL",
                    [(now, session_id) for session_id in ids])
        return ids

    def session_ids_of_tasks(self, session_id: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE session_id=? ORDER BY created_at, task_id",
                (session_id,)).fetchall()
        return [row["task_id"] for row in rows]

    # -- 保留期：第三次后续体检 -------------------------------------------------

    def later_terminal_instants(self, task_id: str, *, limit: int = 3) -> list[float | None]:
        """这份任务之后**按提交顺序**前 limit 个已经进入终态的任务，各自的结束时刻。

        "该次体检之后的第三次新体检结束"就是这里第 limit 个的结束时刻。顺序用 ``rowid``：
        那才是真正的插入顺序。``created_at`` 只到浮点精度，同一瞬间提交的两次体检会拿到
        同一个值，于是排序会退化成按任务标识（一个 UUID）比较 —— 那不是"提交顺序"，而是
        一个看起来稳定、实际与提交无关的顺序，测试里冻结时钟时立刻就会露出来。

        重试、补查与幂等重发都不在这里出现：它们不新建 ``tasks`` 行。
        """
        with self._connection() as connection:
            row = connection.execute("SELECT rowid FROM tasks WHERE task_id=?",
                                     (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound(task_id)
            rows = connection.execute(
                "SELECT finished_at FROM tasks WHERE rowid > ?"
                " AND status IN ('completed','failed','cancelled')"
                " ORDER BY rowid LIMIT ?", (row["rowid"], limit)).fetchall()
        return [item["finished_at"] for item in rows]

    def tasks_created_before(self, instant: float) -> list[str]:
        """早于这个时刻创建的任务：没有会话归属、也没有人替它们承担保留义务的那些。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE created_at < ? ORDER BY created_at, task_id",
                (instant,)).fetchall()
        return [row["task_id"] for row in rows]

    def mark_details_cleared(self, task_id: str, *, now: float) -> bool:
        """给清理盖墓碑：两个并发的清理者里只有一个能拿到 True。

        先落墓碑再删文件 —— 反过来的话，删到一半失败的那次重启会让"已清理"这件事
        既没有记录、也不可重试。
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE tasks SET details_cleared_at=? WHERE task_id=? AND details_cleared_at IS NULL",
                (now, task_id))
            changed = cursor.rowcount == 1
            if changed:
                # Continuation state contains details too; retain paid accounting only.
                connection.execute("DELETE FROM checkup_checkpoints WHERE task_id=?", (task_id,))
                connection.execute("UPDATE checkup_requests SET context='{}', response=NULL WHERE task_id=?",
                                   (task_id,))
            connection.execute("COMMIT")
            return changed

    def details_cleared_tasks(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE details_cleared_at IS NOT NULL"
                " ORDER BY created_at, task_id").fetchall()
        return [row["task_id"] for row in rows]

    def task_directories(self) -> list[str]:
        """磁盘上真实存在的任务目录，包括**索引里已经没有行**的那些。

        只按索引删是错的：一个目录可能留下没有索引行的修订文件（写文件成功、写索引前
        进程就没了），照索引清理会把这些明细永远留在盘上。
        """
        tasks = self.root / "tasks"
        if not tasks.is_dir():
            return []
        return sorted(item.name for item in tasks.iterdir() if item.is_dir())


    def create(self, *, task_id: str, client_request_id: str, engine: str,
               fingerprint: str, payload: dict, budget: int,
               session_id: str | None = None) -> tuple[TaskRecord, bool]:
        """Insert once per client request id; identical repeats return the same task."""
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tasks WHERE client_request_id=?", (client_request_id,)).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                if existing["fingerprint"] != fingerprint:
                    raise RequestIdConflict(client_request_id)
                return _record(existing), False
            if connection.execute('SELECT 1 FROM checkup_rounds WHERE request_id=?',
                                  (client_request_id,)).fetchone():
                raise RequestIdConflict(client_request_id)
            connection.execute(
                "INSERT INTO tasks (task_id, client_request_id, engine, fingerprint, payload,"
                " status, stage, revision, budget, created_at, updated_at, session_id)"
                " VALUES (?,?,?,?,?,'queued',NULL,0,?,?,?,?)",
                (task_id, client_request_id, engine, fingerprint,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), budget, now, now,
                 session_id))
            connection.execute("COMMIT")
            return self._get(task_id), True

    def _get(self, task_id: str) -> TaskRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return _record(row)

    def get(self, task_id: str) -> TaskRecord:
        return self._get(task_id)

    def by_request(self, client_request_id: str) -> TaskRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT task_id FROM tasks WHERE client_request_id=?",
                                     (client_request_id,)).fetchone()
        if row is None:
            continuation = self.round_request(client_request_id)
            if continuation is not None:
                return self._get(continuation['task_id'])
            raise TaskNotFound(client_request_id)
        return self._get(row["task_id"])

    def next_queued(self) -> TaskRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._get(row["task_id"]) if row else None

    def claim(self, task_id: str) -> bool:
        """Move queued -> running exactly once, so two workers cannot both start it."""
        now = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET status='running', stage='isochrone', started_at=?,"
                " stage_started_at=?, activity_at=?, progress=NULL, updated_at=?"
                " WHERE task_id=? AND status='queued'", (now, now, now, now, task_id))
            return cursor.rowcount == 1

    def update(self, task_id: str, *, activity: bool = True, **fields) -> TaskRecord:
        """Write the named fields; a stage that changes restarts its clock and step.

        ``activity`` is False for writes that are not the worker's doing -- a
        client asking to cancel -- so "the server last did something at" stays
        a statement about the computation, not about who called in.
        """
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        if not fields:
            return self._get(task_id)
        now = time.time()
        if "progress" in fields and fields["progress"] is not None:
            fields["progress"] = json.dumps(fields["progress"], ensure_ascii=False, sort_keys=True)
        assignments = [f"{name}=?" for name in fields]
        values = [int(value) if isinstance(value, bool) else value for value in fields.values()]
        if "stage" in fields:
            # SQLite evaluates every right-hand side against the old row, so the
            # comparison below sees the stage being left, not the one written.
            assignments.append("stage_started_at=CASE WHEN stage IS ? THEN stage_started_at ELSE ? END")
            values += [fields["stage"], now]
            if "progress" not in fields:
                assignments.append("progress=CASE WHEN stage IS ? THEN progress ELSE NULL END")
                values.append(fields["stage"])
        if activity:
            assignments.append("activity_at=?")
            values.append(now)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)}, updated_at=? WHERE task_id=?",
                (*values, now, task_id))
        return self._get(task_id)

    def publish(self, task_id: str, *, stage: str, snapshot: dict, result_hash: str,
                summary: dict | None = None, cancelled=None) -> int:
        """Freeze one revision. The payload is durable before its index row exists."""
        record = self._get(task_id)
        revision = record.revision + 1
        # A cancelled report can finish serializing after a continuation starts.
        # Its unindexed file must never overwrite that continuation's revision.
        suffix = '' if cancelled is None else f'-{uuid4().hex}'
        relative = Path("tasks") / task_id / f"revision-{revision:04d}-{stage}{suffix}.json"
        if cancelled is not None and cancelled():
            from ..accessibility.grid import AssessmentCancelled
            raise AssessmentCancelled()
        atomic_dump(self.root / relative, snapshot)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if cancelled is not None and cancelled():
                from ..accessibility.grid import AssessmentCancelled
                raise AssessmentCancelled()
            connection.execute(
                "INSERT INTO revisions (task_id, revision, stage, created_at, result_hash, payload,"
                " summary) VALUES (?,?,?,?,?,?,?)",
                (task_id, revision, stage, time.time(), result_hash, str(relative.as_posix()),
                 None if summary is None else retention.dump(summary)))
            now = time.time()
            connection.execute(
                "UPDATE tasks SET revision=?, stage=?, updated_at=?, activity_at=?,"
                " stage_started_at=CASE WHEN stage IS ? THEN stage_started_at ELSE ? END"
                " WHERE task_id=?", (revision, stage, now, now, stage, now, task_id))
            connection.execute("COMMIT")
        return revision

    def revision_row(self, task_id: str, revision: int | None = None) -> dict | None:
        """索引行本身（修订号、阶段、哈希），**不**读修订文件。

        到期之后还能不能回答"这是哪一版、它的哈希是什么"，不能取决于那个文件还在不在 ——
        所以凡是不需要文件内容的读取都走这里。
        """
        self._get(task_id)
        with self._connection() as connection:
            if revision is None:
                row = connection.execute(
                    "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                    " WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                    " WHERE task_id=? AND revision=?", (task_id, revision)).fetchone()
        return None if row is None else dict(row)

    def set_summary(self, task_id: str, revision: int, summary: dict) -> bool:
        """给一条**还没有**汇总的修订补写汇总。返回是否写进去了。

        ``AND summary IS NULL`` 是刻意的：已经冻结的汇总不接受覆盖 —— 它是那一版结论的
        记录，而"补写"只能发生在明细还在、能当场导出的时候。
        """
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE revisions SET summary=? WHERE task_id=? AND revision=? AND summary IS NULL",
                (retention.dump(summary), task_id, revision))
            return cursor.rowcount == 1

    def summary(self, task_id: str, revision: int | None = None) -> dict | None:
        """某一版冻结下来的白名单汇总；没有就是没有（旧修订、或那一版还没有汇总）。"""
        self._get(task_id)
        with self._connection() as connection:
            if revision is None:
                row = connection.execute(
                    "SELECT summary FROM revisions WHERE task_id=?"
                    " ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT summary FROM revisions WHERE task_id=? AND revision=?",
                    (task_id, revision)).fetchone()
        if row is None or row["summary"] is None:
            return None
        return json.loads(row["summary"])

    def terminal_tasks(self) -> list[TaskRecord]:
        """所有已经走到终态的任务。清理只碰这些：正在跑的明细还有人要写。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status IN ('completed','failed','cancelled')"
                " ORDER BY rowid").fetchall()
        return [_record(row) for row in rows]

    def revisions(self, task_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                " WHERE task_id=? ORDER BY revision", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def revision(self, task_id: str, revision: int | None = None) -> dict | None:
        """Latest revision by default. Raises for an unknown task id."""
        self._get(task_id)
        with self._connection() as connection:
            if revision is None:
                row = connection.execute(
                    "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                    " WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT revision, stage, created_at, result_hash, payload FROM revisions"
                    " WHERE task_id=? AND revision=?", (task_id, revision)).fetchone()
        if row is None:
            return None
        return {**dict(row), "snapshot": json.loads((self.root / row["payload"]).read_text("utf-8"))}

    # -- 按需补查 -----------------------------------------------------------

    def create_extension(self, *, extension_id: str, task_id: str, client_request_id: str,
                         identity: dict, fingerprint: str, base_revision: int, categories, budget: int,
                         kind: str = "extension"):
        """Insert once per (task, client request id); identical repeats return the same row.

        幂等键按任务隔离：两个任务里的同一个客户端请求标识是两次不同的补查。

        The check happens inside the same ``BEGIN IMMEDIATE`` transaction as the
        insert, so two callers racing on one request id cannot both create a row:
        the loser reads the winner's row back and is told it did not create
        anything, and only a creator is enqueued. ``fingerprint`` is kept as the
        audit value of the declared identity; the decision itself is
        :func:`extension_matches`, which is the one rule both callers share.
        """
        now = time.time()
        encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM facility_extensions WHERE task_id=? AND client_request_id=?",
                (task_id, client_request_id)).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                # ``budget`` here is the resolution the caller already computed for this
                # request; the comparison consults it only when the stored row cannot
                # answer the question on its own declaration alone.
                if not extension_matches(_extension_record(existing), identity=identity,
                                         resolved_budget=budget):
                    raise RequestIdConflict(client_request_id)
                return _extension_record(existing), False
            connection.execute(
                "INSERT INTO facility_extensions (extension_id, task_id, client_request_id,"
                " fingerprint, base_revision, categories, budget, status, created_at, updated_at,"
                " intent, kind) VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?)",
                (extension_id, task_id, client_request_id, fingerprint, base_revision,
                 json.dumps(list(categories), ensure_ascii=False), budget, now, now, encoded, kind))
            connection.execute("COMMIT")
            return self._extension(extension_id), True

    def find_extension(self, task_id: str, client_request_id: str) -> ExtensionRecord | None:
        """The row this request id already names, if any. Read-only, no side effects.

        A lookup and not a create: answering a repeat must not enqueue, reserve or
        run anything, and it must not need the parent's boundary or the day's
        balance to have been consulted first.
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM facility_extensions WHERE task_id=? AND client_request_id=?",
                (task_id, client_request_id)).fetchone()
        return None if row is None else _extension_record(row)

    def _extension(self, extension_id: str) -> ExtensionRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM facility_extensions WHERE extension_id=?",
                                     (extension_id,)).fetchone()
        if row is None:
            raise ExtensionNotFound(extension_id)
        return _extension_record(row)

    def extension(self, extension_id: str) -> ExtensionRecord:
        return self._extension(extension_id)

    def extensions_of(self, task_id: str) -> list[ExtensionRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM facility_extensions WHERE task_id=? ORDER BY created_at,"
                " extension_id", (task_id,)).fetchall()
        return [_extension_record(row) for row in rows]

    def claim_extension(self, extension_id: str) -> bool:
        """queued -> running 恰好一次，所以两个 worker 不会同时开始同一次补查。"""
        now = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE facility_extensions SET status='running', stage='poi', updated_at=?"
                " WHERE extension_id=? AND status='queued'", (now, extension_id))
            return cursor.rowcount == 1

    def update_extension(self, extension_id: str, **fields) -> ExtensionRecord:
        unknown = set(fields) - EXTENSION_UPDATABLE
        if unknown:
            raise ValueError(f"unknown extension fields: {sorted(unknown)}")
        if not fields:
            return self._extension(extension_id)
        if "counts_by_category" in fields and fields["counts_by_category"] is not None:
            fields["counts_by_category"] = json.dumps(fields["counts_by_category"],
                                                      ensure_ascii=False, sort_keys=True)
        assignments = [f"{name}=?" for name in fields]
        with self._connection() as connection:
            connection.execute(
                f"UPDATE facility_extensions SET {', '.join(assignments)}, updated_at=?"
                " WHERE extension_id=?", (*fields.values(), time.time(), extension_id))
        return self._extension(extension_id)

    def finish_extension(self, extension_id: str, *, status: str, document_path: str | None,
                         counts_by_category: dict | None = None, requests: int = 0,
                         network_requests: int = 0, facilities_status: str | None = None,
                         error: str | None = None, stop_reason: str | None = None,
                         initial_plan: dict | None = None) -> ExtensionRecord:
        """定稿一次补查。``document_path`` 在行上出现就意味着结果已经落到磁盘。"""
        return self.update_extension(
            extension_id, status=status, stage="ready" if document_path else None,
            document=document_path, counts_by_category=counts_by_category,
            requests=requests, network_requests=network_requests,
            facilities_status=facilities_status, error=error, finished_at=time.time(),
            stop_reason=stop_reason,
            initial_plan=(None if initial_plan is None
                          else json.dumps(initial_plan, ensure_ascii=False, sort_keys=True)))

    def extension_document(self, extension_id: str) -> dict | None:
        record = self._extension(extension_id)
        if record.document_path is None:
            return None
        return json.loads((self.root / record.document_path).read_text("utf-8"))

    def publish_extension_document(self, extension_id: str, task_id: str, document: dict) -> str:
        """先把结果写到磁盘，再由调用方把它记进行里 —— 与修订同序，半写不入索引。"""
        relative = Path("tasks") / task_id / f"extension-{extension_id}.json"
        atomic_dump(self.root / relative, document)
        return relative.as_posix()
