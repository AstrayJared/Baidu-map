import asyncio
import logging
from .algorithms.osm_offline.lazy import GraphLoadCancelled

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from .config import Settings, load_settings
from .analysis import router
from .checkups import CheckupError, build_checkups, capabilities_router, checkup_router
from .engines import UnknownEngine
from .contracts import AnalysisResponse, FacilityCatalog, Issue
from .analyses import AnalysisManager, analysis_router
from .hybrid_api import HybridManager, hybrid_router
from .osm_api import router as osm_router
from .quota import Quota
from .catalog import catalog_payload

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: str
    baidu_ak_configured: bool
    default_analysis_engine: str
    osm_state: str


def create_app(settings: Settings | None = None, *, provider_factory=None,
               hybrid_provider_factory=None, place_factory=None, route_factory=None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    # One allocation entry for the whole application: every walking-route request
    # (both engines, the legacy endpoints, verification, click-detail routes) paces
    # on the one direction gate and every place search on the one place gate, both
    # at the active tier, so no two limiters can add up on the same key.
    quota = Quota(config)
    manager = AnalysisManager(config, provider_factory, gate=quota.direction.gate,
                              place_gate=quota.place.gate)
    hybrid = HybridManager(config, manager.gate, hybrid_provider_factory)
    # The holder remains lazy for callers and tests, while configured deployments
    # warm the graph during serving startup so the first estimate/task does not
    # pay the 50+ second cache and spatial-index construction cost.
    from .algorithms.osm_offline.lazy import LazyOsmOfflineEngine
    offline = LazyOsmOfflineEngine(config)
    checkups = build_checkups(config, manager.gate, offline, quota=quota,
                              provider_factory=provider_factory,
                              hybrid_provider_factory=hybrid_provider_factory,
                              place_factory=place_factory, route_factory=route_factory)

    @asynccontextmanager
    async def lifespan(app):
        # 重启清点在**开始服务**时做，不在导入时：导入 app 对象（比如导出 OpenAPI）
        # 不该把别人正在跑的体检判成中断。清点只改状态，不重放任何已付费的请求。
        checkups.interrupt_unfinished()
        # §5 B2 决策 2：到期是一个事件（最后一次心跳、第三次后续体检结束），
        # 没有任何请求会因为它的到来而发生，所以必须有人定期去看一眼。
        checkups.start_maintenance(interval=config.retention_maintenance_seconds)
        preload_task = None

        async def preload_osm():
            try:
                await asyncio.to_thread(offline.get)
                logger.info("OSM walking graph preloaded before serving requests: %s", offline.state)
            except GraphLoadCancelled:
                logger.info('OSM preload stopped for application shutdown')
            except Exception:
                # OSM is optional; a corrupt or incompatible package should not
                # take the Baidu-only application offline.
                logger.exception("OSM walking graph preload failed")
        if (config.osm_data_version != "unconfigured"
                and config.osm_graph_cache_path is not None
                and offline.begin_preload()):
            preload_task = asyncio.create_task(preload_osm())
        if config.analysis_provider == "synthetic" and provider_factory is None:
            # 环境变量优先于 .env：终端里设过一次 synthetic，之后每次启动都是合成模式。
            logger.warning("ANALYSIS_PROVIDER=synthetic：离线合成模式，E8.2 只会画出半径约 1080 米的正圆，"
                           "设施检索、服务覆盖与核验不运行。去掉该环境变量（或设为 baidu）后重启即为百度模式。")
        try:
            yield
        finally:
            # Stop new work first; the optional city load must not keep a
            # shutting-down server waiting for a complete graph rebuild.
            offline.stop()
            await checkups.close()
            await hybrid.close()
            await manager.close()
            if preload_task is not None:
                await preload_task

    app = FastAPI(title="Life Circle Backend", version="2.0.0", debug=False, lifespan=lifespan)
    app.state.osm_offline = offline
    app.state.analyses = manager
    app.state.hybrid = hybrid
    app.state.checkups = checkups
    app.state.quota = quota
    # The two legacy algorithms intentionally coexist on separate, stable
    # contracts. /api/analyses is Baidu-only; /api/v1/analysis/hybrid is OSM +
    # Baidu. /api/v2/checkups is the versioned orchestrator over both.
    app.include_router(analysis_router(manager))
    app.include_router(hybrid_router(hybrid))
    app.include_router(checkup_router(checkups))
    app.include_router(capabilities_router(checkups, config, offline))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Checkup-Session"],
    )
    app.include_router(router)
    app.include_router(osm_router)

    def failure_response(status_code: int, code: str, message: str):
        body = AnalysisResponse(status="failed", source="system", errors=[
            Issue(code=code, message=message, scope="request", severity="error")
        ])
        return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))

    def checkup_failure(status_code: int, code: str, message: str):
        return JSONResponse(status_code=status_code, content={"code": code, "message": message})

    @app.exception_handler(CheckupError)
    async def checkup_error(request, exc):
        return checkup_failure(exc.status_code, exc.code, exc.message)

    @app.exception_handler(UnknownEngine)
    async def unknown_engine(request, exc):
        # An engine or budget tier the registry does not serve is a request
        # error; the other algorithm is never entered as a substitute.
        status = 422 if exc.reason == "unsupported_budget" else 404
        return checkup_failure(status, f"checkup_{exc.reason}", exc.reason)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith("/api/v2/"):
            # Never echo raw input, URLs or credentials from a validation error.
            return checkup_failure(422, "checkup_invalid_request", "请求字段无效，请核对引擎、坐标和预算。")
        if request.url.path.startswith("/api/v1/analysis/hybrid"):
            return JSONResponse(status_code=422, content={"code": "hybrid_invalid_request", "message": "Invalid Hybrid request"})
        if request.url.path.startswith("/api/analyses"):
            # Keep the task API's response shape without reflecting raw inputs.
            return JSONResponse(status_code=422, content={"detail": "Invalid analysis request"})
        # Never echo raw input, URLs or credentials from a validation exception.
        return failure_response(422, "INVALID_REQUEST", "请求字段无效，请核对坐标、坐标系、场景和参数。")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if request.url.path.startswith("/api/v2/"):
            # Routing and method errors keep the checkup envelope, like Hybrid.
            return checkup_failure(exc.status_code, "checkup_http_error", "请求路径或方法不可用。")
        if request.url.path.startswith("/api/v1/analysis/hybrid"):
            detail = exc.detail if isinstance(exc.detail, dict) and "code" in exc.detail else {
                "code": "hybrid_http_error", "message": "Hybrid request unavailable"}
            return JSONResponse(status_code=exc.status_code, content=detail)
        if request.url.path.startswith("/api/analyses"):
            return await http_exception_handler(request, exc)
        return failure_response(exc.status_code, "HTTP_ERROR", "请求路径或方法不可用。")

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        if request.url.path.startswith("/api/v2/"):
            return checkup_failure(500, "checkup_internal_error", "体检暂不可用，请稍后重试。")
        return failure_response(500, "INTERNAL_ERROR", "分析暂不可用，请稍后重试。")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            baidu_ak_configured=config.ak_configured,
            default_analysis_engine="synthetic" if config.analysis_provider == "synthetic" else "baidu",
            osm_state=app.state.osm_offline.state,
        )

    @app.get("/api/facility-catalog", response_model=FacilityCatalog)
    def facility_catalog() -> FacilityCatalog:
        """Return the versioned facility taxonomy used by search and responses."""
        return catalog_payload()

    return app


app = create_app()
