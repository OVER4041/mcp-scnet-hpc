"""Validated local configuration; credentials remain in OpenSSH."""
from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib


class BridgeError(RuntimeError):
    """An actionable error that may be shown to the MCP client."""


@dataclass(frozen=True)
class EnvironmentProfile:
    name: str
    description: str = ""
    setup: tuple[str, ...] = ()
    executables: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, name: str, item: dict) -> "EnvironmentProfile":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
            raise BridgeError("Invalid environment profile name")
        if not isinstance(item, dict) or set(item) - {"description", "setup", "executables"}:
            raise BridgeError(f"Invalid options for environment {name}")
        description = item.get("description", "")
        setup = item.get("setup", [])
        executables = item.get("executables", {})
        if not isinstance(description, str):
            raise BridgeError("Environment description must be text")
        if not isinstance(setup, list) or not all(
            isinstance(command, str) and command.strip() and len(command) <= 8192
            and not any(ord(char) < 32 for char in command)
            for command in setup
        ):
            raise BridgeError("Environment setup must be a list of single-line shell commands")
        if not isinstance(executables, dict):
            raise BridgeError("Environment executables must be a mapping")
        for executable, path in executables.items():
            if not isinstance(executable, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}", executable):
                raise BridgeError("Invalid executable name in environment profile")
            if not isinstance(path, str) or not path.startswith("/") or ":" in path or any(ord(char) < 32 for char in path):
                raise BridgeError("Environment executable paths must be absolute POSIX paths without control characters or colons")
            if path.rsplit("/", 1)[-1] != executable:
                raise BridgeError("Executable mapping keys must match the executable filename")
        if not setup and not executables:
            raise BridgeError("An environment needs setup commands or executables")
        return cls(name, description, tuple(setup), dict(executables))


@dataclass(frozen=True)
class Cluster:
    name: str
    ssh_host: str
    remote_root: str = "~"
    connect_timeout: int = 15
    environments: dict[str, EnvironmentProfile] = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    local_root: Path
    state_dir: Path
    clusters: dict[str, Cluster]
    command_timeout: int = 30
    transfer_timeout: int = 300
    max_output_bytes: int = 65536
    max_transfer_bytes: int = 1073741824
    allow_remote_commands: bool = False
    ssh_command: str = "ssh"
    ssh_config: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Settings":
        path = path.resolve()
        try:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise BridgeError(f"Cannot read config {path}: {exc}") from exc
        server = data.get("server", {})
        allowed = {
            "local_root", "state_dir", "command_timeout", "transfer_timeout",
            "max_output_bytes", "max_transfer_bytes", "allow_remote_commands",
            "ssh_command", "ssh_config",
        }
        if set(server) - allowed:
            raise BridgeError(f"Unknown server options: {sorted(set(server) - allowed)}")
        def local_path(value: str) -> Path:
            candidate = Path(value).expanduser()
            return (candidate if candidate.is_absolute() else path.parent / candidate).resolve()
        root = local_path(server.get("local_root", str(path.parent)))
        if not root.is_dir():
            raise BridgeError(f"local_root is not an existing directory: {root}")
        clusters = {}
        for name, item in data.get("clusters", {}).items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
                raise BridgeError(f"Invalid cluster name: {name!r}")
            if set(item) - {"ssh_host", "remote_root", "connect_timeout", "environments"}:
                raise BridgeError(f"Unknown options for cluster {name}")
            host = item.get("ssh_host", "")
            if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,253}", host):
                raise BridgeError("ssh_host must be a concrete alias from the local SSH config")
            remote_root = item.get("remote_root", "~")
            if not isinstance(remote_root, str) or any(ord(c) < 32 for c in remote_root):
                raise BridgeError("remote_root contains invalid characters")
            if remote_root != "~" and (not remote_root.startswith("/") or remote_root == "/"):
                raise BridgeError("remote_root must be '~' or an absolute project directory other than '/'")
            timeout = item.get("connect_timeout", 15)
            if type(timeout) is not int or not 1 <= timeout <= 120:
                raise BridgeError("connect_timeout must be an integer from 1 to 120")
            raw_environments = item.get("environments", {})
            if not isinstance(raw_environments, dict):
                raise BridgeError("Cluster environments must be a mapping")
            environments = {
                environment_name: EnvironmentProfile.load(environment_name, profile)
                for environment_name, profile in raw_environments.items()
            }
            clusters[name] = Cluster(name, host, remote_root, timeout, environments)
        if not clusters:
            raise BridgeError("Configure at least one [clusters.NAME] entry")
        kwargs = {}
        for key, default, low, high in (
            ("command_timeout", 30, 1, 300),
            ("transfer_timeout", 300, 1, 3600),
            ("max_output_bytes", 65536, 1024, 1048576),
            ("max_transfer_bytes", 1073741824, 1, 1099511627776),
        ):
            value = server.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise BridgeError(f"{key} must be an integer from {low} to {high}")
            kwargs[key] = value
        raw_commands = server.get("allow_remote_commands", False)
        if type(raw_commands) is not bool:
            raise BridgeError("allow_remote_commands must be true or false")
        ssh_config = local_path(server["ssh_config"]) if "ssh_config" in server else None
        if ssh_config is not None and not ssh_config.is_file():
            raise BridgeError("ssh_config is not an existing file")
        return cls(
            local_root=root,
            state_dir=local_path(server.get("state_dir", ".state")),
            clusters=clusters,
            allow_remote_commands=raw_commands,
            ssh_command=server.get("ssh_command", "ssh"),
            ssh_config=ssh_config,
            **kwargs,
        )
