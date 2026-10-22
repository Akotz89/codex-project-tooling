"""One poll for an outer observer; injected adopted APIs, no launcher or signals."""
import hashlib
import json
import math
import time
from pathlib import Path, PurePosixPath


class CapacityFailure(RuntimeError):
    def __init__(self, receipt):
        super().__init__(receipt['primary_error'])
        self.receipt = receipt


_CODES = frozenset((
    'address_space_actual',
    'address_space_manager',
    'app_cap_drift',
    'app_contract_ancestor_interface',
    'app_contract_ancestor_or_topology_drift',
    'app_contract_event_reset',
    'app_contract_events_keys',
    'app_contract_events_shape',
    'app_contract_high',
    'app_contract_job_coverage',
    'app_contract_job_topology',
    'app_contract_path',
    'app_contract_pressure_event',
    'app_contract_topology',
    'app_identity_drift',
    'authority_pin_drift',
    'capacity_infeasible',
    'capacity_sample_invalid',
    'cgroup_domain',
    'cpu_not_delegated',
    'compute_release_authority_drift',
    'compute_release_birth_coverage_drift',
    'compute_release_birth_present',
    'compute_release_cas_drift',
    'compute_release_duplicate_allocation',
    'compute_release_identity_drift',
    'compute_release_pin_drift',
    'compute_release_reference_incomplete',
    'compute_release_row_not_zero',
    'compute_release_storage_metadata_drift',
    'compute_release_unit_or_cgroup_present',
    'cpu_budget',
    'cpu_manager_timespan',
    'duplicate_held_binding',
    'delegation_manager',
    'dynamic_capacity_refused',
    'grant_cap_drift',
    'grant_config_pin_drift',
    'grant_continuity_drift',
    'grant_control_pin_drift',
    'grant_cpu_cap_drift',
    'grant_custody_drift',
    'grant_duplicate_ledger_job',
    'grant_intent_drift',
    'grant_ledger_identity_drift',
    'grant_ledger_missing',
    'grant_live_missing_custody',
    'grant_missing_bound_intent',
    'grant_physical_cap_drift',
    'grant_postmemory_custody_drift',
    'grant_promise_exceeds_cap',
    'grant_ready_binding_drift',
    'grant_ready_drift',
    'held_custody_binding_drift',
    'held_expected_state_drift',
    'held_immutable_drift',
    'held_ledger_identity_drift',
    'held_promise_exceeds_reserve',
    'held_result_binding_drift',
    'held_result_drift',
    'held_terminal_changed',
    'held_terminal_custody_drift',
    'inconsistent_file_shmem_counters',
    'intent_binding',
    'invalid_counter_app_event',
    'invalid_counter_baseline_app_event',
    'invalid_counter_charged',
    'invalid_counter_contract_limit',
    'invalid_counter_available_bytes',
    'invalid_counter_disk_free_bytes',
    'invalid_counter_free_inodes',
    'invalid_counter_own_forecast',
    'invalid_counter_promise_memory',
    'invalid_counter_file',
    'invalid_counter_shmem',
    'invalid_external_reserve',
    'invalid_memory_stat_shape',
    'job_id_binding',
    'job_path_binding',
    'ledger_version',
    'manager_budget',
    'management_throttled',
    'missing_cgroup',
    'missing_file_shmem_counter',
    'own_config_limits_drift',
    'own_config_pin_drift',
    'own_intent_binding_drift',
    'own_ledger_binding_drift',
    'own_limit_drift',
    'own_measurement_changed',
    'own_missing_bound_intent',
    'own_missing_bound_ready',
    'own_not_running',
    'own_ready_binding_drift',
    'own_unexpected_ledger_record',
    'nonempty_service_root',
    'process_limit_actual',
    'physical_budget',
    'finite_limits_unsupported',
    'ready_binding',
    'ready_missing',
    'release_birth_unverified',
    'release_proof_coverage_drift',
    'release_proof_pin_drift',
    'released_birth_present',
    'released_metadata_drift',
    'released_recorded_birth_present',
    'released_row_drift',
    'released_row_not_terminal',
    'released_storage_present',
    'released_unit_or_cgroup_present',
    'runtime_manager',
    'runtime_budget',
    'unexpected_native_ledger_record',
    'unit_inventory_failed',
    'topology_binding',
    'unexpected_topology',
    'unknown_active_unit',
    'unknown_nonterminal_owner',
    'unsupported_finite_ancestor',
    'workload_cpu_readback',
))


