"""Read-only exact held-promise admission; no controller imports or release operations."""
import hashlib
import importlib.util
import json
import math
import re
import os
import resource
from pathlib import Path, PurePosixPath
import time

HERE = Path(__file__).resolve().parent
TERMINAL = {'terminal', 'failed-before-launch', 'cancelled-before-launch'}


# In-process continuity only; no writable registry or controller hook.
_seen_grants = {}


def unlimited(limits):
    return (type(limits) is dict
            and set(limits) == {'memory_bytes', 'cpu_percent', 'address_space_bytes', 'runtime_seconds'}
            and all(type(value) is str and value == 'infinity' for value in limits.values()))


def unlimited_budget_check(intent, ready, info, api):
    actual = api.budget_check(intent, {**ready, 'process': {**ready['process'], 'cgroup': api.service_cgroup(ready)}}, info)
    if intent.get('topology') == api.TOPOLOGY:
        api.topology_check(intent, ready, info)
    return actual


def promise_memory(binding, approved, api):
    if unlimited(approved['limits']):
        api.need(binding.get('cap_bytes') in (None, 'infinity'), 'grant_cap_drift')
        forecast = approved['estimates']['memory_bytes'] if 'estimates' in approved else binding.get('memory_forecast_bytes')
        if type(forecast) is not int:
            raise ValueError('invalid_counter_promise_memory')
        return checked_counter(forecast, 'promise_memory')
    api.need(approved['limits']['memory_bytes'] == binding['cap_bytes'], 'grant_cap_drift')
    return checked_counter(binding['cap_bytes'], 'promise_memory')


def granted_growth(binding, ledger, api, env):
    for path, expected in binding['control_pins'].items():
        api.need(hashlib.sha256(api.read_bytes(Path(path), private=False)).hexdigest() == expected,
                 'grant_control_pin_drift')
    config_raw = api.read_bytes(Path(binding['config_path']), private=False)
    api.need(hashlib.sha256(config_raw).hexdigest() == binding['config_sha256'], 'grant_config_pin_drift')
    approved = json.loads(config_raw)
    promised = promise_memory(binding, approved, api)
    job = Path(binding['job'])
    matches = [(key, row) for key, row in ledger['jobs'].items() if row.get('job') == str(job)]
    api.need(len(matches) <= 1, 'grant_duplicate_ledger_job')
    if not (job / 'intent.json').exists():
        api.need(not matches and not binding.get('intent_sha256'), 'grant_missing_bound_intent')
        return {'job': str(job), 'state': 'PENDING_ROOT_GRANTED', 'remaining_bytes': promised,
                'current_charged_bytes': 0, 'file_allowance_bytes': 0, 'cap_bytes': approved['limits']['memory_bytes'], 'owner': binding['owner']}, None
    job, intent = api.load_job(job)
    ihash = hashlib.sha256(api.read_bytes(job / 'intent.json')).hexdigest()
    api.need(intent['config'] == approved and intent['config_sha256'] == binding['config_sha256']
             and intent['source_pins'] == binding['intent_source_pins']
             and ihash == binding.get('intent_sha256', ihash), 'grant_intent_drift')
    api.pins(binding['intent_source_pins'])
    if binding['ledger_expected']:
        api.need(len(matches) == 1, 'grant_ledger_missing')
        key, row = matches[0]
        api.need(key == intent['job_id'] and row['owner'] == binding['owner']
                 and row['lease'] == binding['lease'] and row['source_pins'] == intent['source_pins']
                 and row['estimates'] == approved['estimates'], 'grant_ledger_identity_drift')
        api.need(all(type(row.get(field)) is int and 0 <= row[field] <= limit for field, limit in
                     [('remaining_memory_bytes', promised),
                      ('remaining_disk_bytes', binding['disk_bytes']), ('remaining_inodes', binding['inodes'])]),
                 'grant_promise_exceeds_cap')
    else:
        api.need(not matches, 'unexpected_native_ledger_record')
    ready = api.read(job / 'ready.json') if (job / 'ready.json').exists() else None
    rhash = hashlib.sha256(api.read_bytes(job / 'ready.json')).hexdigest() if ready else None
    api.need(not binding.get('ready') or (binding['ready'] == ready and binding['ready_sha256'] == rhash),
             'grant_ready_drift')
    seen = _seen_grants.get(str(job))
    api.need(seen is None or (seen[0] == ihash and (seen[1] is None or seen[1] == rhash)),
             'grant_continuity_drift')
    if ready:
        api.need(ready['job_id'] == intent['job_id'] and ready['intent_sha256'] == ihash,
                 'grant_ready_binding_drift')
    _seen_grants[str(job)] = (ihash, rhash)
    info = api.unit_show(intent['unit'], env)
    charged = 0
    file_allowance = 0
    allowed = None
    if info.get('LoadState') != 'not-found' and ready:
        api.need(api.custody(job, intent, info) == ready, 'grant_custody_drift')
        allowed = intent['unit']
    if info.get('ActiveState') == 'active' and info.get('SubState') == 'running':
        api.need(ready is not None and int(info.get('MainPID', '0')) > 0, 'grant_live_missing_custody')
        cg = api.cgdir(api.service_cgroup(ready))
        def caps(current):
            if unlimited(approved['limits']):
                unlimited_budget_check(intent, ready, current, api)
                return
            quota, period = map(int, (cg / 'cpu.max').read_text().split())
            api.need(quota > 0 and period > 0 and quota * 100 <= period * approved['limits']['cpu_percent']
                     and cpu_quota_seconds(api, current['CPUQuotaPerSecUSec']) <= approved['limits']['cpu_percent'] / 100,
                     'grant_cpu_cap_drift')
            api.need(current['MemoryMax'] == str(binding['cap_bytes']) and current['MemorySwapMax'] == '0'
                     and (cg / 'memory.max').read_text().strip() == str(binding['cap_bytes'])
                     and (cg / 'memory.swap.max').read_text().strip() == '0'
                     and runtime_limit_matches(api, current['RuntimeMaxUSec'], approved['limits']['runtime_seconds'])
                     and current['KillMode'] == 'mixed' and current['Restart'] == 'no', 'grant_physical_cap_drift')
        caps(info)
        charged, file_allowance = charged_and_cache(cg)
        after = api.unit_show(intent['unit'], env)
        api.need(charged >= 0 and after.get('ActiveState') == 'active' and after.get('SubState') == 'running'
                 and int(after.get('MainPID', '0')) > 0 and api.custody(job, intent, after) == ready,
                 'grant_postmemory_custody_drift')
        caps(after)
        state = 'LIVE_ROOT_GRANTED'
    else:
        state = 'KNOWN_NONRUNNING_FULL_HOLD'
    return {'job': str(job), 'job_id': intent['job_id'], 'state': state,
            'remaining_bytes': max(0, promised - charged), 'current_charged_bytes': charged,
            'file_allowance_bytes': file_allowance,
            'intent_sha256': ihash, 'ready_sha256': rhash, 'ready': ready,
            'cap_bytes': approved['limits']['memory_bytes'], 'owner': binding['owner'], 'unit': info,
            'canonical': api.public_status(job, env) if state == 'KNOWN_NONRUNNING_FULL_HOLD' else None}, allowed


