"""在既有 Ubuntu 服务器上按完整 Git 提交发布；代码与会话数据分开保存。"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

if __package__:
    from .runtime_profile import ENV_PATH, make_profile, read_environment, write_profile
else:
    from runtime_profile import ENV_PATH, make_profile, read_environment, write_profile

BASE = Path("/opt/agentflow")
REPO = BASE / "repo.git"
DATA = Path("/var/lib/agentflow")
# 服务器专属的值（公网地址、证书目录、uv 路径、同机要保持运行的服务）只放在服务器上，不进入 Git。
# 可选的 nginx_template 指向服务器上自己的 Nginx 模板，见 nginx_template()。
SITE_PATH = Path("/etc/agentflow/site.json")
SITE_KEYS = ("public_url", "server_name", "cert_dir", "uv", "watch_units")
NGINX_SITE = Path("/etc/nginx/sites-available/agentflow")
ACCESS_CONF = Path("/etc/nginx/agentflow-access.conf")
REMOTE = "https://github.com/z2995299523-zrz/agentflow.git"


def load_site(path: Path = SITE_PATH) -> dict:
    """读服务器上的站点配置，缺文件、缺键或格式不对时在动手前报错。"""
    if not path.is_file():
        raise RuntimeError(f"缺少 {path}，先按部署交接在服务器上建好站点配置")
    site = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in SITE_KEYS if key not in site]
    if missing:
        raise RuntimeError(f"{path} 缺少：{', '.join(missing)}")
    if not str(site["public_url"]).startswith("https://") or not isinstance(site["watch_units"], list):
        raise RuntimeError(f"{path} 的 public_url 必须是 https 地址，watch_units 必须是列表")
    if site.get("nginx_template") and not Path(site["nginx_template"]).is_file():
        raise RuntimeError(f"{path} 的 nginx_template 指向的文件不存在：{site['nginx_template']}")
    return site


def render_nginx(template: bytes, site: dict) -> bytes:
    """把 Nginx 模板里的站点变量换成服务器上的值；旧版本里写死的配置原样返回。"""
    content = (template.replace(b"__SERVER_NAME__", site["server_name"].encode())
               .replace(b"__CERT_DIR__", site["cert_dir"].encode())
               .replace(b"__PUBLIC_URL__", site["public_url"].encode()))
    left = sorted({name.decode() for name in re.findall(rb"__[A-Z_]+__", content)})
    if left:
        raise ValueError(f"Nginx 模板还有没填的变量：{', '.join(left)}")
    return content


def nginx_template(release: Path, site: dict) -> bytes:
    """选 Nginx 模板：站点配置写了 nginx_template 就用服务器上的这份，否则用该版本仓库里的模板。

    入口只在私人通道监听（本机转发、内网地址）的服务器，监听地址和证书写在服务器上的模板里；
    回退到旧版本时也用这份，不会按旧版仓库模板改回所有地址监听。
    """
    custom = site.get("nginx_template")
    return Path(custom).read_bytes() if custom else (release / "deploy/nginx.conf").read_bytes()


def run(*arguments: str, cwd: Path | None = None, capture: bool = False) -> str:
    """执行一个部署步骤；命令失败立即退出，不输出运行密钥。"""
    result = subprocess.run(arguments, cwd=cwd, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def save(path: Path, value: dict) -> None:
    """保存发布证据，避免仅凭终端输出判断上线成功。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def as_app(release: Path, *arguments: str) -> None:
    """以应用账户运行导入和造数，模型缓存与生成物只写持久目录。"""
    run("sudo", "-u", "agentflow", "env", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1",
        "HF_HOME=/var/lib/agentflow/huggingface", "HF_ENDPOINT=https://huggingface.co",
        "OMP_NUM_THREADS=1", "MKL_NUM_THREADS=1", "OPENBLAS_NUM_THREADS=1",
        "TOKENIZERS_PARALLELISM=false", str(release / ".venv/bin/python"), *arguments, cwd=release)


