"""离线浏览器集成套件的受控后端：真实任务链路，零外部请求、零百度额度。

它替换的只是**出进程的那一层**（合成设施页、直线步行核验），其余全是生产代码：任务、
修订、进度、取消、恢复、图层、报告、预算账本与页面缓存都是生产的那一套。进程里装了
套接字守卫，任何非回环地址的解析与连接一律拒绝并记数；验收以 ``/control/state`` 的
``external.attempts == 0`` 证明这一轮一次额度都没花。

与 ``checkup_lifecycle_app.py`` 的分工：那一套用**真底图**验收长任务与实时进度，启动时
要预载本机 OSM 路网（首次十几分钟）；这一套不碰路网（``osm_data_version`` 留
``unconfigured``），配离线的地图 SDK 替身，几十秒跑完，管的是"预算、缓存、补查、幂等、
取消"这些在浏览器里看得见的语义。

配置一律显式给（``Settings(_env_file=None, …)``）：**绝不读 backend/.env**，真实 AK 不会
被这个进程碰到，合成传输拿到的是一个自造的假 key，它从不离开进程。

启动（在 backend 目录，服务端 AK 不需要）::

    BROWSER_DIR=../.tmp/checkup-browser BROWSER_ORIGIN=http://127.0.0.1:5184 \\
      .venv/bin/python -m uvicorn checkup_browser_app:app --app-dir tests \\
      --host 127.0.0.1 --port 8022 --no-access-log

控制接口只在这个应用上：

    POST /control/respond    {"mode": "sparse|dense|rate_limit|upstream_error", "delay": 0}
    POST /control/allowance  {"placeDailyBudget": 0}   改当天生效的地点日额度
    POST /control/reset      清空派发记录与当天账本，回到默认应答
    GET  /control/state      派发数、账本、缓存、任务、外部连接
"""
import ipaddress
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import SecretStr

# -- 套接字守卫：先装上，再导入任何可能联网的东西 --------------------------------

EXTERNAL = {"attempts": 0, "targets": []}
_GUARD_LOCK = threading.Lock()


def _refuse(target) -> None:
    with _GUARD_LOCK:
        EXTERNAL["attempts"] += 1
        EXTERNAL["targets"].append(str(target)[:80])
    raise ConnectionRefusedError("browser harness: external connections are refused")


def _loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _address_host(address):
    return address[0] if isinstance(address, tuple) else address


_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not _loopback(host):
        _refuse(host)
    return _getaddrinfo(host, *args, **kwargs)


def _guard_connect(original):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return original(self, address)
    return connect


