# Westlake AIStation Skill

通过命令行使用西湖大学 AIStation：开发调试、正式任务提交、日志监控、结果核验与资源释放。每个人使用自己的账号；这是社区适配器，不是官方 SDK，也不是 Slurm 客户端。

**v0.3.1 已通过任务管理的真实 1×V100 验证：CUDA 矩阵计算、持久结果与回执回读、SHA-256 核验、任务终态及临时 CPU 环境释放。** 单节点多 GPU 可申请，程序须自行使用这些卡；多节点 DDP 和批量调度尚未实现。

## 安装与更新

需要 macOS/Linux、Python 3.11+ 和校园网络或学校 VPN。

```bash
git clone https://github.com/WeStrainGroup/westlake-aistation-skill.git
cd westlake-aistation-skill
python3 -m venv scripts/.venv
scripts/.venv/bin/python -m pip install -r scripts/requirements.txt
scripts/.venv/bin/python scripts/install_local.py \
  --runtime-python "$PWD/scripts/.venv/bin/python"
```

安装器保存不可变源码快照，原子更新本地 skill。已有旧 CLI 时附加 `--cli-path /absolute/old/wlai`，只替换并备份入口，不覆盖私有配置、证书、数据和旧记录。**每次 GitHub 发布后都要同步本机 skill 和旧入口，并核对相同提交。** 详见 [安装、更新与回滚](references/usage.md#installation-and-updates)。

## 直接输入账号密码

```bash
scripts/wlai init --connection-only --server-cert /absolute/trusted-server.pem
scripts/wlai login
scripts/wlai resources --group all --format table
scripts/wlai configure --purpose development
scripts/wlai configure --purpose training
```

初始化时直接输入自己的账号。在线命令默认直接提示密码，以隐藏输入保护终端记录；**默认不访问钥匙串**，密码只在当前进程内存中使用。`login` 验证账号，不保存密码；系统凭据库仅是主动选择的可选功能，见使用参考。

门户、服务器证书和容器 SSH 指纹必须由使用者通过可信渠道核实。工具不会下载证书后自动信任。首次账号请先按手册修改初始密码；WebVPN 不适用于这个入口。

## 正式 GPU 任务

先把代码、依赖和数据放入个人持久目录。训练通过“任务管理”运行，不受开发环境四小时租期约束，但仍须遵守所选训练组时限并保存 checkpoint。

```bash
scripts/wlai job --purpose training --remote-script /personal/train.py \
  --gpus 1 --cpus 12 --minutes 720 --out training.json -- --output '{run_dir}/results'
scripts/wlai task-stage --spec training.json --out training.staged.json --execute
scripts/wlai task-plan --spec training.staged.json --online
scripts/wlai task-submit --spec training.staged.json --execute
scripts/wlai task-watch --run-id RUN_ID --timeout 600
```

`task-stage` 使用短暂的 **1 CPU / 0 GPU** 调试环境，上传并校验检查器和参数文件，然后确认释放；它不运行科学计算。需要配置开发组和可信 SSH 指纹。真正训练由独立的原生任务执行，不依赖该调试环境或本机持续在线。代码与大数据只需预置一次；这个步骤上传的是小型启动文件。

默认命令预算是客户端保守设置的 1000 字节，超限在联网前拒绝并提示 `task-stage`，避免把约 5.8 KB 的内嵌检查器发给平台。短命令不含会被平台改写的双引号，启动前校验两份文件的完整 SHA-256。检查器核验真实 GPU 数量/型号和持久挂载后才启动程序。

创建请求省略 `taskType`：它是列表/前端路由使用的分类字段，反写到普通创建请求会把 GPU 训练变成 CPU 数据处理任务。此前的“平台 GPU 故障”判断已被本次单变量实测纠正。

```bash
scripts/wlai task-status --run-id RUN_ID
scripts/wlai task-logs --run-id RUN_ID
scripts/wlai task-reconcile --run-id RUN_ID
scripts/wlai task-stop --run-id RUN_ID
```

`task-stop` 会按当前状态选择取消方式：排队/拉取镜像阶段取消未完成任务，Running 阶段发送停止请求；已结束任务直接确认终态。平台可能在取消后仍回读 Pending，但带 deleteFlag=true；客户端再核对实例均已终止/不存在，才确认释放。默认等待60秒，可用 `--timeout 120 --interval 2` 调整；未确认释放时返回非零退出码。先前被平台拒绝的停止可以再次显式执行，不会被旧标记卡住；已接受的请求只观察，不反复发送。

本机观察超时不会取消平台任务。`UNKNOWN` 表示网络等原因让创建结果不明，先对账、不重复 POST；平台明确参数拒绝记为 `REJECTED`。平台成功但缺少有效终态检查器回执记为 `COMPLETED_UNVERIFIED`。科学结果仍须检查，退出码零不代表研究结论正确。

## 开发调试

```bash
scripts/wlai job --script ./main.py --minutes 30 --out debug.json
scripts/wlai plan --spec debug.json --online
scripts/wlai run --spec debug.json --execute
```

开发环境总租期最多四小时，准备、传输、程序、回收和清理都计入。后台开发控制器依赖本机持续运行，区别于平台管理的原生任务。结果写个人真实挂载；默认 master，SSD `ssdwork` 需明确选择，有独立配额与费用。

[官方手册对照](references/manual.md) · [完整使用与恢复](references/usage.md)

本仓库不包含作者账号、密码、内网地址、证书、资源组 ID 或私有运行历史。自动测试无须账号、网络或 GPU；MIT License。
