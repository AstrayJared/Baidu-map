"""Manual continuation of a frozen region; boundary calls are never replayed."""
import asyncio
import time

from .. import catalog
from ..engines import EngineContext, IsochroneSnapshot, canonical_hash
from ..poi.online import RETRY_ERRORS, FATAL
from ..request_control import wait_with_cancel
from .facilities import collect_facilities
from .facilities import FacilityOutcome
from .models import CheckupCompletion, CheckupRequest, DEFAULT_POI_REQUESTS, DISTANCE_RULE, RULE_VERSION, TERMINAL
from .progress import StepReporter
from .rounds import RoundConflict


def merge_saved_facilities(outcome, prior):
    """Carry observations forward without claiming any historical page completed."""
    group = outcome.group
    if prior is None:
        return outcome
    if group is None:
        return FacilityOutcome(group=prior, status='partial', issues=outcome.issues,
                               requests=outcome.requests, network_requests=outcome.network_requests)
    fields = ('facilities', 'nearby_facilities', 'review_candidates', 'excluded_candidates')
    observed = {item.get('id') for field in fields for item in getattr(group, field)}
    values = {field: [item for item in getattr(prior, field) if item.get('id') not in observed]
                    + getattr(group, field) for field in fields}
    retained = sum(len(values[field]) - len(getattr(group, field)) for field in fields)
    values['counts_by_category'] = {major: sum(catalog.major_of(item.get('category')) == major
        for item in values['facilities']) for major in group.counts_by_category}
    times = [t for t in (group.data_obtained_at, prior.data_obtained_at) if t is not None]
    values['data_obtained_at'] = min(times) if times else None
    values['statistics'] = {**group.statistics, 'retainedPreviousFacilities': retained,
        'acceptedRecords': len(values['facilities']), 'nearbyServiceSources': len(values['nearby_facilities'])}
    if retained:
        values['warnings'] = list(dict.fromkeys(group.warnings +
            ['保留历史已取得的设施观测；历史摘要不作为本轮分页完成证据。']))
    return FacilityOutcome(group=group.model_copy(update=values), status=outcome.status,
        issues=outcome.issues, requests=outcome.requests, network_requests=outcome.network_requests)


