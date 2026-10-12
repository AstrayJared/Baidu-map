"""§6.3 百度重点核验：两层路线证据，各自回答自己能回答的问题。

标准 120 次路线预算分成两层：

* **中心可达性（最多 40 次）**：从体检中心到设施的路线。它只回答"中心这一点能不能在
  规则内走到这家设施"，不回答任何一格有没有服务。
* **覆盖抽检（最多 80 次）**：从抽样格到模型认为最近的候选设施的路线，与模型结论比较。
  抽样兼顾边界疑点（缺口格、接近阈值的覆盖格、误差带里的未知格）与空间分散的覆盖格。
  模型说覆盖、路线说走不到时先问一家替代设施；冲突与"实测补定"交给管理器做一次
  局部重算——只在实测点所在的 25 米格定论，不整片改面积。

五条口径决定了它长成这样：

* **直线只用来排队。** 候选排序和保守筛选可以用直线距离，但任何"在服务范围内"的结论
  都必须由返回的路线距离支撑。
* **端点容差层是主用结果。** 百度会把起终点吸附到路上，严格映射（端点完全重合）在结构上
  几乎不可能成立。起终点偏移都不超过 50 米、入口没有接入冲突时，按"路线距离＋两端偏移"
  估计接入距离并据此判定；这是估计，不是数学上的保守上界。严格层照旧保留为附加标志。
* **单点不覆盖整格。** 一条路线只说明这条路线的两端，只替它所在的 25 米格说话。
* **失败不等于盲区。** 一家设施走不通，说明的是"这一家不行"，不是"这里没有服务"：
  它最多把一格降为未知，永远不会把一格判成缺口。
* **中心路线不核验覆盖图。** 报告按实际验证对象措辞，两层分开写。
"""
import hashlib
import math
from dataclasses import dataclass, field as dataclass_field

from life_circle.coordinates import LocalProjection, normalize

from .. import service_rules
from ..catalog import major_of
from ..contracts import Issue
from ..poi_evidence import poi_evidence
from .models import DISTANCE_RULE, VerificationEvidence

#: 保守筛选：直线这么远的设施，步行不可能落在 1000 米规则之内（见 ``service_rules``）。
CANDIDATE_STRAIGHT_LINE_M = service_rules.CANDIDATE_STRAIGHT_LINE_M
#: 路线预算的两层划分（含重试）：中心可达性 40 次，覆盖抽检 80 次。
CENTER_ATTEMPTS = 40
SPOT_ATTEMPTS = 80
#: 模型距离达到这个值的覆盖格算"接近阈值"的边界疑点。
SPOT_SUSPECT_FROM_M = service_rules.THRESHOLD_M - 200.0
#: 抽检里边界疑点与空间分散样本的交替比例。
SPOT_SUSPECTS_PER_SPREAD = 2

NO_TRANSPORT = 'route_transport_unavailable'
NO_FACILITIES = 'facility_stage_unavailable'


def usable_route(observation, requested) -> bool:
    """这条观测能不能参与判定：端点核实过、有结果，且不是"零距离的别处"。

    口径与设施阶段（``facilities.py``）逐字一致：一次零距离路线只有在它确实就是被问的
    那一点时才算数 —— 否则那是上游把"没走"当成了结论。
    """
    if observation is None or not observation.endpoint_verified or observation.duration is None:
        return False
    return not (observation.duration == 0
                and requested != (observation.destination[0], observation.destination[1]))


def within_rule(distance) -> bool | None:
    """这条路线距离是否落在规则之内；不能判定时是 None，而不是 False。

    判定误差带（1000 ± 100 米）里的距离既不支持"已覆盖"也不支持"缺口"：把它算成
    缺口会让误差带变成一条假的服务边界，算成覆盖则相反。详情证据用的是同一个函数，
    所以点击一条设施不会得到与核验阶段不同的判定口径。
    """
    if distance is None or not math.isfinite(distance) or distance < 0:
        return None
    if abs(distance - DISTANCE_RULE.threshold_m) <= DISTANCE_RULE.tolerance_m \
            and distance != DISTANCE_RULE.threshold_m:
        return None
    return distance <= DISTANCE_RULE.threshold_m


def _offsets_within(observation) -> bool:
    offsets = (observation.origin_offset_m, observation.destination_offset_m)
    return all(value is not None and math.isfinite(value) and 0 <= value <= service_rules.ENDPOINT_TOLERANCE_M
               for value in offsets)