def _error_type(error):
    name = type(error).__name__
    return name if name in ('OSError', 'ValueError', 'RuntimeError', 'TimeoutError',
                           'FileNotFoundError', 'ProcessLookupError', 'KeyError',
                           'TypeError', 'Refused', 'AppContractError') else 'Exception'


def _number(value):
    return (type(value) is int and 0 <= value <= 2**63 - 1
            or type(value) is float and math.isfinite(value) and 0 <= value <= 10**12)


def _witness(sample):
    """Copy only fixed numeric fields; never serialize arbitrary callback data."""
    if type(sample) is not dict:
        return None
    result = {}
    if (type(sample.get('measurement_context')) is str
            and sample['measurement_context'] in ('real', 'injected_negative_headroom')):
        result['measurement_context'] = sample['measurement_context']
    for key in ('at', 'memory_required_bytes', 'disk_required_bytes', 'inode_required',
                'own_future_bytes', 'own_file_allowance_bytes', 'policy_reserve_bytes'):
        if _number(sample.get(key)):
            result[key] = sample[key]
    if type(sample.get('feasible')) is bool:
        result['feasible'] = sample['feasible']
    if type(sample.get('forecast_only')) is bool:
        result['forecast_only'] = sample['forecast_only']
    deltas = sample.get('app_event_deltas')
    if type(deltas) is dict:
        result['app_event_deltas'] = {key: deltas[key] for key in
                                     ('low', 'high', 'max', 'oom', 'oom_kill',
                                      'oom_group_kill', 'sock_throttled')
                                     if type(deltas.get(key)) is int
                                     and 0 <= deltas[key] <= 2**64 - 1}
    metrics = sample.get('metrics')
    if type(metrics) is dict:
        result['metrics'] = {key: metrics[key] for key in
                             ('available_bytes', 'disk_free_bytes', 'free_inodes')
                             if _number(metrics.get(key))}
        pressure = metrics.get('pressure_avg10')
        if type(pressure) is dict:
            result['metrics']['pressure_avg10'] = {
                key: pressure[key] for key in ('memory', 'io')
                if _number(pressure.get(key)) and pressure[key] <= 100}
    app = sample.get('app')
    if type(app) is dict:
        result['app'] = {key: app[key] for key in
                         ('current_bytes', 'cap_bytes', 'remaining_bytes', 'file_bytes',
                          'shmem_bytes', 'file_allowance_bytes') if _number(app.get(key))}
    reasons = []
    for metric, required, label in (('available_bytes', 'memory_required_bytes', 'memory'),
                                    ('disk_free_bytes', 'disk_required_bytes', 'disk'),
                                    ('free_inodes', 'inode_required', 'inodes')):
        observed = result.get('metrics', {}).get(metric)
        needed = result.get(required)
        if observed is not None and needed is not None:
            result[label + '_headroom'] = observed if result.get('forecast_only') else observed - needed
            if observed <= (0 if result.get('forecast_only') else needed):
                reasons.append(label + '_capacity')
    pressure = result.get('metrics', {}).get('pressure_avg10', {})
    for name, threshold in (('memory', 5), ('io', 10)):
        if not result.get('forecast_only') and pressure.get(name, -1) >= threshold:
            reasons.append(name + '_pressure')
    result['reasons'] = reasons or ['sample_reported_infeasible' if result.get('feasible') is False
                                    else 'guard_failure']
    return result


def _error_observation(error, code):
    if type(error).__name__ != 'AppContractError' or code not in _CODES:
        return None
    try:
        observed = error.observation
    except Exception:
        return None
    if type(observed) is not dict:
        return None
    result = {}
    for group in ('baseline_events', 'current_events', 'event_deltas'):
        counters = observed.get(group)
        if type(counters) is dict:
            result[group] = {key: counters[key] for key in
                             ('low', 'high', 'max', 'oom', 'oom_kill', 'oom_group_kill', 'sock_throttled')
                             if type(counters.get(key)) is int and abs(counters[key]) <= 2**64 - 1
                             and (group == 'event_deltas' or counters[key] >= 0)}
    for key in ('manager_memory_high', 'cgroup_memory_high', 'ancestor_memory_max',
                'ancestor_memory_high', 'app_memory_max'):
        value = observed.get(key)
        if (type(value) is int and 0 <= value <= 2**64 - 1
                or type(value) is str and value in ('infinity', 'max', 'missing', 'invalid')):
            result[key] = value
    value = observed.get('app_memory_cap_bytes')
    if type(value) is int and 0 <= value <= 2**64 - 1:
        result['app_memory_cap_bytes'] = value
    path = observed.get('ancestor_path')
    if (type(path) is str and 0 < len(path) <= 512 and path.startswith('/')
            and str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts
            and all(char.isascii() and (char.isalnum() or char in '/_.-:@') for char in path)):
        result['ancestor_path'] = path
    if type(observed.get('topology_valid')) is bool:
        result['topology_valid'] = observed['topology_valid']
    return result


