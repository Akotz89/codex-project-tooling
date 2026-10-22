"""Focused offline checks for the generic unlimited launch contract."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = load(HERE / 'runner.py', 'capfree_base')
adaptive = load(HERE / 'adaptive/runner.py', 'capfree_adaptive')


class LimitsContract(unittest.TestCase):
    def valid(self):
        return {key: 'infinity' for key in
                ('memory_bytes', 'cpu_percent', 'address_space_bytes', 'runtime_seconds')}

    def test_only_exact_four_infinities_are_accepted(self):
        self.assertEqual(base.limits_config(self.valid()), self.valid())
        for invalid in (
                {'memory_bytes': 'infinity'},
                {**self.valid(), 'extra': 'infinity'},
                {**self.valid(), 'memory_bytes': 1},
                {**self.valid(), 'memory_bytes': True},
                {**self.valid(), 'cpu_percent': 0},
                {**self.valid(), 'runtime_seconds': None}):
            with self.subTest(invalid=invalid), self.assertRaises(base.Refused):
                base.limits_config(invalid)

    def test_unlimited_properties_clear_limits_and_keep_lifecycle_controls(self):
        props = base.unlimited_properties()
        self.assertEqual((props['MemoryMax'], props['MemoryHigh'], props['MemorySwapMax']),
                         ('infinity', 'infinity', 'infinity'))
        self.assertEqual(props['CPUQuota'], '')
        self.assertEqual((props['TasksMax'], props['LimitNPROC'], props['LimitAS'],
                          props['RuntimeMaxSec']), ('infinity',) * 4)
        self.assertEqual((props['KillMode'], props['Restart']), ('mixed', 'no'))

    def test_readback_proves_all_resource_properties_unlimited(self):
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            expected = {'memory.max': 'max', 'memory.high': 'max', 'memory.swap.max': 'max',
                        'pids.max': 'max', 'cpu.max': 'max 100000'}
            for name, value in expected.items():
                (cgroup / name).write_text(value)
            ready = {'process': {'cgroup': '/fixture'},
                     'address_space': [base.resource.RLIM_INFINITY] * 2,
                     'process_limit': [base.resource.RLIM_INFINITY] * 2}
            info = {name: 'infinity' for name in
                    ('LimitAS', 'LimitASSoft', 'RuntimeMaxUSec', 'MemoryMax', 'MemoryHigh',
                     'MemorySwapMax', 'CPUQuotaPerSecUSec', 'TasksMax', 'LimitNPROC', 'LimitNPROCSoft')}
            info.update(KillMode='mixed', Restart='no')
            intent = {'config': {'limits': self.valid()}}
            with patch.object(base, 'cgdir', return_value=cgroup):
                self.assertEqual(base.budget_check(intent, ready, info), expected)
                (cgroup / 'pids.max').write_text('256')
                with self.assertRaisesRegex(base.Refused, 'physical_budget'):
                    base.budget_check(intent, ready, info)

    def test_only_complete_historical_finite_receipts_take_legacy_audit_path(self):
        intent = {'config': {'limits': {'memory_bytes': 16777216, 'cpu_percent': 25,
                                        'address_space_bytes': 'infinity', 'runtime_seconds': 10}}}
        with patch.object(base, 'historical_budget_check', return_value={'historical': True}) as audit:
            self.assertEqual(base.budget_check(intent, {}, {}), {'historical': True})
            audit.assert_called_once()
        intent['config']['limits']['cpu_percent'] = True
        with self.assertRaisesRegex(base.Refused, 'finite_limits_unsupported'):
            base.budget_check(intent, {}, {})

    def test_adaptive_configuration_keeps_large_estimates_as_forecasts(self):
        with tempfile.TemporaryDirectory(dir=HERE) as temp:
            root = Path(temp)
            executable = Path('/usr/bin/true').resolve()
            config = {'version': 2, 'argv': [str(executable)], 'cwd': str(root),
                      'limits': self.valid(),
                      'sources': {str(executable): base.source_digest(executable)},
                      'owner': 'offline-test', 'lease': 'fixture', 'kind': 'command',
                      'inputs': [str(executable)],
                      'estimates': {'memory_bytes': 2**50, 'disk_bytes': 2**50, 'inodes': 2**40},
                      'foreground': False, 'cooperative': {'enabled': True}}
            path = root / 'config.json'
            path.write_text(__import__('json').dumps(config))
            path.chmod(0o600)
            parsed, _ = adaptive.configuration(path)
            self.assertEqual(parsed['estimates'], config['estimates'])
            config['limits']['memory_bytes'] = 2**30
            path.write_text(__import__('json').dumps(config))
            path.chmod(0o600)
            with self.assertRaisesRegex(adaptive.base.Refused, 'finite_limits_unsupported'):
                adaptive.configuration(path)


if __name__ == '__main__':
    unittest.main()
