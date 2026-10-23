"""Focused controller-topology and pre-admission failure receipts."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adaptive = load(HERE / 'adaptive/runner.py', 'cgroup_admission_adaptive')
base = adaptive.base


class CgroupAdmissionTests(unittest.TestCase):
    def setUp(self):
        original = base.subprocess.run

        def refuse_systemd_run(args, *positional, **kwargs):
            if args and Path(args[0]).name == 'systemd-run':
                raise AssertionError('native systemd-run forbidden in focused fixtures')
            return original(args, *positional, **kwargs)

        self.native_guard = patch.object(base.subprocess, 'run',
                                         side_effect=refuse_systemd_run)
        self.native_guard.start()
        self.addCleanup(self.native_guard.stop)

    def test_native_subprocess_guard_fails_before_execution(self):
        with self.assertRaisesRegex(AssertionError,
                                    'native systemd-run forbidden'):
            base.command(['systemd-run', '--user', '--unit=must-not-run'], {})

    def topology_fixture(self, root):
        service = root / 'service'
        management = service / 'management'
        workload = service / 'workload'
        management.mkdir(parents=True)
        workload.mkdir()
        (service / 'cgroup.procs').write_text('')
        (service / 'cgroup.controllers').write_text('cpu memory pids')
        (service / 'cgroup.subtree_control').write_text('cpu memory pids')
        (service / 'cgroup.type').write_text('domain')
        (management / 'cgroup.procs').write_text('1\n')
        (workload / 'cgroup.procs').write_text('')
        for group in (service, management, workload):
            for name, value in {
                    'memory.max': 'max', 'memory.high': 'max',
                    'memory.swap.max': 'max', 'pids.max': 'max',
                    'cpu.max': 'max 100000'}.items():
                (group / name).write_text(value)
        for group in (management, workload):
            (group / 'cgroup.procs').write_text('')
        return service, management, workload

    def test_required_controllers_are_requested_available_and_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            service, management, workload = self.topology_fixture(Path(temp))
            shutil.rmtree(workload)
            info = {'ControlGroup': '/user.slice/job.service', 'Delegate': 'yes',
                    'DelegateSubgroup': 'management'}
            fake_process = {'pid': 123, 'uid': os.getuid(), 'start_ticks': 77,
                            'cgroup': '/user.slice/job.service/management'}
            original_write_text = Path.write_text

            def kernel_write(path, data, *args, **kwargs):
                if path == service / 'cgroup.subtree_control':
                    self.assertEqual(data, '+cpu +memory +pids')
                    return original_write_text(path, 'cpu memory pids')
                return original_write_text(path, data, *args, **kwargs)

            with patch.object(base, 'cgdir', return_value=service), \
                    patch.object(base, 'process', return_value=fake_process), \
                    patch.object(Path, 'write_text', kernel_write):
                topology = base.setup_topology(
                    {'topology': base.TOPOLOGY, 'unit': 'job.service'}, info)
                for name, value in {
                        'cgroup.procs': '', 'memory.max': 'max',
                        'memory.high': 'max', 'memory.swap.max': 'max',
                        'pids.max': 'max', 'cpu.max': 'max 100000'}.items():
                    (workload / name).write_text(value)
                self.assertEqual(topology, {
                    'version': base.TOPOLOGY,
                    'service': '/user.slice/job.service',
                    'management': '/user.slice/job.service/management',
                    'workload': '/user.slice/job.service/workload'})
                ready = {'process': {**fake_process}, 'topology': topology,
                         'address_space': [resource.RLIM_INFINITY] * 2,
                         'process_limit': [resource.RLIM_INFINITY] * 2}
                info.update({name: 'infinity' for name in (
                    'LimitAS', 'LimitASSoft', 'RuntimeMaxUSec', 'MemoryMax',
                    'MemoryHigh', 'MemorySwapMax', 'CPUQuotaPerSecUSec',
                    'TasksMax', 'LimitNPROC', 'LimitNPROCSoft')})
                info.update(KillMode='mixed', Restart='no')
                with patch.object(base, 'cgdir', side_effect={
                        '/user.slice/job.service': service,
                        '/user.slice/job.service/management': management,
                        '/user.slice/job.service/workload': workload}.get):
                    actual = base.budget_check(
                        {'topology': base.TOPOLOGY,
                         'config': {'limits': {key: 'infinity' for key in (
                             'memory_bytes', 'cpu_percent', 'address_space_bytes',
                             'runtime_seconds')}}}, ready, info)
                self.assertEqual(set(actual), {
                    'memory.max', 'memory.high', 'memory.swap.max',
                    'pids.max', 'cpu.max'})
                self.assertEqual(actual['cpu.max'], 'max 100000')

    def test_unexpected_missing_or_unreadable_required_interface_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            service, management, workload = self.topology_fixture(Path(temp))
            ready = {'process': {'cgroup': '/user.slice/job.service/management'},
                     'topology': {'version': base.TOPOLOGY,
                                  'service': '/user.slice/job.service',
                                  'management': '/user.slice/job.service/management',
                                  'workload': '/user.slice/job.service/workload'}}
            intent = {'topology': base.TOPOLOGY,
                      'config': {'limits': {key: 'infinity' for key in (
                          'memory_bytes', 'cpu_percent', 'address_space_bytes',
                          'runtime_seconds')}}}
            info = {}
            paths = {'/user.slice/job.service': service,
                     '/user.slice/job.service/management': management,
                     '/user.slice/job.service/workload': workload}
            with patch.object(base, 'cgdir', side_effect=paths.get):
                (workload / 'memory.max').unlink()
                with self.assertRaises(FileNotFoundError):
                    base.budget_check(intent, ready, info)
                (workload / 'memory.max').write_text('max')
                original_read_text = Path.read_text

                def unreadable(path, *args, **kwargs):
                    if path == management / 'pids.max':
                        raise PermissionError(errno.EACCES, 'permission denied', str(path))
                    return original_read_text(path, *args, **kwargs)

                with patch.object(Path, 'read_text', unreadable):
                    with self.assertRaises(PermissionError):
                        base.budget_check(intent, ready, info)

    def test_controller_absence_and_launcher_identity_mismatch_refuse(self):
        with tempfile.TemporaryDirectory() as temp:
            service = Path(temp) / 'service'
            (service / 'management').mkdir(parents=True)
            (service / 'cgroup.procs').write_text('')
            (service / 'cgroup.controllers').write_text('cpu memory')
            info = {'ControlGroup': '/user.slice/job.service', 'Delegate': 'yes',
                    'DelegateSubgroup': 'management'}
            with patch.object(base, 'cgdir', return_value=service), \
                    patch.object(base, 'process', return_value={
                        'cgroup': '/user.slice/job.service/management'}):
                with self.assertRaisesRegex(base.Refused, 'controller_unavailable'):
                    base.setup_topology({'topology': base.TOPOLOGY,
                                         'unit': 'job.service'}, info)
            self.assertFalse((service / 'cgroup.subtree_control').exists())
            self.assertFalse((service / 'workload').exists())

            job = Path(temp) / 'job'
            job.mkdir(mode=0o700)
            (job / 'launch-intent.json').write_text(json.dumps({
                'launcher_process': {'pid': 123, 'uid': os.getuid(),
                                     'start_ticks': 77,
                                     'cgroup': '/user.slice/manager'}}))
            (job / 'launch-intent.json').chmod(0o600)
            with patch.object(base, 'process', return_value={
                    'pid': 123, 'uid': os.getuid(), 'start_ticks': 78,
                    'cgroup': '/user.slice/manager'}):
                with self.assertRaisesRegex(base.Refused, 'admission_launcher_lost'):
                    base.admission_wait_ok(job, False, float('inf'),
                                           require_launcher=True)

    def test_start_unit_preserves_primary_pre_admission_path_and_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp) / 'job'
            job.mkdir(mode=0o700)
            job_id = 'a' * 32
            intent = {'unit': 'wow-task-' + job_id + '.service',
                      'job_id': job_id, 'topology': base.TOPOLOGY,
                      'source_pins': {}, 'environment_sha256': 'env',
                      'config': {'sources': {}, 'cooperative': {'enabled': False}}}
            record = {'allocation': {'cpu': 1.0, 'fanout': 0,
                                     'checkpoint': False}}
            missing = FileNotFoundError(
                errno.ENOENT, 'missing required interface',
                '/sys/fs/cgroup/user.slice/' + intent['unit'] +
                '/management/memory.max')
            with patch.object(adaptive, 'intent_project_binding',
                              return_value={'unit_prefix': 'wow-task'}), \
                    patch.object(base, 'process', return_value={
                        'pid': 5, 'uid': os.getuid(), 'start_ticks': 8,
                        'cgroup': '/user.slice/manager'}), \
                    patch.object(base, 'command', return_value=SimpleNamespace(
                        returncode=0, stderr='')) as command, \
                    patch.object(base, 'wait_ready', side_effect=missing):
                with self.assertRaises(FileNotFoundError):
                    adaptive.start_unit(job, intent, record, {})
            self.assertIn('--property=Delegate=cpu memory pids',
                          command.call_args.args[0])
            receipt = json.loads((job / 'pre-admission-error.json').read_text())
            self.assertEqual(receipt, {
                'phase': 'readiness',
                'exception_type': 'FileNotFoundError',
                'path': '/sys/fs/cgroup/user.slice/' + intent['unit'] +
                        '/management/memory.max'})
            self.assertFalse((job / 'admission.json').exists())

            second_job = Path(temp) / 'second-job'
            second_job.mkdir(mode=0o700)
            with patch.object(adaptive, 'intent_project_binding',
                              return_value={'unit_prefix': 'wow-task'}), \
                    patch.object(base, 'process', return_value={
                        'pid': 5, 'uid': os.getuid(), 'start_ticks': 8,
                        'cgroup': '/user.slice/manager'}), \
                    patch.object(base, 'command', return_value=SimpleNamespace(
                        returncode=0, stderr='')), \
                    patch.object(base, 'wait_ready', side_effect=base.Refused(
                        'admission_launcher_lost')):
                with self.assertRaisesRegex(base.Refused, 'admission_launcher_lost'):
                    adaptive.start_unit(second_job, intent, record, {})
            identity_receipt = json.loads(
                (second_job / 'pre-admission-error.json').read_text())
            self.assertEqual(identity_receipt, {
                'phase': 'readiness', 'exception_type': 'Refused',
                'reason': 'admission_launcher_lost'})


if __name__ == '__main__':
    unittest.main()