def _bound(binding, base):
    job = Path(binding['job'])
    if not job.is_absolute() or job.resolve() != job:
        raise ValueError('job_path_binding')
    loaded, intent = base.load_job(job)
    if Path(loaded) != job or intent['job_id'] != binding['job_id']:
        raise ValueError('job_id_binding')
    raw = base.read_bytes(job / 'intent.json')
    if hashlib.sha256(raw).hexdigest() != binding['intent_sha256'] or json.loads(raw) != intent:
        raise ValueError('intent_binding')
    try:
        raw = base.read_bytes(job / 'ready.json')
    except FileNotFoundError:
        if binding['ready_sha256'] is not None:
            raise ValueError('ready_missing') from None
    else:
        ready = json.loads(raw)
        if (binding['ready_sha256'] is None
                or hashlib.sha256(raw).hexdigest() != binding['ready_sha256']
                or ready['job_id'] != binding['job_id']
                or ready['intent_sha256'] != binding['intent_sha256']):
            raise ValueError('ready_binding')
    return job


def poll(snapshot, *, binding, controller, base, env, record=None):
    """Return a feasible sample, otherwise stop exact custody and always raise.

    binding has job (absolute canonical path), job_id, intent_sha256 and
    ready_sha256. None for ready_sha256 means ready must still be absent;
    the caller must explicitly bind its digest once ready exists. Queued
    cancellation without a ready receipt cannot prove physical closure here.
    record, when supplied, must durably publish its private bounded receipt
    before returning. It receives an independent JSON copy before stop; its
    failure is secondary and does not prevent exact-owned cleanup. Caller owns
    cadence, final receipt publication and primary handling.
    """
    stage = 'snapshot'
    sample = None
    try:
        sample = snapshot()
        stage = 'capacity'
        if type(sample) is not dict or type(sample.get('feasible')) is not bool:
            raise ValueError('capacity_sample_invalid')
        if not sample['feasible']:
            raise RuntimeError('capacity_infeasible')
        stage = 'custody'
        _bound(binding, base)
        return sample
    except Exception as primary:
        code = primary.args[0] if primary.args and type(primary.args[0]) is str else None
        receipt = {'failed': True, 'primary_error': _error_type(primary), 'failure_stage': stage,
                   'primary_code': code if code in _CODES else stage + '_failed',
                   'primary_message': {'snapshot': 'Capacity snapshot failed.',
                                       'capacity': 'Capacity checks failed.',
                                       'custody': 'Job custody validation failed.'}[stage],
                   'failed_at': time.time(), 'first_failed_sample': _witness(sample),
                   'error_observation': _error_observation(primary, code),
                   'record_error': None,
                   'stop_called': False, 'closure_verified': False,
                   'cleanup_error': None, 'terminal_state': None}
        if record is not None:
            try:
                record(json.loads(json.dumps(receipt, allow_nan=False)))
            except Exception as record_error:
                receipt['record_error'] = _error_type(record_error)
        try:
            job = _bound(binding, base)
            receipt['stop_called'] = True
            controller.stop(job)
            _bound(binding, base)
            status = controller.terminal_proof(job, env)
            cleanup = status.get('cleanup') or {}
            if (status.get('job_id') != binding['job_id']
                    or status.get('identity_verified') is not True
                    or status.get('state') not in ('SUCCEEDED', 'FAILED', 'CANCELLED')
                    or cleanup.get('quiescent') is not True
                    or cleanup.get('errors') != []):
                raise ValueError('terminal_closure_unverified')
            receipt.update(closure_verified=True, terminal_state=status['state'])
        except Exception as cleanup_error:
            receipt['cleanup_error'] = _error_type(cleanup_error)
        raise CapacityFailure(receipt) from primary