class ContinuationMixin:
    def _round_identity(self, payload, frozen):
        paths = [self.settings.osm_graph_cache_path, self.settings.osm_coverage_boundary_path,
                 self.settings.hybrid_obstacle_path, self.settings.hybrid_risk_path]
        if self.settings.water_review_dir and self.settings.water_review_dir.is_dir():
            paths.extend(sorted(self.settings.water_review_dir.rglob('*.json')))
            paths.extend(sorted(self.settings.water_review_dir.rglob('*.geojson')))
        files = []
        for path in paths:
            if path is not None:
                stat = path.stat() if path.exists() else None
                files.append((str(path), None if stat is None else (stat.st_size, stat.st_mtime_ns)))
        return canonical_hash({'input': payload.fingerprint(), 'geometry': frozen.geometry,
                               'engineVersion': frozen.engine_version, 'catalog': catalog.DATA,
                               'rule': RULE_VERSION, 'distance': DISTANCE_RULE.model_dump(),
                               'osm': self.settings.osm_data_version,
                               'projection': self.settings.osm_metric_crs, 'dataFiles': files})

    def _bind_round(self, task_id, budget):
        current = self.store.round(task_id)
        if current is None:
            return
        budget.poi_prior = current['poi_before']
        def reserve(pool, context):
            request_id = self.store.reserve_request(task_id, current['number'], pool, context)
            return request_id
        budget.on_reserve = reserve
        budget.on_result = self.store.complete_request

    def _last_report(self, task_id):
        revisions = self.store.revisions(task_id)
        found = next((row for row in reversed(revisions) if row['stage'] == 'reporting'), None)
        return None if found is None else self.store.revision(task_id, found['revision'])['snapshot']

    def _completion(self, task_id, document=None, *, frozen=False):
        record = self.store.get(task_id)
        if not self.retention_of(record).details_available:
            return None
        current = self.store.round(task_id)
        if document is None:
            stored = self.store.revision(task_id)
            document = {} if stored is None else stored['snapshot']
        payload = CheckupRequest.from_stored(record.payload)
        group = document.get('facilities') or {}
        complete = group.get('statistics', {}).get('queryCompleteByMajor', {})
        complete = {major: major in payload.facilities.categories and
                    bool(complete.get(major, group.get('queryStatus') == 'completed'))
                    for major in payload.facilities.categories}
        score = document.get('scores') or {}
        report = document.get('report') or {}
        rows = report.get('categories') or score.get('categories') or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        evaluated = sum(bool(row.get('supported')) and row.get('assessablePct', 0) is not None
                        and row.get('assessablePct', 0) > 0 for row in rows)
        overall = report.get('overall') or score.get('overall') or {}
        all_queried = all(complete.values())
        full = all_queried and evaluated == len(complete) and bool(overall.get('available'))
        checkpoint = self.store.checkpoint(task_id)
        retryable = False
        if checkpoint is not None:
            retryable = bool(checkpoint['queue'] or any(checkpoint['later'].values())) or any(
                s['stop'] in RETRY_ERRORS | FATAL | {'cancelled', 'budget_exhausted', 'unfinished'}
                for s in checkpoint['sequences'])
        can_continue = bool((document.get('isochrone') or {}).get('geometry')) and not all_queried
        can_continue = can_continue and (checkpoint is None or retryable)
        limits = document.get('trace', {}).get('budgets', {})
        poi = limits.get('poi', {}).get('spent', 0)
        route = limits.get('route', {}).get('spent', 0)
        used = {}
        if current:
            used = self.store.round_spend(task_id, current['number'])
            poi = current['poi_before'] + used.get('poi', 0)
            route = current['route_before'] + used.get('route', 0)
        notes = []
        if not all_queried:
            notes.append('设施检索尚未完成；未查到不代表不存在。')
        if evaluated < len(complete):
            notes.append('部分类别缺少有效覆盖判定，全部未知不计为已评估。')
        reasons = {item.get('stopReason') for items in
                   group.get('statistics', {}).get('unfinishedByMajor', {}).values() for item in items}
        for keys, message in (
            ({'network_error', 'timeout', 'upstream_error'}, '仍有请求因网络或上游服务失败尚未完成。'),
            ({'permission_denied', 'invalid_ak', 'quota_exceeded', 'rate_limited'},
             '接口权限或配额拒绝了部分请求，需要恢复服务后再续查。'),
            ({'possible_truncation', 'page_limit', 'repeated_page'},
             '接口返回仍有截断或重复页限制，不能据此宣称检索完成。')):
            if reasons & keys:
                notes.append(message)
        if not can_continue and not full:
            notes.append('继续检索无法解决当前限制，请检查入口、路网或接口返回的数据范围。')
        return CheckupCompletion(
            report_revision=(document.get('revision', 0) if document.get('report') else
                next((row['revision'] for row in reversed(self.store.revisions(task_id))
                      if row['stage'] == 'reporting'), 0)),
            round_number=current['number'] if current else 1,
            round_poi_limit=current['poi_limit'] if current else
                record.payload.get('facilities', {}).get('max_poi_requests',
                    record.payload.get('facilities', {}).get('maxPoiRequests', 60)),
            round_poi_requests=used.get('poi', 0) if current else poi,
            cumulative_poi_requests=poi, route_requests=route,
            route_remaining=max(0, payload.facilities.max_route_requests - route),
            query_complete_by_major=complete, evaluated_categories=evaluated,
            total_categories=len(complete), evaluation_status='complete' if full else
                'limited' if all_queried or not can_continue else 'partial',
            can_continue=can_continue and (frozen or record.status in TERMINAL),
            restart_retrieval=checkpoint is None, stop_reason=group.get('stopReason') or record.error,
            limitations=notes)

    def continue_checkup(self, task_id, request):
        from .manager import CheckupError
        record = self.get(task_id)
        if not self.retention_of(record).details_available:
            raise CheckupError(410, 'checkup_details_expired', '体检明细已到期，无法继续检索')
        if any(item.kind == 'retry' and item.status in ('queued', 'running')
               for item in self.store.extensions_of(task_id)):
            raise CheckupError(409, 'checkup_retry_in_progress', '已有重试正在进行，请等它结束')
        existing = self.store.round_request(request.client_request_id)
        if existing:
            if existing['task_id'] != task_id or existing['base_revision'] != request.base_revision:
                raise CheckupError(409, 'checkup_request_id_conflict', '该续查请求标识已用于其他参数')
            return self.view(record)
        if record.status not in TERMINAL or record.revision != request.base_revision:
            raise CheckupError(409, 'checkup_round_conflict', '任务仍在运行或报告版本已更新，请刷新后重试')
        previous, _ = self.snapshot(task_id)
        completion = self._completion(task_id)
        if not completion.can_continue:
            raise CheckupError(409, 'checkup_cannot_continue', '当前报告没有可继续的检索工作，请查看数据限制')
        payload = CheckupRequest.from_stored(record.payload)
        frozen = IsochroneSnapshot(**previous.isochrone)
        identity = self._round_identity(payload, frozen)
        old = self.store.round(task_id)
        if (old and old['identity'] != identity or previous.rules != DISTANCE_RULE
                or previous.scope.data_version != self.settings.osm_data_version
                or previous.report and previous.report.category_directory_version not in (None, catalog.VERSION)):
            raise CheckupError(409, 'checkup_incompatible_continuation',
                               '地区范围、目录或评估规则已改变，需要重新体检')
        try:
            created = self.store.begin_round(task_id, request.client_request_id, request.base_revision,
                                            identity, poi_before=completion.cumulative_poi_requests,
                                            route_before=completion.route_requests, legacy=old is None,
                                            poi_limit=DEFAULT_POI_REQUESTS)
        except RoundConflict as exc:
            raise CheckupError(409, 'checkup_' + str(exc), '续查状态发生冲突，请刷新后重试') from None
        if created:
            self.queue.put_nowait(task_id)
            self.start()
        return self.view(self.store.get(task_id))

    async def _continue_stages(self, task_id, payload, record, token):
        from .manager import DEADLINE_SECONDS
        current = self.store.round(task_id)
        previous, _ = self.snapshot(task_id, current['base_revision'])
        frozen = IsochroneSnapshot(**previous.isochrone)
        budget = self.quota.task_budget(isochrone=record.budget, poi=current['poi_limit'],
                                       route=payload.facilities.max_route_requests)
        budget.spent['route'] = current['route_before']
        self._bind_round(task_id, budget)
        # Preserve the existing detail bucket as well as the verification bucket.
        detail = self.detail_budgets.get(task_id)
        if detail:
            budget.spent['detail'] = detail.spent.get('detail', 0)
        self._remember_budget(task_id, budget)
        context = EngineContext(task_id=task_id, token=token,
                                deadline=time.monotonic() + DEADLINE_SECONDS,
                                artifact_dir=self.store.artifact_dir(task_id))
        report = StepReporter(lambda **fields: self.store.update(task_id, **fields))
        self.store.update(task_id, stage='poi')
        # Use the admitted round limit, including after a deployment or restart.
        continuation_payload = payload.model_copy(update={'facilities': payload.facilities.model_copy(
            update={'max_poi_requests': current['poi_limit']})})
        outcome = await collect_facilities(
            continuation_payload, frozen, settings=self.settings, context=context, quota=self.quota,
            budget=budget, cache=self.cache, places_factory=self.place_factory, store=self.store,
            progress=lambda sent, limit, label=None: report('places', count=sent, limit=limit, label=label,
                requests=current['attempts_before'] + sent,
                network_requests=current['network_before'] + sent))
        prior_facilities = previous.facilities
        if prior_facilities is None or not (prior_facilities.facilities or prior_facilities.nearby_facilities):
            from .models import FacilityGroup
            for revision in reversed(self.store.revisions(task_id)):
                saved = self.store.revision(task_id, revision['revision'])['snapshot'].get('facilities')
                if saved and (saved.get('facilities') or saved.get('nearbyFacilities')):
                    prior_facilities = FacilityGroup(**saved)
                    break
        outcome = merge_saved_facilities(outcome, prior_facilities)
        self._publish_facilities(task_id, payload, frozen, budget, outcome)
        self.store.update(task_id, requests=current['attempts_before'] + outcome.requests,
                          network_requests=current['network_before'] + outcome.network_requests)
        if token.cancelled:
            self._finish(task_id, status='cancelled')
            return
        assessment = await self._publish_accessibility(task_id, payload, frozen, budget, outcome,
                                                       report=report, token=token)
        if token.cancelled:
            self._finish(task_id, status='cancelled')
            return
        prior = previous
        if prior.verification is None or not (prior.verification.facilities or prior.verification.spot_checks):
            from .models import CheckupSnapshot
            for revision in reversed(self.store.revisions(task_id)):
                saved = self.store.revision(task_id, revision['revision'])['snapshot']
                evidence = saved.get('verification') or {}
                if evidence.get('facilities') or evidence.get('spotChecks'):
                    prior = CheckupSnapshot(**saved)
                    break
        verification, assessment = await self._publish_verification(
            task_id, payload, frozen, budget, outcome, assessment, deadline=context.deadline,
            report=report, token=token, previous=prior)
        if token.cancelled:
            self._finish(task_id, status='cancelled')
            return
        self.store.update(task_id, stage='reporting')
        report('report')
        business = await wait_with_cancel(asyncio.to_thread(
            self._publish_reporting, task_id, payload, frozen, budget, outcome, assessment,
            verification, token=token), token, keep_running=True)
        if token.cancelled:
            self._finish(task_id, status='cancelled')
            return
        self._finish(task_id, status='completed', business_status=business)
        self.store.update(task_id, stage='ready')
