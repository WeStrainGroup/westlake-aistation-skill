# Westlake AIStation Skill

按西湖大学 AIStation 手册组织开发调试、资源检查、短任务运行和正式训练准备和原生接口诊断。支持命令行，也可作为 Codex Skill 使用；每个人使用自己的账号和连接配置。

Community CLI and Codex skill. This is not an official university SDK, and the manual does not promise compatibility with these observed API endpoints.

**预览版 `v0.2.0-preview.3`：公开改版尚未取得实机 GPU 全流程 PASS。此前检查通过了资源申请和实际 V100 型号核验，但共享目录创建超时；测试资源已释放。自动测试通过不能替代自己部署的小任务验证。**

## 安装

需要 macOS 或 Linux、Python 3.11+，以及可访问目标 AIStation 的网络。远端镜像需提供 Python 3、SSH/SFTP、`timeout` 和 `sha256sum`；Windows 原生运行尚不支持。

```bash
git clone https://github.com/WeStrainGroup/westlake-aistation-skill.git
cd westlake-aistation-skill
python3 -m venv scripts/.venv
scripts/.venv/bin/python -m pip install -r scripts/requirements.txt
scripts/.venv/bin/python scripts/install_local.py \
  --runtime-python "$PWD/scripts/.venv/bin/python"
~/.codex/skills/westlake-aistation/scripts/wlai --version
```

安装器保存不可变版本并原子切换本机 skill，保留私有配置。普通 CLI 也可直接使用源码目录的 `scripts/wlai`。已有旧 CLI 入口时，用 `--cli-path /absolute/old/wlai` 同步该入口；只替换并备份入口文件，不改动旁边的配置、证书、数据和状态。`--profile`、`--state-root` 可设置包装器的默认私有配置与共享状态根目录，不读取或复制凭据。

