import copy
import gc
import hashlib
from pathlib import Path
import json
import sqlite3
import sys
import subprocess

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from project_coordinator.runtime import InvalidEvent, apply_event, build_graph, completion_valid
from test_runtime import h, project_policy


def model(h):
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        return build_graph(SqliteSaver(conn)).get_state({'configurable': {'thread_id': 'project-coordinator-v1'}}).values['model']


def rebind(h, **changes):
    return h.call('rebind', worker='worker-1', expected_old_thread_id='thread1', new_thread_id='replacement', evidence='verified retirement and replacement identity', **changes)


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
        h.add(f'active{i}', f'worker-{i}')
        h.issue(f'active{i}')
    h.call('observe', worker='worker-1', state='UNKNOWN', evidence='retired')
    before = model(h)
    assert rebind(h)['action'] == 'REBOUND'
    after = model(h)
    for i in range(2, 5):
        assert after['tasks'][f'active{i}'] == before['tasks'][f'active{i}']
        assert after['workers'][f'worker-{i}'] == before['workers'][f'worker-{i}']
    old = copy.deepcopy(before['tasks']['A'])
    old['dispatch_thread_id'] = 'thread1'
    assert after['tasks']['A'] == old
    assert completion_valid(after, after['tasks']['A'])
    assert h.prepare()['action'] == 'REUSE'
    assert 'native_request' not in h.call('issue', task_id='A', dispatch_id=dispatch)
    h.call('observe', worker='worker-1', state='IDLE', evidence='fresh replacement observation')
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
    h.call('observe', worker='worker-1', state='UNKNOWN', evidence='cannot assume idle')
    before = model(h)
    with pytest.raises(InvalidEvent, match='held or uncertain'):
        rebind(h)
    assert model(h) == before


@pytest.mark.parametrize('change', [{'expected_old_thread_id': 'wrong'}, {'new_thread_id': 'root'}, {'new_thread_id': 'thread2'}, {'project_id': 'wrong'}, {'root': '/wrong'}, {'evidence': ''}, {'worker': 'wrong'}])
def test_rebind_exact_identity_refusal(h, change):
    event = {**h.binding, 'op': 'rebind', 'event_id': 'replace', 'worker': 'worker-1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof', **change}
    before = model(h)
    with pytest.raises(InvalidEvent):
        apply_event(h.db, event)
    assert model(h) == before


def test_released_failed_rebind_replay_and_restart(h):
    h.add()
    dispatch = h.issue()
    h.call('reconcile', task_id='A', dispatch_id=dispatch, quiescent=True, evidence='actual stopped/no pending send')
    h.call('observe', worker='worker-1', state='UNKNOWN', evidence='retired')
    event = {**h.binding, 'op': 'rebind', 'event_id': 'replace-once', 'worker': 'worker-1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof'}
    assert apply_event(h.db, event)['action'] == 'REBOUND'
    assert apply_event(h.db, event)['action'] == 'REPLAY'
    assert h.call('recover')['project']['workers']['worker-1'] == 'replacement'
    assert len(model(h)['rebindings']) == 1
    assert 'native_request' not in h.call('issue', task_id='A', dispatch_id=dispatch)
    with pytest.raises(InvalidEvent, match='already released'):
        h.result(dispatch)


def pending(h, event):
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {'configurable': {'thread_id': 'project-coordinator-v1'}}
        inputs = {'event': {**h.binding, **event}}
        if event.get('op') == 'register':
            inputs['project_policy'] = test_policy(h)
        stream = graph.stream(inputs, config, stream_mode='checkpoints', durability='sync')
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
    pending(h, {'op': 'rebind', 'event_id': 'crash-rebind', 'worker': 'worker-1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof'})
    assert h.call('summary')['project']['workers']['worker-1'] == 'thread1'
    recovered = h.call('recover')
    assert recovered['project']['workers']['worker-1'] == 'replacement'
    assert len(model(h)['rebindings']) == 1
    assert 'native_request' not in recovered