def unpack_frontend(archive: Path, release: Path, commit: str) -> None:
    """核对前端来源提交与逐文件摘要，只解包普通文件。"""
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        for member in members:
            if (not member.isfile() or PurePosixPath(member.name).is_absolute()
                    or ".." in PurePosixPath(member.name).parts or "\\" in member.name):
                raise ValueError("前端包包含不允许的条目")
        manifest_file = bundle.extractfile("frontend-manifest.json")
        if manifest_file is None:
            raise ValueError("缺少前端清单")
        manifest = json.load(manifest_file)
        assert manifest["commit"] == commit, "前端构建提交不匹配"
        assert set(manifest["files"]) == {m.name for m in members if m.name != "frontend-manifest.json"}
        for name, digest in manifest["files"].items():
            assert name.startswith("web/dist/"), "前端文件路径超出允许范围"
            content = bundle.extractfile(name).read()
            assert hashlib.sha256(content).hexdigest() == digest, f"摘要不匹配: {name}"
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        save(release / "frontend-manifest.json", manifest)


def prepare(commit: str, frontend: Path, mart_dir: str | None = None, executor: str | None = None) -> None:
    """拉取指定提交、运行全套测试；准备模拟库或核验仓库外的真实资料快照。"""
    release = BASE / "releases" / f"git-{commit}"
    evidence = BASE / "deployments" / commit
    if (evidence / "prepared.json").exists():
        raise RuntimeError("该提交已经准备完成，请直接执行 activate 或 verify")
    uv = Path(load_site()["uv"])
    run("install", "-d", "-m", "755", str(BASE), str(BASE / "releases"), str(evidence))
    if not REPO.exists():
        run("git", "clone", "--mirror", REMOTE, str(REPO))
    run("git", f"--git-dir={REPO}", "fetch", "--prune", "origin")
    assert run("git", f"--git-dir={REPO}", "rev-parse", f"{commit}^{{commit}}", capture=True) == commit
    if not release.exists():
        archive = evidence / "source.tar"
        run("git", f"--git-dir={REPO}", "archive", "--format=tar", f"--output={archive}", commit)
        release.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(release, filter="data")
    unpack_frontend(frontend, release, commit)
    # uv 是同机已有项目安装的工具，使用前重新核对其登记摘要。
    assert hashlib.sha256(uv.read_bytes()).hexdigest() == uv.with_suffix(".sha256").read_text().split()[0]
    if not (release / ".venv").exists():
        run(str(uv), "venv", "--python", "/usr/bin/python3", str(release / ".venv"))
    python = str(release / ".venv/bin/python")
    run(str(uv), "pip", "install", "--python", python, "--index-url",
        "https://download.pytorch.org/whl/cpu", "torch==2.7.1+cpu")
    run(str(uv), "pip", "install", "--python", python, "--index-url", "https://pypi.org/simple",
        "-r", str(release / "deploy/requirements-server.txt"))
    freeze = run(str(uv), "pip", "freeze", "--python", python, capture=True)
    (evidence / "requirements-linux.lock").write_text(freeze + "\n", encoding="utf-8")
    with (evidence / "tests.log").open("w") as output:
        subprocess.run([python, "-m", "pytest", "tests", "-q", "--tb=short", "-p", "no:cacheprovider"],
                       cwd=release, stdout=output, stderr=subprocess.STDOUT, check=True,
                       env={**os.environ, "METADATA_EMBEDDER": "hash", "PYTHONDONTWRITEBYTECODE": "1"})
    run("id", "agentflow")
    run("install", "-d", "-o", "agentflow", "-g", "agentflow", "-m", "750",
        str(DATA), str(DATA / "huggingface"), str(DATA / "marts"), "/var/backups/agentflow")
    settings = read_environment(ENV_PATH)
    selected_mart = mart_dir or settings.get("MART_DIR", "./data/mock_mart")
    selected_executor = executor or settings.get("EXECUTOR", "duckdb")
    if selected_mart not in ("./data/mock_mart", "data/mock_mart"):
        root = Path(selected_mart).resolve()
        if not root.is_relative_to(DATA / "datasets") or selected_executor != "none":
            raise ValueError("真实资料只能使用 datasets 下的独立快照，且不连接模拟执行库")
        access = ACCESS_CONF
        if not access.is_file() or "auth_basic_user_file" not in access.read_text():
            raise RuntimeError("真实资料发布前必须先启用站点访问密码")
        profile = make_profile(selected_mart, selected_executor, release)
        if not Path(profile["history_path"]).is_file():
            raise RuntimeError("真实资料的稳定会话库尚未初始化")
        save(evidence / "prepared.json", {"commit": commit, "release": str(release), "profile": profile,
             "frontend_sha256": hashlib.sha256(frontend.read_bytes()).hexdigest(),
             "tests": (evidence / "tests.log").read_text()[-4000:]})
        return
    build = DATA / "marts" / commit / "build"
    run("install", "-d", "-o", "agentflow", "-g", "agentflow", "-m", "750", str(build))
    # 只连接生成物目录。每个版本有独立元数据及模拟库，会话库跨版本稳定保存。
    link = release / "data/mock_mart/build"
    # 全套测试会在源码目录生成模拟库。保留其证据后连接正式持久目录，不能沿用 root 的测试目录。
    if link.exists() and not link.is_symlink():
        preserved = evidence / f"test-build-{time.time_ns()}"
        link.rename(preserved)
    if not link.is_symlink():
        link.symlink_to(build, target_is_directory=True)
    if link.resolve() != build:
        raise RuntimeError("生成物目录没有指向本次发布的持久目录")
    if not (DATA / "history.db").exists():
        as_app(release, "-c", "from pathlib import Path; from nl2sql.history import ConversationStore; "
               "ConversationStore(Path('/var/lib/agentflow/history.db'))")
    history = build / "history.db"
    if not history.exists():
        history.symlink_to(DATA / "history.db")
    as_app(release, "-m", "metadata.importer", "data/mock_mart")
    as_app(release, "-m", "data.mock_mart.build_mock")
    save(evidence / "prepared.json", {"commit": commit, "release": str(release),
         "profile": make_profile(selected_mart, selected_executor, release),
         "frontend_sha256": hashlib.sha256(frontend.read_bytes()).hexdigest(),
         "tests": (evidence / "tests.log").read_text()[-4000:], "vector_model": "bge-small-zh-v1.5"})


