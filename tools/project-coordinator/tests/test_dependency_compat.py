"""Focused upgrade regressions and a subprocess probe for private compatibility copies."""
from contextlib import closing
import copy
import gc
import hashlib
from importlib.metadata import distributions, version
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from project_coordinator.runtime import InvalidEvent, apply_event, build_graph
from test_runtime import Harness, h
from test_control import model, pending, register_event, test_policy

CONFIG = {'configurable': {'thread_id': 'project-coordinator-v1'}}
BASELINE_CHECKPOINT = '1f1ccb41-eeb5-6eae-88a2-7f0e70baf450'


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@pytest.mark.parametrize('legacy_schema', [False, True])
def test_summary_schema_detection_is_readonly(h, monkeypatch, legacy_schema):
    gc.collect()
    if legacy_schema:
        with closing(sqlite3.connect(h.db)) as conn:
            conn.execute('ALTER TABLE writes DROP COLUMN task_path')
            conn.commit()
    before = h.db.read_bytes()
    queries = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        assert args[0].endswith('?mode=ro') and kwargs['uri']
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', traced)
    assert h.call('summary')['bounds']['total_tasks'] == 0
    assert all(q.split()[0].upper() in {'PRAGMA', 'BEGIN', 'SELECT'} for q in queries)
    assert any('table_info(writes)' in q for q in queries)
    assert h.db.read_bytes() == before
    (h.root / 'summary-trace.json').write_text(json.dumps(queries, indent=2))


def read_snapshot(db):
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, check_same_thread=False)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        saver = SqliteSaver(conn)
        saver._has_task_path = any(row[1] == 'task_path' for row in conn.execute('PRAGMA table_info(writes)'))
        saver.is_setup = True
        graph = build_graph(saver)
        snapshot = graph.get_state(CONFIG)
        history = hashlib.sha256()
        retained_history = hashlib.sha256()
        count = retained_count = 0
        for state in graph.get_state_history(CONFIG):
            entry = bytes.fromhex(sha({'values': state.values, 'next': state.next,
                                       'config': state.config, 'parent': state.parent_config}))
            history.update(entry)
            count += 1
            if state.config['configurable']['checkpoint_id'] <= BASELINE_CHECKPOINT:
                retained_history.update(entry)
                retained_count += 1
        # Hash every original column and row, including pending writes; new task_path is additive.
        tables = {}
        for table in ('checkpoints', 'writes'):
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})') if row[1] != 'task_path']
            checksum = hashlib.sha256()
            rows = 0
            for row in conn.execute(f'SELECT {",".join(columns)} FROM {table} WHERE checkpoint_id<=? ORDER BY ' + ','.join(columns[:3]) + ',rowid', (BASELINE_CHECKPOINT,)):
                for value in row:
                    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
                    checksum.update(len(raw).to_bytes(8, 'big'))
                    checksum.update(raw)
                rows += 1
            tables[table] = {'rows': rows, 'sha256': checksum.hexdigest()}
        stored = snapshot.values['model']
        binding = {k: stored['project'][k] for k in ('project_id', 'root')}
        summary = apply_event(db, {**binding, 'op': 'summary'})
        return {'model_sha256': sha(stored), 'components': {k: sha(v) for k, v in stored.items()},
                'tasks': len(stored['tasks']), 'events': len(stored['events']),
                'workers': stored['project']['workers'],
                'held': [{'task': t['id'], 'dispatch': t.get('dispatch_id'), 'state': t['status']}
                         for t in stored['tasks'].values() if t.get('lease')],
                'checkpoint': snapshot.config['configurable']['checkpoint_id'],
                'pending': bool(snapshot.next), 'summary_sha256': sha(summary),
                'history': {'count': count, 'sha256': history.hexdigest()},
                'retained_history': {'count': retained_count, 'sha256': retained_history.hexdigest()}, 'retained_tables': tables,
                'task_path_column': saver._has_task_path}