def test_status_compact_bounded_and_readonly(h):
    for i in range(7):
        h.call('add', task={'id': f'T{i}', 'worker': 'worker-1', 'linear_ref': 'linear:test-1', 'beads_ref': 'beads:399', 'prompt': 'SECRET_PROMPT' * 10000, 'source_digest': 's', 'scope_digest': 's', 'writer_paths': [], 'dependencies': ['missing' + str(n) for n in range(40)]})
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
    for command, body in [('rebind', {'event_id': 'cli-rebind', 'worker': 'worker-1', 'expected_old_thread_id': 'thread1', 'new_thread_id': 'replacement', 'evidence': 'proof'}), ('summary', {})]:
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
    h.call('observe', worker='worker-1', state='UNKNOWN', evidence='retired')
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
    assert 'evidence' in full['workers']['worker-1']
    assert task['status'] == 'UNKNOWN' and task['lease']
    h.result(dispatch)
    complete = h.call('status')['tasks'][0]
    assert 'result_receipt' in complete and 'artifacts' in complete
    summary = h.call('summary')
    assert not summary['pending_checkpoint'] and summary['uncertain_held_dispatches'] == 0
    h.call('observe', worker='worker-1', state='IDLE', evidence='fresh')
    h.add('uncertain')
    h.issue('uncertain')
    summary = h.call('summary')
    assert not summary['pending_checkpoint'] and summary['uncertain_held_dispatches'] == 1
    assert summary['recovery_required']


def test_summary_giant_ids_lists_and_diagnostics_are_bounded(h):
    giant = 'GIANT' * 5000
    h.call('add', task={'id': giant, 'worker': 'worker-1', 'linear_ref': 'l', 'beads_ref': 'b', 'prompt': 'HIDDEN_PROMPT', 'source_digest': 's', 'scope_digest': 's', 'dependencies': [giant + str(n) for n in range(35)], 'writer_paths': [('segment' * 14 + '/') * 8 + str(n) for n in range(35)]})
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
            state['workers'][role] = {'state': 'UNKNOWN', 'evidence': 'not output'}
        graph.update_state(config, {'model': state}, as_node='readiness')
    response = h.call('summary')
    assert set(response['project']['workers']) == {f'worker-{i}' for i in range(1, 5)}
    assert set(response['workers']) == {f'worker-{i}' for i in range(1, 5)}
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


REGISTERED_THREAD = 'research-thread'


def register_event(h, **changes):
    return {**h.binding, 'op': 'register', 'event_id': 'register-researcher', 'worker': 'researcher',
            'new_thread_id': REGISTERED_THREAD, 'expected_workers': h.workers.copy(),
            'evidence': 'verified existing registered worker and manual reservation handoff', **changes}


def test_policy(h):
    return project_policy(h, registrations=[('researcher', REGISTERED_THREAD)])


def register(h, **changes):
    return apply_event(h.db, register_event(h, **changes), project_policy=test_policy(h))


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


