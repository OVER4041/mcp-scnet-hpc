"""Environment setup and Slurm script rendering without changing the source file."""
from pathlib import PurePosixPath
import re
import shlex

from .config import BridgeError, EnvironmentProfile

MARKER = "# SCNET_MCP_ENVIRONMENT_BEGIN"


def setup_shell(profile: EnvironmentProfile) -> str:
    lines = [
        MARKER,
        "_scnet_mcp_restore_nounset=0",
        "case $- in *u*) _scnet_mcp_restore_nounset=1; set +u;; esac",
    ]
    # Vendor env.sh files often reference undefined SLURM_JOBID/LD_LIBRARY_PATH.
    # Disable only nounset during setup and restore its original state afterward.
    for command in profile.setup:
        lines += [
            command,
            "_scnet_mcp_setup_status=$?",
            'if [ "$_scnet_mcp_setup_status" -ne 0 ]; then',
            f"  printf '%s\\n' {shlex.quote('Environment setup failed: ' + profile.name)} >&2",
            '  exit "$_scnet_mcp_setup_status"',
            "fi",
        ]
    directories = list(dict.fromkeys(str(PurePosixPath(path).parent) for path in profile.executables.values()))
    if directories:
        lines.append("export PATH=" + shlex.quote(":".join(directories)) + ':"$PATH"')
    lines.append("export SCNET_MCP_ENVIRONMENT=" + shlex.quote(profile.name))
    for name, path in profile.executables.items():
        lines.append(
            "test -x " + shlex.quote(path) + " || { printf '%s\\n' "
            + shlex.quote("Configured executable is unavailable: " + name) + " >&2; exit 66; }"
        )
    lines += [
        'if [ "$_scnet_mcp_restore_nounset" -eq 1 ]; then set -u; fi',
        "unset _scnet_mcp_restore_nounset _scnet_mcp_setup_status",
        "# SCNET_MCP_ENVIRONMENT_END",
    ]
    return "\n".join(lines) + "\n"


def render_job(script: str, profile: EnvironmentProfile, script_path: str) -> dict:
    if "\0" in script or "\ufffd" in script:
        raise BridgeError("Environment injection requires a complete valid UTF-8 Bash script")
    if MARKER in script:
        raise BridgeError("Script already contains an injected environment; use its original source or submit without environment")
    script = script.replace("\r\n", "\n")
    lines = script.splitlines(keepends=True)
    if not lines or not lines[0].startswith("#!"):
        raise BridgeError("Environment injection requires a Bash shebang")
    try:
        interpreter = shlex.split(lines[0][2:].strip())
    except ValueError as exc:
        raise BridgeError("Invalid script shebang") from exc
    shell = PurePosixPath(interpreter[0]).name if interpreter else ""
    is_bash = shell == "bash" or (
        shell == "env" and (
            interpreter[1:2] == ["bash"] or interpreter[1:3] == ["-S", "bash"]
        )
    )
    if not is_bash:
        raise BridgeError("Environment injection supports Bash scripts only")
    # Slurm stops reading directives at the first executable line.
    insertion = 1
    while insertion < len(lines) and (
        not lines[insertion].strip() or lines[insertion].lstrip().startswith("#")
    ):
        insertion += 1
    header = "".join(lines[:insertion])
    if not header.endswith("\n"):
        header += "\n"
    has_job_name = False
    for line in lines[:insertion]:
        line = line.lstrip()
        if line.startswith("#SBATCH"):
            try:
                options = shlex.split(line[len("#SBATCH"):], comments=True)
            except ValueError as exc:
                raise BridgeError("Invalid quoted #SBATCH options") from exc
            has_job_name |= any(option.startswith("-J") or option == "--job-name" or option.startswith("--job-name=") for option in options)
    if not has_job_name:
        header += "#SBATCH --job-name=" + shlex.quote(PurePosixPath(script_path).name) + "\n"
    body = "".join(lines[insertion:])
    conflicts = [
        str(index + 1) for index, line in enumerate(lines)
        if index >= insertion and re.match(r"^\s*(module\s+(?:purge|load|unload|use|swap|switch)\b|source\s|\.\s)", line)
    ]
    warnings = []
    if conflicts:
        warnings.append(
            "Original script initializes modules/environment on lines " + ", ".join(conflicts)
            + "; those commands run after the injected environment and may override it."
        )
    return {
        "environment": profile.name,
        "script_path": script_path,
        "script": header + setup_shell(profile) + body,
        "warnings": warnings,
        "setup_location": "batch_script",
    }
