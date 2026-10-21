import copy
import gc
import json
import sqlite3
import sys
import subprocess

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from project_coordinator.runtime import InvalidEvent, apply_event, build_graph, completion_valid
from test_runtime import h


def model(h):
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        return build_graph(SqliteSaver(conn)).get_state({'configurable': {'thread_id': 'project-coordinator-v1'}}).values['model']


def rebind(h, **changes):
    return h.call('rebind', worker='core1', expected_old_thread_id='thread1', new_thread_id='replacement', evidence='verified retirement and replacement identity', **changes)


def test_rebind_preserves_history_and_other_three_leases(h):
    h.add()
    dispatch = h.issue()
    h.result(dispatch)
    # Actual pre-candidate checkpoint shape, including completion receipts.
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {'configurable': {'thread_id': 'project-coordinator-v1'}}
        state = graph.get_state(config).values['model']
        state['tasks']['A'].pop('dispatch_thread_id')
        graph.update_state(config, {'model': state}, as_node='readiness')
    for i in range(2, 5):
        h.add(f'active{i}', f'core{i}')
        h.issue(f'active{i}')
    h.call('observe', worker='core1', state='UNKNOWN', evidence='retired')
    before = model(h)
    assert rebind(h)['action'] == 'REBOUND'
    after = model(h)
    for i in range(2, 5):
        assert after['tasks'][f'active{i}'] == before['tasks'][f'active{i}']
        assert after['workers'][f'core{i}'] == before['workers'][f'core{i}']
    old = copy.deepcopy(before['tasks']['A'])
    old['dispatch_thread_id'] = 'thread1'
    assert after['tasks']['A'] == old
    assert completion_valid(after, after['tasks']['A'])
    assert h.prepare()['action'] == 'REUSE'
    assert 'native_request' not in h.call('issue', task_id='A', dispatch_id=dispatch)
    h.call('observe', worker='core1', state='IDLE', evidence='fresh replacement observation')
    h.add('new')
    new_dispatch = h.prepare('new')['dispatch_id']
    request = h.call('issue', task_id='new', dispatch_id=new_dispatch)['native_request']
    assert request['arguments']['threadId'] == 'replacement'
    wrong = h.file('wrong-ack.json', {'threadId': 'thread1'})
    with pytest.raises(InvalidEvent, match='target'):
        h.call('ack', task_id='new', dispatch_id=new_dispatch, receipt=wrong)


@pytest.mark.parametrize('stage', ['prepared', 'issued', 'acknowledged', 'failed-held'])
def test_rebind_refuses_held_and_uncertain(h, stage):
    h.add()
    if stage == 'prepared':
        h.prepare()
    else:
        dispatch = h.issue()
        if stage == 'acknowledged':
            h.call('ack', task_id='A', dispatch_id=dispatch, receipt=h.file('ack.json', {'threadId': 'thread1'}))
        if stage == 'failed-held':
            h.result(dispatch, acceptance=False, quiescent=False)
    h.call('observe', worker='core1', state='UNKNOWN', evidence='cannot assume idle')
    before = model(h)
    with pytest.raises(InvalidEvent, match='held or uncertain'):
        rebind(h)
    assert model(h) == before


@pytest.mark.parametrize('change', [{'expected_old_thread_id': 'wrong'}, {'new_thread_id': 'root'}, {'new_thread_id': 'thread2'}, {'project_id': 'wrong'}, {'root': '/wrong'}, {'evidence': ''}, {'worker': 'wrong'}])
def test_rebind_exact_identity_refusal(h, change):
    event = {**h.binding, 'op': 'rebind', 'event_id': 'replace', 'worker': 'core1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof', **change}
    before = model(h)
    with pytest.raises(InvalidEvent):
        apply_event(h.db, event)
    assert model(h) == before


def test_released_failed_rebind_replay_and_restart(h):
    h.add()
    dispatch = h.issue()
    h.call('reconcile', task_id='A', dispatch_id=dispatch, quiescent=True, evidence='actual stopped/no pending send')
    h.call('observe', worker='core1', state='UNKNOWN', evidence='retired')
    event = {**h.binding, 'op': 'rebind', 'event_id': 'replace-once', 'worker': 'core1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof'}
    assert apply_event(h.db, event)['action'] == 'REBOUND'
    assert apply_event(h.db, event)['action'] == 'REPLAY'
    assert h.call('recover')['project']['workers']['core1'] == 'replacement'
    assert len(model(h)['rebindings']) == 1
    assert 'native_request' not in h.call('issue', task_id='A', dispatch_id=dispatch)
    with pytest.raises(InvalidEvent, match='already released'):
        h.result(dispatch)


