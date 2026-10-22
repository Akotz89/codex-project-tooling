"""Read-only Linux v2 sampler and pure project allocation policy.

sample_host(root, cgroup=None) returns timestamp (monotonic seconds), memory
(bytes, host and effective cgroup headroom), cpu (cores, quota, interval busy),
pressure (PSI avg10 percent), disk (available bytes/inodes), io (own cgroup
counters and interval rates), missing (unavailable required measurements).
The first sample has no interval: take another before admission. Only the own
cgroup and its ancestors are read. No processes or application memory are read.

decide(snapshot, jobs, previous=None, now=None, tuning=None) returns allocations,
admissions, headroom, status/reasons and state (pass state as previous next time).
Jobs require id/owner/lease/state and nonnegative working_set_bytes,
actual_memory_bytes, remaining_memory_bytes, remaining_disk_bytes. Remaining
values are unconsumed promises; actual footprints are ALREADY in host usage.
Optional remaining_inodes (default zero) reserves future files. actual_cpu
(cores) identifies project load; absent means conservatively
all measured load is external. cooperative controls checkpoint/fanout hints derived from observed CPU capacity.
Estimates and pressure are observations; this module changes no limits.
"""

import math
import os
from pathlib import Path
import time

_PREVIOUS = {}
_CGROUP_ROOT = Path('/sys/fs/cgroup')


def _read(path):
    with open(path, encoding='ascii') as source:
        value = source.read(65537)
    if len(value) > 65536:
        raise ValueError('measurement exceeds read bound')
    return value


def _pairs(value):
    return {line.split()[0]: int(line.split()[1]) for line in value.splitlines()}


def _cpus(value):
    result = set()
    for part in value.strip().split(','):
        bounds = part.split('-')
        start, end = int(bounds[0]), int(bounds[-1])
        if not 0 <= start <= end < 65536:
            raise ValueError('invalid cpuset')
        result.update(range(start, end + 1))
    return len(result)


