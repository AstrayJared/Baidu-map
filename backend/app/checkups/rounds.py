"""Durable round admission, request reservations and POI checkpoints.

Reservations survive a killed process. Responses can be replayed locally if the
process stopped after storing the response but before advancing the checkpoint.
"""
import json
import time


ROUND_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkup_rounds (
 task_id TEXT NOT NULL, number INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE,
 base_revision INTEGER NOT NULL, identity TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0,
 poi_before INTEGER NOT NULL DEFAULT 0, route_before INTEGER NOT NULL DEFAULT 0,
 network_before INTEGER NOT NULL DEFAULT 0, attempts_before INTEGER NOT NULL DEFAULT 0,
 poi_limit INTEGER NOT NULL DEFAULT 60,
 PRIMARY KEY(task_id, number)
);
CREATE TABLE IF NOT EXISTS checkup_checkpoints (
 task_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkup_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, round_number INTEGER NOT NULL,
 pool TEXT NOT NULL, context TEXT NOT NULL, reserved_at REAL NOT NULL,
 completed_at REAL, response TEXT, reason TEXT
);
"""


class RoundConflict(ValueError):
    pass


class RoundStore:
    def round(self, task_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM checkup_rounds WHERE task_id=? ORDER BY number DESC LIMIT 1',
                             (task_id,)).fetchone()
        return dict(row) if row else None

    def round_request(self, request_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM checkup_rounds WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None

    def begin_round(self, task_id, request_id, base_revision, identity, *, initial=False,
                    poi_before=0, route_before=0, legacy=False, poi_limit=60, requeue=True):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM checkup_rounds WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing['task_id'] != task_id or existing['base_revision'] != base_revision:
                    raise RoundConflict('request_id_conflict')
                return False
            task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if task is None:
                raise RoundConflict('task_not_found')
            if not initial and (task['status'] not in ('completed', 'cancelled', 'failed')
                                or task['revision'] != base_revision):
                raise RoundConflict('round_conflict')
            other = db.execute('SELECT task_id FROM tasks WHERE client_request_id=?', (request_id,)).fetchone()
            if other and not initial:
                raise RoundConflict('request_id_conflict')
            previous = db.execute('SELECT number FROM checkup_rounds WHERE task_id=? ORDER BY number DESC LIMIT 1',
                                  (task_id,)).fetchone()
            number = previous['number'] + 1 if previous else (2 if legacy else 1)
            db.execute('INSERT INTO checkup_rounds (task_id,number,request_id,base_revision,identity,legacy,'
                       'poi_before,route_before,network_before,attempts_before,poi_limit) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id, number, request_id, base_revision, identity, int(legacy),
                        poi_before, route_before, task['network_requests'], task['requests'], poi_limit))
            if not initial and requeue:
                db.execute("UPDATE tasks SET status='queued', stage='poi', cancel_requested=0,"
                           " error=NULL, finished_at=NULL, progress=NULL, updated_at=? WHERE task_id=?",
                           (time.time(), task_id))
            db.execute('COMMIT')
        return True

    def checkpoint(self, task_id):
        with self._connection() as db:
            row = db.execute('SELECT state FROM checkup_checkpoints WHERE task_id=?', (task_id,)).fetchone()
        return json.loads(row['state']) if row else None

    def save_checkpoint(self, task_id, state):
        with self._connection() as db:
            db.execute('INSERT INTO checkup_checkpoints VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE'
                       ' SET state=excluded.state, updated_at=excluded.updated_at',
                       (task_id, json.dumps(state, ensure_ascii=False), time.time()))

    def reserve_request(self, task_id, number, pool, context):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('INSERT INTO checkup_requests '
                                '(task_id,round_number,pool,context,reserved_at) VALUES (?,?,?,?,?)',
                                (task_id, number, pool, json.dumps(context, ensure_ascii=False), time.time()))
            request_id = cursor.lastrowid
            if pool in ('poi', 'route'):
                current = db.execute('SELECT * FROM checkup_rounds WHERE task_id=? AND number=?',
                                     (task_id, number)).fetchone()
                count = db.execute("SELECT COUNT(*) FROM checkup_requests WHERE task_id=? AND round_number=?"
                                   " AND pool IN ('poi','route')", (task_id, number)).fetchone()[0]
                db.execute('UPDATE tasks SET requests=MAX(requests,?), network_requests=MAX(network_requests,?),'
                           ' activity_at=?, updated_at=? WHERE task_id=?',
                           (current['attempts_before'] + count, current['network_before'] + count,
                            time.time(), time.time(), task_id))
            db.execute('COMMIT')
            return request_id

    def complete_request(self, request_id, response, reason):
        with self._connection() as db:
            db.execute('UPDATE checkup_requests SET completed_at=?, response=?, reason=? WHERE id=?',
                       (time.time(), None if response is None else json.dumps(response, ensure_ascii=False),
                        reason, request_id))

    def round_spend(self, task_id, number):
        with self._connection() as db:
            rows = db.execute('SELECT pool, COUNT(*) AS count FROM checkup_requests '
                              'WHERE task_id=? AND round_number=? GROUP BY pool', (task_id, number)).fetchall()
        return {row['pool']: row['count'] for row in rows}

    def saved_pages(self, task_id):
        with self._connection() as db:
            rows = db.execute("SELECT context,response,completed_at FROM checkup_requests "
                              "WHERE task_id=? AND pool='poi' AND response IS NOT NULL ORDER BY id",
                              (task_id,)).fetchall()
        return [{**json.loads(row['context']), 'payload': json.loads(row['response']),
                 'obtainedAt': row['completed_at']} for row in rows]

    def saved_routes(self, task_id):
        with self._connection() as db:
            rows = db.execute("SELECT context,response FROM checkup_requests WHERE task_id=? "
                              "AND pool='route' AND response IS NOT NULL AND reason IS NULL ORDER BY id",
                              (task_id,)).fetchall()
        return [(json.loads(row['context']), json.loads(row['response'])) for row in rows]