def pending(h, event):
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {'configurable': {'thread_id': 'project-coordinator-v1'}}
        stream = graph.stream({'event': {**h.binding, **event}}, config, stream_mode='checkpoints', durability='sync')
        next(stream)
        stream.close()
    conn.close()


def test_pending_issue_status_readonly_and_rebind_refusal(h):
    h.add()
    dispatch = h.prepare()['dispatch_id']
    pending(h, {'op': 'issue', 'event_id': 'crash', 'task_id': 'A', 'dispatch_id': dispatch})
    gc.collect()  # Close earlier synthetic writer cycles before file-layout comparison.
    before = h.db.read_bytes()
    view = h.call('summary')
    assert view['recovery_required'] and 'native_request' not in view
    assert before == h.db.read_bytes()
    with pytest.raises(InvalidEvent, match='pending checkpoint'):
        rebind(h)
    h.call('recover')
    with pytest.raises(InvalidEvent):
        rebind(h)
    assert 'native_request' not in h.call('issue', task_id='A', dispatch_id=dispatch)


def test_crashed_rebind_recovers_once_without_send(h):
    pending(h, {'op': 'rebind', 'event_id': 'crash-rebind', 'worker': 'core1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof'})
    assert h.call('summary')['project']['workers']['core1'] == 'thread1'
    recovered = h.call('recover')
    assert recovered['project']['workers']['core1'] == 'replacement'
    assert len(model(h)['rebindings']) == 1
    assert 'native_request' not in recovered


def test_status_compact_bounded_and_readonly(h):
    for i in range(7):
        h.call('add', task={'id': f'T{i}', 'worker': 'core1', 'linear_ref': 'linear:399', 'beads_ref': 'beads:399', 'prompt': 'SECRET_PROMPT' * 10000, 'source_digest': 's', 'scope_digest': 's', 'writer_paths': [], 'dependencies': ['missing' + str(n) for n in range(40)]})
    gc.collect()  # Close earlier synthetic writer cycles before file-layout comparison.
    before = h.db.read_bytes()
    response = h.call('summary', limit=3)
    encoded = json.dumps(response)
    assert 'SECRET_PROMPT' not in encoded and 'native_request' not in encoded and 'events' not in response
    assert len(encoded) < 15000
    assert response['bounds']['total_tasks'] == 7 and response['bounds']['next_offset'] == 3
    assert response['bounds']['truncated_fields']
    assert len(response['tasks'][0]['dependencies']) == 32
    assert h.call('summary', offset=6, limit=3)['bounds']['next_offset'] is None
    assert before == h.db.read_bytes()