def judge_route(observation, origin, destination, facility_id, *, entrance_status=None) -> dict:
    """一条路线观测 → 证据层级、接入距离估计与规则判定。

    * ``strict``：现有 :func:`poi_evidence` 的严格映射成立（端点几乎完全重合），估计就是
      返回的路线距离。
    * ``endpoint_tolerance``：路线可用、起终点偏移都不超过 ``ENDPOINT_TOLERANCE_M``，且入口
      没有已知的接入冲突（模型接入不了的入口不确认）。估计为"路线距离＋两端偏移"——
      直线偏移不证明有同样长的可走接入路径，所以这是估计，不是保守上界。
    * 其余为 ``None``：只留下返回的路线距离，不给判定。
    """
    strict = poi_evidence(observation, origin, destination, facility_id)
    usable = usable_route(observation, destination) and observation.distance_m is not None
    returned = observation.distance_m if usable else None
    layer, estimate = None, None
    if usable and strict.status != 'pending':
        layer, estimate = 'strict', float(observation.distance_m)
    elif usable and _offsets_within(observation) and entrance_status != 'unresolved':
        layer = 'endpoint_tolerance'
        estimate = float(observation.distance_m + observation.origin_offset_m
                         + observation.destination_offset_m)
    return dict(strict=strict, layer=layer, returned=returned, estimate=estimate,
                within=within_rule(estimate))


@dataclass
class VerificationOutcome:
    """核验阶段的产出：证据，以及不能核验时带名字的原因。

    ``conflicts`` 就写在证据里（它要被冻结进修订），这里不再另存一份 —— 同一个结论有两
    个来源时，早晚会出现一边改了另一边没改。``overrides`` 是覆盖抽检里要交给局部重算的
    实测点（冲突与实测补定），同样也冻结在证据里。
    """
    evidence: VerificationEvidence | None
    status: str
    issues: list = dataclass_field(default_factory=list)
    network_requests: int = 0
    overrides: list = dataclass_field(default_factory=list)


def refusal(reason: str, *, message: str | None = None) -> VerificationOutcome:
    """没有可用的路线传输：带着名字拒绝，而不是报一份空核验。"""
    text = message or {
        NO_TRANSPORT: '本次体检没有可用的步行路线服务，灰区只有模型证据。',
        NO_FACILITIES: '设施阶段没有建立结果，核验阶段没有可核验的对象。',
        'missing_ak': '未配置百度地图 AK，本次体检没有可用的步行路线服务，灰区只有模型证据。',
        'synthetic_mode_offline': '当前为离线合成模式，未接入真实路线服务，灰区只有模型证据。',
        'task_budget_exhausted': '本任务的路线预算已用尽，核验阶段未开始。',
        # 上游给上来的原因照原样报告，而不是当成一条缺失的说明。
    }.get(reason, f'核验阶段未运行：{reason}')
    return VerificationOutcome(
        evidence=VerificationEvidence(status='not_integrated', reason=reason, notes=[text]),
        status='not_integrated',
        issues=[Issue(code='VERIFICATION_UNAVAILABLE', message=text, scope='verification')])


