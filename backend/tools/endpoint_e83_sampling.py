"""Optional research queue: spatial diversity and reversible stagnation pauses."""
import math


class DiverseEdgeQueue:
    def __init__(self, cell_m=75, recovery_limit=1, *, spread=True, pause=True):
        self.cell_m = cell_m
        self.recovery_limit = recovery_limit
        self.spread = spread
        self.pause = pause
        self.endpoints = set()
        self.rows = set()
        self.failures = {}
        self.recoveries = {}
        self.reactivated = 0
        self.deferred = 0

    def cell(self, xy):
        return tuple(math.floor(v / self.cell_m) for v in xy)

    def observe(self, row_id, requested, actual):
        if row_id in self.rows:
            return
        self.rows.add(row_id)
        key = self.cell(requested)
        actual = tuple(actual)
        if actual in self.endpoints:
            self.failures[key] = self.failures.get(key, 0) + 1
        else:
            self.endpoints.add(actual)
            self.progress(requested)
            self.progress(actual)

    def progress(self, xy):
        key = self.cell(xy)
        self.reactivated += int(self.pause and self.failures.get(key, 0) >= 2)
        self.failures[key] = 0

    def paused(self, xy):
        return self.pause and self.failures.get(self.cell(xy), 0) >= 2

    @staticmethod
    def midpoint(edge):
        return [(a+b)/2 for a, b in zip(edge[1]['xy'], edge[2]['xy'])]

    def update(self, session):
        for row in session.log:
            if row['accepted']:
                self.observe(row['id'], session.projection.to_local(row['destination']),
                             session.projection.to_local(row['route_destination']))

    def select(self, edges, size):
        selected, cells, paused = [], set(), []
        for key in sorted(edges, key=lambda k: (edges[k][0], k), reverse=True):
            xy = self.midpoint(edges[key]); cell = self.cell(xy)
            if self.paused(xy):
                paused.append(key); self.deferred += 1
            elif not self.spread or cell not in cells:
                selected.append(key); cells.add(cell)
                if len(selected) == size:
                    break
        # A region is not permanently banned: once others have no useful work,
        # permit one new edge probe per paused cell, then retain it as unresolved.
        if not selected:
            for key in paused:
                cell = self.cell(self.midpoint(edges[key]))
                if cell not in cells and self.recoveries.get(cell, 0) < self.recovery_limit:
                    selected.append(key); cells.add(cell)
                    self.recoveries[cell] = self.recoveries.get(cell, 0) + 1
                    if len(selected) == size:
                        break
        return selected

    def diagnostics(self):
        return dict(cell_m=self.cell_m, spread=self.spread, pause=self.pause,
            paused_cells=sum(self.pause and v >= 2 for v in self.failures.values()),
            deferred_candidate_visits=self.deferred, recovery_probes=sum(self.recoveries.values()),
            reactivations=self.reactivated, rule='two_repeated_endpoints_without_new_evidence_or_bracket_progress')
