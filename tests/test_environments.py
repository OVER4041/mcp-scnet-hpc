import asyncio
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scnet_mcp.bridge import Bridge
from scnet_mcp.config import BridgeError, Cluster, EnvironmentProfile, Settings
from scnet_mcp.environments import MARKER, render_job, setup_shell
from scnet_mcp.transport import Result


class FakeSSH:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def run(self, cluster, script, **kwargs):
        self.calls.append((script, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def bash_executable():
    if Path("/bin/bash").is_file():
        return "/bin/bash"
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


class EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.profile = EnvironmentProfile(
            "qe", "QE test", ("export TEST_QE=1",),
            {"pw.x": "/opt/test/qe/bin/pw.x"},
        )
        self.settings = Settings(
            local_root=self.root, state_dir=self.root / "state",
            clusters={"scnet": Cluster("scnet", "test-host", environments={"qe": self.profile})},
        )
        self.script = "#!/bin/bash\n#SBATCH -J science\n#SBATCH -N 2\n#SBATCH -p compute\n\npw.x -in input.in\n"

    def test_injection_preserves_slurm_directives_and_commands(self):
        prepared = render_job(self.script, self.profile, "run.slurm")
        before_code = prepared["script"].split("_scnet_mcp_restore_nounset=0")[0]
        self.assertIn("#SBATCH -N 2", before_code)
        self.assertIn("#SBATCH -p compute", before_code)
        self.assertIn("#SBATCH -J science", before_code)
        self.assertEqual(prepared["script"].count("#SBATCH -J"), 1)
        self.assertIn("export TEST_QE=1", prepared["script"])
        self.assertTrue(prepared["script"].endswith("pw.x -in input.in\n"))
        self.assertEqual(prepared["setup_location"], "batch_script")

    def test_no_explicit_job_name_keeps_the_script_filename(self):
        prepared = render_job("#!/bin/bash\necho hello\n", self.profile, "dir/test.slurm")
        self.assertIn("#SBATCH --job-name=test.slurm", prepared["script"])

    def test_existing_long_or_indented_job_name_is_preserved(self):
        prepared = render_job("#!/bin/bash\n  #SBATCH --job-name=chosen\necho hello\n", self.profile, "test.slurm")
        self.assertNotIn("--job-name=test.slurm", prepared["script"])

    def test_non_bash_and_repeated_injection_are_rejected(self):
        for script in ["#!/bin/sh\necho hello\n", "#!/usr/bin/python\nprint(1)\n", "echo hello\n"]:
            with self.subTest(script=script), self.assertRaises(BridgeError):
                render_job(script, self.profile, "run.slurm")
        prepared = render_job(self.script, self.profile, "run.slurm")
        with self.assertRaisesRegex(BridgeError, "already contains"):
            render_job(prepared["script"], self.profile, "run.slurm")

    def test_crlf_and_environment_overrides_are_reported(self):
        script = self.script.replace("pw.x", "module purge\nsource /opt/test/env.sh\npw.x").replace("\n", "\r\n")
        prepared = render_job(script, self.profile, "run.slurm")
        self.assertNotIn("\r", prepared["script"])
        self.assertEqual(len(prepared["warnings"]), 1)
        self.assertIn("may override", prepared["warnings"][0])

    def test_python_heredoc_assignments_do_not_warn(self):
        script = "#!/bin/bash\npython3 - <<'PY'\nsource = 'checkpoint'\nmodule = 'data'\nPY\necho ready\n"
        prepared = render_job(script, self.profile, 'run.slurm')
        self.assertEqual(prepared['warnings'], [])
        self.assertTrue(prepared['script'].endswith(script.split('\n', 1)[1]))

    def test_heredoc_content_is_skipped_but_later_shell_setup_warns(self):
        script = '#!/bin/bash\ncat <<"DATA"\nmodule purge\nsource /opt/data/env.sh\nDATA\nsource /opt/real/env.sh\n'
        warnings = render_job(script, self.profile, 'run.slurm')['warnings']
        self.assertEqual(len(warnings), 1)
        self.assertIn('lines 6;', warnings[0])

    def test_tab_stripped_and_multiple_heredocs(self):
        script = '#!/bin/bash\ncat <<-FIRST <<\'SECOND\'\n\tmodule purge\n\tFIRST\nsource /opt/data/env.sh\nSECOND\nmodule load actual\n'
        warnings = render_job(script, self.profile, 'run.slurm')['warnings']
        self.assertEqual(len(warnings), 1)
        self.assertIn('lines 7;', warnings[0])

    def test_quoted_operator_and_here_string_do_not_hide_later_commands(self):
        for command in ['printf "%s" "<<DATA"', 'cat <<< "payload"', 'echo hello # <<DATA']:
            with self.subTest(command=command):
                script = '#!/bin/bash\n' + command + '\nmodule purge\n'
                self.assertEqual(len(render_job(script, self.profile, 'run.slurm')['warnings']), 1)

    async def test_environment_is_submitted_in_stdin_and_not_loaded_on_login_node(self):
        transport = FakeSSH([Result(self.script, "", 0), Result("45678\n", "", 0)])
        bridge = Bridge(self.settings, transport)
        result = await bridge.submit_job("run.slurm", "with-qe", environment="qe")
        self.assertEqual(result["environment"], "qe")
        self.assertEqual(result["job_id"], "45678")
        self.assertEqual(len(transport.calls), 2)
        shell, kwargs = transport.calls[-1]
        self.assertIn("sbatch --parsable", shell)
        self.assertNotIn("export TEST_QE", shell)
        self.assertIn("export TEST_QE=1", kwargs["input_bytes"].decode())
        retry_transport = FakeSSH([])
        retry = await Bridge(self.settings, retry_transport).submit_job("run.slurm", "with-qe", environment="qe")
        self.assertTrue(retry["reused"])
        self.assertEqual(retry_transport.calls, [])

    async def test_environment_changes_cannot_reuse_request_id(self):
        bridge = Bridge(self.settings, FakeSSH([Result(self.script, "", 0), Result("45678\n", "", 0)]))
        await bridge.submit_job("run.slurm", "fixed", environment="qe")
        changed = replace(self.profile, setup=("export TEST_QE=2",))
        settings = replace(self.settings, clusters={"scnet": Cluster("scnet", "test-host", environments={"qe": changed})})
        transport = FakeSSH([])
        with self.assertRaisesRegex(BridgeError, "different submission arguments"):
            await Bridge(settings, transport).submit_job("run.slurm", "fixed", environment="qe")
        self.assertEqual(transport.calls, [])

    async def test_unknown_environment_does_not_reach_ssh(self):
        transport = FakeSSH([])
        with self.assertRaisesRegex(BridgeError, "Unknown environment"):
            await Bridge(self.settings, transport).submit_job("run.slurm", "unknown", environment="not-configured")
        self.assertEqual(transport.calls, [])

    async def test_truncated_script_is_not_submitted(self):
        transport = FakeSSH([Result(self.script, "", 0, truncated=True)])
        bridge = Bridge(self.settings, transport)
        with self.assertRaisesRegex(BridgeError, "truncated"):
            await bridge.submit_job("run.slurm", "truncated", environment="qe")
        self.assertEqual(len(transport.calls), 1)

    async def test_environment_check_returns_program_and_mpi_paths(self):
        transport = FakeSSH([Result("SCNET_MCP_EXEC=pw.x=/opt/test/qe/bin/pw.x\nSCNET_MCP_MPI=/opt/intel/bin/mpirun\n", "", 0)])
        result = await Bridge(self.settings, transport).check_environment("qe")
        self.assertTrue(result["checked"])
        self.assertEqual(result["mpirun"], "/opt/intel/bin/mpirun")

    def test_profile_config_parses_and_rejects_multiline_commands(self):
        profile = EnvironmentProfile.load("qe", {"setup": ["module use /opt/modules"], "executables": {"pw.x": "/opt/qe/bin/pw.x"}})
        self.assertEqual(profile.setup, ("module use /opt/modules",))
        with self.assertRaisesRegex(BridgeError, "single-line"):
            EnvironmentProfile.load("qe", {"setup": ["module use /opt/modules\nother command"]})
        with self.assertRaisesRegex(BridgeError, "filename"):
            EnvironmentProfile.load("qe", {"executables": {"pw.x": "/opt/qe/bin/epw.x"}})

    def test_setup_allows_vendor_unset_variables_and_restores_shell_flags(self):
        bash = bash_executable()
        if not bash:
            self.skipTest("Bash is unavailable")
        profile = EnvironmentProfile("vendor", setup=('printf "%s" "$SCNET_UNDEFINED_VENDOR_VARIABLE" >/dev/null',))
        strict = 'set -u\n' + setup_shell(profile) + '\ncase $- in *u*) echo restored;; *) exit 9;; esac\n'
        result = subprocess.run([bash, "--noprofile", "--norc", "-c", strict], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restored", result.stdout)
        loose = setup_shell(profile) + '\ncase $- in *u*) exit 9;; *) echo unchanged;; esac\n'
        result = subprocess.run([bash, "--noprofile", "--norc", "-c", loose], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unchanged", result.stdout)

    def test_failed_setup_stops_original_program(self):
        bash = bash_executable()
        if not bash:
            self.skipTest("Bash is unavailable")
        profile = EnvironmentProfile("broken", setup=("false",))
        result = subprocess.run([bash, "--noprofile", "--norc", "-c", setup_shell(profile) + "echo must-not-run\n"], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("must-not-run", result.stdout)


if __name__ == "__main__":
    unittest.main()