@pytest.mark.parametrize('stage', ['PREPARED', 'ACKNOWLEDGED', 'FAILED', 'ISSUING'])
def test_register_preserves_all_old_bytes_and_skips_external_readiness(h, monkeypatch, stage):
    h.add('complete')
    h.result(h.issue('complete'), 'complete')
    h.call('observe', worker='worker-1', state='IDLE', evidence='fresh')
    (h.root / 'first').mkdir()
    (h.root / 'second').mkdir()
    alias = h.root / 'alias'
    alias.symlink_to(h.root / 'first', target_is_directory=True)
    h.add('queued', paths=['alias'])
    h.add('dependent', 'worker-3', dependencies=['complete'])
    h.add('held', 'worker-2', paths=['held-path'])
    dispatch = h.prepare('held')['dispatch_id']
    if stage != 'PREPARED':
        h.call('issue', task_id='held', dispatch_id=dispatch)
        if stage == 'ACKNOWLEDGED':
            h.call('ack', task_id='held', dispatch_id=dispatch,
                   receipt=h.file('held-ack.json', {'threadId': 'thread2'}))
        elif stage == 'FAILED':
            h.result(dispatch, 'held', acceptance=False, quiescent=False)
    # These inputs would change the old READY tasks if registration ran readiness.
    alias.unlink()
    alias.symlink_to(h.root / 'second', target_is_directory=True)
    (h.root / 'complete-output.json').write_text('external artifact drift')
    before = model(h)
    assert before['tasks']['held']['status'] == stage
    receipt_bytes = {str(p): p.read_bytes() for p in h.root.glob('*.json')}
    manual = h.root / '.beads' / 'record.json'
    manual.write_text(json.dumps({'id': 'global-legacy-b', 'assignee': 'legacy-actor-b',
                                  'status': 'in_progress', 'manual_reservation': True}))
    manual_before = manual.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('registration must not reevaluate old readiness or Beads')
    monkeypatch.setattr('project_coordinator.runtime.readiness', forbidden)
    monkeypatch.setattr('project_coordinator.beads.subprocess.run', forbidden)
    response = register(h)
    after = model(h)
    assert response['action'] == 'REGISTERED' and 'native_request' not in response
    assert after['project']['workers'].pop('researcher') == REGISTERED_THREAD
    assert after['workers'].pop('researcher') == {'state': 'UNKNOWN', 'evidence': register_event(h)['evidence']}
    audit = after['events'].pop('register-researcher')
    assert audit['action'] == 'REGISTERED'
    assert canonical_bytes(after) == canonical_bytes(before)
    proof = response['preservation']
    assert proof['before_sha256'] == proof['after_sha256'] == hashlib.sha256(canonical_bytes(before)).hexdigest()
    assert proof['task_count'] == 4 and proof['worker_binding_count'] == proof['worker_observation_count'] == 4
    assert proof['event_count'] == len(before['events'])
    assert manual.read_bytes() == manual_before
    assert all(Path(path).read_bytes() == data for path, data in receipt_bytes.items())
    # Replay also skips readiness, preserving even raw ISSUING history.
    assert register(h)['action'] == 'REPLAY'
    assert model(h)['tasks']['held']['status'] == stage


@pytest.mark.parametrize('change', [
    {'worker': 'implementer'}, {'worker': 'Bad/Role'}, {'new_thread_id': 'other'},
    {'new_thread_id': 'root'}, {'new_thread_id': 'thread2'}, {'project_id': 'wrong'}, {'root': '/wrong'},
    {'expected_workers': {f'worker-{i}': f'wrong{i}' for i in range(1, 5)}},
    {'expected_workers': {}}, {'expected_workers': []}, {'expected_workers': {'worker-1': 'thread1'}},
    {'evidence': ''}, {'evidence': '  '}, {'unexpected': True},
])
def test_register_identity_and_input_refusal_without_checkpoint(h, change):
    gc.collect()
    before = h.db.read_bytes()
    state = model(h)
    with pytest.raises(InvalidEvent):
        register(h, **change)
    assert h.db.read_bytes() == before and model(h) == state


@pytest.mark.parametrize('collision', ['root', 'worker', 'observed', 'already-bound'])
def test_register_refuses_existing_or_duplicate_thread(h, collision):
    if collision == 'already-bound':
        register(h)
    else:
        with sqlite3.connect(h.db, check_same_thread=False) as conn:
            graph = build_graph(SqliteSaver(conn))
            config = {'configurable': {'thread_id': 'project-coordinator-v1'}}
            state = graph.get_state(config).values['model']
            if collision == 'root':
                state['project']['coordinator_thread_id'] = REGISTERED_THREAD
            elif collision == 'worker':
                state['project']['workers']['worker-1'] = REGISTERED_THREAD
            else:
                state['workers']['researcher'] = {'state': 'IDLE', 'evidence': 'unbound observation'}
            graph.update_state(config, {'model': state}, as_node='readiness')
    before = model(h)
    with pytest.raises(InvalidEvent):
        register(h, event_id='different-registration', expected_workers={k: v for k, v in before['project']['workers'].items() if k != 'researcher'})
    assert model(h) == before


def test_register_replay_conflict_and_dynamic_role_query_only_summary(h):
    response = register(h)
    assert response['action'] == 'REGISTERED'
    assert register(h)['action'] == 'REPLAY'
    before = model(h)
    gc.collect()
    db_bytes = h.db.read_bytes()
    summary = h.call('summary')
    assert summary['project']['workers']['researcher'] == REGISTERED_THREAD
    assert summary['workers']['researcher'] == {'state': 'UNKNOWN'}
    assert summary['bounds']['worker_role_count'] == len(h.workers) + 1 and summary['bounds']['omitted_worker_roles'] == 0
    assert not summary['bounds']['truncated_fields'] and 'native_request' not in summary
    assert h.db.read_bytes() == db_bytes
    with pytest.raises(InvalidEvent, match='conflicting payload'):
        register(h, evidence='changed proof')
    assert model(h) == before


