# SCNet HPC MCP

[English](README.en.md) | 中文

通过本机 OpenSSH 管理 SCNet 等 Slurm 超算集群的 MCP 服务。超算端无需安装 Codex、MCP 服务或 Python 包。

适用于拥有 SSH 访问权限的 SCNet E-Shell、超算登录节点和其他 Slurm 集群；连接能力以目标网关实际支持情况为准。

## 功能

- 16 个工具：连接检测、文件读写及上传下载、Slurm 分区和作业管理
- 使用已有 SSH 配置和密钥，保留主机密钥校验
- SSH 流式传输二进制文件，不要求网关开放 SFTP
- 文件工具检查本机和远端根目录；默认拒绝覆盖
- 限制返回长度、文件大小和运行时间
- 本机 SQLite 保存提交请求，防止相同 request_id 在重试或重启后重复提交
- 任意 Bash 命令为可选功能，默认关闭

## 快速开始

本机需要 Python 3.11+、OpenSSH 客户端、uv；若使用安装脚本，还需要 Codex CLI。

### 1. 验证 SSH

按照平台提供的主机、端口、用户名和密钥设置本机 SSH 配置，例如：

    Host scnet
        HostName your-cluster.example.com
        Port 65000
        User your-username
        IdentityFile ~/.ssh/your-cluster-key
        IdentitiesOnly yes

运行 ssh scnet，核对平台提供的主机指纹并确认登录正常。私钥带口令时，先在本机 ssh-agent 中解锁。MCP 不弹出密码窗口，也不会自动接受未知主机密钥。

### 2. 安装并配置

进入克隆后的仓库目录：

    uv sync --locked

Windows PowerShell：

    Copy-Item config.example.toml config.toml

macOS/Linux：

    cp config.example.toml config.toml

编辑 config.toml：

    [server]
    local_root = "."
    state_dir = ".state"

    [clusters.scnet]
    ssh_host = "scnet"
    remote_root = "~"

local_root 的相对路径以配置文件所在目录为基准。需要上传其他项目的文件时，将它改成该项目目录。remote_root 可以是 "~" 或已存在的远程项目绝对目录。

config.toml、config.local.toml、私钥、运行状态、测试报告和虚拟环境均被 Git 忽略。仓库只提供通用示例配置。

### 3. 注册到 Codex

Windows PowerShell：

    ./install-codex.ps1

macOS/Linux：

    bash ./install-codex.sh

安装脚本使用当前克隆目录生成绝对路径，并设置普通 Windows 环境变量及工具超时。它不会覆盖同名已有 MCP 配置。使用其他 MCP 客户端时，参考 codex-config.example.toml 中的启动命令。

在 Codex 中刷新或重启 MCP 连接，必要时新开聊天。服务器名为 scnet-hpc。

## 使用示例

可以对 Codex 说：

- “检查超算连接，并查看我当前的作业。”
- “列出超算 calculations 目录中的文件。”
- “上传本机 inputs/scf.in 到超算 calculations/scf.in。”
- “读取 calculations/slurm-12345.out 的最后 100 行。”
- “检查 calculations/run.slurm 的资源参数，再按我的计算需求提交；工作目录使用 calculations，请求编号使用 scf-run-001。”
- “下载超算 calculations/result.dat 到本机 results/result.dat。”

提交计算会消耗算力，资源参数来自已有 Slurm 脚本。本项目的验证脚本不会提交或取消真实作业。

## 工具

| 工具 | 用途 |
|---|---|
| list_clusters | 查询配置的连接和文件根目录 |
| check_connection | 检测 SSH、工作目录及常用命令 |
| list_files | 列出目录直接子项 |
| read_file | 读取文本或日志末尾 |
| write_file | 写入 UTF-8 输入文件或 Slurm 脚本 |
| upload_file | 上传单个文件 |
| download_file | 下载单个文件 |
| list_partitions | 查询 Slurm 分区及节点状态 |
| list_jobs | 查询当前账户的排队和运行作业 |
| job_details | 查询活跃作业详情或 sacct 历史记录 |
| list_environments | 查看可选软件环境，例如 qe、epw |
| check_environment | 加载环境并检查程序及 MPI 路径 |
| preview_job | 预览环境注入后的脚本，不提交计算 |
| submission_record | 查询本机持久化的提交记录 |
| submit_job | 提交已有脚本，支持 afterok 依赖 |
| cancel_job | 取消当前账户拥有的指定作业 |
| run_command | 可选；启用 allow_remote_commands 后出现 |

远程文件路径及 workdir 均相对于 remote_root，使用正斜杠，不使用绝对路径或 ".."。本机路径相对于 local_root，或使用位于该根目录内的绝对路径。

文件工具的目录检查不是进程沙箱；提交的脚本以及启用后的 run_command 拥有 SSH 账户的访问权限。大型计算应交给 Slurm 调度，避免在登录节点执行。

## 选择 QE / EPW 环境

可以在每个集群下配置多个软件环境。环境配置只存在于本机 config.toml，公开仓库中的 config.example.toml 提供脱敏示例。

例如：

    [clusters.scnet.environments.qe]
    description = "Quantum ESPRESSO"
    setup = [
        "module unload mpi/hpcx/gcc-7.3.1",
        "module use /public/software/modules/base /opt/hpc/software/modules",
        "source /path/to/qe/scripts/env.sh",
    ]

    [clusters.scnet.environments.qe.executables]
    "pw.x" = "/path/to/qe/app/bin/pw.x"
    "ph.x" = "/path/to/qe/app/bin/ph.x"