def checked_counter(value, name):
    if type(value) is str:
        value = value.strip()
        if not value or len(value) > 20 or not value.isascii() or not value.isdecimal():
            raise ValueError('invalid_counter_' + name)
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2**64 - 1:
        raise ValueError('invalid_counter_' + name)
    return value


def checked_file_allowance(charged, file_bytes, shmem_bytes):
    charged = checked_counter(charged, 'charged')
    file_bytes = checked_counter(file_bytes, 'file')
    shmem_bytes = checked_counter(shmem_bytes, 'shmem')
    if shmem_bytes > file_bytes or file_bytes > charged:
        raise ValueError('inconsistent_file_shmem_counters')
    return max(0, file_bytes - shmem_bytes)


def memory_stat_counters(raw):
    counters = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in counters:
            raise ValueError('invalid_memory_stat_shape')
        counters[fields[0]] = fields[1]
    if not {'file', 'shmem'} <= counters.keys():
        raise ValueError('missing_file_shmem_counter')
    return counters


def cpu_quota_seconds(api, value):
    """CPU quota timespans accept subsecond units; lifetime parser remains strict."""
    api.need(type(value) is str and 0 < len(value) <= 256, 'cpu_manager_timespan')
    if value.isascii() and value.isdecimal():
        total = int(value) / 1000000
    else:
        units = {'h': 3600, 'min': 60, 's': 1, 'ms': .001, 'us': .000001, 'µs': .000001, 'μs': .000001}
        total = 0; previous = float('inf')
        for part in value.split():
            match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(h|min|ms|us|µs|μs|s)', part)
            api.need(match is not None and units[match[2]] < previous, 'cpu_manager_timespan')
            previous = units[match[2]]
            total += float(match[1]) * previous
    api.need(math.isfinite(total) and total > 0, 'cpu_manager_timespan')
    return total


def runtime_limit_matches(api, actual, seconds):
    if seconds == 'infinity':
        return actual == 'infinity'
    api.need(type(seconds) is int and seconds >= 10, 'runtime_budget')
    return api.runtime_microseconds(actual) == seconds * 1000000


def own_budget_check(config, intent, ready, info, api):
    if unlimited(intent['config']['limits']):
        return unlimited_budget_check(intent, ready, info, api)
    if not config.get('own_adaptive_cpu', False):
        return api.budget_check(intent, ready, info)
    limit = intent['config']['limits']
    cg = api.cgdir(api.service_cgroup(ready))
    api.need(cg is not None, 'missing_cgroup')
    api.need((cg / 'memory.max').read_text().strip() == str(limit['memory_bytes'])
             and (cg / 'memory.swap.max').read_text().strip() == '0', 'physical_budget')
    quota, period = map(int, (cg / 'cpu.max').read_text().split())
    manager_cpu = cpu_quota_seconds(api, info['CPUQuotaPerSecUSec'])
    api.need(quota > 0 and period > 0 and quota * 100 <= period * limit['cpu_percent']
             and 0 < manager_cpu <= limit['cpu_percent'] / 100, 'cpu_budget')
    as_value = resource.RLIM_INFINITY if limit['address_space_bytes'] == 'infinity' else limit['address_space_bytes']
    wanted_as = 'infinity' if as_value == resource.RLIM_INFINITY else str(as_value)
    api.need(ready['address_space'] == [as_value, as_value], 'address_space_actual')
    api.need(info['LimitAS'] == info['LimitASSoft'] == wanted_as, 'address_space_manager')
    api.need(runtime_limit_matches(api, info['RuntimeMaxUSec'], limit['runtime_seconds']),
             'runtime_manager')
    api.need(info['MemoryMax'] == str(limit['memory_bytes']) and info['MemorySwapMax'] == '0'
             and info['KillMode'] == 'mixed' and info['Restart'] == 'no', 'manager_budget')


