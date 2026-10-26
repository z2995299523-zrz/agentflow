# AgentFlow 服务器部署交接

这份交接给接手本项目的 Claude Code 使用。目标是在现有服务器上更新 AgentFlow，并保持真实专题库资料、访问密码、会话历史和其它网站可用。日常更新采用指定 Git 提交发布；真实资料通过独立快照传输，不能加入 Git。

先阅读本文件，再检查当前代码和服务器。`CLAUDE.md` 里的 Docker Compose 是可选启动方式；本服务器实际使用 **Nginx + systemd + Python 虚拟环境**，无需安装 Docker 或启动 Vite 开发服务器。

## 当前站点和服务器

| 项目 | 值 |
| --- | --- |
| 本机项目 | `D:\agentflow` |
| Git 远程 | `git@github.com:z2995299523-zrz/agentflow.git` |
| 服务器拉取地址 | `https://github.com/z2995299523-zrz/agentflow.git` |
| 部署分支 | `codex/agentflow-server-deploy`，实际部署仍以完整提交 SHA 为准 |
| 系统 | Ubuntu 24.04，Python 3.12 |
| SSH | `<SSH用户>@<服务器IP>`，端口 22 |
| 本机 SSH 程序 | `C:\Windows\System32\OpenSSH\ssh.exe` |
| 本机私钥文件 | `<本机私钥文件>`，只读取路径，不展示内容 |
| sudo | <SSH用户> 已有免密授权；连接后仍应执行 `sudo -n id -u` 核实 |
| HTTPS | `https://<服务器IP>:8447/`，入口有密码保护 |
| API | `127.0.0.1:8790`，单个 Uvicorn 进程 |
| 应用系统账户 | `agentflow`，不能交互登录 |
| 当前资料 | 真实专题库元数据、字段血缘、加工 SQL、BGE 向量 |
| 真实资料数量 | 833 张表、22,730 个字段、12,693 条血缘、1,714 条关联、45 条加工语句、22,730 条字段向量 |
| 执行层 | `none`，生成并校验 SQL，没有连接单位业务数据库 |

服务器约 2 GB 内存，与其它站点和服务共用，保留它们的服务、端口、配置和数据。

2026-10-19 实际运行代码提交为 `e957221e55cbed3d2339aee679f2b18eb06bda2b`。本地与 Linux 各 1,564 项测试通过。后续文档补充提交不代表线上代码自动更新，活动版本始终用 `readlink -f /opt/agentflow/current` 核对。

仓库是公开的，服务器地址、SSH 用户、私钥文件名、同机其它站点和服务的实际信息不写进仓库，本文件用 `<服务器IP>`、`<SSH用户>`、`<本机私钥文件>`、`<同机其它服务>` 代替；实际值只在本机 `.runtime-deploy/private-docs/`（不入库）。2026-10-19 为此改写过本分支历史：线上的 `e957221…` 对应改写后的 `6b3b9964033d7605738dcfa5d3cd83ffc7e4f974`，代码相同，只差文档和占位符；改写前的历史打包在本机 `.runtime-deploy/codex-before-sanitize.bundle`。

SSH 登录示例：

```powershell
& 'C:\Windows\System32\OpenSSH\ssh.exe' `
  -i '<本机私钥文件>' `
  -p 22 -o BatchMode=yes -o StrictHostKeyChecking=yes `
  <SSH用户>@<服务器IP>