EPW 可以建立名为 epw 的配置，使用相同 setup，将 executables 指向 epw.x。程序路径必须是绝对路径，键名需要与文件名一致。setup 是受信任本机配置中的单行 Shell 命令，可能执行安装包的初始化逻辑；其权限与 SSH 账户相同。

使用流程：

1. list_environments：查看环境名称。
2. check_environment(environment="qe")：短 SSH 会话中加载环境，检查程序和 mpirun 路径，不运行计算程序。
3. preview_job(script_path="calculations/run.slurm", environment="qe")：检查注入后的脚本及提示。
4. submit_job(script_path="calculations/run.slurm", workdir="calculations", request_id="qe-run-001", environment="qe")：按选定环境提交。

可以直接说：“使用 QE 环境预览并提交这份脚本”，或“使用 EPW 环境提交作业”。environment 不设置时，按原始脚本提交。

初始化命令注入在全部开头注释和 #SBATCH 资源设置之后、原始计算命令之前，在计算节点执行。脚本通过 sbatch 标准输入提交，原始文件不会被改写。要求 UTF-8 Bash 脚本；Windows CRLF 会转换为 LF。脚本没有指定作业名时，保留原文件名作为作业名。

环境初始化期间暂时关闭 nounset（set -u），兼容 env.sh 对未设置变量的引用，之后恢复原状态。初始化失败或配置的程序不可执行时，作业停止。

如果原脚本还包含 module purge、module load 或 source，预览会提示它们可能覆盖注入环境。常见 Bash here-document 中的文本会跳过检查，例如嵌入 Python 的 source = ... 不再误报；支持引号分隔符、<<- 缩进形式及多个 here-document。后续真实环境命令仍按原脚本行号提示。此扫描仅提供建议，不是完整 Bash 解析器或安全边界。建议计算脚本只保留资源设置和计算命令，交给所选环境负责软件初始化。

环境名称及配置内容纳入提交请求的去重检查；同一 request_id 不能切换到其他环境或修改环境后重用。新增或修改环境配置后，需要重启 MCP 连接。

## 防重复提交

每次有意提交新作业，使用新的 request_id。相同参数和编号在提交成功后再次调用，会返回之前保存的 job_id。

记录保存于 state_dir/submissions.sqlite3：

- submitted：已记录 job_id
- submitting：正在提交，或处理期间服务中断
- uncertain：执行失败、连接中断、超时或输出无法确认
- not_found：没有这个编号的记录

遇到 submitting 或 uncertain，先查看 list_jobs、job_details 和 submission_record。确认未产生作业后，才考虑使用新的编号。不同编号、不同状态数据库或其他提交渠道不受同一条记录的去重保护。

作业离开队列后需要集群的 sacct 记账支持，历史保留时间由集群决定。

## 排查作业失败

job_details 返回调度器记录的真实状态。FAILED 表示整个作业脚本失败；科学程序正常结束后，检查命令或后处理仍可能使脚本返回非零。结合 Slurm ExitCode、最早错误、科学程序日志和单次进程返回码判断原因，不根据一个结束字符串改写调度器状态。

QE 与 EPW 的结束信息不同：EPW 5.8.1 不要求打印 QE 的 JOB DONE。运行脚本应保存实际返回码；PH 达到自身时间限制也可能留下有效恢复数据，但请求的阶段仍未完成。重启文件检查与版本对应的结束检查可使用 [qe-epw skill](https://github.com/OVER4041/qe-epw-skill) 的 check_runtime.py，下载日志后在本机执行，无需在超算安装 Codex 或额外 Python 包。

MPI 在程序启动前失败时，先检查所选环境中的启动器、库和调度器集成。单节点启动的替代方案需在已分配节点验证，不应作为通用环境配置；MCP 不自动更换 MPI 或重复提交作业。

## 限制与默认值

- 普通操作超时 30 秒，传输超时 300 秒
- 文本返回上限 64 KiB，单个传输文件上限 1 GiB
- 远程需具备 Bash、GNU realpath、find、mktemp、head、tail、cat、stat；写入时需支持硬链接
- 本机不需要额外安装 SSH Python 库
- 单文件传输；文件夹可在本机打包后上传
- 密钥到期后，更新本机 SSH 配置
- 多集群可增加 [clusters.NAME]，调用时指定 cluster
- 当前没有作业完成推送；由客户端主动查询

## 验证与开发

单元测试：

    uv run --locked python -m unittest discover -s tests -v

MCP 协议测试（不连接超算）：

    uv run --locked python scripts/verify.py --config config.example.toml

真实连接与文件传输测试：

    uv run --locked python scripts/verify.py --live --config config.toml

--live 创建唯一命名的临时测试目录，验证 UTF-8 文本、特殊文件名、1 MiB 二进制往返、拒绝覆盖和远端符号链接越界，最后清理测试文件。报告保存在 verification-live.json，已被 Git 忽略。

构建安装包：

    uv build

本次离线单元测试运行 33 项：32 项通过，1 项因 Windows 无符号链接创建权限跳过；MCP 协议测试通过并发现 16 个工具。更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 参考

- [SCNet SSH 连接](https://www.scnet.cn/help/docs/mainsite/hpc/cmd/connect-to-hpc/)
- [SCNet 作业管理](https://www.scnet.cn/help/docs/mainsite/hpc/cmd/cmd-submit-job/index.html)
- [Codex MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [官方 MCP Python SDK v1](https://py.sdk.modelcontextprotocol.io/v1/)