@pytest.mark.parametrize('pending_op', ['observe', 'issue', 'register'])
def test_pending_register_refuses_before_recovery_and_preserves_database(h, pending_op):
    if pending_op == 'issue':
        h.add()
        body = {'op': 'issue', 'event_id': 'pending-issue', 'task_id': 'A', 'dispatch_id': h.prepare()['dispatch_id']}
    elif pending_op == 'register':
        body = register_event(h)
    else:
        body = {'op': 'observe', 'event_id': 'pending-observe', 'worker': 'worker-2', 'state': 'UNKNOWN', 'evidence': 'pending'}
    pending(h, body)
    gc.collect()
    summary = h.call('summary')
    before = h.db.read_bytes()
    state = model(h)
    assert summary['pending_checkpoint']
    with pytest.raises(InvalidEvent, match='pending checkpoint'):
        register(h)
    assert h.db.read_bytes() == before and model(h) == state
    after = h.call('summary')
    assert after['checkpoint_id'] == summary['checkpoint_id'] and after['pending_checkpoint']
    recovered = h.call('recover')
    assert 'native_request' not in recovered
    if pending_op == 'register':
        assert recovered['project']['workers']['researcher'] == REGISTERED_THREAD
        assert register(h)['action'] == 'REPLAY'


def test_registered_researcher_unknown_leases_completion_original_target_and_reuse(h):
    register(h)
    h.add('researcher-task', 'researcher', paths=['owned'])
    assert h.prepare('researcher-task')['action'] == 'WAIT'
    assert model(h)['tasks']['researcher-task']['lease'] is False
    h.call('observe', worker='researcher', state='IDLE', evidence='fresh actual registered worker observation')
    prepared = h.prepare('researcher-task')
    dispatch = prepared['dispatch_id']
    assert prepared['action'] == 'PREPARED'
    h.add('same-worker', 'researcher')
    (h.root / 'owned').mkdir()
    (h.root / 'owned-alias').symlink_to(h.root / 'owned', target_is_directory=True)
    h.add('same-path', 'worker-2', paths=['owned-alias/subpath'])
    assert any(reason.startswith('worker-lease:researcher-task:') for reason in h.prepare('same-worker')['reasons'])
    assert any(reason.startswith('writer-lease:researcher-task:') for reason in h.prepare('same-path')['reasons'])
    issued = h.call('issue', task_id='researcher-task', dispatch_id=dispatch)
    assert issued['native_request']['arguments']['threadId'] == REGISTERED_THREAD
    wrong = h.file('wrong-researcher-ack.json', {'threadId': 'thread5'})
    with pytest.raises(InvalidEvent, match='target'):
        h.call('ack', task_id='researcher-task', dispatch_id=dispatch, receipt=wrong)
    h.call('ack', task_id='researcher-task', dispatch_id=dispatch,
           receipt=h.file('researcher-ack.json', {'threadId': REGISTERED_THREAD}))
    native = h.file('actual-researcher-native.json', {'threadId': REGISTERED_THREAD, 'dispatch_id': dispatch, 'terminal': True})
    assert h.result(dispatch, 'researcher-task', thread_id=REGISTERED_THREAD, native_evidence=native)['action'] == 'COMPLETE'
    h.call('observe', worker='researcher', state='UNKNOWN', evidence='verified original registered worker is quiescent')
    h.call('rebind', worker='researcher', expected_old_thread_id=REGISTERED_THREAD,
           new_thread_id='replacement-five', evidence='verified replacement')
    stored = model(h)
    assert stored['tasks']['researcher-task']['dispatch_thread_id'] == REGISTERED_THREAD
    assert completion_valid(stored, stored['tasks']['researcher-task'])
    assert h.prepare('researcher-task')['action'] == 'REUSE'
    assert 'native_request' not in h.call('issue', task_id='researcher-task', dispatch_id=dispatch)