def test_cli_rebind_and_status(h):
    for command, body in [('rebind', {'event_id': 'cli-rebind', 'worker': 'core1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof'}), ('summary', {})]:
        path = h.root / 'event.json'
        path.write_text(json.dumps({**h.binding, **body}))
        proc = subprocess.run([sys.executable, '-m', 'project_coordinator.cli', '--db', str(h.db), command, '--input', str(path)], text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        assert 'native_request' not in json.loads(proc.stdout)


def test_legacy_new_receipt_pins_identity_before_validation(h):
    h.add()
    dispatch = h.issue()
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {'configurable': {'thread_id': 'project-coordinator-v1'}}
        state = graph.get_state(config).values['model']
        state['tasks']['A'].pop('dispatch_thread_id')
        graph.update_state(config, {'model': state}, as_node='readiness')
    h.call('ack', task_id='A', dispatch_id=dispatch, receipt=h.file('legacy-ack.json', {'threadId': 'thread1'}))
    assert model(h)['tasks']['A']['dispatch_thread_id'] == 'thread1'
    h.result(dispatch)
    h.call('observe', worker='core1', state='UNKNOWN', evidence='retired')
    rebind(h)
    assert h.prepare()['action'] == 'REUSE'


@pytest.mark.parametrize('fields', [{'limit': 0}, {'limit': 101}, {'limit': True}, {'offset': -1}, {'offset': False}])
def test_status_invalid_bounds_no_database_creation(tmp_path, fields):
    db = tmp_path / 'absent' / 'new.sqlite'
    with pytest.raises(InvalidEvent):
        apply_event(db, {'op': 'summary', 'project_id': 'P', 'root': str(tmp_path), **fields})
    assert not db.parent.exists()


def test_existing_full_status_compatibility_and_summary_uncertainty(h):
    h.add()
    dispatch = h.issue()
    full = h.call('status')
    task = full['tasks'][0]
    assert all(field in task for field in ('source_digest', 'scope_digest', 'prompt_sha256', 'writer_claims'))
    assert 'evidence' in full['workers']['core1']
    assert task['status'] == 'UNKNOWN' and task['lease']
    h.result(dispatch)
    complete = h.call('status')['tasks'][0]
    assert 'result_receipt' in complete and 'artifacts' in complete
    summary = h.call('summary')
    assert not summary['pending_checkpoint'] and summary['uncertain_held_dispatches'] == 0
    h.call('observe', worker='core1', state='IDLE', evidence='fresh')
    h.add('uncertain')
    h.issue('uncertain')
    summary = h.call('summary')
    assert not summary['pending_checkpoint'] and summary['uncertain_held_dispatches'] == 1
    assert summary['recovery_required']


def test_summary_giant_ids_lists_and_diagnostics_are_bounded(h):
    giant = 'GIANT' * 5000
    h.call('add', task={'id': giant, 'worker': 'core1', 'linear_ref': 'l', 'beads_ref': 'b', 'prompt': 'HIDDEN_PROMPT', 'source_digest': 's', 'scope_digest': 's', 'dependencies': [giant + str(n) for n in range(35)], 'writer_paths': [('segment' * 14 + '/') * 8 + str(n) for n in range(35)]})
    response = h.call('summary', limit=1)
    diagnostics = response['bounds']['truncated_fields']
    assert all(len(path) < 80 for path in diagnostics)
    assert len(diagnostics) <= 12 and len(diagnostics) == len(set(diagnostics))
    assert all('GIANT' not in path for path in diagnostics)
    assert len(response['tasks'][0]['id']) == 512
    assert len(response['tasks'][0]['writer_paths']) == 32
    assert len(json.dumps(response)) < 60000
    full = h.call('status')['tasks'][0]
    assert full['id'] == giant and len(full['writer_paths']) == 35
    assert full['dependencies'][-1] == giant + '34'
    assert 'HIDDEN_PROMPT' not in json.dumps(response)


def test_summary_extra_worker_keys_are_omitted_with_honest_bounds(h):
    giant_role = 'GIANT_ROLE' * 5000
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {'configurable': {'thread_id': 'project-coordinator-v1'}}
        state = graph.get_state(config).values['model']
        for role in [giant_role, *[f'extra{n}' for n in range(110)]]:
            state['project']['workers'][role] = 'extra-thread'
            state['workers'][role] = {'state': 'UNKNOWN', 'evidence': 'not output'}
        graph.update_state(config, {'model': state}, as_node='readiness')
    response = h.call('summary')
    assert set(response['project']['workers']) == {f'core{i}' for i in range(1, 5)}
    assert set(response['workers']) == {f'core{i}' for i in range(1, 5)}
    assert response['bounds']['worker_role_count'] == 115
    assert response['bounds']['omitted_worker_roles'] == 111
    assert response['bounds']['truncated_fields'] == ['workers.extra_roles']
    assert len(json.dumps(response)) < 3000 and 'GIANT_ROLE' not in json.dumps(response)
    assert giant_role in h.call('status')['workers']


def test_summary_sql_trace_is_query_only(h, monkeypatch):
    queries = []
    original = sqlite3.connect
    connections = []
    def connect(*args, **kwargs):
        assert args[0].endswith('?mode=ro') and kwargs['uri'] is True
        conn = original(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        connections.append(conn)
        return conn
    monkeypatch.setattr(sqlite3, 'connect', connect)
    h.call('summary')
    assert queries and any('query_only=ON' in query for query in queries)
    assert all(query.split()[0].upper() in {'PRAGMA', 'BEGIN', 'SELECT'} for query in queries)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')