def probe(operation, path):
    if operation == 'inspect':
        def database_bytes():
            hashes = {}
            for file in (path, Path(str(path) + '-wal')):
                if file.exists() and (file == path or file.stat().st_size):
                    with file.open('rb') as stream:
                        hashes[file.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
            return hashes
        before = database_bytes()
        result = read_snapshot(path)
        assert before == database_bytes()
        return result
    if operation == 'recover-snapshot':
        before = read_snapshot(path)
        with closing(sqlite3.connect(path, check_same_thread=False)) as conn:
            saver = SqliteSaver(conn)
            saver.setup()
            graph = build_graph(saver)
            binding = {k: graph.get_state(CONFIG).values['model']['project'][k] for k in ('project_id', 'root')}
        response = apply_event(path, {**binding, 'op': 'recover'})
        assert 'native_request' not in response and not response['resumed_checkpoint']
        after = read_snapshot(path)
        assert after['model_sha256'] == before['model_sha256']
        assert after['checkpoint'] != before['checkpoint']
        return after
    if operation.startswith('create-pending-'):
        path.mkdir()
        harness = Harness(path)
        if operation.endswith('register'):
            harness.add('held', 'worker-2', paths=['held'])
            dispatch = harness.issue('held')
            harness.call('ack', task_id='held', dispatch_id=dispatch,
                         receipt=harness.file('ack.json', {'threadId': 'thread2'}))
            harness.add('uncertain', 'worker-3', paths=['uncertain'])
            harness.issue('uncertain')
            event = register_event(harness)
            (path / 'project-policy.json').write_text(json.dumps(test_policy(harness)))
        else:
            harness.add(paths=['held'])
            dispatch = harness.prepare()['dispatch_id']
            event = {**harness.binding, 'op': 'issue', 'event_id': 'pending-issue', 'task_id': 'A', 'dispatch_id': dispatch}
        before = model(harness)
        (path / 'protected.json').write_text(json.dumps(before))
        (path / 'pending-input.json').write_text(json.dumps(event))
        pending(harness, event)
        gc.collect()
        assert harness.call('summary')['pending_checkpoint']
        return {'synthetic': True, 'input_sha256': sha(event), 'protected_sha256': sha(before)}
    if operation == 'recover-pending':
        event = json.loads((path / 'pending-input.json').read_text())
        protected = json.loads((path / 'protected.json').read_text())
        db = path / 'state.sqlite'
        policy_path = path / 'project-policy.json'
        policy = json.loads(policy_path.read_text()) if policy_path.exists() else None
        before = db.read_bytes()
        with pytest.raises(InvalidEvent, match='pending checkpoint'):
            apply_event(db, event, project_policy=policy)
        assert before == db.read_bytes()
        response = apply_event(db, {**{k: event[k] for k in ('project_id', 'root')}, 'op': 'recover'})
        assert response['resumed_checkpoint'] and 'native_request' not in response
        def read_model():
            with closing(sqlite3.connect(db, check_same_thread=False)) as conn:
                return build_graph(SqliteSaver(conn)).get_state(CONFIG).values['model']
        after = read_model()
        assert not apply_event(db, {**{k: event[k] for k in ('project_id', 'root')}, 'op': 'summary'})['pending_checkpoint']
        if event['op'] == 'register':
            assert after['project']['workers'].pop('researcher') == event['new_thread_id']
            assert after['workers'].pop('researcher')['state'] == 'UNKNOWN'
            after['events'].pop(event['event_id'])
            assert after == protected
        else:
            expected = copy.deepcopy(protected)
            task = expected['tasks'][event['task_id']]
            task.update(status='UNKNOWN', reason='native send uncertain; reconcile retained receipts')
            expected['workers'][task['worker']] = {'state': 'BUSY', 'evidence': f"dispatch:{event['dispatch_id']}"}
            expected['events'][event['event_id']] = {'sha256': sha(event), 'action': 'ISSUING'}
            assert after == expected
        replay = apply_event(db, event, project_policy=policy)
        assert replay['action'] == 'REPLAY' and 'native_request' not in replay
        if event['op'] == 'issue':
            assert read_model() == expected
        again = apply_event(db, {**{k: event[k] for k in ('project_id', 'root')}, 'op': 'recover'})
        assert not again['resumed_checkpoint'] and 'native_request' not in again
        proof = {}
        if event['op'] == 'issue':
            assert read_model() == expected
            proof = {'complete_expected_model_sha256': sha(expected),
                     'complete_model_checked_after': ['recovery', 'replay', 'second_recovery']}
        return {'synthetic': True, 'pending_op': event['op'], 'protected_state_preserved': True,
                'replay': replay['action'], 'no_native_request': True, **proof}
    raise AssertionError(operation)


if __name__ == '__main__':
    result = probe(sys.argv[1], Path(sys.argv[2]).resolve())
    result['environment'] = {'python': sys.version, 'executable': sys.executable,
                             'packages': {d.metadata['Name'].lower(): d.version for d in distributions()},
                             'langgraph': version('langgraph'), 'sqlite_saver': version('langgraph-checkpoint-sqlite')}
    print(json.dumps(result, sort_keys=True))
