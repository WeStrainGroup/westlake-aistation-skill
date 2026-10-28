# 官方手册对照与支持边界

核验日期：2026-10-28。入口：[西湖大学 AIStation 手册](https://hpc.westlake.edu.cn/document/ai/index-aistation.html)。以下是简要对照，不复制手册全文、示例账号或内网配置。运行时以实际资源组、挂载和管理员公布的政策为准。

## 手册规定及推荐

| 官方依据 | 对应工作方式 |
| --- | --- |
| [账号与登录](https://hpc.westlake.edu.cn/document/ai/aistation/2.account.html)：首次修改初始密码；校外使用学校 VPN，WebVPN 无效 | 用户先完成账号开通及首次密码设置；CLI 配置自己的连接和隐藏凭据 |
| [开发环境](https://hpc.westlake.edu.cn/document/ai/aistation/5.dev.html)：用于代码调试；默认挂载个人目录 | 开发后端用于调试和短验证，执行前核验配置的实际挂载 |
| [任务提交](https://hpc.westlake.edu.cn/document/ai/aistation/6.task.html)：调试后在任务管理运行；时限按资源组，许多组五天；应做 checkpoint | 正式训练生成原生 `task-plan` 并核对组时限、镜像、绝对命令、挂载、checkpoint 和输出；原生任务通过短启动器和逐次硬件/挂载检查；保留 `task-guide` 人工清单 |
| [资源限制](https://hpc.westlake.edu.cn/document/ai/aistation/3.resource.html#id3)：开发环境四小时；单用户默认最多 25 张卡、500 核 CPU；组存储配额限制 | 全部开发阶段使用同一总时限；单作业参数范围不等于账号剩余配额证明；写入失败需检查组配额 |
| [排队条件](https://hpc.westlake.edu.cn/document/ai/aistation/9.faq.html#id18)：CPU 与 GPU 都需满足节点剩余资源 | 联网计划和执行检查同一节点的 CPU/GPU；离线计划不声明有资源 |
| [存储管理](https://hpc.westlake.edu.cn/document/ai/aistation/4.data_file.html#id2)：master 在 `/<account>`，SSD 在 `/ssdwork/<account>`；SSD 空间和组配额有限 | 默认 master；用户明确选择 SSD 后才使用，不自动迁移；自定义存储须明确真实挂载 |
| [持久输出](https://hpc.westlake.edu.cn/document/ai/aistation/9.faq.html#id22)：输出需写入挂载的个人目录才能在容器退出后保留 | 不把 `/tmp`、`/root` 或普通容器目录当持久输出；提前列出结果与回收路径 |
| [文件操作](https://hpc.westlake.edu.cn/document/ai/aistation/4.data_file.html#id3)及 [SFTP](https://hpc.westlake.edu.cn/document/ai/aistation/4.data_file.html#sftp)：网页上传有限制；大文件推荐独立 SFTP，开发环境连接有时限 | 计划显示上传字节；批量数据尽量提前准备；不把浏览器大小限制套用于所有 SFTP 传输 |
| [镜像与 Conda](https://hpc.westlake.edu.cn/document/ai/aistation/7.images.html#conda)：大镜像拉取较久；Conda 可放个人目录；长期不用的镜像可能清理 | 使用个人持久解释器的绝对路径，检查当前镜像；不要仅靠历史镜像名或短 readiness 超时判断环境就绪 |
| [日志刷新](https://hpc.westlake.edu.cn/document/ai/aistation/9.faq.html#id20)与 [任务命令](https://hpc.westlake.edu.cn/document/ai/aistation/9.faq.html#id25)：`python -u`/显式 flush；开发与任务的 shell 环境可能不同 | 生成 Python 命令使用绝对解释器和及时日志；不依赖交互 shell 激活环境 |
| [任务实例访问](https://hpc.westlake.edu.cn/document/ai/aistation/9.faq.html#id21)：正式任务不支持开发环境相同的 SSH 调试入口 | 正式任务按日志与实例监控诊断，不能把开发容器 SSH 工作流直接套到训练任务 |
| [计算计费](https://hpc.westlake.edu.cn/document/ai/aistation/3.resource.html#gpu-billing-rules)与 [存储计费](https://hpc.westlake.edu.cn/document/ai/aistation/3.resource.html#storage-billing-rules)：按资源运行计费，CPU 使用也可能计费，SSD 无免费容量 | 程序 GPU 小时不等于完整账单；准备、传输、等待和回收仍可占资源。实际费率与组账单需核对当前政策 |
| [资源利用率规则](https://hpc.westlake.edu.cn/document/ai/aistation/3.resource.html#id3)：公共资源组禁止占卡，长期低利用率任务可能删除 | 根据监控合理申请、及时释放；不制造无意义负载“保卡” |

## 客户端自己的保护

以下不是手册额外规定，也不代表官方 API 保证：

- 13,800 秒程序上限和各阶段默认预算。手册规定的是开发环境最长四小时；客户端把准备、传输、程序、回收和清理计入这个总预算。
- 默认为离线的 `plan`、显式 `--execute`、每运行独立锁和唯一 ID、未知创建结果必须先对账。
- HTTPS 证书与 SSH 指纹核验、系统凭据库、原子状态、SHA-256 传输校验和不可覆盖发布。
- 执行前挂载检查及小写入探测。`df` 只反映文件系统容量，不是实验室组配额查询；一次小写入通过也不保证大型计算不会耗尽配额。
- 开发后端使用本机控制器及超时看门狗，长期断网不保证立即回收结果或释放资源。原生任务由平台管理，本机观察断开不取消任务；两者都不能修复共享存储服务延迟。
- 原生启动 guard 检查真实挂载与 GPU，保留每次 attempt 的执行证据；无终态证据的成功任务标记未验证。科学结果仍需独立检查。
- 发布 GitHub 后必须同步本机 skill 与 canonical CLI，核对同一提交版本。

## 部署实测与边界

- 当前版本已完成真实单节点 V100 原生任务：请求及实际1张GPU、CUDA矩阵计算、持久结果与独立回执回读、SHA-256核验、任务终态和暂存CPU环境释放。
- 先前GPU被保存为CPU的根因是把前端列表路由/返回分类字段taskType带入普通创建请求；正常创建应省略它。单变量验证已纠正先前平台故障判断。
- 平台会改写command中的双引号与换行；启动器使用不受该改写影响的编码封装。长内嵌命令曾被长度限制拒绝；task-stage预置小文件，默认1000字节为客户端保守预算。
- 登录默认直接输入密码，不访问钥匙串。系统凭据库只有显式选择才使用。
- 原生执行不依赖客户端持续在线，时限仍由资源组决定。多节点DDP、生产批量调度和原生自动下载结果尚未实现。
- 独立存储SFTP尚未自动化；核实自己的入口、身份、路径及权限，不从示例推断。真实运行仍必须验证自己的镜像、硬件、挂载、配额和科学产物。
