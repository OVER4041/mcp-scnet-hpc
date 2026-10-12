"""Protocol smoke test; --live also queries SSH and verifies temporary file transfer."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from scnet_mcp.bridge import Bridge
from scnet_mcp.config import BridgeError, Settings

ROOT = Path(__file__).resolve().parents[1]


async def verify(live: bool, config: Path) -> dict:
    settings = Settings.load(config)
    bridge = Bridge(settings)
    report = {"protocol": {}, "live": {}}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-X", "utf8", "-m", "scnet_mcp.server", "--config", str(config.resolve())],
        env={key: os.environ[key] for key in ("USERDOMAIN", "WINDIR", "PROGRAMDATA", "COMSPEC", "TMP") if key in os.environ},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert all(tool.outputSchema is not None for tool in tools.tools)
            assert {"check_connection", "submit_job", "read_file", "upload_file", "download_file"} <= names
            assert ("run_command" in names) == settings.allow_remote_commands
            result = await session.call_tool("list_clusters", {})
            assert not result.isError
            rejected = await session.call_tool("read_file", {"path": "../outside"})
            assert rejected.isError
            report["protocol"] = {"tool_count": len(names), "tools": sorted(names), "invalid_path_rejected": True}
            if live:
                result = await session.call_tool("check_connection", {})
                assert not result.isError, result
                report["live"]["connection"] = result.structuredContent
                jobs = await session.call_tool("list_jobs", {})
                assert not jobs.isError, jobs
                partitions = await session.call_tool("list_partitions", {})
                assert not partitions.isError, partitions
                report["live"]["job_query"] = "passed"
                report["live"]["partition_query"] = "passed"
                files = await session.call_tool("list_files", {})
                assert not files.isError, files
                report["live"]["directory_query"] = "passed"

                suffix = uuid.uuid4().hex
                directory = f".scnet-mcp-verify-{suffix}"
                local_directory = ROOT / ".state" / "verification" / suffix
                local_directory.mkdir(parents=True, exist_ok=False)
                text_path = f"{directory}/space ' dollar $literal.txt"
                text = "SCNet MCP UTF-8 test: 铜 Cu\nsecond line\n"
                try:
                    written = await session.call_tool("write_file", {"path": text_path, "content": text})
                    assert not written.isError, written
                    read = await session.call_tool("read_file", {"path": text_path})
                    assert not read.isError, read
                    assert read.structuredContent["text"] == text, read
                    blocked = await session.call_tool("write_file", {"path": text_path, "content": "must not replace"})
                    assert blocked.isError
                    tail = await session.call_tool("read_file", {"path": text_path, "tail_lines": 1})
                    assert not tail.isError and tail.structuredContent["text"] == "second line\n"
                    report["live"]["text_and_quoted_paths"] = "passed"
                    report["live"]["overwrite_refused"] = "passed"

                    payload = bytes(range(256)) * 4096
                    source, destination = local_directory / "source.bin", local_directory / "download.bin"
                    source.write_bytes(payload)
                    uploaded = await session.call_tool("upload_file", {
                        "local_path": str(source), "remote_path": f"{directory}/binary.bin",
                    })
                    assert not uploaded.isError, uploaded
                    downloaded = await session.call_tool("download_file", {
                        "remote_path": f"{directory}/binary.bin", "local_path": str(destination),
                    })
                    assert not downloaded.isError, downloaded
                    assert destination.read_bytes() == payload
                    report["live"]["binary_roundtrip"] = {
                        "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                    chosen = bridge.cluster("scnet")
                    symlink = bridge.guard(directory, kind="directory") + 'ln -s /tmp -- "$target/outside"\n'
                    (await bridge.ssh.run(chosen, bridge.preamble(chosen) + symlink)).checked()
                    escaped = await session.call_tool("list_files", {"path": f"{directory}/outside"})
                    assert escaped.isError
                    report["live"]["remote_symlink_escape_refused"] = "passed"
                finally:
                    # Only remove this uniquely named directory created by this verification.
                    assert directory.startswith(".scnet-mcp-verify-") and len(suffix) == 32
                    chosen = bridge.cluster("scnet")
                    cleanup = bridge.guard(directory) + 'rm -rf -- "$target"\n'
                    (await bridge.ssh.run(chosen, bridge.preamble(chosen) + cleanup)).checked()
                    for owned in (local_directory / "source.bin", local_directory / "download.bin"):
                        owned.unlink(missing_ok=True)
                    local_directory.rmdir()
                report["live"]["test_files_removed"] = True
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    default_config = ROOT / "config.toml"
    if not default_config.is_file():
        default_config = ROOT / "config.example.toml"
    parser.add_argument("--config", type=Path, default=default_config)
    args = parser.parse_args()
    report = asyncio.run(verify(args.live, args.config))
    report["verification"] = "passed"
    report_path = ROOT / ("verification-live.json" if args.live else "verification-protocol.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