```

不要为了连通而关闭主机密钥检查，也不要把私钥、DeepSeek Key 或访问密码复制进聊天、Git、日志和这份交接。

## 目录和文件职责

| 路径 | 职责 |
| --- | --- |
| `/opt/agentflow/repo.git` | 服务器 Git 镜像 |
| `/opt/agentflow/releases/git-<SHA>` | 该提交独立的代码、`.venv` 和前端 `web/dist` |
| `/opt/agentflow/current` | 原子切换的活动版本指针 |
| `/opt/agentflow/deployments/<SHA>` | 测试、依赖、资料契约、上线与验收证据 |
| `/etc/agentflow/app.env` | 运行配置，root:agentflow 0640；包含模型凭据，不展示全文 |
| `/var/lib/agentflow/datasets/<资料版本>` | 仓库外的真实资料快照；清单、SQL、`build/metadata.db`、逐文件摘要 |
| `/var/lib/agentflow/datasets/real-20261019-v1` | 本次真实资料快照，后续更新使用新目录，不覆盖它 |
| `/var/lib/agentflow/packages/real-20261019-v1.tar.gz` | 保留的原始真实资料包，SHA256 为 `632ca1a805572b6761c34c72d1e2687f90056cb99806af5f0cfe5ed192b8dc14` |
| `/var/lib/agentflow/history-real.db` | 真实资料的稳定会话库，各真实资料版本通过链接使用它 |
| `/var/lib/agentflow/history.db` | 原模拟资料会话，保留，不与真实资料会话混用 |
| `/var/lib/agentflow/marts/<SHA>/build` | 历史模拟库生成物，保留 |
| `/var/lib/agentflow/huggingface` | BGE 模型缓存，代码版本共用 |
| `/var/backups/agentflow` | SQLite 在线备份、摘要、隔离恢复检核结果 |
| `/etc/nginx/sites-available/agentflow` | 本站 HTTPS 和 `/api/` 转发 |
| `/etc/nginx/agentflow-access.conf` | 独立访问控制；Nginx 版本配置通过 include 引用 |
| `/etc/nginx/agentflow.htpasswd` | 密码摘要，root:www-data 0640，不写进源码 |
| `/etc/agentflow/site.json` | 站点配置，root 所有、0644，不进入 Git：`public_url`、`server_name`、`cert_dir`（Nginx 模板里的变量）、`uv`（同机既有部署工具，使用前核对旁边 `uv.sha256`，不修改其它网站的运行环境）、`watch_units`（验收时要保持运行的同机服务）；实际内容见本机 `.runtime-deploy/private-docs/site.json` |

本机登录凭据单独保存在 `.runtime-deploy/服务器访问凭据.txt`；程序化验证可以读取同目录的 `access-credentials.json`。这些文件已被 Git 忽略。只将本文件交给 Claude Code，不需要把密码正文贴入提示词。

程序职责：

- `deploy/package_frontend.py`：本机构建前端，绑定完整 Git 提交及逐文件 SHA256。
- `deploy/dataset.py`：本机制作私有资料快照、服务器校验并安装；不会复制本地会话、`.env`、评估题或转换器配置。
- `deploy/runtime_profile.py`：读取运行配置、核对资料契约；切换资料时保留密钥原文和权限。
- `deploy/release.py`：准备、激活、验收和回退，支持外部真实资料，不再把健康检查固定为 14 张模拟表。
- `deploy/backup_history.py`：根据当前运行配置或 `--source` 参数备份相应会话库，并做隔离恢复。

## 开始更新前先检查

本机：

```powershell
git status --short
git branch --show-current
git remote -v
git log -5 --oneline
```

保留用户和其它窗口的改动。提交时明确指定本次文件，不能直接把整个暂存区的其它改动一起提交。真实表名、模式名、单位名称及真实来源路径不得出现在任何入库文件、测试、提交说明或部署文档中。

服务器：

```bash
sudo -n id -u
readlink -f /opt/agentflow/current
systemctl is-active agentflow nginx <同机其它服务>
free -h
df -h /
ss -lnt
sudo nginx -t
sudo systemctl status agentflow --no-pager
```

查询运行配置时只打印需要的非秘密项，例如 `MART_DIR`、`EXECUTOR`、`AGENTFLOW_HISTORY_DB`。不要 `cat app.env`、输出全部环境变量，或把密钥当命令参数。

## 真实资料如何准备

真实资料来源是本机 `data/real_mart/`。该目录不入库，部署时按下面的步骤制作独立包。资料包只包含：

1. 标准 CSV 清单。
2. `scripts/` 下的加工 SQL。
3. 用 SQLite 在线备份生成的 `build/metadata.db`，含已计算的 BGE 向量和版本号。
4. `version.json`：版本号、数量、输入和代码的摘要、关卡结果、需确认项条数；不含文件名和需确认项原文。
5. `dataset-manifest.json`，登记数量、向量器、版本号和逐文件 SHA256。

本地 `build/history.db`、`.env`、业务数据、评估文件、`convert.ini`、源资料路径和保密词文件都不会被复制。这个操作是发布模型资料，不是迁移单位数据库。

资料包内的加工 SQL 仅用于提供模型和血缘证据，不要把它们作为初始化脚本连到单位数据库执行。

资料有变化时，先在本机导成新版本（元数据按版本管理，见 `notes/2026-10-23_design_元数据更新机制.md`）：

```powershell
python data/real_mart/convert.py                 # 来源资料变了时先转换
python -m metadata.importer data/real_mart       # 导成新版本，和上一版比出差异报告
```

导入后没有需确认项就自动切换；有需确认项时，先看 `data/real_mart/build/versions/<版本号>/diff_report.md`，确认没问题再运行 `python -m metadata.versions promote <版本号> --accept --mart data/real_mart`。差异报告里有真实表名，只留在本机。

```powershell
python deploy/dataset.py package `
  --mart-dir data/real_mart `
  --output .runtime-deploy/private-dataset-<版本号>.tar.gz