def candidate_order(facilities, *, majors, zones, entrances, origin, projection=None):
    """要问哪几家设施，按什么顺序问（§6.3）。

    排序依据全部来自已经冻结的证据，不引入新的判断。档位决定**类别内**谁先被问：

    * **疑似灰区**：某一处灰区自己报出来的"最近已知设施"是第一档 —— 它就是模型说
      "太远"的那个旁边最近的设施。此处仅提升核验优先级，中心到设施路线不能直接检验灰区距离。
    * **未知门禁**：入口解析不出合法接入的设施排在第二档：它的距离结论本来就带着未决项。
    * **决定边缘**：其余按 |直线距离 − 规则阈值| 升序 —— 最接近 1000 米的那些设施，一次
      路线最可能把结论从"覆盖"翻成"缺口"或反过来。同档内按设施 ID 稳定排序。

    类别之间轮转交错（§6.3 要求的"按类别轮转"）：两类设施数量差十倍时，预算不该按数量
    分配。所以一个类别里的"决定边缘"可能排在另一个类别里的"疑似灰区"之前 —— 档位管的是
    同一类别内部的次序，轮转管的是类别之间。
    """
    projection = LocalProjection(origin) if projection is None else projection
    ranked: dict[str, list] = {major: [] for major in majors}
    suspected = set()
    for zone in zones or ():
        nearest = zone.get('nearestFacility')
        if nearest:
            suspected.add(nearest)
    for item in facilities or ():
        major = major_of(item.get('category'))
        if major not in ranked:
            continue
        location = item.get('location')
        if not isinstance(location, dict):
            continue
        x, y = projection.to_local((location['lng'], location['lat']))
        straight = (x * x + y * y) ** .5
        if straight > CANDIDATE_STRAIGHT_LINE_M:
            continue
        entrance_status, entrance_offset = _entrance(entrances, item.get('id'))
        unresolved = entrance_status == 'unresolved'
        rank = 0 if str(item.get('id')) in suspected else 1 if unresolved else 2
        ranked[major].append({
            'facilityId': str(item.get('id')), 'category': item.get('category'), 'major': major,
            'location': (float(location['lng']), float(location['lat'])),
            'straightLineM': straight, 'priority': ('suspected_zone', 'unresolved_entrance',
                                                    'decision_edge')[rank],
            'entranceStatus': entrance_status, 'entranceOffsetM': entrance_offset,
            'rank': (rank, abs(straight - DISTANCE_RULE.threshold_m), str(item.get('id'))),
        })
    for items in ranked.values():
        items.sort(key=lambda item: item['rank'])
    ordered, queues = [], [ranked[major] for major in majors]
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
    return ordered


