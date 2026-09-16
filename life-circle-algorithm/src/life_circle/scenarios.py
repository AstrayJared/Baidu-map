"""Independent synthetic truth. No sampler gets a reference grid or another run's cache."""
import heapq
from dataclasses import dataclass

import numpy as np


@dataclass
class Scenario:
    name: str
    description: str
    truth: object
    failure: object = None

    def observed(self, x, y):
        if self.failure is not None and self.failure(x, y):
            return None
        return self.truth(x, y)


class TestRoadNetwork:
    """Test-only 50 m orthogonal road graph with one permitted barrier crossing.

    Destinations use nearest-node travel plus the Euclidean connector. This is
    a synthetic time function, not a production routing or coordinate service.
    """
    def __init__(self, kind):
        self.step, self.extent = 50, 1600
        self.axis = np.arange(-1600, 1601, 50)
        n = len(self.axis)
        self.distances = np.full((n, n), np.inf)
        middle = n // 2
        self.distances[middle, middle] = 0
        queue = [(0, middle, middle)]

        def allowed(a, b):
            if kind == "river":
                return not ((a[0] <= 100 < b[0] or b[0] <= 100 < a[0]) and a[1] != 600)
            inside = lambda p: 200 <= p[0] <= 900 and -400 <= p[1] <= 400
            return inside(a) == inside(b) or {a, b} == {(900, 0), (950, 0)}

        while queue:
            distance, i, j = heapq.heappop(queue)
            if distance > self.distances[j, i]:
                continue
            a = (self.axis[i], self.axis[j])
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ni, nj = i + di, j + dj
                if not (0 <= ni < n and 0 <= nj < n):
                    continue
                b = (self.axis[ni], self.axis[nj])
                if allowed(a, b) and distance + 50 < self.distances[nj, ni]:
                    self.distances[nj, ni] = distance + 50
                    heapq.heappush(queue, (distance + 50, ni, nj))

    def __call__(self, x, y):
        x, y = np.asarray(x), np.asarray(y)
        i = np.clip(np.rint((x + 1600) / 50).astype(int), 0, len(self.axis) - 1)
        j = np.clip(np.rint((y + 1600) / 50).astype(int), 0, len(self.axis) - 1)
        return (self.distances[j, i] + np.hypot(x - self.axis[i], y - self.axis[j])) / 1.2


def scenarios():
    plane = lambda x, y: np.hypot(x, y) / 1.2
    cases = {
        "plane": Scenario("plane", "匀速平面，解析真值半径 1080 米", plane),
        "anisotropic": Scenario("anisotropic", "方向不对称椭圆时间场", lambda x, y: np.hypot(x / 1.4, y / .8)),
        "river_bridge": Scenario("river_bridge", "50 米测试路网，河流仅允许在 y=600 米处过桥", TestRoadNetwork("river")),
        "wall_entrance": Scenario("wall_entrance", "50 米测试路网，围墙仅东侧一个入口", TestRoadNetwork("wall")),
        "hole": Scenario("hole", "圆形可达区中保留偏离起点的孔洞", lambda x, y: np.maximum(plane(x, y), 900 + 230 - np.hypot(x - 500, y))),
        "components": Scenario("components", "主分量与独立小分量的专用标量场", lambda x, y: np.maximum(0, 900 + np.minimum(np.hypot(x, y) - 600, np.hypot(x - 1100, y) - 180))),
        "jump": Scenario("jump", "x=500 米处耗时突变", lambda x, y: plane(x, y) + np.where(np.asarray(x) > 500, 700, 0)),
        "local_failure": Scenario("local_failure", "局部未知，参考真值仍为匀速平面", plane, lambda x, y: 300 < x < 750 and -350 < y < 350),
        "global_failure": Scenario("global_failure", "所有外部采样失败，参考真值仍为匀速平面", plane, lambda x, y: True),
    }
    for width in (20, 50, 100):
        for offset in (0, 37):
            name = f"channel_{width}_{offset}"

            def channel(x, y, width=width, offset=offset):
                # Signed-field union: main disk, thin rectangular corridor, end disk.
                corridor = np.maximum.reduce(np.broadcast_arrays(400 - np.asarray(x), np.asarray(x) - 1350, np.abs(np.asarray(y) - offset) - width / 2))
                distance = np.minimum(np.minimum(np.hypot(x, y) - 550, corridor), np.hypot(np.asarray(x) - 1350, np.asarray(y) - offset) - 150)
                return np.maximum(0, 900 + distance)
            cases[name] = Scenario(name, f"{width} 米窄通道，偏移 {offset} 米；用于暴露漏采限制", channel)
    return cases


def accuracy_scenario(definition):
    """Frozen P1 models; truth and observation failures have separate functions.

    Layout parameters are generated before any run and stored in the protocol.
    Rotation is about the fixed zero anchor. Road models reuse TestRoadNetwork.
    Signed-field pockets/channels are synthetic scalar models, not road claims.
    """
    family, variant = definition['family'], definition['variant']
    angle = definition['angle']
    cosine, sine = np.cos(angle), np.sin(angle)
    def local(x,y):
        return cosine*np.asarray(x)+sine*np.asarray(y), -sine*np.asarray(x)+cosine*np.asarray(y)
    speed = definition['speed']
    failure = None
    network = TestRoadNetwork('river' if variant % 2 == 0 else 'wall') if family == 2 else None
    def truth(x,y):
        u,v = local(x,y)
        radius = np.hypot(u,v)
        if family == 0:
            return (radius if variant % 2 == 0 else np.abs(u)+np.abs(v)) / speed
        if family == 1:
            theta = np.arctan2(v,u)
            boundary = 960 + 140*np.sin(3*theta) + 70*np.sin(7*theta)
            near = radius/boundary*900
            far = 2050 + 550*np.sin(u/135)*np.cos(v/175)
            return np.where(radius > 1280, far, near)
        if family == 2:
            return network(u,v)
        if family == 3:
            offset = definition['offset']
            island = np.hypot(u-1100,v-offset)-60
            main = radius-700
            distance = np.minimum(main,island)
            if variant % 2:
                corridor = np.maximum.reduce(np.broadcast_arrays(600-u,u-1100,np.abs(v-offset)-25))
                distance = np.minimum(distance,corridor)
            return np.maximum(0,900+3*distance)
        return radius/speed
    if family == 4:
        def failure(x,y):
            u,v = local(x,y)
            return bool(300 < u < 750 and abs(v-definition['offset']) < 150)
    case = Scenario(definition['id'], f'P1 family {family}, frozen layout {variant}', truth, failure)
    if family == 4:
        u,v = 500,definition['offset']
        case.failure_center = (cosine*u-sine*v,sine*u+cosine*v)
    return case
