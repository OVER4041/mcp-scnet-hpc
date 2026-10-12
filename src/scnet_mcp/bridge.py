"""Structured files and Slurm operations; no remote Python or Codex required."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sqlite3
import tempfile
from datetime import datetime, timezone

from .config import BridgeError, Cluster, Settings
from .transport import SSH, Result


def quote(value: str) -> str:
    return shlex.quote(value)


def remote_path(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise BridgeError("Remote path must be nonempty and contain no control characters")
    if value.startswith(("/", "~")) or "\\" in value:
        raise BridgeError("Use a POSIX path relative to remote_root (for example cu_hpc/run.slurm)")
    if ".." in PurePosixPath(value).parts:
        raise BridgeError("Parent directory traversal is not allowed")
    return value


def job_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*(?:_[0-9]+)?", value):
        raise BridgeError("job_id must be a numeric Slurm job ID or array task ID")
    return value


def table(output: str, fields: list[str]) -> list[dict[str, str]]:
    rows = []
    for line in output.splitlines():
        values = line.strip().split("|", len(fields) - 1)
        if len(values) == len(fields):
            rows.append(dict(zip(fields, values)))
    return rows


class Bridge:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.ssh = transport if transport is not None else SSH(settings)

    def cluster(self, name: str) -> Cluster:
        try:
            return self.settings.clusters[name]
        except KeyError as exc:
            raise BridgeError(f"Unknown cluster {name!r}; call list_clusters") from exc

    def preamble(self, cluster: Cluster) -> str:
        root = '"$HOME"' if cluster.remote_root == "~" else quote(cluster.remote_root)
        return (
            "set -eu\nexport LC_ALL=C\n"
            f"root=$(realpath -e -- {root})\n"
            'test "$root" != / || { echo "remote_root cannot be /" >&2; exit 64; }\n'
            'cd -- "$root"\n'
        )

    def guard(self, value: str, variable: str = "target", kind: str = "") -> str:
        value = remote_path(value)
        result = (
            f"{variable}=$(realpath -m -- {quote(value)})\n"
            f'case "${variable}" in "$root"|"$root"/*) ;; '
            '*) echo "Path escapes remote_root (possibly through a symlink)" >&2; exit 64;; esac\n'
        )
        if kind:
            flag = {"file": "-f", "directory": "-d"}[kind]
            result += f'test {flag} "${variable}" || {{ echo "Missing {kind}" >&2; exit 66; }}\n'
        return result

    def local_path(self, value: str, *, exists: bool = False) -> Path:
        path = Path(value).expanduser()
        path = (path if path.is_absolute() else self.settings.local_root / path).resolve()
        try:
            path.relative_to(self.settings.local_root)
        except ValueError as exc:
            raise BridgeError("Local path escapes configured local_root") from exc
        if exists and not path.is_file():
            raise BridgeError("Local source is not an existing regular file")
        return path

    async def run(self, name: str, body: str, **kwargs) -> Result:
        chosen = self.cluster(name)
        return (await self.ssh.run(chosen, self.preamble(chosen) + body, **kwargs)).checked()

    def list_clusters(self) -> dict:
        return {
            "clusters": [
                {"name": c.name, "ssh_host": c.ssh_host, "remote_root": c.remote_root}
                for c in self.settings.clusters.values()
            ],
            "local_root": str(self.settings.local_root),
            "allow_remote_commands": self.settings.allow_remote_commands,
        }

    async def check_connection(self, cluster: str = "scnet") -> dict:
        body = (
            'printf "SCNET_MCP_HOST=%s\\n" "$(hostname)"\n'
            'printf "SCNET_MCP_USER=%s\\n" "$(id -un)"\n'
            'printf "SCNET_MCP_ROOT=%s\\n" "$root"\n'
            "for tool in sbatch squeue sacct scontrol scancel sinfo realpath find mktemp head tail cat stat; do\n"
            '  if command -v "$tool" >/dev/null 2>&1; then printf "SCNET_MCP_TOOL=%s\\n" "$tool"; fi\n'
            "done\n"
        )
        result = await self.run(cluster, body)
        fields, available = {}, []
        for line in result.stdout.splitlines():
            if line.startswith("SCNET_MCP_TOOL="):
                available.append(line.partition("=")[2])
            elif line.startswith("SCNET_MCP_"):
                key, _, value = line.partition("=")
                fields[key.removeprefix("SCNET_MCP_").lower()] = value
        if not {"host", "user", "root"} <= fields.keys():
            raise BridgeError("SSH returned unexpected output; check whether the gateway permits command execution")
        return {"cluster": cluster, **fields, "available_commands": available, "stderr": result.stderr}

    async def list_files(self, path: str = ".", cluster: str = "scnet") -> dict:
        body = self.guard(path, kind="directory") + (
            'find "$target" -mindepth 1 -maxdepth 1 -printf \'%y\\0%s\\0%f\\0\'\n'
        )
        result = await self.run(cluster, body)
        fields = result.stdout.split("\0")
        entries = [
            {"type": fields[i], "size_bytes": fields[i + 1], "name": fields[i + 2]}
            for i in range(0, len(fields) - 3, 3)
        ]
        return {"path": path, "entries": entries, "truncated": result.truncated}

    async def read_file(self, path: str, tail_lines: int = 0, cluster: str = "scnet") -> dict:
        if type(tail_lines) is not int or not 0 <= tail_lines <= 10000:
            raise BridgeError("tail_lines must be from 0 to 10000; 0 reads the beginning")
        body = self.guard(path, kind="file")
        if tail_lines:
            body += f'tail -n {tail_lines} -- "$target"\n'
        else:
            body += f'head -c {self.settings.max_output_bytes + 1} -- "$target"\n'
        result = await self.run(cluster, body)
        return {"path": path, "text": result.stdout, "truncated": result.truncated, "stderr": result.stderr}

    def write_body(self, path: str, overwrite: bool) -> str:
        if type(overwrite) is not bool:
            raise BridgeError("overwrite must be true or false")
        body = self.guard(path)
        body += (
            'test ! -d "$target" || { echo "Destination is a directory" >&2; exit 73; }\n'
            'mkdir -p -- "$(dirname -- "$target")"\n'
            'temporary=$(mktemp -- "${target}.mcp.XXXXXXXX")\n'
            'trap \'rm -f -- "$temporary"\' EXIT\n'
            'cat > "$temporary"\n'
        )
        if overwrite:
            body += 'mv -fT -- "$temporary" "$target"\n'
        else:
            body += 'ln -- "$temporary" "$target"\n'
        body += 'printf "SCNET_MCP_WRITTEN=%s\\n" "$target"\n'
        return body

    async def write_file(self, path: str, content: str, overwrite: bool = False, cluster: str = "scnet") -> dict:
        encoded = content.encode("utf-8")
        if len(encoded) > min(1048576, self.settings.max_transfer_bytes):
            raise BridgeError("Text is too large; use upload_file for larger files")
        result = await self.run(cluster, self.write_body(path, overwrite), input_bytes=encoded)
        return {"path": path, "bytes": len(encoded), "written": True, "stderr": result.stderr}

    async def upload_file(self, local_path: str, remote_path: str, overwrite: bool = False, cluster: str = "scnet") -> dict:
        source = self.local_path(local_path, exists=True)
        size = source.stat().st_size
        if size > self.settings.max_transfer_bytes:
            raise BridgeError("Source exceeds max_transfer_bytes")
        result = await self.run(
            cluster, self.write_body(remote_path, overwrite),
            input_path=source, timeout=self.settings.transfer_timeout,
        )
        return {"local_path": str(source), "remote_path": remote_path, "bytes": size, "uploaded": True, "stderr": result.stderr}

    async def download_file(self, remote_path: str, local_path: str, overwrite: bool = False, cluster: str = "scnet") -> dict:
        if type(overwrite) is not bool:
            raise BridgeError("overwrite must be true or false")
        destination = self.local_path(local_path)
        if destination.exists() and not overwrite:
            raise BridgeError("Destination already exists; set overwrite=true to replace it")
        if destination.is_dir():
            raise BridgeError("Destination is a directory")
        body = self.guard(remote_path, kind="file")
        size_result = await self.run(cluster, body + 'stat -c %s -- "$target"\n')
        try:
            size = int(size_result.stdout.strip())
        except ValueError as exc:
            raise BridgeError("Could not determine remote file size") from exc
        if size > self.settings.max_transfer_bytes:
            raise BridgeError("Remote file exceeds max_transfer_bytes")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = self.local_path(str(destination))
        descriptor, temporary_name = tempfile.mkstemp(prefix=".scnet-mcp-", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            result = await self.run(
                cluster, body + 'cat -- "$target"\n',
                output_path=temporary, timeout=self.settings.transfer_timeout,
            )
            if overwrite:
                os.replace(temporary, destination)
            else:
                if os.name == "nt":
                    os.rename(temporary, destination)
                else:
                    os.link(temporary, destination)
                    temporary.unlink()
            return {
                "local_path": str(destination), "remote_path": remote_path,
                "bytes": result.stdout_bytes, "downloaded": True, "stderr": result.stderr,
            }
        finally:
            temporary.unlink(missing_ok=True)

    async def list_partitions(self, cluster: str = "scnet") -> dict:
        result = await self.run(cluster, "sinfo --noheader --format='%P|%a|%l|%D|%t|%C'\n")
        return {
            "partitions": table(result.stdout, ["partition", "availability", "time_limit", "nodes", "state", "cpus"]),
            "truncated": result.truncated, "stderr": result.stderr,
        }

    async def list_jobs(self, cluster: str = "scnet") -> dict:
        result = await self.run(
            cluster, 'squeue --noheader --user="$(id -un)" --format=\'%i|%j|%T|%M|%l|%D|%R\'\n',
        )
        return {
            "jobs": table(result.stdout, ["job_id", "name", "state", "elapsed", "time_limit", "nodes", "reason_or_nodes"]),
            "truncated": result.truncated, "stderr": result.stderr,
        }

    async def job_details(self, job_id_value: str, cluster: str = "scnet") -> dict:
        identifier = job_id(job_id_value)
        result = await self.ssh.run(
            self.cluster(cluster), self.preamble(self.cluster(cluster)) + f"scontrol show job {quote(identifier)}\n",
        )
        if result.returncode == 0:
            return {"job_id": identifier, "source": "scontrol", **result.as_dict()}
        if result.returncode == 255:
            result.checked()
        accounting = await self.run(
            cluster,
            f"sacct --jobs={quote(identifier)} --user=\"$(id -un)\" --parsable2 --noheader "
            "--format=JobID,JobName,State,ExitCode,Elapsed,AllocCPUS,MaxRSS\n",
        )
        return {
            "job_id": identifier, "source": "sacct",
            "records": table(accounting.stdout, ["job_id", "name", "state", "exit_code", "elapsed", "cpus", "max_rss"]),
            "truncated": accounting.truncated, "stderr": accounting.stderr,
        }

    def database(self):
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.settings.state_dir / "submissions.sqlite3", timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE IF NOT EXISTS submissions ("
            "cluster TEXT NOT NULL, request_id TEXT NOT NULL, signature TEXT NOT NULL, "
            "state TEXT NOT NULL, job_id TEXT, detail TEXT, updated_at TEXT NOT NULL, "
            "PRIMARY KEY(cluster, request_id))"
        )
        return connection

    def submission_record(self, request_id: str, cluster: str = "scnet") -> dict:
        self.cluster(cluster)
        self.validate_request_id(request_id)
        connection = self.database()
        try:
            row = connection.execute(
                "SELECT request_id, state, job_id, detail, updated_at FROM submissions WHERE cluster=? AND request_id=?",
                (cluster, request_id),
            ).fetchone()
            return dict(row) if row else {"request_id": request_id, "state": "not_found"}
        finally:
            connection.close()

    @staticmethod
    def validate_request_id(value: str):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
            raise BridgeError("request_id must be 1-80 letters, digits, dots, underscores or hyphens")

    async def submit_job(
        self, script_path: str, request_id: str, workdir: str = ".",
        dependencies: list[str] | None = None, cluster: str = "scnet",
    ) -> dict:
        self.validate_request_id(request_id)
        chosen = self.cluster(cluster)
        script_path = remote_path(script_path)
        workdir = remote_path(workdir)
        dependencies = [job_id(v) for v in dependencies or []]
        if len(dependencies) > 100:
            raise BridgeError("At most 100 dependency job IDs are allowed")
        signature = hashlib.sha256(json.dumps(
            [chosen.ssh_host, chosen.remote_root, script_path, workdir, dependencies],
            ensure_ascii=True,
        ).encode()).hexdigest()
        connection = self.database()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM submissions WHERE cluster=? AND request_id=?", (cluster, request_id),
            ).fetchone()
            if row:
                if row["signature"] != signature:
                    raise BridgeError("request_id was already used with different submission arguments")
                if row["state"] == "submitted":
                    return {"job_id": row["job_id"], "request_id": request_id, "reused": True}
                raise BridgeError(
                    f"Submission {request_id!r} is {row['state']}. It will not be submitted again. "
                    "Check list_jobs/job_details and submission_record before considering a new request_id."
                )
            connection.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, 'submitting', NULL, NULL, ?)",
                (cluster, request_id, signature, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        finally:
            connection.close()
        body = self.guard(script_path, "script_file", "file") + self.guard(workdir, "working_dir", "directory")
        dependency = f" --dependency=afterok:{':'.join(dependencies)}" if dependencies else ""
        body += f'sbatch --parsable --chdir="$working_dir"{dependency} -- "$script_file"\n'
        try:
            result = await self.run(cluster, body)
            lines = [line.strip() for line in result.stdout.splitlines() if re.fullmatch(r"[1-9][0-9]*(?:;[A-Za-z0-9_.-]+)?", line.strip())]
            if len(lines) != 1:
                raise BridgeError("Submission returned an ambiguous job ID; check the queue before retrying")
            identifier = lines[0].split(";")[0]
        except BaseException as exc:
            self.update_submission(cluster, request_id, "uncertain", None, str(exc)[:4096])
            raise
        self.update_submission(cluster, request_id, "submitted", identifier, result.stderr[:4096])
        return {"job_id": identifier, "request_id": request_id, "reused": False, "stderr": result.stderr}

    def update_submission(self, cluster, request_id, state, identifier, detail):
        connection = self.database()
        try:
            connection.execute(
                "UPDATE submissions SET state=?, job_id=?, detail=?, updated_at=? WHERE cluster=? AND request_id=?",
                (state, identifier, detail, datetime.now(timezone.utc).isoformat(), cluster, request_id),
            )
            connection.commit()
        finally:
            connection.close()

    async def cancel_job(self, job_id_value: str, cluster: str = "scnet") -> dict:
        identifier = job_id(job_id_value)
        result = await self.run(cluster, f'scancel --user="$(id -un)" -- {quote(identifier)}\n')
        return {"job_id": identifier, "cancel_requested": True, "stderr": result.stderr}

    async def run_command(self, command: str, workdir: str = ".", cluster: str = "scnet") -> dict:
        if not self.settings.allow_remote_commands:
            raise BridgeError("run_command is disabled in the local configuration")
        if not command.strip() or len(command) > 8192 or "\0" in command:
            raise BridgeError("Command is empty or too long")
        result = await self.run(
            cluster, self.guard(workdir, kind="directory") + 'cd -- "$target"\n' + command + "\n",
        )
        return result.as_dict()
