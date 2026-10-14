"""Single-point official Geoconv v2, with a separately metered service pool."""
import math
import time
from typing import Literal

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .checkups.models import CheckupModel
from .contracts import Origin
from .quota import DailyBudgetExhausted, DeadlineReached
from .baidu import silence_transport_logs


class CoordinateRequest(CheckupModel):
    point: Origin
    source_coordinate_system: Literal['wgs84', 'gcj02', 'bd09ll']


class CoordinateResult(CheckupModel):
    point: Origin
    coordinate_system: Literal['bd09ll'] = 'bd09ll'
    source_coordinate_system: Literal['wgs84', 'gcj02', 'bd09ll']
    provider: Literal['identity', 'baidu_geoconv_v2']


def coordinates_router(settings, quota, transport=None):
    router = APIRouter(prefix='/api/v2/coordinates', tags=['coordinates'])

    def failure(status, code, message):
        return JSONResponse(status_code=status, content={'code': code, 'message': message})

    @router.post('/convert', response_model=CoordinateResult)
    async def convert(payload: CoordinateRequest):
        source = payload.source_coordinate_system
        if source == 'bd09ll':
            return CoordinateResult(point=payload.point, source_coordinate_system=source, provider='identity')
        if settings.analysis_provider == 'synthetic' or not settings.ak_configured:
            return failure(503, 'coordinates_unavailable', '当前环境未启用官方坐标转换；可直接输入百度坐标。')
        silence_transport_logs()
        try:
            async with quota.geoconv.attempt(time.monotonic() + 10) as attempt:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport) as client:
                    response = await client.get('https://api.map.baidu.com/geoconv/v2/',
                        params={'ak': settings.baidu_map_ak.get_secret_value(),
                                'coords': f'{payload.point.lng},{payload.point.lat}',
                                'model': 2 if source == 'wgs84' else 1}, timeout=8)
                if response.status_code != 200:
                    attempt.outcome('http_error')
                    if response.status_code == 429:
                        return failure(429, 'coordinates_rate_limited', '官方坐标转换已限流，请稍后重试；地图中心未改变。')
                    if response.status_code in (401, 403):
                        return failure(502, 'coordinates_permission_denied', '官方坐标转换权限校验失败，请检查服务端 AK 与接口权限。')
                    return failure(502, 'coordinates_upstream_error', '官方坐标转换暂不可用，请稍后重试。')
                body = response.json()
                status = body.get('status')
                if type(status) is not int:
                    raise ValueError('invalid_status')
                if status != 0:
                    # Official business statuses can arrive with HTTP 200. Keep
                    # Geoconv's status=4 (conversion failure) distinct from POI codes.
                    if status in (301, 302):
                        attempt.outcome('quota')
                        return failure(429, 'coordinates_upstream_quota', '百度账号的坐标转换配额已用尽；本应用余额不代表账号权益。')
                    if status in (401, 402):
                        attempt.outcome('rate_limit')
                        return failure(429, 'coordinates_rate_limited', '官方坐标转换已限流，请稍后重试；地图中心未改变。')
                    if status in (101, 102, 200, 201, 202, 203, 210, 211, 220, 230, 240, 250, 251, 252, 260, 261):
                        attempt.outcome('permission')
                        return failure(502, 'coordinates_permission_denied', '官方坐标转换权限校验失败，请检查服务端 AK、服务开关与校验配置。')
                    attempt.outcome('upstream_rejected')
                    if status == 4:
                        return failure(502, 'coordinates_conversion_failed', '官方坐标转换失败，请检查坐标范围与来源坐标系；地图中心未改变。')
                    return failure(502, 'coordinates_rejected', '百度拒绝坐标转换，请核对服务权限、额度与来源坐标。')
                result = body['result']
                if not isinstance(result, list) or len(result) != 1:
                    raise ValueError('invalid_result')
                x, y = result[0]['x'], result[0]['y']
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in (x, y)):
                    raise ValueError('invalid_coordinate')
                point = Origin(lng=x, lat=y)
                attempt.outcome(None)
                return CoordinateResult(point=point, source_coordinate_system=source, provider='baidu_geoconv_v2')
        except (DailyBudgetExhausted, DeadlineReached):
            return failure(429, 'coordinates_budget_exhausted', '本应用坐标转换额度或等待时限已用尽，请稍后重试。')
        except httpx.TimeoutException:
            return failure(504, 'coordinates_timeout', '官方坐标转换超时，请重试；地图中心未改变。')
        except (httpx.RequestError, ValueError, KeyError, TypeError, AttributeError):
            return failure(502, 'coordinates_invalid_response', '官方坐标转换未返回有效结果，请重试。')
    return router
