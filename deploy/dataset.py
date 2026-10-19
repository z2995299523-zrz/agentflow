"""打包和安装仓库外的专题库资料快照；不复制本地会话、业务数据或密钥。"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path, PurePosixPath

if __package__:
    from .runtime_profile import metadata_contract
else:
    from runtime_profile import metadata_contract

STANDARD_CSV_NAMES = ("表清单.csv", "字段清单.csv", "血缘清单.csv", "关联关系.csv")


def digest(path: Path) -> str:
    """分块计算文件摘要，避免把元数据库整体额外读入内存。"""
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def package_dataset(mart_dir: Path, destination: Path) -> dict:
    """复制清单、SQL 脚本和在线备份的元数据库，生成可核验的私有资料包。"""
    mart_dir = mart_dir.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("资料包目标已存在，不能覆盖历史快照")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_db = mart_dir / "build/metadata.db"
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary)
        (staging / "build").mkdir()
        with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(staging / "build/metadata.db")) as target:
                source.backup(target)
        inputs = [mart_dir / name for name in STANDARD_CSV_NAMES if (mart_dir / name).is_file()]
        for path in inputs + sorted((mart_dir / "scripts").glob("*.sql")):
            relative = path.relative_to(mart_dir)
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)
        files = sorted(path for path in staging.rglob("*") if path.is_file())
        manifest = {"format": 1, **metadata_contract(staging / "build/metadata.db"),
                    "files": {path.relative_to(staging).as_posix(): digest(path) for path in files}}
        content = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        with tarfile.open(destination, "w:gz") as bundle:
            for path in files:
                bundle.add(path, arcname=path.relative_to(staging).as_posix(), recursive=False)
            entry = tarfile.TarInfo("dataset-manifest.json")
            entry.size = len(content)
            bundle.addfile(entry, io.BytesIO(content))
    destination.chmod(0o600)
    return {"archive": str(destination), "sha256": digest(destination), "files": len(manifest["files"]),
            "counts": manifest["counts"], "vector_model": manifest["vector_model"]}


def unpack_dataset(archive: Path, staging: Path) -> dict:
    """检查完整清单、摘要及路径后解包，拒绝任何链接、额外文件或会话数据库。"""
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("资料包含重复路径")
        for member in members:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts or "\\" in member.name):
                raise ValueError("资料包含不允许的条目")
        if "dataset-manifest.json" not in names:
            raise ValueError("资料包缺少清单")
        manifest = json.load(bundle.extractfile("dataset-manifest.json"))
        if manifest.get("format") != 1:
            raise ValueError("不支持的资料包格式")
        if set(manifest["files"]) != set(names) - {"dataset-manifest.json"}:
            raise ValueError("资料包条目与清单不一致")
        for name, expected in manifest["files"].items():
            path = PurePosixPath(name)
            allowed = (name == "build/metadata.db" or name in STANDARD_CSV_NAMES
                       or (len(path.parts) == 2 and path.parts[0] == "scripts" and path.suffix == ".sql"))
            if not allowed:
                raise ValueError("资料包只能包含元数据库、CSV 清单和 SQL 脚本")
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(name) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if digest(target) != expected:
                raise ValueError("资料包文件摘要不一致")
        contract = metadata_contract(staging / "build/metadata.db")
        if contract["counts"] != manifest["counts"] or contract["vector_model"] != manifest["vector_model"]:
            raise ValueError("元数据库与清单契约不一致")
        (staging / "dataset-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def install_dataset(archive: Path, dataset_id: str, history: Path) -> dict:
    """以独立版本安装私有资料，不覆盖现有资料；会话库在版本目录外稳定保存。"""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise PermissionError("安装资料必须由服务器 root 执行")
    if not re.fullmatch(r"(?:real|private)-[a-z0-9-]+", dataset_id):
        raise ValueError("资料版本名只能使用 real/private 前缀及英文数字连字符")
    root = Path("/var/lib/agentflow/datasets")
    history = history.resolve()
    if history.parent != root.parent or not re.fullmatch(r"history-[a-z0-9-]+\.db", history.name):
        raise ValueError("稳定会话库必须位于应用数据目录下，不能指向其它服务")
    destination = root / dataset_id
    if destination.exists():
        raise FileExistsError("该资料版本已经存在，不能覆盖")
    root.mkdir(mode=0o750, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
    manifest = unpack_dataset(archive, staging)
    import grp
    group = grp.getgrnam("agentflow").gr_gid
    os.chown(root, 0, group)
    root.chmod(0o750)
    for path in [staging, *staging.rglob("*")]:
        os.chown(path, 0, group)
        path.chmod(0o750 if path.is_dir() else 0o440)
    (staging / "build/history.db").symlink_to(history)
    staging.rename(destination)
    return {"dataset_id": dataset_id, "mart_dir": str(destination), "history_path": str(history),
            "archive_sha256": digest(archive), "counts": manifest["counts"], "vector_model": manifest["vector_model"]}


def main() -> None:
    """提供本地打包和服务器安装命令，终端只输出数量和摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("package")
    pack.add_argument("--mart-dir", required=True, type=Path)
    pack.add_argument("--output", required=True, type=Path)
    install = commands.add_parser("install")
    install.add_argument("--archive", required=True, type=Path)
    install.add_argument("--id", required=True)
    install.add_argument("--history", type=Path, default=Path("/var/lib/agentflow/history-real.db"))
    args = parser.parse_args()
    result = (package_dataset(args.mart_dir, args.output) if args.command == "package"
              else install_dataset(args.archive, args.id, args.history))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