def health(expected: dict | None = None) -> dict:
    """核对实际资料和准备阶段契约，兼容真实资料只生成 SQL 的部署。"""
    if expected is None:
        settings = read_environment(ENV_PATH)
        expected = make_profile(settings["MART_DIR"], settings["EXECUTOR"], BASE / "current")
    with urllib.request.urlopen("http://127.0.0.1:8790/health", timeout=10) as response:
        result = json.load(response)
    assert result["status"] == "ok" and not result["hint"], result
    assert result["executor"] == expected["executor"], "执行层不符合发布契约"
    assert result["mart_dir"] == expected["mart_dir"], "资料目录不符合发布契约"
    assert result["counts"] == expected["counts"], "元数据数量不符合发布契约"
    if expected.get("version_id") and "metadata_version" in result:
        assert result["metadata_version"] == expected["version_id"], "资料版本不符合发布契约"
    assert result["counts"]["字段向量"] > 0, result
    return result


def switch(target: Path) -> None:
    """原子切换代码指针，持久会话库保留在原处。"""
    temporary = BASE / "current.next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    temporary.replace(BASE / "current")


def activate(commit: str, profile_override: dict | None = None) -> None:
    """备份后切换版本，健康检查失败时恢复上一版代码和服务文件。"""
    release = BASE / "releases" / f"git-{commit}"
    evidence = BASE / "deployments" / commit
    assert (evidence / "prepared.json").is_file(), "必须先完成 prepare"
    assert Path("/etc/agentflow/app.env").is_file(), "必须先配置仓库外的 app.env"
    site = load_site()
    nginx_site = render_nginx(nginx_template(release, site), site)
    old = (BASE / "current").resolve() if (BASE / "current").exists() else None
    prepared = json.loads((evidence / "prepared.json").read_text())
    profile = profile_override or prepared.get("profile") or make_profile("./data/mock_mart", "duckdb", release)
    if profile["external"] and (not ACCESS_CONF.is_file()
                                or "auth_basic_user_file" not in ACCESS_CONF.read_text()):
        raise RuntimeError("真实资料激活前必须保留站点访问保护")
    settings = read_environment(ENV_PATH)
    old_profile = make_profile(settings["MART_DIR"], settings["EXECUTOR"], old or release)
    old_env = ENV_PATH.read_bytes()
    env_backup = evidence / f"runtime-before-{time.time_ns()}.env"
    env_backup.touch(mode=0o600)
    env_backup.write_bytes(old_env)
    if old is not None:
        as_app(old, str(old / "deploy/backup_history.py"), "--source", old_profile["history_path"])
    targets = {
        Path("/etc/systemd/system/agentflow.service"): release / "deploy/agentflow.service",
        Path("/etc/systemd/system/agentflow-backup.service"): release / "deploy/agentflow-backup.service",
        Path("/etc/systemd/system/agentflow-backup.timer"): release / "deploy/agentflow-backup.timer",
        NGINX_SITE: release / "deploy/nginx.conf",
    }
    previous = {str(path): path.read_bytes() if path.exists() else None for path in targets}
    save(evidence / "previous.json", {"release": str(old) if old else None, "profile": old_profile,
                                      "runtime_backup": str(env_backup)})
    for index, (name, content) in enumerate(previous.items()):
        if content is not None:
            (evidence / f"config-before-{index}").write_bytes(content)
    enabled = Path("/etc/nginx/sites-enabled/agentflow")
    was_enabled = enabled.is_symlink()
    try:
        write_profile(ENV_PATH, profile)
        switch(release)
        for target, source in targets.items():
            content = nginx_site if target == NGINX_SITE else source.read_bytes()
            # 回退到历史模板时也保留现有密码保护，不因旧代码模板缺少 include 而公开站点。
            if (target == NGINX_SITE
                    and ACCESS_CONF.is_file() and b"agentflow-access" not in content):
                content = content.replace(b"    server_tokens off;", b"    server_tokens off;\n"
                                          + f"    include {ACCESS_CONF};".encode())
                if b"agentflow-access" not in content:
                    raise RuntimeError("无法在旧版 Nginx 模板中保留访问保护")
            target.write_bytes(content)
            target.chmod(0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "agentflow")
        run("systemctl", "restart", "agentflow")
        for attempt in range(30):
            try:
                status = health(profile)
                break
            except Exception:
                if attempt == 29:
                    raise
                time.sleep(2)
        if not was_enabled:
            enabled.symlink_to("/etc/nginx/sites-available/agentflow")
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        run("systemctl", "enable", "--now", "agentflow-backup.timer")
        run("systemctl", "start", "agentflow-backup.service")
        save(evidence / "deployment.json", {"commit": commit, "previous_release": str(old) if old else None,
                                            "health": status, "profile": profile,
                                            "url": site["public_url"]})
    except Exception:
        ENV_PATH.write_bytes(old_env)
        if old:
            switch(old)
        else:
            run("systemctl", "stop", "agentflow")
            (BASE / "current").unlink(missing_ok=True)
        for name, content in previous.items():
            path = Path(name)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        if not was_enabled:
            enabled.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        if old:
            run("systemctl", "restart", "agentflow")
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        raise