```

目标文件已存在时脚本拒绝覆盖。打包前脚本会核对版本状态，下面任何一条不满足就拒绝打包：资料目录有活动版本；活动版本不是旧库登记的（有输入记录）；资料目录里现在的清单和脚本与活动版本登记的一致（改了没重新导入会被拦下）；`build/metadata.db` 的版本号就是活动版本。BGE 向量与元数据必须配套。

上传前后核对脚本输出的 SHA256。使用 SSH/SCP 传输，临时包应为 0600，安装后的真实资料目录只给 root 和应用账户读取。服务器安装命令：

```bash
sudo python3 /opt/agentflow/current/deploy/dataset.py install \
  --archive /tmp/agentflow-private-<版本号>.tar.gz \
  --id real-<版本号> \
  --history /var/lib/agentflow/history-real.db
```

`--id` 只接受 real/private 前缀、英文小写字母、数字和连字符；用本机的版本号命名，例如版本 `20261023-1450` 装成 `real-20261023-1450`，服务器上的快照和本机版本一一对得上。已安装的同名目录拒绝覆盖。脚本会阻止路径越界、链接、摘要不一致、清单外文件和本地会话库进入资料快照；包里有 `version.json` 时，它的版本号必须和元数据库里的一致。旧格式的包（没有版本号）照旧能装。随新代码一起发布资料时，用新代码目录 `/opt/agentflow/releases/git-<新SHA>/deploy/dataset.py` 安装。

首次使用真实会话库时，先以 `agentflow` 账户调用 `ConversationStore` 初始化稳定库，再安装资料或启动服务。不要复制本地真实会话来替代初始化。后续更新复用服务器现有稳定会话库。

## 常规代码发布顺序

### 本机测试和推送

```powershell
python run_tests.py
# 改过前端时单独构建；打包脚本也会执行构建
cd web
npm run build
cd ..

# 检查并提交明确的本次文件后，再推送所选分支
git -c core.sshCommand=C:/Windows/System32/OpenSSH/ssh.exe push origin HEAD
git rev-parse HEAD

python deploy/package_frontend.py
```

`package_frontend.py` 检查前端源码没有未提交的修改，将当前完整 SHA 写入包内。源码提交发生变化后，不能把旧包冒充新提交重新部署。

将生成的 `.runtime-deploy/frontend.tar.gz` 上传为 `/tmp/agentflow-frontend-<完整SHA>.tar.gz`。SSH/SCP 使用本文件前面登记的私钥路径。

### 服务器取出新提交

服务器上的 Git 镜像已经存在。新提交第一次准备时，先从镜像取出它自己的发布脚本；不能在正在运行的 `current` 中执行 `git pull`。

```bash
release_sha='<40位完整SHA>'
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || exit 1
release_dir="/opt/agentflow/releases/git-$release_sha"

sudo git --git-dir=/opt/agentflow/repo.git fetch --prune origin
sudo git --git-dir=/opt/agentflow/repo.git cat-file -e "$release_sha^{commit}"
if ! sudo test -d "$release_dir"; then
    sudo install -d -m 755 "$release_dir"
    sudo git --git-dir=/opt/agentflow/repo.git archive \
      --format=tar --output="/opt/agentflow/releases/source-$release_sha.tar" "$release_sha"
    sudo tar -xf "/opt/agentflow/releases/source-$release_sha.tar" -C "$release_dir"