def charged_and_cache(cg):
    charged = checked_counter((cg / 'memory.current').read_text(), 'charged')
    stat = memory_stat_counters((cg / 'memory.stat').read_text())
    return charged, checked_file_allowance(charged, stat['file'], stat['shmem'])


def canonical_reader(config, api):
    if api is None:
        spec = importlib.util.spec_from_file_location('r28_existing_canonical_reader', config['runner'])
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
    return api


class AppContractError(RuntimeError):
    def __init__(self, code, observation):
        super().__init__(code)
        self.code = code
        self.observation = observation


def contract_need(ok, code, observation=None):
    if not ok:
        raise AppContractError(code, observation or {})


def observed_limit(raw):
    if raw in ('max', 'infinity'):
        return raw
    try:
        return checked_counter(raw, 'contract_limit')
    except ValueError:
        return 'invalid'


def event_observation(baseline, current):
    keys = ('low', 'high', 'max', 'oom', 'oom_kill', 'oom_group_kill', 'sock_throttled')
    before = {key: baseline[key] for key in keys if key in baseline}
    after = {key: current[key] for key in keys if key in current}
    return {'baseline_events': before, 'current_events': after,
            'event_deltas': {key: after[key] - before[key] for key in before.keys() & after.keys()}}


def app_contract_state(config, job_parents, api, env):
    app = config['app']
    def canonical_path(value):
        contract_need(type(value) is str and value.startswith('/')
                 and not value.startswith('//') and len(value) <= 512 and value.isascii()
                 and str(PurePosixPath(value)) == value and '..' not in PurePosixPath(value).parts,
                 'app_contract_path')
        return PurePosixPath(value)
    app_path = canonical_path(app['cgroup'])
    jobs = {config['own_job']} | {binding['job'] for binding in config.get('granted', [])}
    contract_need(set(job_parents) == jobs,
             'app_contract_job_coverage')
    seen_paths = {app_path}
    for job, value in job_parents.items():
        parent = canonical_path(value)
        contract_need(parent == app_path.parent, 'app_contract_topology', {'topology_valid': False})
        if (Path(job) / 'intent.json').exists():
            directory, intent = api.load_job(Path(job))
            unit = intent['unit']
            contract_need(type(unit) is str and unit not in ('', '.', '..') and '/' not in unit,
                     'app_contract_job_topology')
            path = parent / unit
            contract_need(path not in seen_paths, 'app_contract_job_topology', {'topology_valid': False})
            seen_paths.add(path)
            if (directory / 'ready.json').exists():
                contract_need(api.service_cgroup(api.read(directory / 'ready.json')) == str(path),
                         'app_contract_job_topology', {'topology_valid': False})
    app_cg = api.cgdir(app['cgroup'])
    cgroot = app_cg
    for _ in app_path.parts[1:]:
        cgroot = cgroot.parent
    contract_need(cgroot.resolve() == cgroot, 'app_contract_path')
    def directory(path):
        result = cgroot.joinpath(*path.parts[1:])
        contract_need(result.resolve() == result and result.is_dir(), 'app_contract_path')
        return result
    directory(app_path)
    info = api.unit_show(app['unit'], env)
    contract_need(info.get('ActiveState') == 'active' and info.get('SubState') == 'running'
             and info.get('InvocationID') == app['invocation_id']
             and info.get('MainPID') == str(app['main']['pid'])
             and info.get('ControlGroup') == app['cgroup']
             and info.get('MemoryMax') == ('infinity' if app['cap_bytes'] == 'infinity' else str(app['cap_bytes']))
             and api.process(app['main']['pid']) == app['main']
             and api.process(app['backend']['pid']) == app['backend'], 'app_identity_drift')
    actual_cap = (app_cg / 'memory.max').read_text().strip()
    contract_need(actual_cap == ('max' if app['cap_bytes'] == 'infinity' else str(app['cap_bytes'])), 'app_cap_drift',
                  {'app_memory_max': observed_limit(actual_cap), 'app_memory_cap_bytes': app['cap_bytes']})
    high = api.command(['systemctl', '--user', 'show', app['unit'], '--property=MemoryHigh',
                        '--value', '--no-pager'], env)
    try:
        cg_high = (app_cg / 'memory.high').read_text().strip()
    except FileNotFoundError:
        cg_high = 'missing'
    contract_need(high.returncode == 0 and high.stdout.strip() == 'infinity'
             and cg_high == 'max', 'app_contract_high',
             {'manager_memory_high': observed_limit(high.stdout.strip()) if high.returncode == 0 else 'missing',
              'cgroup_memory_high': 'missing' if cg_high == 'missing' else observed_limit(cg_high)})
    events = {}
    for line in (app_cg / 'memory.events').read_text().splitlines():
        pair = line.split()
        contract_need(len(pair) == 2 and pair[0] not in events, 'app_contract_events_shape')
        events[pair[0]] = checked_counter(pair[1], 'app_event')
    critical = {'high', 'max', 'oom', 'oom_kill', 'oom_group_kill'}
    contract_need(critical | {'low'} <= events.keys(), 'app_contract_events_shape')
    ancestors = {}
    for path in reversed(app_path.parents):
        caps = {}
        for name in ('memory.max', 'memory.high'):
            file = directory(path) / name
            if not file.exists():
                contract_need(str(path) == '/', 'app_contract_ancestor_interface',
                              {'ancestor_path': str(path), 'ancestor_' + name.replace('.', '_'): 'missing'})
                caps[name] = None
            else:
                raw = file.read_text().strip()
                contract_need(raw == 'max', 'unsupported_finite_ancestor',
                              {'ancestor_path': str(path), 'ancestor_' + name.replace('.', '_'): observed_limit(raw)})
                caps[name] = raw
        ancestors[str(path)] = caps
    return {'app_cgroup': str(app_path), 'job_parents': dict(job_parents),
            'events': events, 'ancestor_caps': ancestors}