def test_cli_register_replay_and_summary(h):
    path = h.root / 'registration.json'
    policy_path = h.root / 'project-policy.json'
    path.write_text(json.dumps(register_event(h)))
    policy_path.write_text(json.dumps(test_policy(h)))
    argv = [sys.executable, '-m', 'project_coordinator.cli', '--db', str(h.db), 'register', '--input', str(path),
            '--project-policy', str(policy_path)]
    for expected in ['REGISTERED', 'REPLAY']:
        result = subprocess.run(argv, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert response['action'] == expected and 'native_request' not in response
    path.write_text(json.dumps(h.binding))
    result = subprocess.run([*argv[:5], 'summary', *argv[6:]], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['project']['workers']['researcher'] == REGISTERED_THREAD


def test_schema_accepts_safe_project_roles_and_explicit_project_policy():
    from pathlib import Path

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result, f'duplicate JSON key: {key}'
            result[key] = value
        return result

    schema = json.loads((Path(__file__).resolve().parents[1] / 'schema.json').read_text(), object_pairs_hook=unique_keys)
    assert 'register' in schema['properties']['op']['enum']
    assert 'report' in schema['properties']['op']['enum']
    assert schema['properties']['report_offset']['minimum'] == 0
    assert schema['properties']['report_limit']['maximum'] == 100
    assert schema['properties']['worker']['pattern'] == '^[a-z][a-z0-9_-]*$'
    assert schema['$defs']['task']['properties']['worker']['pattern'] == '^[a-z][a-z0-9_-]*$'
    assert schema['$defs']['project']['properties']['workers']['minProperties'] == 1
    assert '^[a-z][a-z0-9_-]{0,63}$' in schema['$defs']['project']['properties']['workers']['patternProperties']
    assert schema['$defs']['projectPolicy']['additionalProperties'] is False
    report = next(rule['then'] for rule in schema['allOf'] if rule['if']['properties']['op'].get('const') == 'report')
    assert report['properties']['state'] == {'type': 'string', 'minLength': 1, 'maxLength': 512}
    assert report['properties']['evidence']['type'] == 'array'
    assert report['properties']['evidence']['items'] == {'type': 'string', 'minLength': 1, 'maxLength': 512}
    observe = next(rule['then'] for rule in schema['allOf'] if rule['if']['properties']['op'].get('const') == 'observe')
    assert observe['properties']['state']['enum'] == ['IDLE', 'BUSY', 'UNKNOWN']
    assert observe['properties']['evidence']['type'] == 'string'
    for op in ('prepare', 'reconcile', 'rebind', 'retire', 'register'):
        rule = next(rule['then'] for rule in schema['allOf'] if rule['if']['properties']['op'].get('const') == op)
        assert rule['properties']['evidence']['type'] == 'string'
        assert rule['properties']['evidence']['minLength'] == 1
    status = next(rule['then'] for rule in schema['allOf'] if rule['if']['properties']['op'].get('const') == 'status')
    assert set(status['required']) == {'project_id', 'root'}
    rule = next(rule['then'] for rule in schema['allOf'] if rule['if']['properties']['op'].get('const') == 'register')
    assert rule['properties']['new_thread_id']['type'] == 'string'
    assert 'worker' not in rule['properties']
    assert set(rule['required']) == {'event_id', 'project_id', 'root', 'worker', 'new_thread_id', 'expected_workers', 'evidence'}


@pytest.mark.parametrize('change', [{'project_id': 'P-other'}, {'root': '/other'}])
def test_registration_requires_matching_project_policy_without_mutation(h, change):
    before = h.db.read_bytes()
    policy = {**test_policy(h), **change}
    with pytest.raises(InvalidEvent, match='project policy'):
        apply_event(h.db, register_event(h), project_policy=policy)
    assert h.db.read_bytes() == before and 'researcher' not in model(h)['project']['workers']


def test_summary_omits_unbound_researcher_observation_with_diagnostic(h):
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {'configurable': {'thread_id': 'project-coordinator-v1'}}
        state = graph.get_state(config).values['model']
        state['workers']['researcher'] = {'state': 'UNKNOWN', 'evidence': 'unbound'}
        graph.update_state(config, {'model': state}, as_node='readiness')
    summary = h.call('summary')
    assert 'researcher' not in summary['workers']
    assert summary['bounds']['omitted_worker_roles'] == 1
    assert summary['bounds']['truncated_fields'] == ['workers.extra_roles']