def switch_data(commit: str, mart_dir: str, executor: str | None = None, *, how: str = "switch") -> None:
    """在当前提交上切换资料；先备份会话，失败恢复配置，成功登记可回退记录。"""
    release = BASE / "releases" / f"git-{commit}"
    evidence = BASE / "deployments" / commit
    if not (BASE / "current").exists() or (BASE / "current").resolve() != release.resolve():
        raise ValueError("当前指针不是这个提交，不能只切换资料")
    settings = read_environment(ENV_PATH)
    selected_executor = executor or settings["EXECUTOR"]
    root = Path(mart_dir).resolve()
    if not root.is_relative_to((DATA / "datasets").resolve()):
        raise ValueError("新资料必须在 datasets 下的独立快照目录中")
    if not ACCESS_CONF.is_file() or "auth_basic_user_file" not in ACCESS_CONF.read_text():
        raise RuntimeError("资料切换前必须启用站点访问密码保护")
    profile = make_profile(str(root), selected_executor, release)
    if not Path(profile["history_path"]).is_file():
        raise RuntimeError("资料的稳定会话库尚未初始化")
    old_profile = make_profile(settings["MART_DIR"], settings["EXECUTOR"], release)
    old_root = (release / settings["MART_DIR"]).resolve()
    if root == old_root:
        raise ValueError("新资料和当前资料是同一个目录")
    old_env = ENV_PATH.read_bytes()
    stamp = time.time_ns()
    env_backup = evidence / f"runtime-before-{stamp}.env"
    changed = False                          # 运行配置改过才需要恢复、重启；备份失败这类动手前的错误不碰服务
    try:
        as_app(release, str(release / "deploy/backup_history.py"), "--source", old_profile["history_path"])
        evidence.mkdir(parents=True, exist_ok=True)
        # 独占创建并限制权限，运行配置备份中可能含有密钥。
        with env_backup.open("xb") as output:
            env_backup.chmod(0o600)
            output.write(old_env)
        changed = True
        write_profile(ENV_PATH, profile)
        run("systemctl", "restart", "agentflow")
        for attempt in range(30):
            try:
                status = health(profile)
                break
            except Exception:
                if attempt == 29:
                    raise
                time.sleep(2)
        save(evidence / f"data-switch-{stamp}.json", {
            "commit": commit, "how": how, "from": old_profile, "to": profile,
            "runtime_backup": str(env_backup), "health": status,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    except Exception as error:
        if changed:
            try:
                ENV_PATH.write_bytes(old_env)
                run("systemctl", "restart", "agentflow")
            except Exception as recovery_error:
                error.add_note(f"恢复运行配置或重启失败：{recovery_error}")
        raise
    print(json.dumps({"mart_dir": profile["mart_dir"], "version_id": profile["version_id"],
                      "counts": profile["counts"]}, ensure_ascii=False))


def rollback_data(commit: str) -> None:
    """按本提交最近一次资料切换记录回退；再次回退可切回刚才的资料。"""
    records = list((BASE / "deployments" / commit).glob("data-switch-*.json"))
    if not records:
        raise ValueError("这个提交上没有换过资料，没有可回退的")
    latest = max(records, key=lambda path: int(path.stem.removeprefix("data-switch-")))
    previous = json.loads(latest.read_text(encoding="utf-8"))["from"]
    switch_data(commit, previous["mart_dir"], previous["executor"], how="rollback")


def verify(commit: str) -> None:
    """核对活动提交、前端字节、服务与备份任务，保存验收证据。"""
    release = BASE / "releases" / f"git-{commit}"
    assert (BASE / "current").resolve() == release
    manifest = json.loads((release / "frontend-manifest.json").read_text())
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((release / name).read_bytes()).hexdigest() == digest
    for unit in ("agentflow", "agentflow-backup.timer", "nginx", *load_site()["watch_units"]):
        run("systemctl", "is-active", "--quiet", unit)
    result = health()
    save(BASE / "deployments" / commit / "verification.json", {"commit": commit, "health": result,
                                                              "frontend_files": len(manifest["files"])})
    print(json.dumps(result, ensure_ascii=False))


def rollback(commit: str) -> None:
    """回到本次发布登记的上一版；保留最新会话数据。"""
    assert (BASE / "current").resolve() == BASE / "releases" / f"git-{commit}"
    record = json.loads((BASE / "deployments" / commit / "previous.json").read_text())
    previous = record["release"]
    if not previous:
        raise RuntimeError("这是首次发布，没有上一版；停用须人工审阅，数据会保留")
    previous_commit = Path(previous).name.removeprefix("git-")
    activate(previous_commit, profile_override=record.get("profile"))
    verify(previous_commit)


def main() -> None:
    """接受完整提交及发布阶段，仅允许 root 在固定目录执行。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("stage", choices=("prepare", "activate", "verify", "rollback", "switch-data", "rollback-data"))
    parser.add_argument("--frontend", type=Path)
    parser.add_argument("--mart-dir", help="准备阶段选用的资料目录；默认沿用服务器现有配置")
    parser.add_argument("--executor", choices=("none", "duckdb"))
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("必须以 root 执行并提供完整的 40 位 Git 提交")
    os.umask(0o022)
    if args.stage == "prepare":
        if args.frontend is None:
            parser.error("prepare 必须提供 --frontend")
        prepare(args.commit, args.frontend, args.mart_dir, args.executor)
    elif args.stage == "switch-data":
        if args.mart_dir is None:
            parser.error("switch-data 必须提供 --mart-dir")
        switch_data(args.commit, args.mart_dir, args.executor)
    elif args.stage == "rollback-data":
        rollback_data(args.commit)
    else:
        {"activate": activate, "verify": verify, "rollback": rollback}[args.stage](args.commit)


if __name__ == "__main__":
    main()