def prepare_app_baseline(config, job_parents, api=None):
    """Read-only baseline; caller must freeze returned fields in the successor config."""
    api = canonical_reader(config, api)
    env = api.manager_environment()[0]
    first = app_contract_state(config, job_parents, api, env)
    second = app_contract_state(config, job_parents, api, env)
    check_app_baseline(first, second, api)
    return second


def check_app_baseline(baseline, current, api):
    for value in baseline['events'].values():
        checked_counter(value, 'baseline_app_event')
    witness = event_observation(baseline['events'], current['events'])
    contract_need(all(current[key] == baseline[key] for key in ('app_cgroup', 'job_parents', 'ancestor_caps')),
             'app_contract_ancestor_or_topology_drift', {'topology_valid': False})
    contract_need(current['events'].keys() == baseline['events'].keys(), 'app_contract_events_keys', witness)
    deltas = {}
    for key, value in current['events'].items():
        initial = checked_counter(baseline['events'][key], 'baseline_app_event')
        contract_need(value >= initial, 'app_contract_event_reset', witness)
        deltas[key] = value - initial
    contract_need(all(deltas[key] == 0 for key in ('high', 'max', 'oom', 'oom_kill', 'oom_group_kill')),
             'app_contract_pressure_event', witness)
    return deltas


def compute_release(binding, ledger, api, env):
    """Opt-in V1 consumer proposal. Requires an actual separately pinned Root acceptance."""
    def pinned(descriptor):
        raw = api.read_bytes(Path(descriptor['path']), private=False)
        api.need(hashlib.sha256(raw).hexdigest() == descriptor['sha256'], 'compute_release_pin_drift')
        return json.loads(raw)
    proof = pinned(binding['proof'])
    root = pinned(binding['root_acceptance'])
    identity = binding['identity']
    api.need(root['state'] == 'ROOT_ACCEPTED_COMPUTE_CAPACITY_RELEASE_V1'
             and root['proof'] == binding['proof'] and root['binding'] == identity
             and proof['state'] == 'COMPUTE_CAPACITY_RELEASED_STORAGE_GC_PENDING_V1'
             and proof['binding'] == identity, 'compute_release_authority_drift')
    row = ledger['jobs'].get(identity['job_id'])
    api.need(row is not None and row['state'] == 'terminal'
             and all(type(row.get(key)) is int and row[key] == 0 for key in
                     ('remaining_memory_bytes', 'remaining_disk_bytes', 'remaining_inodes')),
             'compute_release_row_not_zero')
    def digest(value):
        return type(value) is str and len(value) == 64 and all(char in '0123456789abcdef' for char in value)
    row_hash = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    api.need(row_hash == proof['target_after_sha256'] == root['target_after_sha256']
             and proof['target_cas_verified'] is True
             and digest(proof['target_before_sha256']) and digest(proof['foreign_before_sha256'])
             and proof['foreign_before_sha256'] == proof['foreign_after_sha256']
             and type(proof['foreign_count']) is int and proof['foreign_count'] >= 0
             and proof['foreign_unchanged'] is True, 'compute_release_cas_drift')
    job, intent = api.load_job(Path(identity['job']))
    ready = pinned(binding['ready'])
    result = pinned(binding['original_result'])
    caller = pinned(binding['caller_terminal'])
    api.need(binding['ready']['path'] == str(job / 'ready.json')
             and binding['original_result']['path'] == str(job / 'result.json')
             and binding['caller_terminal']['path'] == str(job / 'caller-provider-terminal.json')
             and caller == {'job_id': identity['job_id'], 'cleanup_complete': False}
             and hashlib.sha256(api.read_bytes(job / 'intent.json')).hexdigest() == binding['intent_sha256']
             and row['job'] == str(job) and row['owner'] == intent['config']['owner'] == identity['owner']
             and row['lease'] == intent['config']['lease'] == identity['lease']
             and row['source_pins'] == intent['source_pins'] == binding['source_pins']
             and row['estimates'] == intent['config']['estimates']
             and intent['job_id'] == ready['job_id'] == result['job_id'] == identity['job_id']
             and intent['unit'] == identity['unit']
             and ready['invocation_id'] == result['invocation_id'] == identity['invocation_id']
             and ready['intent_sha256'] == result['intent_sha256'] == binding['intent_sha256']
             and ready['process']['cgroup'] == identity['cgroup']
             and result['state'] == 'FAILED'
             and result['cleanup'] == proof['original_canonical_cleanup']
             and proof['original_result'] == binding['original_result']
             and proof['caller_terminal'] == binding['caller_terminal']
             and proof['caller_cleanup_complete'] is False, 'compute_release_identity_drift')
    api.pins(binding['source_pins'])
    census = proof['full_reference_census']
    api.need(census['complete'] is True and census['loss'] is False and census['truncated'] is False
             and all(type(census[key]) is int and census[key] == 0 for key in ('gap_count', 'unreadable'))
             and type(census['entries']) is int and census['entries'] > 0
             and proof['parents_reaped'] is True and proof['transport_reaped'] is True
             and proof['permanent_owner_drop_verified'] is True
             and proof['owner_uid'] == intent['uid']
             and proof['unit_absent'] is True and proof['cgroup_absent'] is True,
             'compute_release_reference_incomplete')
    pinned(binding['partial_inventory'])
    roots = proof['generated_roots']
    api.need(proof['generated_storage_cleanup_pending'] is True and len(roots) == 5
             and len(set(roots)) == 5 and roots == binding['generated_roots']
             and all(type(path) is str and Path(path).is_absolute()
                     and str(Path(path)) == path and '..' not in Path(path).parts
                     and Path(path).is_relative_to(job / 'scratch') for path in roots)
             and proof['partial_inventory'] == binding['partial_inventory'], 'compute_release_storage_metadata_drift')
    births = proof['recorded_births']
    api.need(proof['recorded_birth_coverage_complete'] is True
             and births == binding['recorded_births'] and len(births) > 0
             and all(type(item['pid']) is int and item['pid'] > 0
                     and type(item['start_ticks']) is int and item['start_ticks'] > 0
                     and item['original_birth_absent'] is True for item in births),
             'compute_release_birth_coverage_drift')
    for _ in range(2):
        info = api.unit_show(identity['unit'], env)
        api.need(info.get('LoadState') == 'not-found' and not api.cgdir(identity['cgroup']).exists(),
                 'compute_release_unit_or_cgroup_present')
        for birth in births:
            try:
                current = api.process(birth['pid'])
            except (FileNotFoundError, ProcessLookupError):
                current = None
            api.need(current is None or int(current['start_ticks']) != birth['start_ticks'],
                     'compute_release_birth_present')
    return {'job_id': identity['job_id'], 'future_memory_bytes': 0, 'future_disk_bytes': 0,
            'future_inodes': 0, 'generated_storage_cleanup_pending': True,
            'generated_roots': roots, 'partial_inventory': proof['partial_inventory'],
            'root_acceptance': binding['root_acceptance'], 'proof': binding['proof']}


