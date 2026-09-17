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
from .contracts import AnalysisResponse, Issue
from .analyses import AnalysisManager, analysis_router
from .hybrid_api import HybridManager, hybrid_router
from .osm_api import router as osm_router


class HealthResponse(BaseModel):
    status: str
    baidu_ak_configured: bool
    default_analysis_engine: str
    osm_state: str


def create_app(settings: Settings | None = None, *, provider_factory=None,
               hybrid_provider_factory=None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    manager = AnalysisManager(config, provider_factory)
    hybrid = HybridManager(config, manager.gate, hybrid_provider_factory)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await hybrid.close()
        await manager.close()

    app = FastAPI(title="Life Circle Backend", version="2.0.0", debug=False, lifespan=lifespan)
    from .algorithms.osm_offline.lazy import LazyOsmOfflineEngine
    app.state.osm_offline = LazyOsmOfflineEngine(config)
    app.state.analyses = manager
    app.state.hybrid = hybrid
    # The two algorithms intentionally coexist on separate, stable contracts.
    # /api/analyses is Baidu-only; /api/v1/analysis/hybrid is OSM + Baidu.
    app.include_router(analysis_router(manager))
    app.include_router(hybrid_router(hybrid))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    app.include_router(router)
    app.include_router(osm_router)

    def failure_response(status_code: int, code: str, message: str):
        body = AnalysisResponse(status="failed", source="system", errors=[
            Issue(code=code, message=message, scope="request", severity="error")
        ])
        return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path.startswith("/api/v1/analysis/hybrid"):
            return JSONResponse(status_code=422, content={"code": "hybrid_invalid_request", "message": "Invalid Hybrid request"})
        if request.url.path.startswith("/api/analyses"):
            # Keep the task API's response shape without reflecting raw inputs.
            return JSONResponse(status_code=422, content={"detail": "Invalid analysis request"})
        # Never echo raw input, URLs or credentials from a validation exception.
        return failure_response(422, "INVALID_REQUEST", "请求字段无效，请核对坐标、坐标系、场景和参数。")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if request.url.path.startswith("/api/v1/analysis/hybrid"):
            detail = exc.detail if isinstance(exc.detail, dict) and "code" in exc.detail else {
                "code": "hybrid_http_error", "message": "Hybrid request unavailable"}
            return JSONResponse(status_code=exc.status_code, content=detail)
        if request.url.path.startswith("/api/analyses"):
            return await http_exception_handler(request, exc)
        return failure_response(exc.status_code, "HTTP_ERROR", "请求路径或方法不可用。")

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return failure_response(500, "INTERNAL_ERROR", "分析暂不可用，请稍后重试。")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            baidu_ak_configured=config.ak_configured,
            default_analysis_engine="synthetic" if config.analysis_provider == "synthetic" else "baidu",
            osm_state=app.state.osm_offline.state,
        )

    return app


app = create_app()
