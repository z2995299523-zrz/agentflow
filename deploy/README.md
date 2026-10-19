# AgentFlow 服务器发布

沿用同机已有网站的 Git 指定提交发布方式：本地测试及构建 → 推送 Git → 服务器拉取完整提交 → Linux 测试、准备数据 → 备份 → 原子切换 → 验证。新增站点使用独立账户和端口。

| 项目 | 位置 |
| --- | --- |
| HTTPS | `https://<服务器IP>:8447` |
| 后端 | `127.0.0.1:8790`，单个进程 |
| Git 对象 | `/opt/agentflow/repo.git` |
| 独立版本 | `/opt/agentflow/releases/git-<完整SHA>` |
| 当前版本 | `/opt/agentflow/current` |
| 元数据和模拟库 | `/var/lib/agentflow/marts/<SHA>/build`，每版独立 |
| 稳定会话库 | `/var/lib/agentflow/history.db`，跨版本保留 |
| 模型缓存 | `/var/lib/agentflow/huggingface` |
| 配置 | `/etc/agentflow/app.env`，root:agentflow 0640，不进入 Git |
| 备份 | `/var/backups/agentflow`，每日 UTC 04:25，在线备份并隔离恢复核对行数 |
| 证据 | `/opt/agentflow/deployments/<SHA>`，Linux 测试、依赖清单、发布及验收记录 |

这是使用模拟银行专题库的公开演示站，继承现有应用的共享会话行为；没有用户隔离。不要输入真实客户资料或敏感需求。真实专题库目录和本地会话记录不上传。模型请求使用服务器的 DeepSeek 配置。

## 本地准备

先运行 `python run_tests.py`。提交源码并推送到服务器可拉取的 Git 分支后，运行：

```powershell
python deploy/package_frontend.py
```

它重新构建前端，将完整 Git 提交和每个文件的 SHA256 写入包内清单。将 `.runtime-deploy/frontend.tar.gz` 上传到服务器的 `/tmp/agentflow-frontend-<SHA>.tar.gz`。不能用其他提交构建的前端包混合发布。

## 首次安装

先核对 Ubuntu、端口 8447/8790、现有服务及可用资源。创建系统账户 `agentflow` 和 `/etc/agentflow`，通过 SSH 标准输入传送配置；不要把密钥放进命令参数、Git、笔记或验收输出。配置至少包含：

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=<通过安全通道写入>
MART_DIR=./data/mock_mart
EXECUTOR=duckdb
SQL_DIALECT=postgres
LLM_TIMEOUT=120
```

服务器拉取指定提交后，使用该提交内的 `deploy/release.py`。代码使用同机已有且重新核验摘要的 `<uv 路径>`；先安装官方 CPU 版 torch，再安装与本地一致的直接依赖，记录实际 Linux 依赖版本。该工具不会修改其它网站的运行环境。

```bash
sudo python3 /opt/agentflow/releases/git-<SHA>/deploy/release.py <SHA> prepare --frontend /tmp/agentflow-frontend-<SHA>.tar.gz
sudo python3 /opt/agentflow/releases/git-<SHA>/deploy/release.py <SHA> activate
sudo python3 /opt/agentflow/current/deploy/release.py <SHA> verify
```

第一次需要先通过 `git clone --mirror`、`git fetch`、`git archive` 取出指定提交，作为准备脚本的可信来源。`prepare` 可以继续未完成的准备，已经有 `prepared.json` 时拒绝重复准备。`activate` 会备份当前配置和会话；后端健康或 Nginx 检查失败时恢复上一版。首次发布没有上一版，失败时停用本应用并保留数据和证据。

后端由 systemd 以 `agentflow` 账户运行，代码目录只读；可写位置仅为应用数据目录。向量模型保留 BGE，不用测试哈希模型代替。CPU 线程限制为 1，服务内存硬上限 1100 MiB；应结合整机内存和实际问答观察调整，不能将资源上限视作性能已验收。

Nginx 复用同机已有的 IP 证书和续期任务，只新增 8447 入口。SSE 禁用缓冲，代理超时 600 秒；前端由 Nginx 直接托管。证书续期仍由原任务负责，需核对其定时器和实际证书有效期。

## 验证和回滚

除 `verify` 外，必须验证公网 HTTPS、首页静态资源、前端路由、元数据及字段血缘、真实 DeepSeek 问答、SSE 分步到达、DuckDB 只读结果，以及会话在重启后仍存在。检查原有站点服务与 HTTPS 仍正常。公网验证使用系统证书校验，不能加 `-k` 作为正式验收。

```bash
sudo python3 /opt/agentflow/current/deploy/release.py <当前SHA> rollback
```

回滚只切换本次发布登记的上一版代码，保留最新会话。首次发布没有上一版时脚本拒绝回滚，需人工审阅停用。当前没有会话结构迁移；如后续改动 `nl2sql/history.py` 的建表结构，必须先评估兼容性并单独演练迁移，不能直接套用代码回滚。

备份只包含有价值的会话库和来源版本记录；元数据、模拟数据、模型可以从已登记版本重新生成。服务器本机备份不等于离机备份。已有备份与历史版本不自动清理。