async def verify_facilities(*, facilities, majors, zones, heatmap, entrances, session, origin,
                            projection=None, progress=None, token=None, previous=None) -> VerificationOutcome:
    """跑完核验阶段。``session`` 为 None 表示这个部署没有路线服务。

    ``session`` 一次调用就是一个候选的全部尝试（含重试），每条尝试各扣一次 ``route``
    桶；这里不自己数次数，只读它报回来的尝试量，避免两处计数对不上。

    ``progress(done, candidates, attempts)`` 在第一家之前和每问完一家之后各报一次：
    已问过几家、候选共几家、路线尝试累计几次。
    """
    if session is None:
        return refusal(NO_TRANSPORT)
    if facilities is None:
        return refusal(NO_FACILITIES)
    projection = LocalProjection(origin) if projection is None else projection
    candidates = candidate_order(facilities, majors=majors, zones=zones, entrances=entrances,
                                 origin=origin, projection=projection)
    plan = spot_check_plan(heatmap, facilities, majors=majors, projection=projection)
    old_records = [] if previous is None else list(previous.facilities)
    old_spots = [] if previous is None else list(previous.spot_checks)
    prior_queries = {} if previous is None else previous.queries
    candidate_count = len(candidates)
    candidate_ids = {row['facilityId'] for row in old_records}
    candidates = [row for row in candidates if row['facilityId'] not in candidate_ids]
    spot_keys = {(row['major'], row['cell'], row['facilityId']) for row in old_spots}
    plan = [entry for entry in plan if (entry['major'], entry['point']['cell'],
                                       str(entry['primary']['id'])) not in spot_keys]
    if not candidates and not plan and not old_records and not old_spots:
        return VerificationOutcome(
            evidence=VerificationEvidence(
                status='partial', provider=session.identity, checked=0,
                queries={'routeAttempts': session.attempts},
                notes=['本次检索没有落在直线 1200 米内的设施，也没有可抽检的格，核验阶段没有可核验的对象。']),
            status='partial')
    cancelled = (lambda: token is not None and token.cancelled)
    total = len(candidates) + len(plan)
    records, failures, unresolved = [], 0, 0
    if progress is not None:
        progress(0, total, session.attempts)
    # 第一层：中心可达性，最多 CENTER_ATTEMPTS 次尝试。
    for asked, item in enumerate(candidates, start=1):
        if cancelled() or session.attempts >= max(0, CENTER_ATTEMPTS - prior_queries.get('centerAttempts', 0)):
            # 取消后或本层额度用完不再问下一家：已经问到的留下，其余记为未核验。
            break
        if not getattr(session, 'available', lambda *_: True)(item['facilityId'], origin, item['location']):
            continue
        observation = await session(item['facilityId'], origin, item['location'])
        if progress is not None:
            progress(asked, total, session.attempts)
        if observation is None:
            # 池子用尽或撞上停止类原因：剩下的候选没有结论，不是"走不通"。
            break
        record = _record(item, observation, origin)
        records.append(record)
        if record['verificationLayer'] is None:
            failures += 1
        if record['entranceStatus'] == 'unresolved':
            unresolved += 1
    center_attempts = session.attempts
    # 第二层：覆盖抽检，最多 SPOT_ATTEMPTS 次尝试（本层用不完的额度不回流）。
    spots, overrides = await _spot_checks(plan, session=session, cancelled=cancelled,
                                          limit=center_attempts + max(0, SPOT_ATTEMPTS - prior_queries.get('spotAttempts', 0)),
                                          progress=None if progress is None else (
                                              lambda done: progress(len(records) + done, total,
                                                                    session.attempts)))
    records = old_records + records
    spots = old_spots + spots
    overrides = ([] if previous is None else list(previous.local_overrides)) + overrides
    failures = sum(row['verificationLayer'] is None for row in records)
    unresolved = sum(row['entranceStatus'] == 'unresolved' for row in records)
    flags = [{'cell': spot['cell'], 'category': spot['major'], 'modelStatus': spot['modelStatus'],
              'facilityId': spot['facilityId'], 'routeDistanceM': spot['routeDistanceM'],
              'accessDistanceM': spot['accessDistanceM'], 'outcome': spot['outcome']}
             for spot in spots if spot['outcome'] in CONFLICT_OUTCOMES]
    checked = len(records)
    unverified = max(0, candidate_count - checked)
    stopped = session.stop_reason or ('cancelled' if cancelled() else None)
    summary = _spot_summary(plan + old_spots, spots, attempts=session.attempts - center_attempts
                            + prior_queries.get('spotAttempts', 0))
    notes = ['核验只对本次检索到的设施成立，不构成目录完整性证明。',
             '中心可达性：从体检中心到设施的步行路线，只说明中心这一点能否在规则内到达该设施，'
             '不核验任何一格的服务覆盖。',
             '覆盖抽检：从抽样格到模型认为最近的候选设施的步行路线，与模型结论比较；单条路线只对'
             '它所在的 25 米格定论，一家设施走不通不判缺口。',
             '端点容差层：路线起终点与请求点相差都不超过 50 米时，按"路线距离＋两端偏移"估计接入'
             '距离并判定（估计，不是保守上界）；严格层要求端点完全重合，作为附加标志保留。',
             '直线距离仅用于候选排序与保守筛选，所有"在服务范围内"的结论都来自返回路线距离。']
    if unverified:
        notes.append(f'{unverified} 处中心可达性候选未取到路线'
                     f'（{stopped or "layer_budget_exhausted"}）：未核验不等于走不通。')
    if unresolved:
        notes.append(_unresolved_note(unresolved))
    if flags:
        notes.append(f'覆盖抽检有 {len(flags)} 处与模型结论不一致：已交给局部重算，'
                     f'只在实测点所在的 25 米格定论，不整片改面积。')
    # 只有"中心层每一家都问过、都拿到可用结论，抽检跑过且没有冲突"才算 complete。抽检本来
    # 就是抽样：没抽到的格不是"没核验完"，所以不拿抽到几格去比可抽的格数。
    status = ('complete' if not unverified and not failures and not flags
              and (summary['checked'] > 0 or not plan) else 'partial')
    if previous is not None:
        notes.append('复用仍匹配设施位置与入口的既有路线；新增设施未继承核验结论，核验总预算不重置。')
    queries = {'routeAttempts': session.attempts + prior_queries.get('routeAttempts', 0), 'candidates': candidate_count,
               'checked': checked, 'unverified': unverified, 'stopReason': stopped,
               'centerAttempts': center_attempts + prior_queries.get('centerAttempts', 0),
               'spotAttempts': session.attempts - center_attempts + prior_queries.get('spotAttempts', 0)}
    return VerificationOutcome(
        evidence=VerificationEvidence(
            status=status, provider=session.identity, checked=checked,
            failed=failures, unresolved=unresolved, facilities=records, conflicts=flags,
            **evidence_counts(records),
            spot_checks=spots, spot_check_summary=summary, local_overrides=overrides,
            queries=queries, notes=notes),
        status=status, network_requests=session.attempts, overrides=overrides)


def _unresolved_note(count: int) -> str:
    return f'{count} 处设施的入口在模型里接入不了，路线证据也无法代替入口证据。'