def sample_host(project_root, cgroup=None, previous=None):
    timestamp = time.monotonic()
    missing = []

    def read(path, label, parser):
        try:
            return parser(_read(path))
        except (OSError, ValueError, IndexError, KeyError, ZeroDivisionError):
            missing.append(label)
            return None

    mem = read('/proc/meminfo', 'memory.host', _pairs) or {}
    memory = {name: mem.get(key, 0) * 1024 for name, key in {
        'total_bytes': 'MemTotal:', 'available_bytes': 'MemAvailable:',
        'anon_bytes': 'AnonPages:', 'file_bytes': 'Cached:',
        'reclaimable_bytes': 'SReclaimable:', 'slab_bytes': 'Slab:',
        'swap_total_bytes': 'SwapTotal:', 'swap_free_bytes': 'SwapFree:'}.items()}
    if not {'MemTotal:', 'MemAvailable:', 'AnonPages:', 'Cached:', 'SReclaimable:',
            'Slab:', 'SwapTotal:', 'SwapFree:'} <= mem.keys():
        missing.append('memory.host_fields')
    cpu_ticks = read('/proc/stat', 'cpu.ticks', lambda v: [int(n) for n in v.splitlines()[0].split()[1:9]])
    load = read('/proc/loadavg', 'cpu.load', lambda v: float(v.split()[0]))
    pressure = {}
    for resource in ('memory', 'cpu', 'io'):
        def psi(value):
            return {line.split()[0]: {k: float(v) for k, v in
                    (field.split('=') for field in line.split()[1:])}
                    for line in value.splitlines()}
        pressure[resource] = read('/proc/pressure/' + resource, 'pressure.' + resource, psi)
    try:
        if cgroup is None:
            relative = next(line[3:] for line in _read('/proc/self/cgroup').splitlines() if line.startswith('0::'))
            group = _CGROUP_ROOT / relative.lstrip('/')
        else:
            group = Path(cgroup)
        group = group.resolve(strict=True)
        group.relative_to(_CGROUP_ROOT)
    except (OSError, ValueError, StopIteration):
        group = None
        missing.append('cgroup.path')
    ancestors, quotas = [], []
    own_stat, io_counters, cpuset = {}, None, None
    if group is not None:
        current = group
        while True:
            label = 'cgroup.' + str(current.relative_to(_CGROUP_ROOT))
            # The unified root has no memory/CPU controller limit files.
            used = memory['total_bytes'] - memory['available_bytes'] if current == _CGROUP_ROOT else read(current / 'memory.current', label + '.memory.current', lambda v: int(v))
            maximum = None if current == _CGROUP_ROOT else read(current / 'memory.max', label + '.memory.max', lambda v: None if v.strip() == 'max' else int(v))
            stat = None if current == _CGROUP_ROOT else read(current / 'memory.stat', label + '.memory.stat', _pairs)
            ancestors.append({'path': str(current), 'current_bytes': used,
                              'max_bytes': maximum, 'stat': stat,
                              'headroom_bytes': None if maximum is None or used is None else max(0, maximum - used)})
            if current != _CGROUP_ROOT:
                quota = read(current / 'cpu.max', label + '.cpu.max', lambda v: None if v.split()[0] == 'max' else int(v.split()[0]) / int(v.split()[1]))
                if quota is not None:
                    quotas.append(quota)
            if current == group:
                own_stat = stat
                def parse_io(value):
                    counters = {}
                    for line in value.splitlines():
                        for field in line.split()[1:]:
                            key, number = field.split('=')
                            counters[key] = counters.get(key, 0) + int(number)
                    return counters
                io_counters = read(current / 'io.stat', label + '.io.stat', parse_io)
            if cpuset is None:
                try:
                    cpuset = _cpus(_read(current / 'cpuset.cpus.effective'))
                except FileNotFoundError:
                    if current == _CGROUP_ROOT: missing.append('cgroup.cpuset_unavailable')
                except (OSError, ValueError):
                    missing.append(label + '.cpuset')
            if current == _CGROUP_ROOT:
                break
            current = current.parent
    limits = [memory['available_bytes']] + [entry['headroom_bytes'] for entry in ancestors if entry['headroom_bytes'] is not None]
    memory.update(effective_headroom_bytes=min(limits), cgroup=ancestors[0] if ancestors else None,
                  ancestors=ancestors, cgroup_stat=own_stat)
    capacity = min([float(len(os.sched_getaffinity(0)))] + quotas + ([float(cpuset)] if cpuset else []))
    key = (str(Path(project_root).resolve()), str(group))
    if previous is None:
        previous = _PREVIOUS.get(key)
    if previous and (previous.get('scope') != list(key) or not 0 < timestamp - previous.get('timestamp', timestamp) <= 60):
        previous = None
    elapsed = timestamp - previous['timestamp'] if previous else None
    busy = None
    rates = None
    if previous and elapsed > 0:
        if cpu_ticks is not None and previous['ticks'] is not None:
            deltas = [a - b for a, b in zip(cpu_ticks, previous['ticks'])]
            total = sum(deltas)
            if total > 0 and min(deltas) >= 0:
                busy = (total - sum(deltas[3:5])) / total * (os.cpu_count() or 1)
        if io_counters is not None and previous['io'] is not None:
            delta = {name: count - previous['io'].get(name, 0) for name, count in io_counters.items()}
            if all(count >= 0 for count in delta.values()):
                rates = {name + '_per_second': count / elapsed for name, count in delta.items()}
    if busy is None or rates is None:
        missing.append('interval.warmup_or_reset')
    baseline = {'timestamp': timestamp, 'ticks': cpu_ticks, 'io': io_counters, 'scope': list(key)}
    _PREVIOUS[key] = baseline
    try:
        fs = os.statvfs(project_root)
        disk = {'free_bytes': fs.f_bavail * fs.f_frsize, 'free_inodes': fs.f_favail,
                'total_bytes': fs.f_blocks * fs.f_frsize, 'total_inodes': fs.f_files}
    except OSError:
        disk = {}
        missing.append('disk')
    return {'timestamp': timestamp, 'memory': memory,
            'cpu': {'capacity': capacity, 'load1': load, 'busy_cores': busy,
                    'quota': min(quotas) if quotas else None, 'cpuset_count': cpuset},
            'pressure': pressure, 'disk': disk,
            'io': {'counters': io_counters, 'rates': rates, 'interval_seconds': elapsed},
            'missing': missing, 'interval_baseline': baseline}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _safe(value):
    return value if _number(value) else 0


