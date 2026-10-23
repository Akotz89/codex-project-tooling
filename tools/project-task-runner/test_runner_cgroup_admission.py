"""Focused controller-topology and pre-admission failure receipts."""
import errno
from contextlib import nullcontext
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

    def test_manager_default_nproc_uses_same_user_bus_and_rejects_missing_values(self):
        env = {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/fixture/bus'}
        result = SimpleNamespace(
            returncode=0,
            stdout='DefaultLimitNPROC=256751\nDefaultLimitNPROCSoft=256751\n',
            stderr='')
        with patch.object(base, 'command', return_value=result) as command:
            self.assertEqual(base.manager_default_process_limit(env),
                             [256751, 256751])
        self.assertEqual(command.call_args.args[0], [
            'systemctl', '--user', 'show',
            '--property=DefaultLimitNPROC,DefaultLimitNPROCSoft'])
        self.assertIs(command.call_args.args[1], env)
        self.assertEqual(base.native_process_limit_baseline(
            [256751, 256751], [256751, 256751]), [256751, 256751])
        with self.assertRaisesRegex(base.Refused, 'process_limit_baseline_manager'):
            base.native_process_limit_baseline([256751, 256751], [256750, 256751])

        for output in ('DefaultLimitNPROC=256751\n',
                       'DefaultLimitNPROC=256751\nDefaultLimitNPROCSoft=256752\n',
                       'DefaultLimitNPROC=256751\nDefaultLimitNPROCSoft=garbage\n'):
            with self.subTest(output=output), \
                    patch.object(base, 'command', return_value=SimpleNamespace(
                        returncode=0, stdout=output, stderr='')):
                with self.assertRaises(base.Refused):
                    base.manager_default_process_limit(env)

    def test_adaptive_launch_binds_observed_native_and_manager_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'work').mkdir(mode=0o700)
            state = root / 'state'
            state.mkdir(mode=0o700)
            job = root / 'work' / 'job'
            baseline = base.process_limit_pair(
                base.resource.getrlimit(base.resource.RLIMIT_NPROC),
                'process_limit_baseline')
            previous = (adaptive.ROOT, adaptive.CANONICAL_STATE,
                        adaptive.PROJECT_ROOT, adaptive.PROJECT_ID)

            def restore_context():
                adaptive.ROOT, adaptive.CANONICAL_STATE, \
                    adaptive.PROJECT_ROOT, adaptive.PROJECT_ID = previous

            self.addCleanup(restore_context)
            config = {'argv': ['/usr/bin/true'], 'owner': 'fixture-owner',
                      'lease': 'fixture-lease',
                      'estimates': {'memory_bytes': 128, 'disk_bytes': 128,
                                    'inodes': 1},
                      'foreground': False, 'cooperative': {'enabled': False}}
            binding = {'project_id': 'fixture-project',
                       'unit_prefix': 'fixture-task',
                       'payload_environment': {}}
            captured = {}

            def stop_before_runtime(path, env):
                captured['job'] = path
                captured['intent'] = base.read(path / 'intent.json')
                captured['intent_hash'] = base.digest(
                    base.read_bytes(path / 'intent.json'))
                return {'fixture_only': True}

            with patch.object(adaptive, 'configuration',
                              return_value=(config, 'fixture-config-hash')), \
                    patch.object(base, 'manager_environment',
                                 return_value=({}, 'fixture-manager')), \
                    patch.object(base, 'manager_default_process_limit',
                                 return_value=baseline), \
                    patch.object(adaptive, 'locked',
                                 side_effect=lambda unused: nullcontext()), \
                    patch.object(adaptive, 'read_ledger',
                                 side_effect=lambda unused: {'jobs': {}}), \
                    patch.object(adaptive, 'replace'), \
                    patch.object(adaptive, 'advance', side_effect=stop_before_runtime):
                result = adaptive.launch(
                    job, root / 'config.json', state,
                    project_root=root, project_binding=binding)

            self.assertEqual(result, {'fixture_only': True})
            self.assertEqual(captured['intent']['native_process_limit'], baseline)
            self.assertEqual(captured['intent']['manager_default_process_limit'],
                             baseline)
            self.assertEqual(captured['intent']['job_id'],
                             captured['intent']['unit'].split('-')[-1].removesuffix('.service'))
            self.assertEqual(captured['intent_hash'],
                             base.digest(base.canonical(captured['intent']) + b'\n'))
            without_baseline = dict(captured['intent'])
            without_baseline.pop('native_process_limit')
            without_baseline.pop('manager_default_process_limit')
            self.assertNotEqual(captured['intent_hash'],
                                base.digest(base.canonical(without_baseline) + b'\n'))

    def test_base_launch_omits_nproc_override_for_bound_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / 'actual-use'
            parent.mkdir(mode=0o700)
            job = parent / 'job'
            baseline = base.process_limit_pair(
                resource.getrlimit(resource.RLIMIT_NPROC), 'process_limit_baseline')
            config = {'argv': ['/usr/bin/true'], 'limits': {
                'memory_bytes': 'infinity', 'cpu_percent': 'infinity',
                'address_space_bytes': 'infinity', 'runtime_seconds': 'infinity'}}
            captured = {}

            def reject_before_runtime(argv, env):
                captured['argv'] = argv
                captured['launch'] = base.read(job / 'launch-intent.json')
                return SimpleNamespace(returncode=1, stderr='fixture refusal')

            with patch.object(base, 'configuration', return_value=(config, 'fixture-hash')), \
                    patch.object(base, 'manager_environment', return_value=({}, 'fixture-manager')), \
                    patch.object(base, 'manager_default_process_limit', return_value=baseline), \
                    patch.object(base, 'command', side_effect=reject_before_runtime), \
                    self.assertRaisesRegex(base.Refused, 'manager_launch_rejected'):
                base.launch(job, root / 'config.json')

            self.assertNotIn('LimitNPROC', captured['launch']['properties'])
            self.assertNotIn('--property=LimitNPROC=infinity', captured['argv'])
            self.assertEqual(captured['launch']['native_process_limit'], baseline)
            self.assertEqual(captured['launch']['manager_default_process_limit'], baseline)

    def test_adaptive_start_unit_omits_nproc_override_for_bound_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp) / 'job'
            job.mkdir(mode=0o700)
            baseline = base.process_limit_pair(
                resource.getrlimit(resource.RLIMIT_NPROC), 'process_limit_baseline')
            intent = {'job_id': 'a' * 32, 'unit': 'fixture-' + 'a' * 32 + '.service',
                      'native_process_limit': baseline,
                      'manager_default_process_limit': baseline}
            record = {'allocation': {'cpu': 1}}
            captured = {}

            def reject_before_runtime(argv, env):
                captured['argv'] = argv
                captured['launch'] = base.read(job / 'launch-intent.json')
                return SimpleNamespace(returncode=1, stderr='fixture refusal')

            with patch.object(base, 'manager_default_process_limit', return_value=baseline), \
                    patch.object(adaptive, 'intent_project_binding', return_value={
                        'unit_prefix': 'fixture'}), \
                    patch.object(base, 'command', side_effect=reject_before_runtime), \
                    patch.object(base, 'unit_show', return_value={'LoadState': 'not-found'}):
                adaptive.start_unit(job, intent, record, {})

            self.assertEqual(record['state'], 'failed-before-launch')
            self.assertNotIn('LimitNPROC', captured['launch']['properties'])
            self.assertNotIn('--property=LimitNPROC=infinity', captured['argv'])
            self.assertEqual(captured['launch']['native_process_limit'], baseline)
            self.assertEqual(captured['launch']['manager_default_process_limit'], baseline)

    def test_true_unlimited_bound_baseline_round_trips_without_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / 'actual-use'
            parent.mkdir(mode=0o700)
            job = parent / 'job'
            infinity_pair = [resource.RLIM_INFINITY, resource.RLIM_INFINITY]
            config = {'argv': ['/usr/bin/true'], 'limits': {
                'memory_bytes': 'infinity', 'cpu_percent': 'infinity',
                'address_space_bytes': 'infinity', 'runtime_seconds': 'infinity'}}
            captured = {}

            def reject_before_runtime(argv, env):
                captured['argv'] = argv
                captured['intent'] = base.read(job / 'intent.json')
                captured['launch'] = base.read(job / 'launch-intent.json')
                return SimpleNamespace(returncode=1, stderr='fixture refusal')

            with patch.object(base, 'configuration', return_value=(config, 'fixture-hash')), \
                    patch.object(base, 'manager_environment', return_value=({}, 'fixture-manager')), \
                    patch.object(base, 'manager_default_process_limit',
                                 return_value=infinity_pair), \
                    patch.object(base.resource, 'getrlimit', return_value=tuple(infinity_pair)), \
                    patch.object(base, 'command', side_effect=reject_before_runtime), \
                    self.assertRaisesRegex(base.Refused, 'manager_launch_rejected'):
                base.launch(job, root / 'config.json')

            self.assertEqual(captured['intent']['native_process_limit'], infinity_pair)
            self.assertEqual(captured['intent']['manager_default_process_limit'], infinity_pair)
            self.assertEqual(captured['launch']['native_process_limit'], infinity_pair)
            self.assertEqual(captured['launch']['manager_default_process_limit'], infinity_pair)
            self.assertNotIn('LimitNPROC', captured['launch']['properties'])
            self.assertNotIn('--property=LimitNPROC=infinity', captured['argv'])

            cgroup = root / 'cgroup'
            cgroup.mkdir()
            for name, value in {'memory.max': 'max', 'memory.high': 'max',
                                'memory.swap.max': 'max', 'pids.max': 'max',
                                'cpu.max': 'max 100000'}.items():
                (cgroup / name).write_text(value)
            ready = {'process': {'cgroup': '/fixture'},
                     'address_space': infinity_pair, 'process_limit': infinity_pair}
            info = {name: 'infinity' for name in (
                'LimitAS', 'LimitASSoft', 'RuntimeMaxUSec', 'MemoryMax',
                'MemoryHigh', 'MemorySwapMax', 'CPUQuotaPerSecUSec', 'TasksMax',
                'LimitNPROC', 'LimitNPROCSoft')}
            info.update(KillMode='mixed', Restart='no')
            with patch.object(base, 'cgdir', return_value=cgroup):
                base.budget_check(captured['intent'], ready, info)

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

    def test_custody_requires_launch_baseline_to_match_hashed_intent(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp) / 'job'
            job.mkdir(mode=0o700)
            job_id = 'b' * 32
            baseline = [256751, 256751]
            intent = {'job_id': job_id, 'unit': 'project-task-' + job_id + '.service',
                      'job_dir': str(job), 'uid': os.getuid(),
                      'native_process_limit': baseline,
                      'manager_default_process_limit': baseline}
            base.publish(job / 'intent.json', intent)
            ready = {'job_id': job_id,
                     'intent_sha256': base.digest(base.read_bytes(job / 'intent.json')),
                     'invocation_id': 'fixture-invocation',
                     'process': {'pid': 123, 'uid': os.getuid(), 'start_ticks': 77,
                                 'cgroup': '/user.slice/fixture.service'}}
            base.publish(job / 'ready.json', ready)
            info = {'LoadState': 'loaded', 'InvocationID': 'fixture-invocation',
                    'Description': base.unit_description(intent),
                    'ControlGroup': '/user.slice/fixture.service', 'MainPID': '0'}
            with patch.object(base, 'process', side_effect=FileNotFoundError):
                for launch_record in (
                        {'job_id': job_id},
                        {'job_id': job_id, 'native_process_limit': baseline},
                        {'job_id': job_id, 'native_process_limit': [256750, 256751],
                         'manager_default_process_limit': baseline}):
                    with self.subTest(launch_record=launch_record):
                        launch_path = job / 'launch-intent.json'
                        launch_path.unlink(missing_ok=True)
                        base.publish(launch_path, launch_record)
                        with self.assertRaisesRegex(base.Refused,
                                                    'process_limit_baseline_binding'):
                            base.custody(job, intent, info)

    def test_start_unit_preserves_primary_pre_admission_path_and_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            job = Path(temp) / 'job'
            job.mkdir(mode=0o700)
            job_id = 'a' * 32
            intent = {'unit': 'wow-task-' + job_id + '.service',
                      'job_id': job_id, 'topology': base.TOPOLOGY,
                      'source_pins': {}, 'environment_sha256': 'env',
                      'native_process_limit': base.process_limit_pair(
                          base.resource.getrlimit(base.resource.RLIMIT_NPROC),
                          'process_limit_baseline'),
                      'manager_default_process_limit': base.process_limit_pair(
                          base.resource.getrlimit(base.resource.RLIMIT_NPROC),
                          'process_limit_manager_default'),
                      'config': {'sources': {}, 'cooperative': {'enabled': False}}}
            record = {'allocation': {'cpu': 1.0, 'fanout': 0,
                                     'checkpoint': False}}
            missing = FileNotFoundError(
                errno.ENOENT, 'missing required interface',
                '/sys/fs/cgroup/user.slice/' + intent['unit'] +
                '/management/memory.max')
            with patch.object(adaptive, 'intent_project_binding',
                              return_value={'unit_prefix': 'wow-task'}), \
                    patch.object(base, 'manager_default_process_limit',
                                 return_value=intent['manager_default_process_limit']), \
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
            launch_intent = base.read(job / 'launch-intent.json')
            self.assertEqual(launch_intent['native_process_limit'],
                             intent['native_process_limit'])
            self.assertEqual(launch_intent['manager_default_process_limit'],
                             intent['manager_default_process_limit'])
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
                    patch.object(base, 'manager_default_process_limit',
                                 return_value=intent['manager_default_process_limit']), \
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
