import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from candidate_identity import harness_digest, identity
from test_project_policy import POLICY


class SharedIdentityTests(unittest.TestCase):
    def test_identity_binds_project_revision_policy_and_shared_executables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / 'project'
            project.mkdir()
            subprocess.run(['git', '-C', str(project), 'init', '-q'], check=True)
            (project / 'source').write_text('initial')
            subprocess.run(['git', '-C', str(project), 'add', 'source'], check=True)
            subprocess.run(['git', '-C', str(project), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null',
                            'commit', '-qm', 'initial'], check=True)
            tool = root / 'tool' / 'harness'
            tool.mkdir(parents=True)
            here = Path(__file__).parent
            for name in ('linear_intake.py', 'acceptance.py', 'candidate_identity.py',
                         'project_policy.py', 'post_builder.py', 'prepare_runtime.py'):
                shutil.copyfile(here / name, tool / name)
            policy = copy.deepcopy(POLICY)
            policy.update(shared_harness=True, shared_harness_sha256=harness_digest(tool.parent))
            with self.assertRaises(ValueError):
                identity(project, harness_root=tool.parent, policy=POLICY)
            baseline = identity(project, harness_root=tool.parent, policy=policy)
            with self.assertRaises(ValueError):
                identity(project, harness_root=tool.parent)
            altered = copy.deepcopy(policy)
            altered['require_writer_paths'] = True
            self.assertNotEqual(baseline, identity(project, harness_root=tool.parent, policy=altered))
            with (tool / 'linear_intake.py').open('a') as stream:
                stream.write('\n# changed\n')
            with self.assertRaises(ValueError):
                identity(project, harness_root=tool.parent, policy=policy)
            self.assertNotEqual(baseline, identity(project, harness_root=tool.parent, policy=policy, allow_unapproved_control=True))
            policy_file = root / 'policy.json'
            policy_file.write_text(json.dumps(policy))
            command = ['python3', str(tool / 'candidate_identity.py'), '--project-root', str(project), '--policy', str(policy_file)]
            refused = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(refused.stdout, '')
            control = subprocess.run(command + ['--control-unpinned'], capture_output=True, text=True, check=True)
            self.assertNotEqual(control.stdout.strip(), baseline)
            shutil.copyfile(here / 'linear_intake.py', tool / 'linear_intake.py')
            (project / 'source').write_text('changed')
            subprocess.run(['git', '-C', str(project), 'add', 'source'], check=True)
            subprocess.run(['git', '-C', str(project), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null',
                            'commit', '-qm', 'changed'], check=True)
            self.assertNotEqual(baseline, identity(project, harness_root=tool.parent, policy=policy))


if __name__ == '__main__':
    unittest.main()