def evidence_counts(records: list[dict]) -> dict:
    """Four independent counters; route returns overlap with evidence layers."""
    return {
        'route_returns': sum(item.get('routeDistanceM') is not None for item in records),
        'strict_confirmed': sum(item.get('verificationLayer') == 'strict' for item in records),
        'tolerance_estimated': sum(item.get('verificationLayer') == 'endpoint_tolerance' for item in records),
        'no_usable_decision': sum(item.get('verificationLayer') not in ('strict', 'endpoint_tolerance')
                                  for item in records),
    }


def _entrance(entrances, facility_id):
    entrance = (entrances or {}).get(str(facility_id))
    return (None if entrance is None else getattr(entrance, 'status', None),
            None if entrance is None or entrance.attachment is None else entrance.attachment.distance_m)


def carried_over(evidence: VerificationEvidence, *, entrances, revision: int) -> VerificationOutcome:
    """已发布的路线证据带进离线重算的一版：路线不再请求，入口那一层按新评估重读。

    中心到设施的路线是事实，和水系数据无关，照原样留下；入口能不能接入是模型的结论，
    数据修订会改变它，所以只有这一层按新的入口重新读。候选顺序是原来那一版的灰区
    排出来的，照原样留着，并在说明里写明。
    """
    records = []
    for record in evidence.facilities:
        status, offset = _entrance(entrances, record['facilityId'])
        records.append({**record, 'entranceStatus': status, 'entranceOffsetM': offset})
    unresolved = sum(1 for record in records if record['entranceStatus'] == 'unresolved')
    notes = [note for note in evidence.notes if note != _unresolved_note(evidence.unresolved)]
    if unresolved:
        notes.append(_unresolved_note(unresolved))
    notes.append(f'本版路线沿用第 {revision} 版已取得的结果，未重新请求；'
                 '候选当时按那一版的灰区排序。')
    return VerificationOutcome(
        evidence=evidence.model_copy(update={'facilities': records, 'unresolved': unresolved,
                                             'notes': notes}),
        status=evidence.status)


def compatible_verification(previous, facilities, entrances):
    """Do not transplant a route onto a moved/reclassified facility or changed entrance.

    Negative spot conclusions also depend on the other candidates of that major.
    Retain them only when that candidate set remains unchanged.
    """
    if previous is None or previous.verification is None or previous.facilities is None:
        return None
    old = {f['id']: f for f in previous.facilities.facilities + previous.facilities.nearby_facilities}
    new = {f['id']: f for f in facilities or []}
    def identity(f):
        return tuple(repr(f.get(k)) for k in ('category', 'location', 'navigationLocation', 'confirmedEntrances'))
    stable = {uid for uid in old.keys() & new.keys() if identity(old[uid]) == identity(new[uid])}
    records = []
    for row in previous.verification.facilities:
        uid = row['facilityId']
        status, offset = _entrance(entrances, uid)
        if uid in stable and row.get('entranceStatus') == status and row.get('entranceOffsetM') == offset:
            records.append(row)
    changed_majors = {major_of(f.get('category')) for pool in (old, new)
                      for uid, f in pool.items() if uid not in stable}
    spots = [row for row in previous.verification.spot_checks
             if row['facilityId'] in stable and row['major'] not in changed_majors]
    keys = {(row['major'], row['cell']) for row in spots}
    overrides = [row for row in previous.verification.local_overrides if (row['major'], row['cell']) in keys]
    return previous.verification.model_copy(update={'facilities': records, 'spot_checks': spots,
                                                    'local_overrides': overrides})


def _record(item, observation, origin) -> dict:
    """一条中心可达性路线观测 → 一份带层级的证据。

    ``poiEvidence`` 是严格那一层：端点不重合就是 ``pending``。判定来自 :func:`judge_route`：
    严格层成立时用路线距离，端点容差层用"路线距离＋两端偏移"的接入距离估计。入口在模型里
    接入不了的设施不在容差层确认。
    """
    destination = item['location']
    judged = judge_route(observation, origin, destination, item['facilityId'],
                         entrance_status=item['entranceStatus'])
    strict, returned = judged['strict'], judged['returned']
    return {
        'facilityId': item['facilityId'], 'category': item['category'],
        # 设施自己的位置：图层画的是它，不是被吸附过的路线端点。
        'location': {'lng': destination[0], 'lat': destination[1]},
        'straightLineM': round(item['straightLineM'], 3), 'priority': item['priority'],
        'routeDistanceM': None if returned is None else round(returned, 3),
        'accessDistanceM': None if judged['estimate'] is None else round(judged['estimate'], 3),
        'durationS': observation.observed_duration,
        'withinRule': judged['within'],
        'verificationLayer': judged['layer'],
        # 严格那一层要端点和原目标几乎完全重合；道路端点层留着实际起终点和偏移。
        'poiStatus': strict.status, 'poiReason': strict.reason,
        'endpointVerified': strict.endpoint_verified,
        'routeOrigin': observation.route_origin, 'routeDestination': observation.route_destination,
        'originOffsetM': observation.origin_offset_m,
        'destinationOffsetM': observation.destination_offset_m,
        'evidenceGrade': 'verified' if judged['layer'] is not None else 'model',
        # 中心路线不和任何一格比较：那是覆盖抽检的事。
        'modelCell': None, 'modelStatus': None, 'conflict': False,
        'modelComparison': 'not_comparable_origin_and_destination',
        'entranceStatus': item['entranceStatus'], 'entranceOffsetM': item['entranceOffsetM'],
        'reason': observation.reason,
    }


