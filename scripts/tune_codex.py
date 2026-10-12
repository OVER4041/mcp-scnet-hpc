"""Set timeouts for this registered server without rewriting unrelated settings."""
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib


def tune(path: Path, expected_python: Path):
    original = path.read_text(encoding="utf-8")
    before = tomllib.loads(original)
    entry = before.get("mcp_servers", {}).get("scnet-hpc")
    if not entry or Path(entry["command"]).resolve() != expected_python.resolve():
        raise RuntimeError("Registered scnet-hpc command does not match this project")
    match = re.search(r"(?m)^\[mcp_servers\.scnet-hpc\][ \t]*\r?$", original)
    if not match:
        raise RuntimeError("Cannot find the registered scnet-hpc TOML table")
    start = match.end()
    following = re.search(r"(?m)^\[", original[start:])
    end = start + following.start() if following else len(original)
    block = original[start:end]
    newline = "\r\n" if "\r\n" in original else "\n"
    for key, value in [("startup_timeout_sec", 20), ("tool_timeout_sec", 360)]:
        pattern = rf"(?m)^{key}[ \t]*=.*$"
        if re.search(pattern, block):
            block = re.sub(pattern, f"{key} = {value}", block)
        else:
            block = block.rstrip() + newline + f"{key} = {value}" + newline + newline
    updated = original[:start] + block + original[end:]
    after = tomllib.loads(updated)
    expected = tomllib.loads(original)
    expected["mcp_servers"]["scnet-hpc"].update(startup_timeout_sec=20, tool_timeout_sec=360)
    if after != expected:
        raise RuntimeError("Unexpected changes to unrelated Codex settings")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".scnet-config-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    print("Set scnet-hpc startup timeout to 20 seconds and tool timeout to 360 seconds.")


if __name__ == "__main__":
    tune(Path(sys.argv[1]), Path(sys.argv[2]))
