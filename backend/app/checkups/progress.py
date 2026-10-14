"""运行中任务的阶段内进展：每一步的读者说法与真实计数。

进展只写已经发生的事：发出了几次采样、几次检索请求，判定了几格，核验到第几家。
``limit`` 是这一步不会越过的数（预算、候选数），不是预计总量 —— 很多步骤会提前
停下，把它读成完成百分比就是在编数。没有计数的一步（载入路网、建立评估域）只报
"正在做什么、从什么时候开始"，界面据此显示本步已持续多久。
"""
import threading
import time

from ..catalog import MAJOR_LABELS

#: 步骤代码 -> （读者说法，计数单位）。成圈的前六个是 E8.2 引擎自报的子阶段名。
STEPS: dict[str, tuple[str, str | None]] = {
    "initializing": ("初始化采样网格", "次采样"),
    "expanding": ("向外扩展采样", "次采样"),
    "exploring": ("沿边界探索", "次采样"),
    "refining": ("细化边界", "次采样"),
    "reconstructing": ("重建等时圈边界", "次采样"),
    "completed": ("成圈完成，整理结果", "次采样"),
    "graph": ("载入 OSM 步行路网", None),
    # 首次载入城市路网（本机十几分钟）的四步：读文件、建图、逐条校验、建索引。
    "graph_read": ("载入 OSM 步行路网 · 读入路网文件", None),
    "graph_build": ("载入 OSM 步行路网 · 建立路网", "条边"),
    "graph_check": ("载入 OSM 步行路网 · 校验边的几何与耗时", "条边"),
    "graph_index": ("载入 OSM 步行路网 · 建立空间索引", None),
    "guidance": ("生成 OSM 引导", None),
    "obstacles": ("载入障碍与水系复核层", None),
    "sampling": ("百度步行采样", "次采样"),
    "places": ("检索设施", "次请求"),
    "domain": ("建立评估域", None),
    "views": ("准备步行图", None),
    "attachments": ("建立设施接入索引", None),
    "category": ("评估服务覆盖", "格"),
    "essential_category": ("评估基础三项专项", "格"),
    "routes": ("核验候选设施的步行路线", "家"),
    "report": ("汇总报告", None),
}


def progress_payload(step: str, *, since: float, count: int | None = None,
                     limit: int | None = None, label: str | None = None) -> dict:
    """The stored form of one step. Unknown step codes keep their code as the label.

    A step listed without a unit has nothing to count, whatever the caller
    passes along: the engine's snapshot during the graph load still says "0
    attempts", and a 0 there would read as a count that has not moved.
    """
    default, unit = STEPS.get(step, (step, None))
    if step in STEPS and unit is None:
        count = limit = None
    return {"step": step, "label": label or default, "count": count, "limit": limit,
            "unit": unit if count is not None else None, "since": since}


def category_label(major: str, index: int, total: int) -> str:
    return f"评估服务覆盖 · {MAJOR_LABELS.get(major, major)}（第 {index}/{total} 类）"


class StepReporter:
    """One task's in-stage progress writer.

    ``write`` receives ``progress=...`` plus any task counters and stores them.
    A step keeps its ``since`` across writes of the same step (and ``key``), so
    "this step has lasted" survives a counter tick. ``throttle`` writes -- the
    per-cell tick of the assessment -- go out at most once per ``interval``;
    every other write goes out at once, so a network counter is never shown
    behind what was actually sent.

    Called from the event loop and from the assessment's worker thread, never
    both at the same time; the lock only keeps the step bookkeeping whole.
    """

    def __init__(self, write, *, interval: float = 1.0, clock=time.time):
        self._write, self.interval, self.clock = write, interval, clock
        self._key, self._since, self._last = None, None, float("-inf")
        self._lock = threading.Lock()

    def __call__(self, step: str, *, count: int | None = None, limit: int | None = None,
                 label: str | None = None, key=None, throttle: bool = False, **counters) -> bool:
        now = self.clock()
        with self._lock:
            identity = (step, key)
            if identity != self._key:
                self._key, self._since = identity, now
            elif throttle and now - self._last < self.interval:
                return False
            self._last = now
            payload = progress_payload(step, since=self._since, count=count, limit=limit,
                                       label=label)
        self._write(progress=payload, **counters)
        return True