# -- 覆盖抽检 ------------------------------------------------------------------

#: 与模型结论冲突、要交给局部重算的抽检结果。
CONFLICT_OUTCOMES = ('gap_route_within', 'covered_not_confirmed')


def _stable_order(cell: str) -> str:
    """格号的稳定伪随机次序：抽样可复现，又不会按行列扎堆在一角。"""
    return hashlib.sha256(cell.encode()).hexdigest()


def spot_check_plan(heatmap, facilities, *, majors, projection) -> list[dict]:
    """要抽检哪些格、各问哪家设施（§6.3 覆盖抽检）。

    每类先排边界疑点（缺口格、模型距离接近阈值的覆盖格、误差带里的未知格），再排空间
    分散的覆盖格，两者按 ``SPOT_SUSPECTS_PER_SPREAD``:1 交替；类别之间轮转。每格问的
    是模型认为最近的那家设施（没有时取直线最近的同类设施），并备好一家替代设施。直线
    超过候选范围的格不问：那里走不到任何同类设施是显然的，不需要花一次路线。
    """
    categories = (heatmap or {}).get('categories') or {}
    located = {}
    for item in facilities or ():
        location = item.get('location')
        if not isinstance(location, dict):
            continue
        located[str(item.get('id'))] = (item, projection.to_local((location['lng'], location['lat'])))
    queues = []
    for major in majors:
        pool = [(key, item, xy) for key, (item, xy) in located.items()
                if major_of(item.get('category')) == major]
        suspects, spread = [], []
        for point in categories.get(major) or ():
            if not all(isinstance(point.get(key), (int, float)) for key in ('lng', 'lat')):
                continue  # 没有位置的点问不了路线
            status, distance, cell = point.get('status'), point.get('distanceM'), str(point.get('cell'))
            if status == 'gap':
                suspects.append(((0, _stable_order(cell)), point))
            elif status == 'covered' and distance is not None and distance >= SPOT_SUSPECT_FROM_M:
                suspects.append(((1, -distance, cell), point))
            elif status == 'unknown' and point.get('reason') == 'distance_in_tolerance_band':
                suspects.append(((1, abs((distance or 0) - DISTANCE_RULE.threshold_m), cell), point))
            elif status == 'covered':
                spread.append(((2, _stable_order(cell)), point))
        ordered, suspects, spread = [], sorted(suspects, key=lambda s: s[0]), sorted(spread, key=lambda s: s[0])
        while suspects or spread:
            ordered.extend(point for _, point in suspects[:SPOT_SUSPECTS_PER_SPREAD])
            suspects = suspects[SPOT_SUSPECTS_PER_SPREAD:]
            ordered.extend(point for _, point in spread[:1])
            spread = spread[1:]
        queue = []
        for point in ordered:
            xy = projection.to_local((point['lng'], point['lat']))
            near = sorted((math.dist(xy, fxy), key, item) for key, item, fxy in pool
                          if math.dist(xy, fxy) <= CANDIDATE_STRAIGHT_LINE_M)
            if not near:
                continue
            primary = located.get(str(point.get('nearestFacility')), (None,))[0]
            if primary is None or major_of(primary.get('category')) != major:
                primary = near[0][2]
            alternative = next((item for _, key, item in near if key != str(primary.get('id'))), None)
            queue.append(dict(major=major, point=point, primary=primary, alternative=alternative))
        queues.append(queue)
    plan = []
    while any(queues):
        for queue in queues:
            if queue:
                plan.append(queue.pop(0))
    return plan


