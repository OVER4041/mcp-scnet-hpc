"""OpenSSH subprocess transport with bounded output and binary streaming."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
import shlex
import shutil

from .config import BridgeError, Cluster, Settings


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    returncode: int
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False

    def checked(self) -> "Result":
        if self.returncode:
            detail = self.stderr.strip() or self.stdout.strip() or "No diagnostic output"
            hint = ""
            if self.returncode == 255:
                hint = (
                    " Check the SSH alias, known_hosts, network, and SCNet key expiry. "
                    "Complete any key passphrase setup with ssh-agent outside MCP."
                )
            raise BridgeError(f"Remote command failed (exit {self.returncode}): {detail}{hint}")
        return self

    def as_dict(self) -> dict:
        return {
            "stdout": self.stdout, "stderr": self.stderr,
            "exit_code": self.returncode, "truncated": self.truncated,
        }


class SSH:
    def __init__(self, settings: Settings):
        self.settings = settings
        executable = shutil.which(settings.ssh_command)
        if executable is None:
            raise BridgeError("OpenSSH client is missing; install the local SSH client first")
        self.executable = executable

    def arguments(self, cluster: Cluster, script: str) -> list[str]:
        args = [
            self.executable, "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"ConnectTimeout={cluster.connect_timeout}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2",
        ]
        if self.settings.ssh_config:
            args += ["-F", str(self.settings.ssh_config)]
        return args + [cluster.ssh_host, "bash -lc " + shlex.quote(script)]

    async def run(
        self, cluster: Cluster, script: str, *,
        timeout: int | None = None, input_bytes: bytes | None = None,
        input_path: Path | None = None, output_path: Path | None = None,
    ) -> Result:
        if input_bytes is not None and input_path is not None:
            raise BridgeError("Choose either input_bytes or input_path")
        process = await asyncio.create_subprocess_exec(
            *self.arguments(cluster, script),
            stdin=asyncio.subprocess.PIPE if input_bytes is not None or input_path is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        cap = self.settings.max_output_bytes
        maximum = self.settings.max_transfer_bytes

        async def collect(reader, file_path=None):
            data = bytearray()
            total = 0
            output = file_path.open("xb") if file_path else None
            try:
                while chunk := await reader.read(65536):
                    total += len(chunk)
                    if output is not None:
                        if total > maximum:
                            raise BridgeError("Transfer exceeded max_transfer_bytes")
                        output.write(chunk)
                    elif len(data) < cap:
                        data.extend(chunk[:cap - len(data)])
            finally:
                if output:
                    output.close()
            return bytes(data), total

        async def feed():
            if process.stdin is None:
                return
            sent = 0
            source = input_path.open("rb") if input_path else None
            try:
                if source is None:
                    chunk = input_bytes or b""
                    if len(chunk) > maximum:
                        raise BridgeError("Transfer exceeded max_transfer_bytes")
                    process.stdin.write(chunk)
                    await process.stdin.drain()
                else:
                    while chunk := source.read(65536):
                        sent += len(chunk)
                        if sent > maximum:
                            raise BridgeError("Transfer exceeded max_transfer_bytes")
                        process.stdin.write(chunk)
                        await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if source:
                    source.close()
                process.stdin.close()

        tasks = [
            asyncio.create_task(collect(process.stdout, output_path)),
            asyncio.create_task(collect(process.stderr)),
            asyncio.create_task(feed()),
            asyncio.create_task(process.wait()),
        ]
        try:
            stdout, stderr, _, code = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout or self.settings.command_timeout,
            )
        except BaseException as exc:
            if process.returncode is None:
                process.kill()
                await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, TimeoutError):
                raise BridgeError(
                    "SSH operation timed out. Remote execution may still have completed; "
                    "check its state before retrying a write or submission."
                ) from exc
            raise
        return Result(
            stdout[0].decode("utf-8", errors="replace"),
            stderr[0].decode("utf-8", errors="replace"), code,
            stdout[1], stderr[1],
            (output_path is None and stdout[1] > cap) or stderr[1] > cap,
        )