fi
```

目标目录已有文件但准备未完成时，先查看该次日志和证据，再继续；不要覆盖活动目录或随意删除失败目录。

### 准备阶段

从 2026-10-19 改写历史后的版本起，`release.py` 从 `/etc/agentflow/site.json` 读站点专属的值，服务器上没有这个文件时 `prepare`、`activate`、`verify` 都会在动手前报错。线上的 `e957221…` 还不需要它；第一次用新版本发布前，把本机 `.runtime-deploy/private-docs/site.json` 用 SCP 传到 `/tmp/agentflow-site.json`，核对内容后安装：

```bash
sudo install -m 644 -o root -g root /tmp/agentflow-site.json /etc/agentflow/site.json
sudo python3 -c "import json; print(sorted(json.load(open('/etc/agentflow/site.json'))))"
```

```bash
sudo python3 "$release_dir/deploy/release.py" "$release_sha" prepare \
  --frontend "/tmp/agentflow-frontend-$release_sha.tar.gz" \
  --mart-dir /var/lib/agentflow/datasets/real-20261019-v1 \
  --executor none
```

仅代码更新时，可省略 `--mart-dir` 和 `--executor`，脚本会沿用服务器当前的真实资料配置。只换资料、代码不变时，不走 prepare/activate，用下面「只换真实资料」一节的 `switch-data`；代码和资料都要更新时分两步：先发代码（资料不变），验收后再 `switch-data`，出了问题知道是哪一步。

准备阶段会：核对前端提交和摘要，建立独立 `.venv`，安装官方 CPU 版 torch 与服务器依赖，记录完整 Linux 依赖版本，运行所有单元测试，再核对真实元数据库的完整性、数量、BGE 向量、访问控制和稳定会话路径。真实资料不重新造 DuckDB，也不自动重算全部向量。

长任务可以用 `systemd-run` 运行，并用 `journalctl -u <准备任务名>` 看实际进度。服务器只有约 2 GB 内存，应限制准备任务 CPU、内存和并行下载；不要同时跑多个依赖安装或模型导入任务。

只有出现 `/opt/agentflow/deployments/<SHA>/prepared.json` 才代表准备成功。测试输出在同目录 `tests.log`，完整依赖版本在 `requirements-linux.lock`。

### 激活和验证

```bash
sudo python3 "$release_dir/deploy/release.py" "$release_sha" activate
sudo python3 /opt/agentflow/current/deploy/release.py "$release_sha" verify
```

如果当前指针已经指向同一 SHA，执行 `verify`，不要重复 `activate`，以免把“上一版”记录改成同一版本。

激活阶段先在线备份旧资料会话，保存旧配置及其权限，然后原子更新资料相关配置与代码指针，重启本应用，按准备时登记的契约检查健康状态，检查并平滑重载 Nginx，执行当前真实会话的备份恢复。

失败时恢复原来的运行配置、代码指针和系统服务文件。`runtime-before-*.env` 是仅 root 可读的敏感回退材料，不能下载到源码目录或提交 Git。

运行配置的资料相关项应为：

```dotenv
MART_DIR=/var/lib/agentflow/datasets/real-20261019-v1
EXECUTOR=none
AGENTFLOW_HISTORY_DB=/var/lib/agentflow/history-real.db
SQL_DIALECT=postgres
```

DeepSeek 凭据继续留在 `app.env`，不是从 Git 拉取。真实资料切换时，必须将 `EXECUTOR` 一并设为 `none`，不能向模拟库执行真实表名对应的 SQL。

## 只换真实资料

代码不变、只换真实资料时，在当前代码目录执行：

```bash
current_sha=$(basename "$(readlink -f /opt/agentflow/current)" | sed 's/^git-//')
[[ "$current_sha" =~ ^[0-9a-f]{40}$ ]] || exit 1
sudo python3 /opt/agentflow/current/deploy/release.py "$current_sha" switch-data \
  --mart-dir /var/lib/agentflow/datasets/real-<版本号>
