# Westlake AIStation Skill

面向 Codex 和命令行的 AIStation GPU 任务工具：查询资源、自动申请开发环境、运行程序、监控、下载结果并释放资源。任何使用者都配置自己的账号，通过隐藏输入提供密码。

Community CLI and Codex skill for bounded AIStation development-environment jobs. This is not an official university SDK.

**当前为预览版 `v0.1.0-preview.1`。61 项离线及本地进程测试已通过。发布前实机检查完成了资源申请与实际 V100 型号核验，但共享目录创建出现超时，最新公开改版的 GPU 全流程验证尚未通过；相关测试环境已释放。请先在自己的部署做小任务验证，再投入正式计算。**

## 安装

需要 macOS 或 Linux、Python 3.11+，以及可访问目标 AIStation 的网络。远端镜像需提供 Python 3、SSH/SFTP、`timeout` 和 `sha256sum`。Windows 原生运行尚不支持；可在合适的 Linux 环境中使用。

```bash
git clone https://github.com/WeStrainGroup/westlake-aistation-skill.git \
  ~/.codex/skills/westlake-aistation
cd ~/.codex/skills/westlake-aistation
python3 -m venv scripts/.venv
scripts/.venv/bin/python -m pip install -r scripts/requirements.txt
scripts/wlai --help
```

如果已有同名技能，请先比较已有安装，避免覆盖自己的配置或正在使用的版本。也可以克隆到其他目录作为普通 CLI；包装脚本通过相对位置寻找源码，不依赖作者的本机路径。`WLAI_PYTHON` 可指定已有解释器。

## 使用自己的账号

先从管理员或可信渠道获得门户证书和 SSH 主机公钥指纹，再配置门户、账号、镜像和资源组：

```bash
scripts/wlai init
scripts/wlai login
```

`init` 只保存连接设置，不保存密码。`login` 在终端隐藏输入密码，默认不持久保存；未记住凭据时，每个独立联网命令会再次询问，前台 `run` 的完整流程只需输入一次。需要后台提交时，可自行选择系统凭据库：

```bash
scripts/wlai login --remember
```

macOS 使用系统钥匙串；Linux 可安装可选依赖 `keyring` 并配置受支持的系统 Secret Service 后端。没有安全凭据库时，使用前台 `run`，不通过密码文件、命令参数或环境变量替代。会话令牌不保存到磁盘。

也可以复制 [配置模板](references/profile.example.json)，填好后导入：

```bash
scripts/wlai --profile /absolute/private/profile.json init --from-file /absolute/completed-template.json
scripts/wlai --profile /absolute/private/profile.json doctor
```

配置文件、证书、运行记录和计算结果留在使用者自己的机器，不应提交到本仓库。配置和状态文件采用私有权限，并按门户及账号隔离。工具不会修改全局 SSH 配置，也没有跳过 TLS 验证选项。

## 申请、提交和监控

```bash
scripts/wlai resources --group development
scripts/wlai plan --spec /absolute/project/job.json
scripts/wlai submit --spec /absolute/project/job.json --execute
scripts/wlai status --run-id my_gpu_run
scripts/wlai logs --run-id my_gpu_run
scripts/wlai metrics --run-id my_gpu_run
scripts/wlai watch --run-id my_gpu_run
```

Job JSON 示例：

```json
{
  "run_id": "my_gpu_run",
  "cpus": 12,
  "gpus": 1,
  "max_seconds": 1800,
  "argv": ["/opt/conda/bin/python", "-u", "{run_dir}/main.py", "--output", "{run_dir}/result.json"],
  "uploads": [{"local": "/absolute/project/main.py", "remote": "{run_dir}/main.py"}],
  "downloads": [{"remote": "{run_dir}/result.json", "local": "/absolute/project/results/result.json"}]
}
```

`plan` 或不带 `--execute` 的 `run/submit` 不申请资源。`{run_dir}` 展开到个人持久目录下的唯一运行目录。上传内容按 SHA-256 固定；结果下载检查 SHA-256，并拒绝覆盖已有不同文件。相同运行 ID 和参数会复用记录。

## 效率与稳定性

- 进程内复用 HTTPS 连接，减少重复握手；仅对可安全重试的只读请求进行有限退避重试，写请求不自动重发。
- 监控复用 SSH/SFTP 连接，设置不活动超时，断线在既定期限内恢复；已回收的状态和日志可从本地读取，无需登录。
- 每个运行有独立控制器锁，多个 agent 可以共享记录和观察任务；不会因为一个资源申请阻塞所有其他运行。
- 资源释放等待平台确认；上传与下载采用校验及不可覆盖发布；计算结果和回收错误分别记录。
- 远端运行器用独立看门狗执行超时限制并清理进程组，状态原子保存；创建或启动结果不确定时先对账。
- 平台返回 `GPU:N` 型号别名时，通过容器内 `nvidia-smi` 核验实际数量和型号，避免把元数据别名当作真实硬件证明。

这些是实现与测试覆盖的改进，不代表在所有集群部署上都有确定的加速比例。详见 [运行、认证与恢复说明](references/usage.md)。

## 当前边界

支持的是开发环境后端，单程序最多 **13,800 秒（3 小时 50 分钟）**；启动和清理也必须包含在最长四小时的环境预算内。原生训练 GPU 提交尚未验证，公开版保留禁用保护。其他 GPU 型号、镜像和部署版本需各自验证。

后台监控控制器运行在本机，需保持开机联网。断网或关机时，远端程序仍受超时约束，但本机无法保证立即取回结果或释放环境。平台时限、计费和科学任务参数需由使用者自行核对。共享存储的建目录等操作可能有明显延迟；工具不保证集群 I/O 吞吐。远端状态通过原子替换保证 JSON 完整，不强制同步到存储介质，也不承诺断电后的耐久性。

本项目不包含作者的账号、密码、内网地址、服务器证书、资源组 ID、任务历史或计算数据。此前私用版本通过过真实 V100 流程；公开改版的自动测试使用隔离模拟和本地进程，不能替代每个部署的 GPU 验证。

## 测试

```bash
scripts/.venv/bin/python -m unittest discover -s tests -v
```

测试无需账号、网络或 GPU。MIT License。
