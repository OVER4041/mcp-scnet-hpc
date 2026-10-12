# SCNet HPC MCP

English | [中文](README.md)

A local MCP server for SSH-accessible SCNet and other Slurm HPC clusters. No Codex, MCP server, or Python package installation is required on the cluster.

## Features

- 16 tools for connection checks, files, and Slurm jobs
- Reuses local OpenSSH configuration and keys
- Strict host-key verification and noninteractive authentication
- Binary streaming over SSH without requiring SFTP
- Scoped file paths, explicit overwrite, bounded output, and timeouts
- Durable SQLite submission records to prevent retrying the same request ID
- Optional arbitrary Bash command tool, disabled by default

## Setup

Requirements: Python 3.11+, OpenSSH, uv, and Codex CLI when using the registration scripts.

1. Configure an SSH alias called scnet using your platform-provided host, port, username, and key. Verify it with ssh scnet.
2. Run uv sync --locked in the cloned repository.
3. Copy config.example.toml to config.toml.
4. Set local_root to the local project directory, ssh_host to your SSH alias, and remote_root to "~" or an existing remote project directory.
5. On Windows run ./install-codex.ps1. On macOS/Linux run bash ./install-codex.sh.
6. Restart the MCP connection or open a new Codex chat. The server name is scnet-hpc.

The registration scripts generate absolute paths from the cloned directory. They preserve any existing server with the same name. See codex-config.example.toml for manual configuration.

Private configuration, keys, runtime state, verification reports, and virtual environments are excluded from Git.

## Tools

list_clusters, check_connection, list_files, read_file, write_file, upload_file, download_file, list_partitions, list_jobs, job_details, submission_record, submit_job, cancel_job, list_environments, check_environment, preview_job.

Set allow_remote_commands=true to expose run_command as an additional tool.

Remote paths and workdir are relative to remote_root. Local paths are relative to local_root or absolute paths within that root. File tools reject path traversal and symlinks escaping the configured roots. These checks are not a process sandbox: submitted scripts and optional raw commands have the SSH account's permissions.

Heavy computation belongs in Slurm jobs. Submitting jobs consumes the cluster's compute allocation.

## Software environments

Configure optional profiles under [clusters.scnet.environments.NAME]. A profile contains a description, a setup array of trusted single-line shell commands, and an executables mapping whose keys match executable filenames and values are absolute remote paths. See the commented QE and EPW examples in config.example.toml.

Use list_environments, check_environment, and preview_job first, then pass environment="qe" or environment="epw" to submit_job. Omitting environment preserves the original submission behavior. The optional run_command tool also accepts environment.

Setup is inserted after the leading comments and all initial #SBATCH directives, before the original commands. It runs inside the batch job on the compute node. The generated script is sent through sbatch standard input, without modifying its source file. Scripts must use UTF-8 and a Bash shebang; CRLF is normalized to LF. If no job name is provided, the original filename is retained as the job name.

Nounset is temporarily disabled while loading vendor environment files, then restored. Failed setup or a missing configured executable stops execution. Later module/source commands in the original script can override the selected environment; preview reports these as warnings. Prefer scripts containing only resource directives and computation commands.

Profiles are trusted local configuration, not a process sandbox. Loading a setup script can perform the initialization actions defined by that script. check_environment loads the profile but never launches the configured scientific programs.

The environment name and configuration participate in request deduplication. Do not reuse a request ID after changing its environment. Restart the MCP connection after configuring profiles.

## Submission deduplication

Use one stable, unique request_id for each intended submission. A repeated successful request returns the saved job ID, including after restarting the MCP server.

Records are stored in state_dir/submissions.sqlite3. Requests left submitting or uncertain are not automatically retried. Inspect the queue and submission_record before using a new ID. Deduplication is scoped to the same cluster, request ID, and state database; different IDs or external submission channels can still create duplicate jobs.

Completed job lookup depends on cluster accounting through sacct.

## Defaults and requirements

- Operation timeout: 30 seconds; transfer timeout: 300 seconds
- Text output: 64 KiB; file transfer: 1 GiB
- Remote Bash and GNU realpath, find, mktemp, head, tail, cat, stat; hard-link support for non-overwriting writes
- Existing SSH key or agent authentication; unknown host keys and password prompts are not automatically accepted
- Single-file transfers; archive directories locally before uploading
- Multiple clusters supported through additional configuration tables

## Validation

    uv run --locked python -m unittest discover -s tests -v
    uv run --locked python scripts/verify.py --config config.example.toml
    uv run --locked python scripts/verify.py --live --config config.toml
    uv build

The live check queries the cluster and verifies temporary text and binary transfers. It removes its own test files and never submits or cancels jobs.

Windows registration forwards ordinary OS environment variables needed by OpenSSH. No API token is included in the server configuration.