```

`switch-data` 动手前核对：当前指针就是这个提交；新快照在 `/var/lib/agentflow/datasets/` 下，`EXECUTOR` 保持 `none`；站点访问密码在；新快照的完整性、数量、BGE 向量、版本号都合格；稳定会话库已存在；新资料和当前资料不是同一份。任何一条不满足就停下，什么都不改。然后在线备份当前资料的会话库，把运行配置原文存成 `deployments/<SHA>/runtime-before-*.env`（仅 root 可读），改运行配置里的 `MART_DIR`，重启本应用，按新快照的契约做健康检查（含版本号）。健康检查失败就写回原来的运行配置并重启，再报错。成功后在 `deployments/<SHA>/data-switch-*.json` 记下换前换后的资料，`rollback-data` 用它。代码指针和 `previous.json` 都不动，所以代码回退和资料回退互不干扰。

切回上一份资料：

```bash
sudo python3 /opt/agentflow/current/deploy/release.py "$current_sha" rollback-data
```

`rollback-data` 按最近一次 `data-switch` 记录切回换之前的快照，同样会备份、检查、失败恢复，并记一条新记录，所以连着执行两次就回到原处。它只在 `datasets` 下的快照之间切换；要回到模拟资料，用代码回退 `rollback`。

注意：

- `switch-data`、`rollback-data` 和带版本号的资料包，要服务器上运行的是包含元数据版本功能的代码才有。2026-10-19 上线的 `e957221…` 没有，它的 `dataset.py install` 也不认包里的 `version.json`。第一次先按「常规代码发布顺序」发新代码（资料不变），之后再用 `switch-data` 换资料
- 重启会打断正在跑的查询（没跑完的这一轮不会落库），切换安排在没人用的时候
- 旧快照不自动删；人工清理时至少留当前和上一份（`rollback-data` 要用上一份）

## 上线验收

SSH 内部检查不需要网站访问密码：

```bash
curl --fail http://127.0.0.1:8790/health
```

公网检查必须验证 TLS，并携带访问凭据。命令行 `curl --user agentflow` 会交互询问密码，不把密码写在命令参数里：

```bash
curl --fail --user agentflow https://<服务器IP>:8447/api/health
```

至少检查：

- 未认证访问首页和元数据接口返回 401；已认证访问正常。
- `/api/health` 为 `status=ok`，`executor=none`，`counts` 等于本次资料契约；真实资料当前是 833 张表，不是 14 张表。新版本代码还返回 `metadata_version`，应等于本次资料的版本号（快照名 `real-<版本号>` 里的那段），`reload_error` 为空。
- 前端显示真实资料与“只生成 SQL”；表、字段、搜索及血缘图能加载。
- 使用真实资料的典型需求调用 DeepSeek，SQL 的静态校验通过、实际用表存在于本次元数据库中。
- `exec_result` 为空，或其中 `executed=false`。界面没有业务查询结果是当前执行契约，不应捏造一组数据来演示执行成功。
- SSE 的 `step` 事件在最终 `turn` 之前分批到达，最终会话成功落库。
- 重启本应用后，会话及结果仍然存在；不重启其它站点。
- 对含真实问答的会话库执行备份，并检核隔离恢复的完整性和行数。
- 同机原有站点和服务继续正常。

实际问答和截图可能包含真实模型名称，只保存到被忽略的私有验收目录或 root 私有服务器证据中。入库笔记只写方法、数量、耗时和通过情况。

用户已在 2026-10-19 明确允许本次问答验收向 `api.deepseek.com` 发送命中的真实表名、字段说明、加工表达式和血缘证据。范围不包含业务记录或数据库密码；访问 Key 仅按既有服务配置用于认证，不进入模型提示词。保持已确认的提供方和模型资料范围，不能据此扩展到其它服务或整库业务数据上传。

### 本次真实问答的业务边界

首次“2026 年立项项目数量”在约 7.03 秒生成 SQL，静态校验通过，但模型用了申报资料，把申报日期近似为立项日期，与原评估确认的项目表不一致。追加“只统计正式立项，不能用申报日期近似”的明确追问后，约 6.95 秒生成 SQL，选回已确认的项目表，静态校验通过。

两次都没有执行单位数据库。这证明资料切换、模型调用、SSE 和追问链路正常，不能证明所有问法都满足业务口径。接手时保留这项偏差，后续优化应按项目评估流程处理，不能仅凭 SQL 静态通过就将立项和申报混用标为正确。

## 备份和回退

```bash
sudo systemctl start agentflow-backup.service
sudo journalctl -u agentflow-backup.service -n 10 --no-pager
systemctl list-timers agentflow-backup.timer --no-pager
```

定时器为 UTC 04:25，加最多五分钟随机延迟，即北京时间约 12:25～12:30。备份任务读取 `AGENTFLOW_HISTORY_DB`，当前应指向 `history-real.db`，不能只备份旧模拟会话。

```bash
sudo python3 /opt/agentflow/current/deploy/release.py <需要撤回的当前完整SHA> rollback
```

回退使用本次 `previous.json` 登记的代码和资料配置，保留两套会话数据库的新记录。若上一版是模拟资料，回退后会显示原模拟会话；真实会话仍在 `history-real.db` 中，没有被覆盖。

只回退资料、代码不动时用 `rollback-data`（见「只换真实资料」）。会话库跨资料版本共用，不随资料回退；资料换了以后，追问会先在当前资料上核对上一轮的 SQL，表或字段没了就按完整需求重新生成。

本次真实资料发布的上一版是旧模拟版本。发布脚本在回退旧版 Nginx 模板时会继续保留已有的密码 include；回退后仍须核对匿名访问为 401。后续新版本的模板已保留外部访问控制 include。

回退不是数据库恢复。会话表结构如有变化，要单独评估兼容性和迁移；不能直接拿旧备份覆盖新记录。已有真实资料、模拟资料、会话库、失败证据和备份都不自动删除。服务器本机备份不能替代离机备份。

## 运行排查

```bash
sudo journalctl -u agentflow -n 80 --no-pager
sudo tail -n 40 /var/log/nginx/agentflow.error.log
sudo systemctl show agentflow -p MemoryCurrent -p MemoryPeak -p NRestarts
sudo systemctl status <同机其它服务> nginx --no-pager
```

常见情况：

| 现象 | 优先检查 |
| --- | --- |
| 首页或接口 401 | 站点访问密码，与 DeepSeek Key 无关 |
| 首页能打开，接口 502 | 本应用是否运行，8790 是否仅在本机监听，预热是否完成 |
| 健康检查正常，查数失败 | 模型 Key、服务器到 DeepSeek 的网络、提示词或真实元数据证据；健康状态不等于真实模型调用已经成功 |
| 真实资料显示成 14 张表 | `MART_DIR` 是否误回模拟目录，是否用了旧版只支持模拟资料的发布脚本 |
| 只生成 SQL，没有执行表格 | 确认 `EXECUTOR=none`；这是预期行为，未接入真实数据库 |
| 引号未闭合的错误缺少中文行号 | `sqlglot[rs]==25.34.1` 和 `sqlglotrs==0.3.0` 是否完整安装 |
| 元数据库无法写入 | 检查资料生成目录、稳定会话链接及逐级权限；不要把整个代码目录改成应用可写 |
| 内存到顶或频繁重启 | 单进程和 CPU 线程限制是否保留，是否同时加载多套模型；当前硬限制 1100 MiB，先采样再调整 |
| SSE 全部最后才出现 | Nginx `proxy_buffering off`、响应 `X-Accel-Buffering: no`、600 秒超时是否保留 |

## 给 Claude Code 的执行要求

先核对现场状态，再处理明确的本次变更；保持代码、真实资料、运行秘密、会话历史四者分开。更新前备份，准备成功后切换，用真实请求验证，失败按登记的旧版本回退。输出实际测试数字和未验证边界，不把端口正常、SQL 静态通过或元数据可读当作真实数据库执行验收。

不要覆盖用户改动，不修改其它网站，不恢复已停止跟踪的 `.hermes`，不提交真实资料或凭据。交付时说明运行提交、资料版本、问答结果、会话备份恢复和其它站点状态。