def _guard_sock_connect(original):
    async def sock_connect(self, sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return await original(self, sock, address)
    return sock_connect


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guard_connect(socket.socket.connect)
socket.socket.connect_ex = _guard_connect(socket.socket.connect_ex)
# asyncio 在 Windows 上用 ConnectEx 直接连，不经过 socket.connect：两种事件循环都守住。
from asyncio import proactor_events, selector_events  # noqa: E402

for _loop in (proactor_events.BaseProactorEventLoop, selector_events.BaseSelectorEventLoop):
    _loop.sock_connect = _guard_sock_connect(_loop.sock_connect)

# -- 配置：临时的任务库与账本，显式给出的合成设置 ------------------------------

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.config import Settings  # noqa: E402
from app.quota import PLACE, ServiceTier  # noqa: E402

ROOT = Path(os.environ.get("BROWSER_DIR", "../.tmp/checkup-browser")).resolve()
ORIGIN = os.environ.get("BROWSER_ORIGIN", "http://127.0.0.1:5184")
ROOT.mkdir(parents=True, exist_ok=True)
# 每次从空的任务库与账本开始：这一轮的派发数与账本增量才只属于这一轮。只删自己建的那几样。
for _name in ("checkups", "ledgers"):
    shutil.rmtree(ROOT / _name, ignore_errors=True)
(ROOT / "quota.sqlite3").unlink(missing_ok=True)
#: 合成传输要一个非空的 AK 才会被调用；它从不离开进程（守卫在上面）。
OFFLINE_KEY = "browser-offline-no-network"

settings = Settings(
    _env_file=None, baidu_map_ak=SecretStr(OFFLINE_KEY), analysis_provider="synthetic",
    baidu_place_qps=10000, baidu_direction_qps=10000,
    # 档位不随日历切换：这个受控后端的额度只由 /control/allowance 决定。
    baidu_quota_fallback_at=datetime(2099, 1, 1, tzinfo=timezone(timedelta(hours=8))),
    # 不配置 OSM：评估阶段会具名拒绝，但设施检索、报告与修订照常发布，套件也不必等路网。
    osm_data_version="unconfigured",
    checkup_dir=ROOT / "checkups", hybrid_ledger_dir=ROOT / "ledgers",
    quota_ledger_path=ROOT / "quota.sqlite3",
    hybrid_obstacle_path=Path("missing-browser-obstacles"),
    hybrid_risk_path=Path("missing-browser-risks"),
    cors_origins=[ORIGIN])

# main 在导入时建一个默认应用：别让它去读 backend/.env。
with patch("app.config.load_settings", return_value=settings):
    from app.main import create_app  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_checkup_facilities import (  # noqa: E402
    FastGate, OfflineHybrid, SyntheticPlaces, SyntheticRoutes, analytic, row, straight_routes)

# -- 受控的替身 ------------------------------------------------------------------

#: 合成设施落在任务中心上：界面上的"最近设施"与图上的点位点得着。
CENTRE = (116.405, 39.916)


def one_per_block(sequence, page):
    """每页回一条本类设施，位置给任务中心：首轮就能查完（4 块 × 14 小类 = 56 页）。

    行放在中心而不是块中心：圈面比查询域小，块中心落在圈外，只会变成
    ``outside_boundary_records``，界面上一个设施都拿不到。
    """
    results = [row(sequence, 0, at=CENTRE)]
    return {"status": 0, "total": len(results), "result_type": "poi_type", "results": results}, None


def two_pages(sequence, page):
    """每页 20 条、总数 40：一次检索要翻到第二页才算查完。

    首轮 56 页装得进用例显式设置的 60 次预算，整轮 112 页装不下 —— "首轮"与"查完"
    因此是两件事，正好用来在界面与接口上看 partial 与补查。
    """
    results = [row(sequence, page * 20 + index, at=CENTRE) for index in range(20)]
    return {"status": 0, "total": 40, "result_type": "poi_type", "results": results}, None


def always(reason):
    """上游一直用同一个理由拒绝：用来测有界停止与"不写成零设施"。"""
    return lambda sequence, page: (None, reason)


RESPONDERS = {"sparse": one_per_block, "dense": two_pages,
              "rate_limit": always("rate_limit"), "upstream_error": always("upstream_error")}

places, routes = SyntheticPlaces(one_per_block), SyntheticRoutes(straight_routes())
MODE = {"name": "sparse"}

app = create_app(settings, provider_factory=analytic,
                 hybrid_provider_factory=lambda projection, config: OfflineHybrid(projection),
                 place_factory=lambda _settings: places, route_factory=lambda _settings: routes)
for _engine in app.state.checkups.registry.engines.values():
    _engine.gate = FastGate()


# -- 记账与控制接口 --------------------------------------------------------------


def _tasks() -> list[dict]:
    path = app.state.checkups.store.path
    if not Path(path).exists():
        return []
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT task_id, client_request_id, engine, status, stage, revision, payload"
            " FROM tasks ORDER BY created_at").fetchall()
    tasks = []
    for task_id, request_id, engine, status, stage, revision, payload in rows:
        tasks.append({"taskId": task_id, "clientRequestId": request_id, "engine": engine,
                      "status": status, "stage": stage, "revision": revision,
                      "center": json.loads(payload).get("center")})
    return tasks


def state() -> dict:
    with _GUARD_LOCK:
        external = {"attempts": EXTERNAL["attempts"], "targets": list(EXTERNAL["targets"])}
    quota = app.state.quota
    active = quota.tiers.active()
    return {
        "mode": MODE["name"],
        "external": external,
        "dispatched": len(places.sent),
        "routes": len(routes.sent),
        "ledger": {"service": PLACE, "day": quota.ledger.day(), "spent": quota.ledger.spent(PLACE),
                   "budget": active.place_daily_budget, "remaining": quota.remaining(PLACE)},
        "cachePages": len(app.state.checkups.cache),
        "tasks": _tasks(),
    }


@app.get("/control/state")
def control_state():
    return state()


@app.post("/control/respond")
async def control_respond(request: Request):
    body = await request.json()
    name = body.get("mode", "sparse")
    if name not in RESPONDERS:
        return JSONResponse({"error": "unknown_mode", "modes": sorted(RESPONDERS)}, status_code=422)
    places.respond = RESPONDERS[name]
    places.delay = float(body.get("delay", 0))
    MODE["name"] = name
    return {"mode": name, "delay": places.delay, "dispatched": len(places.sent)}


@app.post("/control/allowance")
async def control_allowance(request: Request):
    """改当天生效的地点日额度。写入两条档位就能立刻生效：每次派发都重读当前档。"""
    body = await request.json()
    budget = int(body["placeDailyBudget"])
    quota = app.state.quota
    active = quota.tiers.active()
    tier = ServiceTier(active.direction_qps, active.place_qps, budget, active.label)
    quota.tiers.current = quota.tiers.fallback = tier
    return {"placeDailyBudget": budget, "remaining": quota.remaining(PLACE)}


@app.post("/control/reset")
async def control_reset():
    """回到默认应答，并清空派发记录与当天账本：每个用例从同一行起跑。"""
    manager = app.state.checkups
    for task in _tasks():
        if task['status'] not in ('completed', 'failed', 'cancelled'):
            manager.cancel(task['taskId'])
    for token in list(manager.extension_tokens.values()):
        token.cancel()
    await asyncio.wait_for(manager.queue.join(), 10)
    places.respond, places.delay = one_per_block, 0.0
    MODE["name"] = "sparse"
    places.sent.clear()
    quota = app.state.quota
    with sqlite3.connect(quota.ledger.path) as connection:
        connection.execute("DELETE FROM daily_spend WHERE service=? AND day=?",
                           (PLACE, quota.ledger.day()))
    return state()