def _spot_outcome(model_status: str, judged: dict) -> str:
    within = judged['within']
    if judged['estimate'] is None:
        return 'inconclusive_unusable_route'
    if within is None:
        return 'inconclusive_tolerance_band'
    if model_status == 'covered':
        return 'agree' if within else 'needs_alternative'
    if model_status == 'gap':
        # 一家在规则内走得到就推翻"缺口"；一家走不到只说明这一家。
        return 'gap_route_within' if within else 'agree'
    return 'resolved_covered' if within else 'beyond_by_route'


async def _spot_checks(plan, *, session, cancelled, limit, progress=None):
    spots, overrides = [], []
    for done, entry in enumerate(plan, start=1):
        if cancelled() or session.attempts >= limit or session.stop_reason:
            break
        point, major = entry['point'], entry['major']
        origin = normalize((point['lng'], point['lat']))
        primary = entry['primary']
        destination = normalize((primary['location']['lng'], primary['location']['lat']))
        if not getattr(session, 'available', lambda *_: True)(str(primary['id']), origin, destination):
            continue
        observation = await session(str(primary['id']), origin, destination)
        if observation is None:
            break
        judged = judge_route(observation, origin, destination, str(primary['id']))
        outcome, alternative = _spot_outcome(point.get('status'), judged), None
        if outcome == 'needs_alternative':
            # 模型最近的那家走不到，不等于这一格没有服务：再问一家替代设施。
            other = entry['alternative']
            alt_judged = None
            if (other is not None and session.attempts < limit and not cancelled()
                    and getattr(session, 'available', lambda *_: True)(str(other['id']), origin,
                        normalize((other['location']['lng'], other['location']['lat'])))):
                alt_destination = normalize((other['location']['lng'], other['location']['lat']))
                alt_observation = await session(str(other['id']), origin, alt_destination)
                if alt_observation is not None:
                    alt_judged = judge_route(alt_observation, origin, alt_destination, str(other['id']))
                    alternative = {'facilityId': str(other['id']),
                                   'routeDistanceM': alt_judged['returned'],
                                   'accessDistanceM': alt_judged['estimate'],
                                   'withinRule': alt_judged['within'],
                                   'verificationLayer': alt_judged['layer']}
            outcome = ('agree_alternative' if alt_judged is not None and alt_judged['within']
                       else 'covered_not_confirmed')
        spot = {'cell': point.get('cell'), 'major': major, 'lng': point['lng'], 'lat': point['lat'],
                'modelStatus': point.get('status'), 'modelDistanceM': point.get('distanceM'),
                'facilityId': str(primary['id']),
                'routeDistanceM': None if judged['returned'] is None else round(judged['returned'], 3),
                'accessDistanceM': None if judged['estimate'] is None else round(judged['estimate'], 3),
                'withinRule': judged['within'], 'verificationLayer': judged['layer'],
                'originOffsetM': observation.origin_offset_m,
                'destinationOffsetM': observation.destination_offset_m,
                'outcome': outcome, 'alternative': alternative, 'reason': observation.reason}
        spots.append(spot)
        # 交给局部重算的只有两类：推翻模型的冲突，以及把误差带里的未知实测补定为覆盖。
        verdict = {'gap_route_within': 'covered', 'covered_not_confirmed': 'unknown',
                   'resolved_covered': 'covered'}.get(outcome)
        if verdict is not None:
            overrides.append({'major': major, 'cell': point.get('cell'), 'lng': point['lng'],
                              'lat': point['lat'], 'verdict': verdict,
                              'contradicted': {'gap_route_within': 'gap',
                                               'covered_not_confirmed': 'covered'}.get(outcome)})
        if progress is not None:
            progress(done)
    return spots, overrides


def _spot_summary(plan, spots, *, attempts: int) -> dict:
    counts = {'agree': 0, 'conflict': 0, 'resolved': 0, 'inconclusive': 0, 'beyond': 0}
    for spot in spots:
        outcome = spot['outcome']
        key = ('conflict' if outcome in CONFLICT_OUTCOMES else 'resolved' if outcome == 'resolved_covered'
               else 'beyond' if outcome == 'beyond_by_route'
               else 'inconclusive' if outcome.startswith('inconclusive') else 'agree')
        counts[key] += 1
    return {'planned': len(plan), 'checked': len(spots), **counts, 'attempts': attempts}
