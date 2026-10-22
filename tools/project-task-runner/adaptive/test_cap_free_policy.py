"""Offline proof that measured headroom drives admission without ceilings."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('capfree_policy', HERE / 'policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def sample():
    return {'timestamp': 100.0, 'missing': [],
            'memory': {'effective_headroom_bytes': 1, 'total_bytes': 2**40,
                       'swap_free_bytes': 2**30},
            'cpu': {'capacity': 8.0, 'busy_cores': 2.0, 'load1': 1.5},
            'disk': {'free_bytes': 1, 'free_inodes': 1},
            'pressure': {name: {'some': {'avg10': 0.0}} for name in ('memory', 'cpu', 'io')},
            'io': {'rates': {'rbytes_per_second': 0.0, 'wbytes_per_second': 0.0}}}


class CapFreePolicy(unittest.TestCase):
    def job(self, ident):
        return {'id': ident, 'owner': 'test', 'lease': 'fixture', 'state': 'ready',
                'ready_since': 1.0, 'working_set_bytes': 2**50, 'actual_memory_bytes': 0,
                'remaining_memory_bytes': 2**50, 'remaining_disk_bytes': 2**50,
                'remaining_inodes': 2**30}

    def test_forecasts_do_not_become_caps_and_share_uses_external_load(self):
        result = policy.decide(sample(), [self.job('a')], now=100.0)
        self.assertTrue(result['admissions']['a']['admit'])
        self.assertEqual(result['allocations']['a']['cpu'], 6.0)
        self.assertEqual(result['headroom']['memory_bytes'], 1)
        self.assertEqual(result['headroom']['disk_bytes'], 1)

    def test_nonpositive_measured_headroom_fails_closed(self):
        state = sample()
        state['memory']['effective_headroom_bytes'] = 0
        result = policy.decide(state, [self.job('a')], now=100.0)
        self.assertFalse(result['admissions']['a']['admit'])
        self.assertIn('memory.capacity', result['reasons'])

    def test_fixed_reserve_and_ceiling_controls_are_not_accepted(self):
        with self.assertRaisesRegex(ValueError, 'unsupported tuning'):
            policy.decide(sample(), [self.job('a')], now=100.0,
                          tuning={'cpu_reserve': 10})

    def test_sample_capacity_respects_process_affinity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'cgroup'
            group = root / 'wow'
            group.mkdir(parents=True)
            project = Path(temp) / 'project'
            project.mkdir()
            for path, value in {
                    group / 'memory.current': '1', group / 'memory.max': 'max',
                    group / 'memory.stat': 'anon 1\nfile 0\n', group / 'cpu.max': 'max 100000',
                    group / 'io.stat': '8:0 rbytes=0 wbytes=0',
                    group / 'cpuset.cpus.effective': '0-63',
                    root / 'cpuset.cpus.effective': '0-63'}.items():
                path.write_text(value)
            proc = {
                '/proc/meminfo': ('MemTotal: 1000 kB\nMemAvailable: 900 kB\nAnonPages: 10 kB\n'
                                  'Cached: 20 kB\nSReclaimable: 5 kB\nSlab: 3 kB\n'
                                  'SwapTotal: 10 kB\nSwapFree: 9 kB\n'),
                '/proc/stat': 'cpu 1 0 1 8 0 0 0 0\n', '/proc/loadavg': '0.10 0.20 0.30 1/10 1\n',
                **{f'/proc/pressure/{name}': 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
                   for name in ('memory', 'cpu', 'io')}}
            original_read = policy._read
            fs = type('StatVFS', (), {'f_bavail': 1, 'f_frsize': 4096,
                                      'f_favail': 1, 'f_blocks': 1, 'f_files': 1})()

            def read(path):
                return proc[str(path)] if str(path) in proc else original_read(path)

            with patch.object(policy, '_CGROUP_ROOT', root), patch.object(policy, '_read', side_effect=read), \
                    patch.object(policy.os, 'cpu_count', return_value=64), \
                    patch.object(policy.os, 'sched_getaffinity', return_value={0, 1}), \
                    patch.object(policy.os, 'statvfs', return_value=fs):
                result = policy.sample_host(project, cgroup=group, previous={})
            self.assertEqual(result['cpu']['capacity'], 2.0)


if __name__ == '__main__':
    unittest.main()
