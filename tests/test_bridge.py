import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

from scnet_mcp.bridge import Bridge, job_id, remote_path
from scnet_mcp.config import BridgeError, Cluster, Settings
from scnet_mcp.transport import SSH, Result


class FakeSSH:
    def __init__(self, results=None):
        self.results = list(results or [Result("12345\n", "", 0)])
        self.calls = []

    async def run(self, cluster, script, **kwargs):
        self.calls.append((cluster, script, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class LocalProcessSSH(SSH):
    def arguments(self, cluster, script):
        return [sys.executable, "-c", script]


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.settings = Settings(
            local_root=self.root, state_dir=self.root / "state",
            clusters={"scnet": Cluster("scnet", "test-host")},
            max_output_bytes=1024,
        )

    async def test_submission_survives_restart_without_resubmitting(self):
        first_transport = FakeSSH()
        result = await Bridge(self.settings, first_transport).submit_job("run.slurm", "one")
        self.assertEqual(result["job_id"], "12345")
        new_transport = FakeSSH()
        recovered = await Bridge(self.settings, new_transport).submit_job("run.slurm", "one")
        self.assertTrue(recovered["reused"])
        self.assertEqual(new_transport.calls, [])

    async def test_lost_connection_is_not_retried(self):
        transport = FakeSSH([BridgeError("timeout after submission")])
        bridge = Bridge(self.settings, transport)
        with self.assertRaises(BridgeError):
            await bridge.submit_job("run.slurm", "lost")
        self.assertEqual(bridge.submission_record("lost")["state"], "uncertain")
        with self.assertRaisesRegex(BridgeError, "will not be submitted again"):
            await bridge.submit_job("run.slurm", "lost")
        self.assertEqual(len(transport.calls), 1)

    async def test_request_id_cannot_be_reused_for_another_job(self):
        bridge = Bridge(self.settings, FakeSSH())
        await bridge.submit_job("first.slurm", "same")
        with self.assertRaisesRegex(BridgeError, "different submission arguments"):
            await bridge.submit_job("second.slurm", "same")

    async def test_concurrent_request_does_not_submit_twice(self):
        started, release = asyncio.Event(), asyncio.Event()
        class DelayedSSH(FakeSSH):
            async def run(inner_self, cluster, script, **kwargs):
                started.set()
                await release.wait()
                return await super(DelayedSSH, inner_self).run(cluster, script, **kwargs)
        transport = DelayedSSH()
        bridge = Bridge(self.settings, transport)
        task = asyncio.create_task(bridge.submit_job("run.slurm", "concurrent"))
        await started.wait()
        try:
            with self.assertRaisesRegex(BridgeError, "submitting"):
                await bridge.submit_job("run.slurm", "concurrent")
        finally:
            release.set()
            await task
        self.assertEqual(len(transport.calls), 1)

    async def test_ambiguous_submit_output_is_recorded(self):
        bridge = Bridge(self.settings, FakeSSH([Result("banner only\n", "", 0)]))
        with self.assertRaisesRegex(BridgeError, "ambiguous"):
            await bridge.submit_job("run.slurm", "ambiguous")
        self.assertEqual(bridge.submission_record("ambiguous")["state"], "uncertain")

    async def test_slurm_accounting_fallback(self):
        bridge = Bridge(self.settings, FakeSSH([
            Result("", "Invalid job id", 1),
            Result("12345|test|COMPLETED|0:0|00:01:00|1|2M\n", "", 0),
        ]))
        detail = await bridge.job_details("12345")
        self.assertEqual(detail["source"], "sacct")
        self.assertEqual(detail["records"][0]["state"], "COMPLETED")

    async def test_ssh_error_is_not_misreported_as_completed_job(self):
        bridge = Bridge(self.settings, FakeSSH([Result("", "Permission denied", 255)]))
        with self.assertRaisesRegex(BridgeError, "key expiry"):
            await bridge.job_details("12345")

    async def test_invalid_paths_and_job_ids_never_reach_ssh(self):
        transport = FakeSSH()
        bridge = Bridge(self.settings, transport)
        for path in ["../private", "/etc/passwd", "~/.ssh/key", "bad\nname", r"folder\file"]:
            with self.subTest(path=path), self.assertRaises(BridgeError):
                await bridge.read_file(path)
        for identifier in ["1; rm -rf /", "-1", "ALL", "", "1_2;id"]:
            with self.subTest(identifier=identifier), self.assertRaises(BridgeError):
                await bridge.cancel_job(identifier)
        self.assertEqual(transport.calls, [])

    def test_local_path_scope_and_symlink(self):
        bridge = Bridge(self.settings, FakeSSH())
        with self.assertRaises(BridgeError):
            bridge.local_path("../outside")
        outside = Path(self.temporary.name).parent
        link = self.root / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks is not available")
        with self.assertRaises(BridgeError):
            bridge.local_path("link/outside")

    async def test_explicit_overwrite_and_command_opt_in(self):
        destination = self.root / "result.txt"
        destination.write_text("original")
        bridge = Bridge(self.settings, FakeSSH())
        with self.assertRaisesRegex(BridgeError, "already exists"):
            await bridge.download_file("result.txt", "result.txt")
        self.assertEqual(destination.read_text(), "original")
        with self.assertRaisesRegex(BridgeError, "disabled"):
            await bridge.run_command("hostname")

    async def test_bounded_stdout_and_stderr_are_drained(self):
        transport = LocalProcessSSH(self.settings)
        result = await transport.run(self.settings.clusters["scnet"],
            "import sys; sys.stdout.write('x'*200000); sys.stderr.write('e'*200000)")
        self.assertEqual(len(result.stdout), 1024)
        self.assertEqual(len(result.stderr), 1024)
        self.assertTrue(result.truncated)
        self.assertEqual(result.returncode, 0)

    async def test_binary_streaming_is_exact(self):
        payload = bytes(range(256)) * 1024
        destination = self.root / "binary"
        result = await LocalProcessSSH(self.settings).run(
            self.settings.clusters["scnet"],
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            input_bytes=payload, output_path=destination,
        )
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(result.stdout_bytes, len(payload))
        self.assertFalse(result.truncated)

    async def test_streaming_limit(self):
        limited = replace(self.settings, max_transfer_bytes=100)
        with self.assertRaisesRegex(BridgeError, "exceeded"):
            await LocalProcessSSH(limited).run(
                limited.clusters["scnet"],
                "import sys; sys.stdout.buffer.write(b'x'*1000)",
                output_path=self.root / "too-large",
            )

    async def test_timeout_terminates_local_ssh_process(self):
        with self.assertRaisesRegex(BridgeError, "may still have completed"):
            await LocalProcessSSH(self.settings).run(
                self.settings.clusters["scnet"],
                "import time; time.sleep(10)",
                timeout=0.2,
            )

    def test_invalid_configuration_is_rejected(self):
        path = self.root / "config.toml"
        path.write_text('[clusters.scnet]\nssh_host = "-oProxyCommand=bad"\n', encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "concrete alias"):
            Settings.load(path)

    def test_shell_metacharacters_are_accepted_as_filename_data(self):
        value = "dir/file ' $literal; data.txt"
        self.assertEqual(remote_path(value), value)
        self.assertEqual(job_id("123_4"), "123_4")


if __name__ == "__main__":
    unittest.main()
