#!/usr/bin/python3
"""Candidate v2 adaptive execution; immutable v1 receipts/helpers stay separate."""
import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parents[2]
PROJECT_ROOT = ROOT = CANONICAL_STATE = PROJECT_ID = None
LEGACY_PROJECT_BINDING = None


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


base = module('adaptive_canonical', CODE_ROOT / 'tools/project-task-runner/runner.py')
policy = module('adaptive_policy', HERE / 'policy.py')
retention = module('adaptive_retention', HERE / 'retention.py')
need = base.need
TERMINAL = retention.TERMINAL


def bind_context(project_root, state, project_id=None, legacy_project_binding=None):
    """Bind explicit existing project/state inputs without selecting a store."""
    global PROJECT_ROOT, ROOT, CANONICAL_STATE, PROJECT_ID, LEGACY_PROJECT_BINDING
    root = Path.cwd() if project_root is None else Path(project_root)
    root = root.resolve(strict=True)
    need(root.is_dir(), 'project_directory')
    if project_id is not None:
        need(type(project_id) is str and re.fullmatch(
            r'[a-z0-9][a-z0-9._-]{0,63}', project_id) is not None, 'project_identity')
    state = base.private_dir(state)
    state = state.resolve(strict=True)
    need(state.is_relative_to(root), 'state_outside_project')
    PROJECT_ROOT = ROOT = root
    CANONICAL_STATE = state
    PROJECT_ID = project_id
    if legacy_project_binding is not None:
        legacy = validate_project_binding(legacy_project_binding, check_context=False)
        need(legacy['project_id'] == project_id, 'legacy_project_binding_identity')
        LEGACY_PROJECT_BINDING = legacy
    return state


def validate_project_binding(value=None, *, check_context=True):
    """Validate caller-supplied project identity and unit/payload data."""
    if value is None:
        if LEGACY_PROJECT_BINDING is not None and (
                PROJECT_ID is None or LEGACY_PROJECT_BINDING['project_id'] == PROJECT_ID):
            return dict(LEGACY_PROJECT_BINDING)
        value = {'project_id': PROJECT_ID or 'generic', 'unit_prefix': 'project-task',
                 'payload_environment': {}}
    need(type(value) is dict and set(value) == {
        'project_id', 'unit_prefix', 'payload_environment'}, 'project_binding_shape')
    project_id = value['project_id']; prefix = value['unit_prefix']; payload = value['payload_environment']
    need(type(project_id) is str and re.fullmatch(
        r'[a-z0-9][a-z0-9._-]{0,63}', project_id) is not None, 'project_binding_identity')
    need(type(prefix) is str and re.fullmatch(
        r'[a-z0-9][a-z0-9-]{0,62}', prefix) is not None, 'project_binding_unit')
    need(not check_context or PROJECT_ID is None or PROJECT_ID == project_id,
         'project_binding_context')
    need(type(payload) is dict, 'project_binding_environment')
    for name, template in payload.items():
        need(type(name) is str and re.fullmatch(
            r'[A-Z][A-Z0-9_]{0,63}', name) is not None
            and type(template) is str and template.count('{job}') == 1
            and template.replace('{job}', '').find('{') < 0
            and template.replace('{job}', '').find('}') < 0,
            'project_binding_environment')
    return {'project_id': project_id, 'unit_prefix': prefix,
            'payload_environment': dict(payload)}


def intent_project_binding(intent):
    value = intent.get('project_binding')
    if value is not None:
        result = validate_project_binding(value, check_context=False)
        need(intent.get('unit') == result['unit_prefix'] + '-' + intent['job_id'] + '.service',
             'project_unit_binding')
        return result
    # Existing v2 records carry their historical unit. Derive that identity
    # from the record so status/stop remain compatible without a fixed prefix.
    unit = intent.get('unit', '')
    suffix = '-' + intent.get('job_id', '') + '.service'
    need(type(unit) is str and type(intent.get('job_id')) is str and unit.endswith(suffix),
         'project_unit_binding')
    prefix = unit[:-len(suffix)]
    need(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', prefix) is not None,
         'project_unit_binding')
    if LEGACY_PROJECT_BINDING is not None:
        need(prefix == LEGACY_PROJECT_BINDING['unit_prefix'], 'legacy_project_unit_binding')
        return dict(LEGACY_PROJECT_BINDING)
    return {'project_id': PROJECT_ID or 'legacy', 'unit_prefix': prefix,
            'payload_environment': {}}


def payload_environment(intent, job):
    binding = intent_project_binding(intent)
    values = {}
    for name, template in binding['payload_environment'].items():
        path = Path(template.replace('{job}', str(job))).resolve(strict=False)
        need(path.is_relative_to(Path(job).resolve(strict=True)), 'project_payload_path')
        values[name] = str(path)
    return values


def project_job(job):
    job = Path(job).resolve(strict=False)
    need(job.is_relative_to(ROOT), 'job_outside_project')
    return job


