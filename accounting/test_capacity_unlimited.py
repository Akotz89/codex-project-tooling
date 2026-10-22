"""Forecast and receipt-boundary checks; never contact a live manager."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


budget = load('capacity_budget_unlimited_test', HERE / 'capacity_budget.py')
guard = load('capacity_guard_unlimited_test', HERE / 'capacity_guard.py')


class API:
    def need(self, condition, code):
        if not condition:
            raise AssertionError(code)


class UnlimitedAccounting(unittest.TestCase):
    def test_only_four_infinities_mark_unlimited(self):
        values = {key: 'infinity' for key in
                  ('memory_bytes', 'cpu_percent', 'address_space_bytes', 'runtime_seconds')}
        self.assertTrue(budget.unlimited(values))
        self.assertFalse(budget.unlimited({**values, 'cpu_percent': True}))
        self.assertFalse(budget.unlimited({**values, 'memory_bytes': 100}))
        self.assertFalse(budget.unlimited({**values, 'extra': 'infinity'}))

    def test_new_unlimited_promises_use_explicit_forecast(self):
        limits = {key: 'infinity' for key in
                  ('memory_bytes', 'cpu_percent', 'address_space_bytes', 'runtime_seconds')}
        approved = {'limits': limits, 'estimates': {'memory_bytes': 2**42}}
        binding = {'cap_bytes': None}
        self.assertEqual(budget.promise_memory(binding, approved, API()), 2**42)
        approved['estimates']['memory_bytes'] = True
        with self.assertRaisesRegex(ValueError, 'invalid_counter_promise_memory'):
            budget.promise_memory(binding, approved, API())

    def test_finite_historical_promise_remains_auditable(self):
        approved = {'limits': {'memory_bytes': 16777216, 'cpu_percent': 25,
                               'address_space_bytes': 'infinity', 'runtime_seconds': 10}}
        self.assertEqual(budget.promise_memory({'cap_bytes': 16777216}, approved, API()), 16777216)

    def test_project_config_is_required_instead_of_generic_budget_file(self):
        with self.assertRaisesRegex(ValueError, 'project_config_required'):
            budget.snapshot()

    def test_guard_sanitizes_forecast_only_without_treating_forecasts_as_caps(self):
        sample = {'forecast_only': True, 'feasible': True, 'memory_required_bytes': 1000,
                  'disk_required_bytes': 1000, 'inode_required': 1000,
                  'metrics': {'available_bytes': 10, 'disk_free_bytes': 10, 'free_inodes': 10,
                              'pressure_avg10': {'memory': 90, 'io': 90}}}
        witness = guard._witness(sample)
        self.assertTrue(witness['forecast_only'])
        self.assertEqual(witness['memory_headroom'], 10)
        self.assertEqual(witness['disk_headroom'], 10)
        self.assertEqual(witness['reasons'], ['guard_failure'])
        sample['forecast_only'] = False
        finite = guard._witness(sample)
        self.assertIn('memory_capacity', finite['reasons'])
        self.assertIn('memory_pressure', finite['reasons'])


if __name__ == '__main__':
    unittest.main()