def snapshot(config=None, api=None, metrics=None):
    if config is None:
        raise ValueError('project_config_required')
    api = canonical_reader(config, api)
    for path, expected in config.get('authority_pins', {}).items():
        api.need(hashlib.sha256(api.read_bytes(Path(path), private=False)).hexdigest() == expected, 'authority_pin_drift')
    ledger = api.read(Path(config['ledger']))
    for ident, expected in config.get('released_rows', {}).items():
        row = ledger['jobs'].get(ident)
        api.need(row is not None and all(row.get(key) == value for key, value in expected.items())
                 and all(type(row.get(key)) is int and row[key] == 0 for key in
                         ('remaining_memory_bytes','remaining_disk_bytes','remaining_inodes')), 'released_row_drift')
    api.need(ledger['version'] == 3, 'ledger_version')
    own_binding = config.get('own_binding')
    if own_binding:
        raw = api.read_bytes(Path(own_binding['config_path']), private=False)
        api.need(hashlib.sha256(raw).hexdigest() == own_binding['config_sha256'], 'own_config_pin_drift')
        own_approved = json.loads(raw)
        api.need(own_approved['limits'] == config['own_limits'], 'own_config_limits_drift')
    held = {row['job_id']: row for row in config['held']}
    api.need(len(held) == len(config['held']), 'duplicate_held_binding')
    api.need(all(key in held or row['state'] in TERMINAL or (own_binding and row.get('job') == config['own_job']
                 and row.get('owner') == own_binding['owner'] and row.get('lease') == own_binding['lease']) or any(b['ledger_expected'] and row.get('job') == b['job']
                 and row.get('owner') == b['owner'] and row.get('lease') == b['lease'] for b in config.get('granted', []))
                 for key, row in ledger['jobs'].items()),
             'unknown_nonterminal_owner')
    env = api.manager_environment()[0]
    app_contract = config.get('app_baseline')
    if app_contract is not None:
        check_app_baseline(app_contract, app_contract_state(config, app_contract['job_parents'], api, env), api)
    compute_releases = []
    for binding in config.get('released_compute_custody', []):
        api.need(binding['identity']['job_id'] not in held
                 and all(item['job'] != binding['identity']['job'] for item in config.get('granted', [])),
                 'compute_release_duplicate_allocation')
        compute_releases.append(compute_release(binding, ledger, api, env))
    released_absences = []
    for binding in config.get('released_custody', []):
        def released_absence():
            info = api.unit_show(binding['unit'], env)
            api.need(info.get('LoadState') == 'not-found'
                     and not api.cgdir(binding['cgroup']).exists(), 'released_unit_or_cgroup_present')
            for birth in binding['births']:
                try:
                    current = api.process(birth['pid'])
                except (FileNotFoundError, ProcessLookupError):
                    current = None
                api.need(current is None or int(current['start_ticks']) != birth['start_ticks'],
                         'released_recorded_birth_present')
        released_absence()
        released_absence()
        released_absences.append({'job_id': binding['job_id'], 'unit': binding['unit'],
                                  'cgroup_absent': True, 'recorded_births_absent': len(binding['births'])})
    app = config['app']
    def app_identity():
        info = api.unit_show(app['unit'], env)
        api.need(info.get('ActiveState') == 'active' and info.get('SubState') == 'running'
                 and info.get('InvocationID') == app['invocation_id']
                 and info.get('MainPID') == str(app['main']['pid'])
                 and info.get('ControlGroup') == app['cgroup']
                 and info.get('MemoryMax') == ('infinity' if app['cap_bytes'] == 'infinity' else str(app['cap_bytes']))
                 and api.process(app['main']['pid']) == app['main']
                 and api.process(app['backend']['pid']) == app['backend'], 'app_identity_drift')
    app_identity()
    app_cg = api.cgdir(app['cgroup'])
    app_memory_max = 'max' if app['cap_bytes'] == 'infinity' else str(app['cap_bytes'])
    api.need((app_cg / 'memory.max').read_text().strip() == app_memory_max, 'app_cap_drift')
    app_current = checked_counter((app_cg / 'memory.current').read_text(), 'charged')
    app_stat = memory_stat_counters((app_cg / 'memory.stat').read_text())
    app_file = checked_file_allowance(app_current, app_stat['file'], app_stat['shmem'])
    app_identity()
    api.need((app_cg / 'memory.max').read_text().strip() == app_memory_max, 'app_cap_drift')
    app_remaining = None if app['cap_bytes'] == 'infinity' else max(0, app['cap_bytes'] - app_current)
    observations = []
    allowed = set()
    for key, binding in held.items():
        row = ledger['jobs'].get(key)
        api.need(row is not None and all(row.get(field) == binding[field]
                 for field in ('job', 'owner', 'lease', 'estimates')), 'held_ledger_identity_drift')
        api.need(all(type(row.get(field)) is int and 0 <= row[field] <= binding[limit]
                 for field, limit in (('remaining_memory_bytes', 'memory_bytes'),
                                      ('remaining_disk_bytes', 'disk_bytes'), ('remaining_inodes', 'inodes'))),
                 'held_promise_exceeds_reserve')
        api.need('expected_state' not in binding or row['state'] == binding['expected_state'],
                 'held_expected_state_drift')
        observed = {'job_id': key, 'ledger_state': row['state'],
                    'reserved_memory_bytes': binding['memory_bytes'],
                    'reserved_disk_bytes': binding['disk_bytes'], 'reserved_inodes': binding['inodes'],
                    'ledger_remaining': {field: row[field] for field in
                        ('remaining_memory_bytes', 'remaining_disk_bytes', 'remaining_inodes')}}
        if 'intent_sha256' in binding:
            job, intent = api.load_job(Path(binding['job']))
            api.need(hashlib.sha256(api.read_bytes(job / 'intent.json')).hexdigest() == binding['intent_sha256']
                     and hashlib.sha256(api.read_bytes(job / 'ready.json')).hexdigest() == binding['ready_sha256'],
                     'held_immutable_drift')
            ready = api.read(job / 'ready.json')
            api.need(intent['job_id'] == key and intent['unit'] == binding['unit']
                     and intent['config']['limits']['memory_bytes'] == binding.get('cap_bytes', binding['memory_bytes'])
                     and ready['intent_sha256'] == binding['intent_sha256']
                     and ready['invocation_id'] == binding['invocation_id']
                     and ready['process'] == binding['process']
                     and row['source_pins'] == intent['source_pins'], 'held_custody_binding_drift')
            api.need(hashlib.sha256(api.read_bytes(job / 'result.json')).hexdigest() == binding['result_sha256'],
                     'held_result_drift')
            result = api.read(job / 'result.json')
            api.need(result['job_id'] == key and result['intent_sha256'] == binding['intent_sha256']
                     and result['invocation_id'] == binding['invocation_id']
                     and result['state'] in ('FAILED', 'SUCCEEDED', 'CANCELLED')
                     and result['cleanup'] == {'errors': [], 'quiescent': True}, 'held_result_binding_drift')
            info = api.unit_show(intent['unit'], env)
            status = api.public_status(job, env)
            cg = api.cgdir(api.service_cgroup(ready))
            try:
                past = api.process(ready['process']['pid'])
            except (FileNotFoundError, ProcessLookupError):
                past = None
            api.need(info.get('LoadState') == 'not-found' and not cg.exists()
                     and status.get('identity_verified') is True
                     and status['state'] in ('FAILED', 'SUCCEEDED', 'CANCELLED')
                     and status.get('cleanup') == {'errors': [], 'quiescent': True}
                     and (past is None or any(past[field] != ready['process'][field]
                                             for field in ('pid', 'uid', 'start_ticks'))), 'held_terminal_custody_drift')
            api.need(api.unit_show(intent['unit'], env).get('LoadState') == 'not-found'
                     and not cg.exists(), 'held_terminal_changed')
            observed.update(invocation_id=ready['invocation_id'], process=ready['process'], unit=info,
                            current_charged_bytes=0, canonical=status)
            if 'release_proof' in binding:
                api.need(row['state'] == 'terminal', 'released_row_not_terminal')
                for pin in ('release_proof', 'root_acceptance'):
                    proof_path = Path(binding[pin]['path'])
                    api.need(hashlib.sha256(api.read_bytes(proof_path, private=False)).hexdigest()
                             == binding[pin]['sha256'], 'release_proof_pin_drift')
                proof = api.read(Path(binding['release_proof']['path']))
                metadata = row.get('engine06_cleanup_reconciliation', {})
                api.need(proof['state'] == 'ENGINE06_PHYSICAL_CLEANUP_AND_PROMISE_RELEASE_CLOSED'
                         and proof['binding'] == binding['release_binding'] == metadata.get('binding')
                         and proof['canonical_FAILED_sha256'] == binding['result_sha256']
                         and proof['canonical_FAILED_bytes_unchanged'] is True
                         and proof['physical_cleanup_pass'] is True
                         and proof['unit_load_state'] == 'not-found' and proof['unit_cgroup_absent'] is True
                         and proof['target_state'] == 'terminal'
                         and all(value == 0 for value in proof['remaining'].values())
                         and len(proof['remaining']) == 3
                         and metadata.get('physical_cleanup_pass') is True
                         and metadata.get('original_failed_result_sha256') == binding['result_sha256']
                         and metadata.get('scope') == 'CORE1_ENGINE06_FAILED_STORES_AND_PROMISES_RECONCILE',
                         'released_metadata_drift')
                known = proof['all_recorded_and_helper_transport_births_absent']
                api.need(len(known) == 19 and len(proof['five_generated_roots_absent']) == 5,
                         'release_proof_coverage_drift')
                for original in known:
                    api.need(original['original_birth_absent'] is True, 'release_birth_unverified')
                    try:
                        current = api.process(original['pid'])
                    except (FileNotFoundError, ProcessLookupError):
                        current = None
                    api.need(current is None or current['start_ticks'] != original['start_ticks'],
                             'released_birth_present')
                for root_path in proof['five_generated_roots_absent']:
                    try:
                        Path(root_path).lstat()
                    except FileNotFoundError:
                        continue
                    api.need(False, 'released_storage_present')
                observed['accepted_release_proof'] = binding['release_proof']
            # No mutation: a zero reserve is accepted only with pinned release/current absence guards.

        observations.append(observed)
    granted = []
    for binding in config.get('granted', []):
        observed, unit = granted_growth(binding, ledger, api, env)
        granted.append(observed)
        if unit: allowed.add(unit)
    own_job = Path(config['own_job'])
    cap_free = unlimited(config['own_limits'])
    api.need(not cap_free or app['cap_bytes'] == 'infinity', 'app_cap_drift')
    own_future = (own_approved['estimates']['memory_bytes'] if cap_free and own_binding and 'estimates' in own_approved
                  else config.get('own_memory_forecast_bytes') if cap_free else config['own_limits']['memory_bytes'])
    if cap_free and type(own_future) is not int:
        raise ValueError('invalid_counter_own_forecast')
    own_future = checked_counter(own_future, 'own_forecast')
    own_file_allowance = 0
    if own_binding and ('ready' in own_binding or 'ready_sha256' in own_binding):
        api.need((own_job / 'ready.json').exists(), 'own_missing_bound_ready')
    own_matches = [(key, row) for key, row in ledger['jobs'].items() if row.get('job') == str(own_job)]
    if own_binding and not (own_job / 'intent.json').exists():
        api.need(not own_matches and not own_binding.get('intent_sha256'), 'own_missing_bound_intent')
    if (own_job / 'intent.json').exists():
        own_job, intent = api.load_job(own_job)
        api.need(all(intent['config']['limits'][key] == value for key, value in config['own_limits'].items()),
                 'own_limit_drift')
        if own_binding:
            intent_hash = hashlib.sha256(api.read_bytes(own_job / 'intent.json')).hexdigest()
            api.need(intent['config'] == own_approved and intent['config_sha256'] == own_binding['config_sha256']
                     and intent['source_pins'] == own_binding['intent_source_pins']
                     and intent_hash == own_binding.get('intent_sha256', intent_hash), 'own_intent_binding_drift')
            api.pins(own_binding['intent_source_pins'])
            if own_binding['ledger_expected']:
                api.need(len(own_matches) == 1 and own_matches[0][0] == intent['job_id']
                         and all(own_matches[0][1].get(key) == own_binding[key] for key in ('owner', 'lease'))
                         and own_matches[0][1]['source_pins'] == intent['source_pins']
                         and own_matches[0][1]['estimates'] == own_approved['estimates'], 'own_ledger_binding_drift')
            else:
                api.need(not own_matches, 'own_unexpected_ledger_record')
        allowed.add(intent['unit'])
        if (own_job / 'ready.json').exists():
            info = api.unit_show(intent['unit'], env)
            api.need(info.get('ActiveState') == 'active' and info.get('SubState') == 'running'
                     and int(info.get('MainPID', '0')) > 0, 'own_not_running')
            ready = api.custody(own_job, intent, info)
            if own_binding and 'ready' in own_binding:
                api.need(ready == own_binding['ready'], 'own_ready_binding_drift')
            if own_binding and 'ready_sha256' in own_binding:
                api.need(hashlib.sha256(api.read_bytes(own_job / 'ready.json')).hexdigest()
                         == own_binding['ready_sha256'], 'own_ready_binding_drift')
            own_budget_check(config, intent, ready, info, api)
            charged, own_file_allowance = charged_and_cache(api.cgdir(api.service_cgroup(ready)))
            after = api.unit_show(intent['unit'], env)
            api.need(charged >= 0 and after.get('ActiveState') == 'active'
                     and after.get('SubState') == 'running' and int(after.get('MainPID', '0')) > 0
                     and api.custody(own_job, intent, after) == ready, 'own_measurement_changed')
            own_budget_check(config, intent, ready, after, api)
            own_future = max(0, own_future - charged)
    inventory = api.command(['systemctl', '--user', 'list-units', 'wow-task-*', '--state=active',
                             '--no-legend', '--plain', '--no-pager'], env)
    api.need(inventory.returncode == 0, 'unit_inventory_failed')
    units = {line.split()[0] for line in inventory.stdout.splitlines() if line.strip()}
    api.need(units <= allowed, 'unknown_active_unit')
    if metrics is None:
        memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        fs = os.statvfs(HERE)
        pressure = {}
        for name in ('memory', 'io'):
            values = (Path('/proc/pressure') / name).read_text().splitlines()[0].split()[1:]
            pressure[name] = float(dict(value.split('=') for value in values)['avg10'])
        metrics = {'available_bytes': int(memory['MemAvailable'].split()[0]) * 1024,
                   'disk_free_bytes': fs.f_bavail * fs.f_frsize, 'free_inodes': fs.f_favail,
                   'pressure_avg10': pressure}
    memory_required = (0 if cap_free else config['policy_reserve_bytes'] + app_remaining + app_file) + sum(row['memory_bytes'] for row in held.values()) + sum(row['remaining_bytes'] + row['file_allowance_bytes'] for row in granted) + own_future + own_file_allowance
    disk_required = (0 if cap_free else config['disk_floor_bytes']) + sum(row['disk_bytes'] for row in held.values()) + sum(row['disk_bytes'] for row in config.get('granted', [])) + config['own_disk_forecast_bytes']
    inode_required = (0 if cap_free else config['inode_floor']) + sum(row['inodes'] for row in held.values()) + sum(row['inodes'] for row in config.get('granted', [])) + config['own_inode_forecast']
    for key in ('available_bytes', 'disk_free_bytes', 'free_inodes'):
        checked_counter(metrics[key], key)
    for name in ('memory', 'io'):
        value = metrics['pressure_avg10'][name]
        api.need(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100, 'capacity_sample_invalid')
    feasible = (metrics['available_bytes'] > memory_required and metrics['disk_free_bytes'] > disk_required
                and metrics['free_inodes'] > inode_required and metrics['pressure_avg10']['memory'] < 5
                and metrics['pressure_avg10']['io'] < 10)
    if cap_free:
        feasible = all(metrics[key] > 0 for key in ('available_bytes', 'disk_free_bytes', 'free_inodes'))
    contract_deltas = None
    if app_contract is not None:
        contract_deltas = check_app_baseline(
            app_contract, app_contract_state(config, app_contract['job_parents'], api, env), api)
    return {'at': time.time(), 'feasible': feasible, 'app_event_deltas': contract_deltas, 'metrics': metrics,
            'memory_required_bytes': memory_required, 'disk_required_bytes': disk_required,
            'inode_required': inode_required, 'own_future_bytes': own_future,
            'own_file_allowance_bytes': own_file_allowance,
            'app': {'current_bytes': app_current, 'cap_bytes': app['cap_bytes'],
                    'remaining_bytes': app_remaining, 'file_allowance_bytes': app_file,
                    'file_bytes': checked_counter(app_stat['file'], 'file'),
                    'shmem_bytes': checked_counter(app_stat['shmem'], 'shmem'),
                    'invocation_id': app['invocation_id'], 'main': app['main'], 'backend': app['backend']},
            'policy_reserve_bytes': 0 if cap_free else config['policy_reserve_bytes'],
            'compute_releases': compute_releases,
            'released_absences': released_absences,
            'held': observations, 'granted': granted, 'active_units': sorted(units),
            'forecast_only': cap_free,
            'accounting': 'Forecast-only on unlimited launch contracts; historical finite accounting retained. MemAvailable already excludes charged usage. Fresh app remaining, each live admitted job remaining cap plus its file-minus-shmem allowance, app file-minus-shmem allowance, configured policy reserve and known held reserves are added once; no promise is released.'}


def refreshed(config, api=None, metrics=None):
    observation = snapshot(config=config, api=api, metrics=metrics)
    if not observation['feasible']:
        raise RuntimeError('dynamic_capacity_refused')
    # Keep the caller's own forecast separate from external commitments.
    external = observation['memory_required_bytes'] - observation['own_future_bytes']
    if external < 0:
        raise RuntimeError('invalid_external_reserve')
    return {**config, 'host_reserve_bytes': external, 'disk_reserve_bytes': observation['disk_required_bytes'],
            'foreign_memory_sample': observation}