def replace(path, value):
    """Private mutable control/ledger; immutable receipts use canonical publish."""
    temp = path.parent / ('.' + uuid.uuid4().hex + '.part')
    try:
        base.publish(temp, value)
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked(state, nonblocking=False):
    state = base.private_dir(state)
    fd = os.open(state / 'capacity.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        info = os.fstat(lock.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and not info.st_mode & 0o077, 'capacity_lock')
        fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield state


def source_pins():
    return {**base.source_pins(), **{str(p): base.source_digest(p) for p in
            [HERE / 'policy.py', HERE / 'retention.py', HERE / 'runner.py']}}


def configuration(path):
    raw = base.read_bytes(path)
    c = json.loads(raw)
    required = {'version', 'argv', 'cwd', 'limits', 'sources', 'owner', 'lease', 'kind', 'inputs', 'estimates', 'foreground', 'cooperative'}
    need(type(c) is dict and required <= c.keys() and c.keys() <= required | {'stdin_file'}, 'v2_config_keys')
    need(c['version'] == 2 and type(c['version']) is int, 'version')
    need(all(type(c[k]) is str and 0 < len(c[k]) <= 256 for k in ('owner', 'lease')), 'ownership')
    need(c['kind'] in ('command', 'search', 'build', 'read', 'analysis'), 'kind')
    need(type(c['foreground']) is bool, 'foreground')
    need(type(c['argv']) is list and c['argv'] and all(type(x) is str and '\0' not in x for x in c['argv']), 'argv')
    executable = Path(c['argv'][0])
    need(executable.is_absolute() and executable == executable.resolve() and executable.is_file() and os.access(executable, os.X_OK), 'executable')
    need(type(c['sources']) is dict and c['sources'], 'sources')
    need(c['sources'].get(str(executable)) == base.source_digest(executable), 'executable_provenance')
    base.private_dir(c['cwd'])
    base.limits_config(c['limits'])
    need(type(c['sources']) is dict and c['sources'], 'sources')
    base.pins(c['sources'])
    need(type(c['estimates']) is dict and set(c['estimates']) == {'memory_bytes', 'disk_bytes', 'inodes'}, 'estimates')
    need(all(type(v) is int and v >= 0 for v in c['estimates'].values()), 'estimates')
    need(type(c['cooperative']) is dict and set(c['cooperative']) == {'enabled'} and type(c['cooperative']['enabled']) is bool, 'cooperative')
    need(type(c['inputs']) is list and c['inputs'], 'explicit_inputs')
    for name in c['inputs']:
        path = Path(name)
        need(path.is_absolute() and path == path.resolve(strict=True) and (path.is_file() or path.is_dir()), 'input_path')
        if c['kind'] in ('search', 'read') and path.is_dir():
            relative = path.relative_to(ROOT)
            need(relative.parts and not any(p.lower() in ('work', 'integration', 'sdk', 'generated', 'evidence', '.git', 'node_modules', 'build', 'research') for p in relative.parts), 'broad_search_root')
            need(path.is_file() or len(relative.parts) >= 2, 'narrow_search_root')
        if path.is_file():
            need(c['sources'].get(str(path)) == base.source_digest(path), 'input_provenance')
    if c['kind'] == 'search':
        need(executable.name == 'rg', 'search_requires_rg')
        args = c['argv'][1:]
        need('--' in args, 'search_pattern_separator')
        index = args.index('--')
        allowed = {'--fixed-strings', '--ignore-case', '--line-number', '--count', '--files-with-matches', '--hidden'}
        need(all(arg in allowed for arg in args[:index]) and len(args[index + 1:]) == 1, 'search_options_or_paths')
        c['argv'] = [str(executable), '--no-follow', '--max-count=1000', *args, *c['inputs']]
    # Executable code is trusted and pinned; argv is never interpreted by a shell.
    if c.get('stdin_file'):
        f, _ = base.open_input(c['stdin_file'])
        f.close()
    return c, base.digest(raw)


def common_cgroup():
    relative = base.process(os.getpid())['cgroup']
    parts = Path(relative).parts
    need('app.slice' in parts, 'common_app_slice_unknown')
    return base.cgdir(str(Path(*parts[:parts.index('app.slice') + 1])))


def read_ledger(state):
    need(Path(state) == CANONICAL_STATE, 'canonical_shared_state_required')
    path = state / 'capacity.json'
    need(path.exists(), 'reviewed_ledger_migration_required')
    result = base.read(path)
    need(result['version'] == 3 and type(result['jobs']) is dict, 'ledger_version')
    for ident, record in result['jobs'].items():
        if record['state'] in ('terminal', 'cancelled-before-launch', 'failed-before-launch'):
            continue
        _, intent = base.load_job(Path(record['job']))
        need(intent.get('contract_version') == 2 and intent.get('candidate') == str(HERE)
             and intent.get('state_dir') == str(CANONICAL_STATE) and intent.get('project_root', str(ROOT)) == str(ROOT)
             and intent['job_id'] == ident
             and intent['source_pins'] == record['source_pins']
             and intent['config']['owner'] == record['owner'] and intent['config']['lease'] == record['lease'],
             'mixed_candidate_or_ledger_custody')
    return result


def terminal_proof(job, env):
    status = base.public_status(job, env)
    need(status['identity_verified'] and status['state'] in TERMINAL and status.get('cleanup', {}).get('quiescent') is True, 'terminal_unverified')
    _, intent = base.load_job(job)
    info = base.unit_show(intent['unit'], env)
    if info.get('LoadState') == 'not-found':
        saved = base.read(Path(job) / 'ready.json')['process']
        try:
            current = base.process(saved['pid'])
        except (FileNotFoundError, ProcessLookupError):
            current = None
        need(current is None or any(current[k] != saved[k] for k in ('uid', 'start_ticks')), 'original_birth_live')
        need(base.subtree_empty(base.service_cgroup(base.read(Path(job) / 'ready.json')), intent.get('topology') == base.TOPOLOGY), 'terminal_subtree_live')
    else:
        base.custody(job, intent, info)
        cg = base.cgdir(info.get('ControlGroup', ''))
        exited = info.get('SubState') == 'exited' and info.get('MainPID') == '0'
        need((info.get('ActiveState') in ('inactive', 'failed') or exited) and base.subtree_empty(base.service_cgroup(base.read(Path(job) / 'ready.json')), intent.get('topology') == base.TOPOLOGY), 'terminal_live')
    return status


def footprint(record, env):
    job = Path(record['job'])
    _, intent = base.load_job(job)
    need(intent.get('contract_version') == 2 and intent['source_pins'] == record['source_pins'] and intent['config']['owner'] == record['owner'] and intent['config']['lease'] == record['lease'], 'ledger_custody')
    base.pins(intent['source_pins'])
    info = base.unit_show(intent['unit'], env)
    ready = base.custody(job, intent, info)
    cg = base.cgdir(base.service_cgroup(ready))
    values = dict(line.split() for line in (cg / 'memory.stat').read_text().splitlines())
    # File cache is observational; only charged anonymous/kernel/dirty work consumes promises.
    actual = int((cg / 'memory.current').read_text())
    consumed = min(actual, sum(int(values[k]) for k in ('anon', 'kernel', 'shmem', 'file_dirty', 'file_writeback')))
    cpu_stat = dict(line.split() for line in (cg / 'cpu.stat').read_text().splitlines())
    now = time.monotonic()
    usage = int(cpu_stat['usage_usec'])
    old = record.get('cpu_sample')
    if old and now > old['at'] and usage >= old['usage_usec']:
        record['actual_cpu'] = (usage - old['usage_usec']) / 1000000 / (now - old['at'])
    record['cpu_sample'] = {'at': now, 'usage_usec': usage}
    files = [job / 'payload.log']
    scratch = job / 'scratch'
    listing = retention.inventory(scratch, max_entries=inventory_budget(record['estimates']))
    need(listing['complete'] or all(gap.get('reason') in ('entry cap reached', 'deadline reached') for gap in listing['gaps']), 'scratch_measurement_unknown')
    disk = sum(row['allocated_bytes'] for row in listing['rows']) + sum(p.stat().st_blocks * 512 for p in files if p.exists())
    record.update(actual_memory_bytes=consumed, memory_current_bytes=actual, file_cache_bytes=int(values['file']),
                  disk_bytes_observed=disk, disk_measurement_complete=listing['complete'],
                  remaining_memory_bytes=max(0, record['estimates']['memory_bytes'] - consumed),
                  remaining_disk_bytes=max(0, record['estimates']['disk_bytes'] - disk),
                  remaining_inodes=max(0, record['estimates']['inodes'] - len(listing['rows'])))
    return ready, info


def inventory_budget(estimates):
    return min(5000, max(256, estimates['inodes'] * 2 + 64))


def apply_allocation(job, allocation, env):
    job, intent = base.load_job(job)
    need(intent.get('contract_version') == 2 and intent.get('candidate') == str(HERE), 'not_opted_in')
    base.pins(intent['source_pins'])
    cpu = allocation['cpu']
    need(type(cpu) in (float, int) and math.isfinite(cpu) and cpu >= 0, 'cpu_target')
    need(type(allocation['fanout']) is int and allocation['fanout'] >= 0
         and type(allocation['checkpoint']) is bool, 'allocation_format')
    info = base.unit_show(intent['unit'], env)
    ready = base.custody(job, intent, info)
    need(info.get('ActiveState') == 'active' and info.get('MainPID') == str(ready['process']['pid']), 'not_running')
    actual = budget_check(intent, ready, info, cpu)
    percent = round(cpu * 100, 2)
    event = {'at': time.time(), 'allocation': allocation, 'cpu_percent': percent,
             'topology': ready.get('topology'), 'cpu_max': actual['cpu.max'].split(),
             'manager': info, 'invocation_id': ready['invocation_id']}
    current = job / 'allocation-current.json'
    previous = base.read(current).get('telemetry', {}) if current.exists() else {}
    points = previous.get('transitions', [])
    point = {k: event[k] for k in ('at', 'cpu_percent', 'cpu_max', 'invocation_id')}
    point.update(fanout=allocation['fanout'], checkpoint=allocation['checkpoint'])
    changed = not points or any(point[k] != points[-1][k] for k in ('fanout', 'checkpoint')) or abs(point['cpu_percent'] - points[-1]['cpu_percent']) >= 25 or point['at'] - points[-1]['at'] >= 30
    count = previous.get('transitions_seen', 0) + changed
    if changed:
        points = points + [point]
        if len(points) > 64: points = [points[0], *points[-63:]]
    minimum = min([point, previous.get('minimum', point)], key=lambda p: p['cpu_percent'])
    maximum = max([point, previous.get('maximum', point)], key=lambda p: p['cpu_percent'])
    event['telemetry'] = {'events_seen': previous.get('events_seen', 0) + 1,
                          'transitions_seen': count, 'transitions': points,
                          'history_complete': count <= 64, 'minimum': minimum, 'maximum': maximum,
                          'latest_checkpoint': point if point['checkpoint'] else previous.get('latest_checkpoint')}
    replace(current, event)
    cooperative = intent['config']['cooperative']
    replace(job / 'control.json', {'version': 2, 'job_id': intent['job_id'], 'cpu': cpu,
             'fanout': allocation['fanout'] if cooperative['enabled'] else None,
             'checkpoint': allocation['checkpoint'] if cooperative['enabled'] else None})
    return event


def reconcile(state, env, snapshot=None, expected_job=None):
    """Caller holds one project lock across sampling, promises, decision and admission."""
    ledger = read_ledger(state)
    if expected_job is not None:
        ident, job = expected_job
        record = ledger['jobs'].get(ident)
        need(type(record) is dict and record.get('job') == str(job), 'job_ledger_mismatch')
    for ident, record in ledger['jobs'].items():
        if record['state'] not in ('running', 'starting'):
            continue
        try:
            terminal_proof(Path(record['job']), env)
        except Exception:
            try:
                footprint(record, env)
                record.pop('measurement_error', None)
            except Exception as error:
                record['measurement_error'] = type(error).__name__
        else:
            record['state'] = 'terminal'
    if snapshot is None:
        try:
            sampled = policy.sample_host(ROOT, common_cgroup(), previous=ledger.get('sampler'))
        except (OSError, ValueError, base.Refused):
            sampled = {'missing': ['common.app.slice.unknown']}
    else:
        sampled = snapshot
    if any(j.get('measurement_error') for j in ledger['jobs'].values() if j['state'] in ('running', 'starting')):
        sampled = dict(sampled, missing=list(sampled.get('missing', [])) + ['owned.footprint_unknown'])
    decision = policy.decide(sampled, list(ledger['jobs'].values()), ledger['policy'])
    for ident, admission in decision['admissions'].items():
        if admission['admit']:
            ledger['jobs'][ident]['state'] = 'starting'
    for ident, allocation in decision['allocations'].items():
        record = ledger['jobs'][ident]
        if record['state'] == 'running':
            try:
                apply_allocation(Path(record['job']), allocation, env)
                record['allocation'] = allocation
            except Exception as error:
                record['allocation_error'] = type(error).__name__
        elif record['state'] == 'starting':
            record['allocation'] = allocation
    ledger.update(policy=decision['state'], sampler=sampled.get('interval_baseline'), snapshot=sampled, decision=decision)
    replace(state / 'capacity.json', ledger)
    return ledger


def budget_check(intent, ready, info, cpu):
    actual = base.budget_check(intent, {**ready, 'process': {**ready['process'], 'cgroup': base.service_cgroup(ready)}}, info)
    if intent.get('topology') == base.TOPOLOGY:
        base.topology_check(intent, ready, info)
        actual['topology'] = ready['topology']
    return actual


def launch(job, config, state, project_root=None, project_binding=None):
    binding = validate_project_binding(project_binding)
    state = bind_context(project_root, state, binding['project_id'])
    need(state == CANONICAL_STATE, 'canonical_shared_state_required')
    job = project_job(job)
    base.private_dir(job.parent)
    with locked(state):
        read_ledger(state)
    c, chash = configuration(config)
    env, version = base.manager_environment()
    job.mkdir(mode=0o700)
    (job / 'scratch').mkdir(mode=0o700)
    ident = uuid.uuid4().hex
    intent = {'contract_version': 2, 'candidate': str(HERE), 'project_root': str(ROOT), 'state_dir': str(state), 'job_id': ident,
              'unit': binding['unit_prefix'] + '-' + ident + '.service', 'project_binding': binding,
              'job_dir': str(job), 'uid': os.getuid(), 'config': c,
              'config_sha256': chash, 'argv_sha256': base.digest(base.canonical(c['argv'])), 'source_pins': source_pins(),
              'environment_sha256': base.digest(base.canonical(env)), 'manager_version': version, 'topology': base.TOPOLOGY}
    if c.get('stdin_file'):
        f, intent['stdin_identity'] = base.open_input(c['stdin_file']); f.close()
    base.publish(job / 'environment.json', env)
    base.publish(job / 'intent.json', intent)
    record = {'id': ident, 'job': str(job), 'state': 'ready', 'ready_since': time.monotonic(), 'owner': c['owner'], 'lease': c['lease'],
              'source_pins': intent['source_pins'], 'estimates': c['estimates'],
              'working_set_bytes': c['estimates']['memory_bytes'], 'actual_memory_bytes': 0,
              'remaining_memory_bytes': c['estimates']['memory_bytes'], 'remaining_disk_bytes': c['estimates']['disk_bytes'],
              'remaining_inodes': c['estimates']['inodes'], 'foreground': c['foreground'], 'cooperative': c['cooperative']['enabled']}
    with locked(state):
        ledger = read_ledger(state)
        ledger['jobs'][ident] = record
        replace(state / 'capacity.json', ledger)
    # Queued jobs have no native unit. Their caller may poll status or stop them.
    return advance(job, env)


def start_unit(job, intent, record, env):
    properties = base.unlimited_properties()
    cpu = record['allocation']['cpu']
    binding = intent_project_binding(intent)
    if intent.get('topology') == base.TOPOLOGY:
        properties.update(Delegate='cpu', DelegateSubgroup='management')
    argv = ['systemd-run', '--user', '--quiet', '--unit=' + intent['unit'], '--description=' + binding['unit_prefix'] + ' ' + intent['job_id'],
            '--service-type=exec', '--remain-after-exit', '--expand-environment=no', '--working-directory=' + str(job)]
    if intent.get('project_binding') is None and LEGACY_PROJECT_BINDING is not None:
        argv.append('--setenv=PROJECT_TASK_LEGACY_BINDING=' + json.dumps(
            LEGACY_PROJECT_BINDING, sort_keys=True, separators=(',', ':')))
    argv += ['--property=' + k + '=' + str(v) for k, v in properties.items()]
    argv += ['--setenv=' + k + '=' + v for k, v in env.items()]
    argv += [sys.executable, '-B', str(HERE / 'runner.py'), '_worker', str(job)]
    base.publish(job / 'launch-intent.json', {'properties': properties, 'job_id': intent['job_id'],
                                            'launcher_process': base.process(os.getpid())})
    try:
        result = base.command(argv, env)
    except subprocess.TimeoutExpired:
        base.publish(job / 'launch-outcome.json', {'state': 'UNKNOWN', 'error': 'launcher_timeout'})
        return
    base.publish(job / 'launch-outcome.json', {'state': 'RETURNED', 'exit': result.returncode, 'stderr': result.stderr[:4096]})
    if result.returncode != 0 and base.unit_show(intent['unit'], env).get('LoadState') == 'not-found' and not (job / 'ready.json').exists():
        record['state'] = 'failed-before-launch'
        return
    need(result.returncode == 0, 'launch_rejected')
    base.wait_ready(job, intent, env)
    info = base.unit_show(intent['unit'], env)
    ready = base.custody(job, intent, info)
    if intent.get('topology') == base.TOPOLOGY:
        apply_allocation(job, record['allocation'], env)
    actual = budget_check(intent, ready, info, cpu)
    need(ready['environment_sha256'] == intent['environment_sha256'], 'environment_changed')
    base.pins(intent['source_pins']); base.pins(intent['config']['sources'])
    base.publish(job / 'custody.json', {'invocation_id': ready['invocation_id'], 'process': ready['process'], 'budget': actual, 'manager': info})
    base.custody(job, intent, base.unit_show(intent['unit'], env))
    allocation = record['allocation']
    descriptor = intent['config']['cooperative']
    replace(job / 'control.json', {'version': 2, 'job_id': intent['job_id'], 'cpu': cpu,
             'fanout': allocation['fanout'] if descriptor['enabled'] else None,
             'checkpoint': allocation['checkpoint'] if descriptor['enabled'] else None})
    base.publish(job / 'admission.json', {'intent_sha256': base.digest(base.read_bytes(job / 'intent.json')), 'invocation_id': ready['invocation_id']})
    record['state'] = 'running'


def advance(job, env=None):
    job, intent = base.load_job(job)
    need(intent.get('contract_version') == 2 and intent['candidate'] == str(HERE), 'candidate_mismatch')
    bind_context(intent.get('project_root'), intent['state_dir'], intent_project_binding(intent)['project_id'])
    job = project_job(job)
    base.pins(intent['source_pins'])
    env = base.manager_environment()[0] if env is None else env
    with locked(intent['state_dir']) as state:
        ledger = reconcile(state, env, expected_job=(intent['job_id'], job))
        record = ledger['jobs'][intent['job_id']]
        if record['state'] == 'starting' and not (job / 'launch-intent.json').exists():
            start_unit(job, intent, record, env)
            replace(state / 'capacity.json', ledger)
        result = {'job_id': intent['job_id'], 'state': record['state'].upper(), 'allocation': record.get('allocation'),
                  'capacity_status': ledger['decision']['status'], 'reasons': ledger['decision']['reasons'],
                  'resource_contract': 'unlimited'}
        if (job / 'ready.json').exists():
            result.update(base.public_status(job, env))
            if record['state'] != 'terminal' and result['state'] in TERMINAL:
                terminal_proof(job, env)
                record['state'] = 'terminal'
                replace(state / 'capacity.json', ledger)
        if record['state'] == 'terminal':
            result['retention'] = cleanup_terminal(job, env)
        return result


def drain(pipe, log, counters, cap=65536):
    try:
        chunk = os.read(pipe.fileno(), 65536)
    except BlockingIOError:
        return None
    counters['bytes_seen'] += len(chunk)
    keep = chunk[:max(0, cap - counters['bytes_kept'])]
    log.write(keep)
    counters['bytes_kept'] += len(keep)
    counters['truncated'] = counters['bytes_seen'] > counters['bytes_kept']
    return bool(chunk)


def cleanup_terminal(job, env):
    status = terminal_proof(job, env)
    if not (job / 'scratch').exists() and not (job / 'scratch').is_symlink() and not (job / 'environment.json').exists():
        return {'already_clean': True}
    _, intent = base.load_job(job)
    if intent['config']['kind'] == 'build' and intent['config']['limits']['runtime_seconds'] == 'infinity':
        return {'retained': 'progress-driven build scratch requires separate retention authority',
                'generated_storage_cleanup_pending': True, 'terminal_verified': True}
    ready = base.read(job / 'ready.json')
    job_identity = {'invocation_id': ready['invocation_id'], 'process': ready['process']}
    inventory = retention.inventory(job / 'scratch', max_entries=inventory_budget(intent['config']['estimates']))
    if not inventory['complete']:
        return {'retained': 'scratch inventory incomplete'}
    rows = [{**row, 'path': 'scratch/' + row['path']} for row in inventory['rows']]
    for path in (job / 'scratch', job / 'environment.json'):
        if path.exists():
            info = path.lstat()
            rows.append({'path': path.name, 'identity': retention.identity(info), 'kind': 'directory' if path.is_dir() else 'file', 'nlink': info.st_nlink})
    for row in rows:
        row.update(owner=intent['config']['owner'], job_id=intent['job_id'], job_identity=job_identity,
                   unique=False, source=False, data=False, rollback=False, active=False, reproducible=True,
                   disposable=True, usefulness='terminal-scratch', dependencies=[], family_baseline=False,
                   reference_state='resolved', refpaths=[], terminal=status['state'], terminal_verified=True)
    refs = lambda: {'complete': True, 'paths': []} if terminal_proof(job, env) else None
    def proof(artifact):
        current = terminal_proof(job, env)
        return {'identity_verified': True, 'state': current['state'], 'quiescent': True, 'job_id': intent['job_id'], 'job_identity': job_identity}
    manifest = {'root': str(job), 'root_identity': retention.identity(job.stat()), 'owner': intent['config']['owner'], 'artifacts': rows}
    result = retention.cleanup(retention.plan_cleanup(manifest, live_refs=refs()), proof, refs)
    base.publish(job / ('retention-' + uuid.uuid4().hex + '.json'), result)
    return result


def stop(job):
    job, intent = base.load_job(job)
    need(intent.get('contract_version') == 2 and intent['candidate'] == str(HERE), 'candidate_mismatch')
    bind_context(intent.get('project_root'), intent['state_dir'], intent_project_binding(intent)['project_id'])
    job = project_job(job)
    base.pins(intent['source_pins'])
    with locked(intent['state_dir']) as state:
        ledger = read_ledger(state)
        record = ledger['jobs'].get(intent['job_id'])
        need(type(record) is dict and record.get('job') == str(job), 'job_ledger_mismatch')
        if not (job / 'ready.json').exists():
            need(record['state'] == 'ready' and not (job / 'launch-intent.json').exists(), 'queued_launch_unknown')
            record['state'] = 'cancelled-before-launch'
            base.publish(job / 'stop-intent.json', {'job_id': intent['job_id'], 'before_launch': True})
            replace(state / 'capacity.json', ledger)
            environment = job / 'environment.json'
            if environment.exists():
                base.read_bytes(environment)
                environment.unlink()
            return {'state': 'CANCELLED', 'job_id': intent['job_id'], 'unit_started': False}
    env = base.manager_environment()[0]
    info = base.unit_show(intent['unit'], env)
    ready = base.custody(job, intent, info)
    if info.get('MainPID') != '0':
        base.signal_members(base.service_cgroup(ready), signal.SIGCONT, intent.get('topology') == base.TOPOLOGY)
    result = base.stop(job)
    terminal_proof(job, env)
    with locked(intent['state_dir']) as state:
        ledger = read_ledger(state)
        record = ledger['jobs'].get(intent['job_id'])
        need(type(record) is dict and record.get('job') == str(job), 'job_ledger_mismatch')
        record['state'] = 'terminal'
        replace(state / 'capacity.json', ledger)
    result['retention'] = cleanup_terminal(job, env)
    return result


def start_helper(intent, env):
    reader, gate = os.pipe()
    try:
        pid = os.fork()
    except BaseException:
        os.close(reader); os.close(gate)
        raise
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.close(gate)
        try:
            approved = os.read(reader, 1) == b'1'
            os.close(reader)
            if not approved: os._exit(0)
            with locked(intent['state_dir'], nonblocking=True) as state:
                reconcile(state, env)
        except BlockingIOError:
            os._exit(0)
        except BaseException:
            os._exit(1)
        os._exit(0)
    fd = None
    try:
        os.close(reader); reader = None
        fd = os.pidfd_open(pid)
        identity = base.process(pid)
        need(identity['uid'] == os.getuid() and identity['cgroup'] == base.process(os.getpid())['cgroup'], 'helper_identity')
        return {'process': identity, 'pidfd': fd, 'gate': gate}
    except BaseException as error:
        # EOF prevents any reconciliation until ownership has reached the main loop.
        os.close(gate)
        os.waitpid(pid, 0)
        if fd is not None: os.close(fd)
        raise base.Refused('helper_handoff_refused_before_reconcile_child_reaped') from error
    finally:
        if reader is not None: os.close(reader)

def release_helper(helper):
    gate = helper.pop('gate')
    try:
        need(os.write(gate, b'1') == 1, 'helper_release')
    finally:
        os.close(gate)

def poll_helper(helper, counters):
    if helper is None: return None
    pid, status = os.waitpid(helper['process']['pid'], os.WNOHANG)
    if not pid: return helper
    os.close(helper['pidfd'])
    counters['helpers_reaped'] = counters.get('helpers_reaped', 0) + 1
    counters['helper_last_status'] = os.waitstatus_to_exitcode(status)
    return None

def finish_helper(helper, management, counters):
    # Reap the writer before final receipts; interrupted physical writes remain uncertain.
    outcome = {'quiescent': False, 'interrupted': helper is not None}
    try:
        if helper is not None:
            if 'gate' in helper: os.close(helper.pop('gate'))
            helper = poll_helper(helper, counters)
            outcome['interrupted'] = helper is not None
            if helper is not None:
                try:
                    current = base.process(helper['process']['pid'])
                except (FileNotFoundError, ProcessLookupError):
                    helper = poll_helper(helper, counters)
                    if helper is not None: raise
                    outcome['interrupted'] = False
                else:
                    need(current == helper['process'], 'helper_identity')
        cleanup = base.cleanup_members(management, None, True)
        outcome.update(cleanup)
        if helper is not None:
            # cleanup_members reaps all adopted children, including this exact helper.
            try:
                os.waitpid(helper['process']['pid'], os.WNOHANG)
            except ChildProcessError: pass
            os.close(helper['pidfd'])
            counters['helpers_reaped'] = counters.get('helpers_reaped', 0) + 1
        outcome['allocation_commit'] = 'uncertain' if outcome['interrupted'] else 'no_writer_outstanding'
    except Exception as error:
        outcome['error'] = str(error)
    return outcome


def worker(job):
    os.umask(0o077)
    job, intent = base.load_job(job)
    need(intent.get('contract_version') == 2 and intent['candidate'] == str(HERE), 'worker_candidate')
    global LEGACY_PROJECT_BINDING
    legacy_raw = os.environ.get('PROJECT_TASK_LEGACY_BINDING')
    if legacy_raw is not None:
        try:
            legacy_value = json.loads(legacy_raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise base.Refused('legacy_project_binding_environment') from error
        need(type(legacy_value) is dict and json.dumps(
            legacy_value, sort_keys=True, separators=(',', ':')) == legacy_raw,
             'legacy_project_binding_environment')
        LEGACY_PROJECT_BINDING = validate_project_binding(legacy_value, check_context=False)
    binding = intent_project_binding(intent)
    bind_context(intent.get('project_root'), intent['state_dir'], binding['project_id'])
    job = project_job(job)
    env = base.read(job / 'environment.json')
    c = intent['config']
    child = None; first = None; code = None; cancelled = False; cancelled_at = None; helper = None; birth = None; placement_error = None
    def cancel(signum, frame):
        nonlocal cancelled, cancelled_at
        cancelled = True
        cancelled_at = cancelled_at or time.monotonic()
    signal.signal(signal.SIGTERM, cancel); signal.signal(signal.SIGINT, cancel)
    cg = base.process(os.getpid())['cgroup']
    delegated = intent.get('topology') == base.TOPOLOGY
    need(cg.endswith('/' + intent['unit'] + ('/management' if delegated else '')), 'supervisor_cgroup')
    info = base.unit_show(intent['unit'], env)
    need(info.get('MainPID') == str(os.getpid()) and base.management_cgroup(intent, info.get('ControlGroup', '')) == cg and info.get('InvocationID') == os.environ.get('INVOCATION_ID') and bool(info.get('InvocationID')) and info.get('Description') == binding['unit_prefix'] + ' ' + intent['job_id'], 'supervisor_unit_identity')
    counters = {'bytes_seen': 0, 'bytes_kept': 0, 'truncated': False}
    try:
        need(ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0, 'subreaper')
        need(all(os.environ.get(k) == v for k, v in env.items()), 'unit_environment')
        topology = base.setup_topology(intent, info) if delegated else None
        service = info['ControlGroup']
        ready = {'job_id': intent['job_id'], 'intent_sha256': base.digest(base.read_bytes(job / 'intent.json')), 'process': base.process(os.getpid()), 'invocation_id': os.environ['INVOCATION_ID'], 'address_space': list(resource.getrlimit(resource.RLIMIT_AS)), 'process_limit': list(resource.getrlimit(resource.RLIMIT_NPROC)), 'environment_sha256': base.digest(base.canonical(env))}
        if delegated: ready['topology'] = topology
        base.publish(job / 'ready.json', ready)
        deadline = time.monotonic() + 6
        while not (job / 'admission.json').exists():
            base.admission_wait_ok(job, cancelled, deadline); time.sleep(.025)
        base.admission_wait_ok(job, cancelled, deadline)
        need(base.read(job / 'admission.json') == {'intent_sha256': ready['intent_sha256'], 'invocation_id': ready['invocation_id']}, 'admission_identity')
        base.pins(intent['source_pins']); base.pins(c['sources']); need(not cancelled, 'cancelled_before_payload')
        payload_env = dict(env, **payload_environment(intent, job))
        stdin = base.open_input(c['stdin_file'], intent['stdin_identity'])[0] if c.get('stdin_file') else subprocess.DEVNULL
        with (job / 'payload.log').open('xb', buffering=0) as log:
            try:
                child = subprocess.Popen(c['argv'], cwd=c['cwd'], env=payload_env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=(lambda: base.enter_workload(topology['workload'])) if delegated else None)
            finally:
                if stdin != subprocess.DEVNULL: stdin.close()
            try: birth = base.process(child.pid)
            except (FileNotFoundError, ProcessLookupError): birth = None
            if delegated and birth is not None:
                need(birth['cgroup'] == topology['workload'], 'payload_placement')
            base.publish(job / 'payload-start.json', {'pid': child.pid, 'process': birth, 'identity_sampled': birth is not None})
            os.set_blocking(child.stdout.fileno(), False)
            next_tick = time.monotonic() + 1
            while child.poll() is None:
                drain(child.stdout, log, counters)
                helper = poll_helper(helper, counters)
                if delegated and birth is not None:
                    try:
                        base.placement_check(topology, ready['process'], helper['process'] if helper else None, birth)
                    except Exception as error:
                        placement_error = str(error)
                        raise
                if cancelled:
                    base.signal_members(service, signal.SIGCONT, delegated)
                    base.signal_members(service, signal.SIGTERM if time.monotonic() - cancelled_at < .5 else signal.SIGKILL, delegated)
                elif time.monotonic() >= next_tick:
                    if helper is None:
                        helper = start_helper(intent, env)
                        counters['helpers_started'] = counters.get('helpers_started', 0) + 1
                        counters['helper_births'] = (counters.get('helper_births', []) + [helper['process']])[-64:]
                        release_helper(helper)
                    next_tick = time.monotonic() + 1
                time.sleep(.025)
            code = child.wait()
            if code != 0: first = {'phase': 'payload', 'code': 'exit', 'returncode': code}
            base.publish(job / 'payload-exit.json', {'job_id': intent['job_id'], 'intent_sha256': ready['intent_sha256'], 'returncode': code, 'first_failure': first})
            counters['stream_eof'] = False
            for _ in range(16):
                drained = drain(child.stdout, log, counters)
                if drained is False: counters['stream_eof'] = True
                if drained is not True: break
            child.stdout.close()
    except Exception as error:
        if first is None: first = {'phase': 'supervisor', 'code': str(error) if isinstance(error, base.Refused) else type(error).__name__}
        if not (job / 'first-failure.json').exists(): base.publish(job / 'first-failure.json', first)
    if delegated and birth is not None:
        try:
            base.placement_check(topology, ready['process'], helper['process'] if helper else None, birth)
        except Exception as error:
            placement_error = str(error)
            if first is None: first = {'phase': 'custody', 'code': placement_error}
    helper_cleanup = finish_helper(helper, cg, counters)
    cleanup = base.cleanup_members(info['ControlGroup'] if delegated else cg, child, delegated, birth)
    if placement_error is not None:
        cleanup['placement_error'] = placement_error
        cleanup['quiescent'] = False
    cleanup['management_helper'] = helper_cleanup
    cleanup['quiescent'] = cleanup['quiescent'] and helper_cleanup['quiescent']
    if delegated:
        try:
            cleanup['physical_readback'] = {name: (base.cgdir(topology['workload']) / name).read_text().strip() for name in ('cpu.max',)}
            cleanup['physical_readback']['ancestor_cpu_max'] = (base.cgdir(info['ControlGroup']) / 'cpu.max').read_text().strip()
        except Exception as error:
            cleanup['physical_readback_error'] = type(error).__name__
            cleanup['quiescent'] = False
    base.publish(job / 'output.json', counters)
    base.publish(job / 'cleanup.json', cleanup)
    base.publish(job / 'result.json', base.result_record(intent, base.digest(base.read_bytes(job / 'intent.json')), os.environ.get('INVOCATION_ID'), code, first, cancelled, cleanup, (job / 'stop-intent.json').exists()))
    return 0


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == '_worker': return worker(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in ('run', 'status', 'stop'):
        p = sub.add_parser(name); p.add_argument('--job', required=True)
        if name == 'run':
            p.add_argument('--config', required=True); p.add_argument('--state', required=True)
            p.add_argument('--project-root', help='existing project root (default: invocation directory)')
    args = parser.parse_args()
    try:
        result = launch(args.job, args.config, args.state, args.project_root) if args.operation == 'run' else stop(args.job) if args.operation == 'stop' else advance(args.job)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception as error:
        print(json.dumps({'state': 'REFUSED', 'error': str(error) if isinstance(error, base.Refused) else type(error).__name__})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