**每次 GitHub 更新后，都要安装同一提交并核对本机版本。** 不在运行中的控制器代码目录内 `git pull`。升级、版本核验与回滚命令见 [使用参考](references/usage.md#installation-and-updates)。旧完整源码安装的迁移需要先结束旧控制器；安装器默认拒绝直接热改。

## 首次配置

首次账号先按平台要求修改初始密码。校外使用学校 VPN，WebVPN 无效。门户和证书请从可信渠道获得，不使用他人的配置。[官方登录说明](https://hpc.westlake.edu.cn/document/ai/aistation/2.account.html)

先只配置连接，再查询自己可用的资源：

```bash
scripts/wlai init --connection-only --server-cert /absolute/trusted-server.pem
scripts/wlai login --remember
scripts/wlai resources --group all --format table
scripts/wlai configure
scripts/wlai doctor
```

`--server-cert` 从使用者提供的单张服务器 PEM 计算证书指纹，不会下载证书并自动信任。`configure` 补全资源组 ID、镜像、GPU 型号、SSH 主机指纹和存储选择；SSH 指纹也需要通过可信渠道核实。若已经掌握这些设置，可直接运行完整向导 `scripts/wlai init`。

默认使用个人 master 目录；也可明确选择 SSD `ssdwork`。SSD 有组配额且没有免费存储容量，工具不会因 master 慢而自动切换或复制数据。其他挂载需要使用者明确配置并通过挂载检查。[存储与配额](https://hpc.westlake.edu.cn/document/ai/aistation/4.data_file.html#id2) · [存储计费](https://hpc.westlake.edu.cn/document/ai/aistation/3.resource.html#storage-billing-rules)

密码隐藏输入，默认不保存；上面的 `--remember` 是可选的系统凭据库保存。macOS 使用钥匙串；Linux 可安装可选依赖 `keyring` 并启用受支持的系统后端。不保存密码时，前台 `run` 可隐藏询问；后台 `submit` 需要系统凭据库。密码不进入配置、命令参数或环境变量，令牌不保存到磁盘。

## 日常开发调试

用自己的脚本生成作业文件，再检查计划：

```bash
scripts/wlai job --script ./main.py --minutes 30 --out job.json -- --input /remote/input.dat
scripts/wlai plan --spec job.json
scripts/wlai plan --spec job.json --online
scripts/wlai run --spec job.json --execute
```

`job` 只生成文件。`plan` 默认离线检查参数、上传大小和完整时间预算，**没有查询实时资源或验证远端可用性**；`--online` 再查询资源。实际执行会重新查资源、核验挂载并检查小写入，之后才启动代码。CPU 和 GPU 必须在同一节点同时满足。

需要后台控制器时，把最后一条换成 `submit --spec job.json --execute`。控制器在本机运行，需保持开机联网；其他 agent 使用同一配置即可共享记录：

```bash
scripts/wlai runs
scripts/wlai status --run-id RUN_ID
scripts/wlai logs --run-id RUN_ID
scripts/wlai metrics --run-id RUN_ID
scripts/wlai watch --run-id RUN_ID
```

`RUN_ID` 使用生成或提交结果中的运行 ID。脚本、参数和资源变更后使用新 ID。结果要写入已核验的个人持久挂载；需要下载结果时，在作业 JSON 的 `downloads` 中明确列出路径。上传和下载核对 SHA-256，并拒绝覆盖不同的既有文件。详见 [作业格式与恢复](references/usage.md)。

## 正式训练

手册的正常流程是先在开发环境调试，再到“业务管理 → 任务管理”提交正式任务。开发环境统一最长四小时；正式任务时限取决于资源组，许多组为五天，应使用 checkpoint。[官方任务说明](https://hpc.westlake.edu.cn/document/ai/aistation/6.task.html)

先把调试通过的代码、环境和数据一次性放入已核验的持久目录，再生成原生任务计划：

```bash
scripts/wlai groups --purpose training
scripts/wlai configure --purpose training
scripts/wlai job --purpose training --remote-script /personal/train.py \
  --minutes 1440 --out training-job.json -- --output '{run_dir}/results'
scripts/wlai task-plan --spec training-job.json
scripts/wlai task-plan --spec training-job.json --online
```

这几条命令不提交任务。命令长度受部署限制；若内嵌检查器被平台拒绝，可预置同版本检查器并通过 SHA-256 校验后用短命令启动，见 [原生任务参考](references/usage.md#native-task-management-plan-and-bounded-diagnostics)。

原生任务有独立注册表与 `task-status/task-logs/task-watch/task-stop/task-reconcile`；无需开发环境 SSH。`task-watch` 的超时只停止本机观察，不取消平台任务。输出留在持久盘，当前不会自动下载；多节点 DDP 和批量派发尚未实现。

**当前原生 GPU 创建仍不满足生产上线条件。** 本轮通过官方表单精确转发的 1 GPU 请求也被服务端改成 CPU/0 GPU，CUDA 验证失败。默认 `task-submit --spec training-job.json --execute` 会拒绝新增原生任务；仅已获授权的小型诊断可显式使用 `--experimental-native`。客户端核对平台回读资源，发现不符会停止本次新任务；容器内 guard 还会验证真实 GPU 和持久挂载，未通过就不启动科学代码。不能凭“创建成功”宣称申请到了 GPU。

`task-guide --spec training-job.json --out task-guide.md` 仍可导出人工网页清单。网页提交也必须核验实际硬件。当前限制不能通过把长训练转到开发环境来规避。生产批次应等待此部署的 GPU 实测通过；不要为测试重复投递正式训练。

## 挂载与传输检查

已有调试环境时，可检查挂载、一次小写入、延迟和文件系统容量：

```bash
scripts/wlai storage-check --run-id RUN_ID
```

它不会另行申请环境。`df` 可用空间不能证明实验室组配额充足；出现无法写入或明显卡顿时，应检查组配额及存储服务，而不是反复申请 GPU。大数据尽量先传好再占用 GPU。手册推荐大文件走独立 SFTP，并提醒开发环境有限时；当前工具的独立存储 SFTP 仍不提供自动化支持。[文件传输说明](https://hpc.westlake.edu.cn/document/ai/aistation/4.data_file.html#sftp)

## 稳定性与边界

- HTTPS、SSH/SFTP 复用连接；只读请求有限重试，创建资源的写请求不自动重发。
- 每个运行独立加锁，状态按门户和账号隔离；计算、回收和释放分别确认。
- 准备、传输、程序、回收和清理使用完整时间预算，不能超过开发环境四小时。13,800 秒是客户端程序保护上限，不是手册额外规定；实际可用程序时长还受其他阶段预算限制。
- 远端独立看门狗限制程序时长；状态使用原子替换，不承诺断电耐久性。短时断线可在原期限内恢复，长期离线不保证立即回收或释放。
- 共享存储操作可能有明显延迟，不保证集群 I/O 吞吐。已接受暂停请求不等于资源已释放。

本仓库不包含作者账号、密码、内网地址、证书、资源组 ID 或运行历史。手册规定、客户端保护和部署实测的区别，见 [手册对照](references/manual.md)。

## 测试

```bash
scripts/.venv/bin/python -m unittest discover -s tests -v
```

自动测试不需要账号、网络或 GPU。持续集成结果见 GitHub Actions；MIT License。