def decide(snapshot, jobs, previous=None, now=None, tuning=None):
    """Keep serialized admission/custody; forecasts never become enforcement."""
    config = {'max_sample_age': 10}
    if tuning is not None:
        if type(tuning) is not dict or not tuning.keys() <= config.keys():
            raise ValueError('unsupported tuning')
        config.update(tuning)
    if not all(_number(value) for value in config.values()):
        raise ValueError('tuning values must be finite and nonnegative')
    now = time.monotonic() if now is None else now
    reasons = list(snapshot.get('missing', []))
    memory, cpu, disk = (snapshot.get(key, {}) for key in ('memory', 'cpu', 'disk'))
    required = [(snapshot.get('timestamp'), 'timestamp'),
                (memory.get('effective_headroom_bytes'), 'memory.headroom'),
                (memory.get('total_bytes'), 'memory.total'),
                (memory.get('swap_free_bytes'), 'memory.swap'),
                (cpu.get('capacity'), 'cpu.capacity'), (cpu.get('busy_cores'), 'cpu.busy'),
                (cpu.get('load1'), 'cpu.load'), (disk.get('free_bytes'), 'disk.bytes'),
                (disk.get('free_inodes'), 'disk.inodes')]
    reasons.extend(name for value, name in required if not _number(value))
    if not reasons and not 0 <= now - snapshot['timestamp'] <= config['max_sample_age']:
        reasons.append('sample.stale_or_future')
    for resource in ('memory', 'cpu', 'io'):
        avg = (snapshot.get('pressure', {}).get(resource) or {}).get('some', {}).get('avg10')
        if not _number(avg): reasons.append('pressure.' + resource)
    rates = (snapshot.get('io') or {}).get('rates')
    if not isinstance(rates, dict) or not all(_number(rates.get(key)) for key in ('rbytes_per_second', 'wbytes_per_second')):
        reasons.append('io.interval')
    active = [job for job in jobs if job.get('state') in ('running', 'starting')]
    ready = sorted((job for job in jobs if job.get('state') == 'ready'),
                   key=lambda job: (job.get('ready_since', now), str(job.get('id', ''))))
    invalid_jobs = {}
    seen = set()
    for job in active + ready:
        ident = job.get('id'); errors = []
        if not isinstance(ident, str) or not ident or ident in seen:
            errors.append('job.id_missing_or_duplicate')
        seen.add(ident if isinstance(ident, str) else '')
        if not job.get('owner') or not job.get('lease'):
            errors.append('job.custody_missing')
        for name in ('working_set_bytes', 'actual_memory_bytes', 'remaining_memory_bytes', 'remaining_disk_bytes'):
            if not _number(job.get(name)): errors.append('job.' + name)
        if not _number(job.get('remaining_inodes', 0)): errors.append('job.remaining_inodes')
        if errors: invalid_jobs[str(ident)] = errors
    if invalid_jobs: reasons.append('jobs.invalid')
    invalid_sample = bool(reasons)
    capacity = _safe(cpu.get('capacity'))
    known_load = sum(job.get('actual_cpu', 0) for job in active if _number(job.get('actual_cpu')))
    external_load = max(0, _safe(cpu.get('busy_cores')) - known_load,
                        _safe(cpu.get('load1')) - known_load)
    available_cpu = max(0, capacity - external_load)
    physical_reasons = []
    for value, label in ((_safe(memory.get('effective_headroom_bytes')), 'memory.capacity'),
                         (_safe(disk.get('free_bytes')), 'disk.capacity'),
                         (_safe(disk.get('free_inodes')), 'disk.inodes'),
                         (available_cpu, 'cpu.capacity')):
        if value <= 0: physical_reasons.append(label)
    admissions = {str(job['id']): {'admit': not (reasons or physical_reasons),
                  'reasons': sorted(set(reasons + physical_reasons)), 'unschedulable': False} for job in ready}
    selected = active + [job for job in ready if admissions[str(job['id'])]['admit']]
    share = available_cpu / len(selected) if selected else 0
    allocations = {} if invalid_sample else {str(job['id']): {'cpu': share,
                    'fanout': max(1, math.floor(share)) if share > 0 else 0,
                    'checkpoint': False} for job in selected}
    return {'allocations': allocations, 'admissions': admissions,
            'headroom': {'memory_bytes': _safe(memory.get('effective_headroom_bytes')),
                         'disk_bytes': _safe(disk.get('free_bytes')), 'external_cpu': external_load,
                         'inodes': _safe(disk.get('free_inodes'))},
            'status': 'unknown' if invalid_sample else 'pressure' if physical_reasons else 'ready',
            'reasons': sorted(set(reasons + physical_reasons)),
            'state': {'last_targets': {key: value['cpu'] for key, value in allocations.items()},
                      'last_sample_at': now, 'swap_free_bytes': memory.get('swap_free_bytes')}}
