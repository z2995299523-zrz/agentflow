"""
元数据版本
命令行导入的每一版写进 资料目录/build/versions/<版本号>/（元数据库、版本清单、导入报告、输入副本），写完不再改。
切换就是把选中版本的库复制成 build/metadata.db，服务、打包、发布脚本照旧只读这一个文件；
current.json 记活动版本、上一个活动版本和切换历史，回退就是切回上一个活动版本。
有需确认项的版本须用 promote <版本> --accept 切换；diff <基线> <版本> 查看纯差异。
用法: python -m metadata.versions {list,promote,diff,rollback,prune} [--mart 资料目录]
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from metadata import importer
from metadata.diff import confirm_items, diff_stores, render_diff
from metadata.examples import EXAMPLE_ENCODINGS, EXAMPLES_DIR_NAME, RawExample, parse_example_text
from metadata.index import BGE_NAME, Embedder, bge_loaded, default_embedder
from metadata.script_parser import SCRIPTS_DIR_NAME
from metadata.store import MetadataStore, read_vectors, read_version_id
from metadata.template import SHEETS, WORKBOOK_NAME

VERSIONS_DIR_NAME = "versions"
VERSION_FILE_NAME = "version.json"
CURRENT_FILE_NAME = "current.json"
INPUTS_DIR_NAME = "inputs"
LOCK_FILE_NAME = ".promote.lock"
LEGACY_PREFIX = "legacy-"                  # 版本功能之前导入的库，登记成 legacy-<文件修改时间>
CONVERT_REPORT_NAME = "转换报告.md"         # 真实资料转换器的报告，资料目录里有就算作输入
KEEP_VERSIONS = 5                          # 清理时保留最新的几个就绪版本（活动、上一个活动版本另外保留）
KEEP_FAILED = 3                            # 清理时保留最新的几个失败版本
HISTORY_LIMIT = 50                         # current.json 里的切换历史最多留几条
CODE_DIRS = ("metadata", "nl2sql")         # 代码指纹覆盖的目录：导入结果只取决于这两处的代码
UNFINISHED = "导入没有跑完（中途被打断，或者进程退出了）"


@dataclass
class VersionResult:
    """一次版本导入的结果；跳过导入时没有报告。"""

    version_id: str
    version_dir: Path
    report: importer.ImportReport | None
    promoted: bool
    previous: str | None
    skipped: bool
    active: str | None
    confirm_items: list[str] = field(default_factory=list)
    diff_report: Path | None = None


@dataclass
class VersionRow:
    """版本列表的一行，状态与活动标签分别保存。"""

    version_id: str
    version_dir: Path
    created_at: str
    status: str
    label: str
    counts: dict[str, int]


def _time(now: datetime | None) -> datetime:
    """带时区的当前时间；测试可以传固定时间"""
    return (now if now is not None else datetime.now().astimezone()).astimezone()


def _read_json(path: Path) -> dict[str, Any]:
    """读版本清单或 current.json；文件不存在时返回空字典

    Raises:
        RuntimeError: 文件读不了，或者内容不是 JSON 对象
    """
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("内容必须是 JSON 对象")
        return value
    except (OSError, ValueError) as error:
        raise RuntimeError(f"读不了版本记录 {path}：{error}") from error


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """先写 文件名.next 再原子替换，读的一方要么看到旧内容、要么看到新内容"""
    next_path = path.with_name(path.name + ".next")
    next_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(next_path, path)


def _version_dir(build_dir: Path, version_id: str) -> Path:
    """版本号对应的目录。版本号必须是单个目录名，命令行传进来的参数不能指到 versions 外面

    Raises:
        ValueError: 版本号不合法
    """
    if not version_id or version_id in (".", "..") or "/" in version_id or "\\" in version_id or ":" in version_id:
        raise ValueError(f"版本号无效: {version_id}")
    root = Path(build_dir) / VERSIONS_DIR_NAME
    path = root / version_id
    if path.resolve().parent != root.resolve():
        raise ValueError(f"版本目录不在 versions 内: {version_id}")
    return path


def _new_version(build_dir: Path, stem: str) -> Path:
    """新建版本目录：版本号是 stem，已有同名目录时依次加 -2、-3……；用 os.mkdir 建，并发时谁建成谁用"""
    root = build_dir / VERSIONS_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        path = root / (stem if suffix == 1 else f"{stem}-{suffix}")
        try:
            os.mkdir(path)
            return path
        except FileExistsError:
            suffix += 1


def input_files(mart_dir: Path) -> list[str]:
    """参与导入的输入文件，按相对路径（/ 分隔）排好序

    有 metadata.xlsx 时是它，否则是四张清单里存在的 CSV；加上 scripts、examples 下的 .sql（不分大小写，
    与脚本解析、样例解析的认法一致），以及资料目录里的转换报告。
    """
    mart_dir = Path(mart_dir)
    workbook = mart_dir / WORKBOOK_NAME
    files = [workbook] if workbook.is_file() else [mart_dir / f"{spec.name}.csv" for spec in SHEETS]
    for directory in (mart_dir / SCRIPTS_DIR_NAME, mart_dir / EXAMPLES_DIR_NAME):
        if directory.is_dir():
            files.extend(path for path in directory.iterdir() if path.suffix.lower() == ".sql")
    files.append(mart_dir / CONVERT_REPORT_NAME)
    return sorted(path.relative_to(mart_dir).as_posix() for path in files if path.is_file())


def _fingerprint(root: Path, names: list[str]) -> tuple[dict[str, str], str]:
    """每个文件的 sha256（先把 \\r\\n 换成 \\n，git 换行符转换不算改动）和按路径排序后的合计摘要"""
    files = {name: hashlib.sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
             for name in sorted(names)}
    digest = hashlib.sha256()
    for name, value in files.items():
        digest.update(name.encode("utf-8"))
        digest.update(value.encode("ascii"))
    return files, digest.hexdigest()


def inputs_fingerprint(mart_dir: Path) -> tuple[dict[str, str], str]:
    """返回与版本导入一致的输入文件指纹及合计摘要。"""
    return _fingerprint(mart_dir, input_files(mart_dir))


def active_manifest(build_dir: Path) -> dict | None:
    """读取活动版本的清单；尚未登记活动版本时返回 None。"""
    active = _read_json(build_dir / CURRENT_FILE_NAME).get("active")
    return _read_json(_version_dir(build_dir, active) / VERSION_FILE_NAME) if active else None


def _code_fingerprint() -> tuple[str, str | None]:
    """(代码摘要, Git 提交)：摘要覆盖 CODE_DIRS 下全部 .py；取不到提交（没有 Git 或 .git）时为 None"""
    root = Path(__file__).resolve().parents[1]
    names = [path.relative_to(root).as_posix() for directory in CODE_DIRS
             for path in (root / directory).rglob("*.py") if path.is_file()]
    _, digest = _fingerprint(root, names)
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, timeout=10,
                                capture_output=True, text=True, check=True)
        commit = result.stdout.strip() or None
    except Exception:  # 发布目录可能没有 Git 或 .git，不影响导入
        commit = None
    return digest, commit


def _registered(build_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """有版本清单的版本目录和清单内容，从新到旧；同一分钟的按后缀，-3 排在 -2 前面"""
    items = []
    for path in (build_dir / VERSIONS_DIR_NAME).glob("*"):
        if path.is_dir() and (path / VERSION_FILE_NAME).is_file():
            _version_dir(build_dir, path.name)
            items.append((path, _read_json(path / VERSION_FILE_NAME)))

    def order(item: tuple[Path, dict[str, Any]]) -> tuple[float, str, int]:
        path, manifest = item
        tail = path.name.rsplit("-", 1)[-1]
        suffix = int(tail) if path.name.count("-") >= (3 if path.name.startswith(LEGACY_PREFIX) else 2) else 1
        return datetime.fromisoformat(manifest["created_at"]).timestamp(), path.name.split("-")[0], suffix

    return sorted(items, key=order, reverse=True)


@contextmanager
def _promote_lock(build_dir: Path, now: datetime | None) -> Iterator[None]:
    """切换期间独占 build/.promote.lock，防止两个切换同时进行；只删自己建的锁

    Raises:
        RuntimeError: 锁文件已经存在
    """
    lock = build_dir / LOCK_FILE_NAME
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(f"另一个切换正在进行（{lock}）；确认没有切换在跑，删掉这个文件再试") from error
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(f"pid={os.getpid()}\ntime={_time(now).isoformat()}\n")
        yield
    finally:
        lock.unlink()


def _switch_record(build_dir: Path, version_id: str, how: str, now: datetime | None,
                   *, fix: bool = False) -> str | None:
    """把切换写进 current.json，返回切换前的活动版本号

    切换前的活动版本成为 previous；切回同一个版本，或者只是按活动文件修正记录（fix）时，previous 不变。
    """
    current = _read_json(build_dir / CURRENT_FILE_NAME)
    previous = current.get("active")
    switched_at = _time(now).isoformat()
    history = current.get("history", []) + [{"version": version_id, "switched_at": switched_at, "how": how}]
    _write_json(build_dir / CURRENT_FILE_NAME, {
        "active": version_id,
        "previous": current.get("previous") if fix or previous == version_id else previous,
        "switched_at": switched_at, "history": history[-HISTORY_LIMIT:],
    })
    return previous


def promote(build_dir: Path, version_id: str, *, how: str = "promote",
            now: datetime | None = None, accept: bool = False) -> str | None:
    """把已就绪版本复制为活动库；有需确认项时必须显式接受，返回原活动版本号。"""
    build_dir = Path(build_dir)
    directory = _version_dir(build_dir, version_id)
    if not directory.is_dir():
        raise ValueError(f"版本目录不存在: {directory}")
    if not (directory / VERSION_FILE_NAME).is_file():
        raise ValueError(f"版本清单不存在: {directory / VERSION_FILE_NAME}")
    manifest = _read_json(directory / VERSION_FILE_NAME)
    if manifest.get("status") != "ready":
        raise ValueError(f"版本 {version_id} 的状态不是 ready，不能切换")
    source = directory / importer.DB_FILE_NAME
    if not source.is_file():
        raise ValueError(f"版本元数据库不存在: {source}")
    with _promote_lock(build_dir, now):
        if read_version_id(source) != version_id:
            raise RuntimeError(f"版本库里的版本号与 {version_id} 不一致，不能切换")
        # 先检查记录是否可读，避免替换库后才发现历史已损坏。
        _read_json(build_dir / CURRENT_FILE_NAME)
        manifest = _read_json(directory / VERSION_FILE_NAME)
        pending = manifest.get("confirm_items", [])
        if pending and not manifest.get("accepted_at"):
            if not accept:
                raise ValueError(f"版本 {version_id} 有 {len(pending)} 项需要确认，看过 "
                                 f"{directory / 'diff_report.md'} 后加 --accept 再切换")
            manifest["accepted_at"] = _time(now).isoformat()
            _write_json(directory / VERSION_FILE_NAME, manifest)
        for name in (importer.DB_FILE_NAME, importer.REPORT_FILE_NAME):
            source = directory / name
            if not source.is_file():
                continue
            target = build_dir / name
            next_path = target.with_name(target.name + ".next")
            try:
                shutil.copyfile(source, next_path)
                os.replace(next_path, target)
            except PermissionError as error:
                raise RuntimeError(
                    f"文件正被其它程序占用，请先关闭占用它的程序再重试，旧文件不受影响: {target}") from error
            finally:
                next_path.unlink(missing_ok=True)
        if read_version_id(build_dir / importer.DB_FILE_NAME) != version_id:
            raise RuntimeError(f"切换后活动库的版本号不是 {version_id}")
        return _switch_record(build_dir, version_id, how, now)


_promote = promote          # import_version 的参数也叫 promote，函数体里经这个名字调切换


def adopt_active(build_dir: Path, now: datetime | None = None) -> None:
    """登记活动文件里的版本号；未登记的旧库归档为可回退的 legacy 版本。"""
    build_dir = Path(build_dir)
    db_path = build_dir / importer.DB_FILE_NAME
    if not db_path.exists():
        return
    try:
        version_id = read_version_id(db_path)
    except RuntimeError:
        return
    if version_id is not None:
        try:
            path = _version_dir(build_dir, version_id)
        except ValueError:
            path = None
        if path is not None and _read_json(path / VERSION_FILE_NAME).get("status") == "ready":
            with _promote_lock(build_dir, now):
                if _read_json(build_dir / CURRENT_FILE_NAME).get("active") != version_id:
                    _switch_record(build_dir, version_id, "fix", now, fix=True)
            return
    try:
        store = MetadataStore(db_path)
        vectors = read_vectors(db_path)
    except RuntimeError:
        return
    stamp = datetime.fromtimestamp(db_path.stat().st_mtime).astimezone()
    directory = _new_version(build_dir, LEGACY_PREFIX + stamp.strftime("%Y%m%d-%H%M"))
    archived = directory / importer.DB_FILE_NAME
    shutil.copyfile(db_path, archived)
    with closing(sqlite3.connect(archived)) as conn:
        conn.execute("INSERT OR REPLACE INTO meta_info VALUES ('version_id', ?)", (directory.name,))
        conn.commit()
    report = build_dir / importer.REPORT_FILE_NAME
    if report.is_file():
        shutil.copyfile(report, directory / importer.REPORT_FILE_NAME)
    _write_json(directory / VERSION_FILE_NAME, {
        "version_id": directory.name, "created_at": _time(now).isoformat(), "status": "ready",
        "baseline": _read_json(build_dir / CURRENT_FILE_NAME).get("active"),
        "inputs": {}, "inputs_digest": None, "code_digest": None, "git_commit": None,
        "embedder": store.embedder_name, "dim": vectors.dim if vectors else None,
        "counts": store.counts(), "errors": 0, "warnings": 0, "error": None,
        "confirm_items": [], "accepted_at": None, "gates": {},
    })
    promote(build_dir, directory.name, how="legacy", now=now)


def import_version(mart_dir: Path, *, build_index: bool = True, embedder: Embedder | None = None,
                   reindex: bool = False, force: bool = False, promote: bool = True,
                   before_embed: Callable[[int], None] | None = None,
                   now: datetime | None = None, replay: bool = False) -> VersionResult:
    """导入独立版本，清单通过且没有需确认项才自动切换；资料与代码没变时跳过。"""
    mart_dir = Path(mart_dir)
    if not mart_dir.is_dir():
        raise FileNotFoundError(f"资料目录不存在: {mart_dir}")
    build_dir = mart_dir / importer.BUILD_DIR_NAME
    adopt_active(build_dir, now)
    current = _read_json(build_dir / CURRENT_FILE_NAME)
    baseline = current.get("active")
    inputs, inputs_digest = _fingerprint(mart_dir, input_files(mart_dir))
    code_digest, commit = _code_fingerprint()
    selected = (embedder if embedder is not None else default_embedder()) if build_index else None
    active_dir = _version_dir(build_dir, baseline) if baseline else None
    active = _read_json(active_dir / VERSION_FILE_NAME) if active_dir else {}
    if (not force and not reindex and active.get("status") == "ready"
            and active.get("inputs_digest") == inputs_digest and active.get("code_digest") == code_digest
            and active.get("embedder") == (selected.name if selected is not None else None)):
        diff_path = active_dir / "diff_report.md"
        return VersionResult(baseline, active_dir, None, False, baseline, True, baseline,
                             active.get("confirm_items", []), diff_path if diff_path.is_file() else None)
    created = _time(now)
    directory = _new_version(build_dir, created.strftime("%Y%m%d-%H%M"))
    snapshot = directory / INPUTS_DIR_NAME
    snapshot.mkdir()
    for name in inputs:
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mart_dir / name, target)
    reuse_from = [build_dir / importer.DB_FILE_NAME] if (build_dir / importer.DB_FILE_NAME).exists() else []
    for path, manifest in _registered(build_dir):
        if path.name != baseline and manifest.get("status") == "ready":
            reuse_from.append(path / importer.DB_FILE_NAME)
            break
    manifest = {
        "version_id": directory.name, "created_at": created.isoformat(), "status": "failed",
        "baseline": baseline, "inputs": inputs, "inputs_digest": inputs_digest,
        "code_digest": code_digest, "git_commit": commit,
        "embedder": selected.name if selected is not None else None,
        "dim": selected.dim if selected is not None else None,
        "counts": {}, "errors": 0, "warnings": 0, "error": UNFINISHED,
        "confirm_items": [], "accepted_at": None, "gates": {},
    }
    # 先记成失败：中途按 Ctrl+C、加载模型时进程被杀，这一版也能在列表里看到、被清理掉
    _write_json(directory / VERSION_FILE_NAME, manifest)
    try:
        report = importer.run_import(
            mart_dir, directory / importer.DB_FILE_NAME, build_index=build_index, embedder=selected,
            reindex=reindex, before_embed=before_embed, report_path=directory / importer.REPORT_FILE_NAME,
            reuse_from=reuse_from, version_id=directory.name)
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        manifest["error"] = " ".join(str(error).splitlines())
        _write_json(directory / VERSION_FILE_NAME, manifest)
        raise
    manifest.update(status="ready" if report.succeeded else "failed", counts=report.counts,
                    errors=len(report.errors), warnings=len(report.warnings), error=None)
    manifest["gates"] = {
        "清单校验": "通过" if report.succeeded else f"未通过，{len(report.errors)} 条错误",
        "样例": (f"收下 {report.counts.get('样例', 0)} 条，"
                 f"{sum(i.rule == 'W08' for i in report.warnings)} 条警告") if report.succeeded else "没跑：清单未通过",
    }
    diff_report = None
    if report.succeeded and active_dir is not None and (active_dir / importer.DB_FILE_NAME).is_file():
        from nl2sql.validator import SqlValidator  # 只有导入后的差异分析需要查询层

        old_store = MetadataStore(active_dir / importer.DB_FILE_NAME)
        new_store = MetadataStore(directory / importer.DB_FILE_NAME)
        difference = diff_stores(old_store, new_store, candidate_examples=_raw_examples(snapshot),
                                 validate=SqlValidator(new_store).validate)
        pending = confirm_items(difference)
        manifest["gates"]["差异"] = f"{len(pending)} 项需确认" if pending else "没有需确认项"
        try:
            from eval.regression import run_regression  # list 等管理命令不加载评估层

            regression = run_regression(mart_dir, old_store, new_store,
                                        removed_tables=set(difference.tables.removed),
                                        replay=replay or new_store.embedder_name != BGE_NAME or bge_loaded())
            pending += regression.confirm_items
            manifest["gates"].update(regression.gates)
            regression_lines = regression.lines
        except Exception as error:  # 回归模块本身出错也不能让已导入的候选版本失败
            reason = " ".join(str(error).splitlines()) or type(error).__name__
            pending.append(f"零成本回归没跑完：{reason}")
            for gate in ("题库静态核对", "召回重放", "会话影响"):
                manifest["gates"][gate] = f"没跑完：{reason}"
            regression_lines = [f"- 零成本回归没跑完：{reason}"]
        manifest["confirm_items"] = pending
        diff_report = directory / "diff_report.md"
        diff_report.write_text(render_diff(difference, header=_diff_header(active, manifest), confirm=pending,
                                          extra_sections=[("影响面（零成本回归）", regression_lines)]), encoding="utf-8")
    _write_json(directory / VERSION_FILE_NAME, manifest)
    promoted = report.succeeded and promote and not manifest["confirm_items"]
    previous = baseline
    if promoted:
        previous = _promote(build_dir, directory.name, how="import", now=now)
    active_id = _read_json(build_dir / CURRENT_FILE_NAME).get("active")
    return VersionResult(directory.name, directory, report, promoted, previous, False, active_id,
                         manifest["confirm_items"], diff_report)


def _raw_examples(snapshot: Path) -> list[RawExample]:
    """从本版输入快照读取样例，编码和后缀规则与样例导入一致。"""
    result = []
    for path in sorted((snapshot / EXAMPLES_DIR_NAME).glob("*")):
        if not path.is_file() or path.suffix.lower() != ".sql":
            continue
        data = path.read_bytes()
        for encoding in EXAMPLE_ENCODINGS:
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            raws, _ = parse_example_text(text, path.name)
            result.extend(raws)
            break
    return result


def _diff_header(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """版本、输入摘要和代码摘要的比较说明，每类文件最多列二十个。"""
    lines = [f"- 版本：{new['version_id']}", f"- 基线：{new.get('baseline') or old.get('version_id', '无')}"]
    old_inputs, new_inputs = old.get("inputs", {}), new.get("inputs", {})
    if not old_inputs:
        lines.append("- 基线没有输入记录（旧库登记的版本）")
    else:
        for label, names in (
            ("新增", sorted(new_inputs.keys() - old_inputs.keys())),
            ("删除", sorted(old_inputs.keys() - new_inputs.keys())),
            ("内容变了", sorted(k for k in old_inputs.keys() & new_inputs.keys() if old_inputs[k] != new_inputs[k])),
        ):
            detail = "、".join(name.replace("|", "\\|").replace("\n", " ").replace("\r", " ") for name in names[:20]) or "没有"
            lines.append(f"- 输入文件{label}：{detail}" + (f"，等共 {len(names)} 项" if len(names) > 20 else ""))
    if old.get("code_digest") == new.get("code_digest"):
        lines.append("- 代码：没有变化")
    else:
        lines.append(f"- 代码：变了（{(old.get('git_commit') or '未知')[:7]} → "
                     f"{(new.get('git_commit') or '未知')[:7]}）")
    return lines


def rollback(build_dir: Path, now: datetime | None = None) -> str:
    """切回上一个活动版本；连续调用可以在最近两版之间往返。"""
    build_dir = Path(build_dir)
    previous = _read_json(build_dir / CURRENT_FILE_NAME).get("previous")
    if not previous:
        raise ValueError("没有上一个活动版本可以回退")
    promote(build_dir, previous, how="rollback", now=now, accept=True)
    return previous


def list_versions(build_dir: Path) -> list[VersionRow]:
    """按导入时间从新到旧列出版本，并标记活动、候选、用过或失败。"""
    build_dir = Path(build_dir)
    current = _read_json(build_dir / CURRENT_FILE_NAME)
    used = {item["version"] for item in current.get("history", [])}
    rows = []
    for path, manifest in _registered(build_dir):
        status = manifest["status"]
        if status == "failed":
            label = "失败"
        elif path.name == current.get("active"):
            label = "活动"
        elif path.name == current.get("previous"):
            label = "上一个活动"
        else:
            label = "用过" if path.name in used else "候选"
        rows.append(VersionRow(path.name, path, manifest["created_at"], status, label, manifest["counts"]))
    return rows


def prune(build_dir: Path, *, keep: int = KEEP_VERSIONS, keep_failed: int = KEEP_FAILED,
          apply: bool = False) -> list[str]:
    """预览或删除超出保留数量的版本，始终保护活动版本和上一个活动版本。"""
    if keep < 0 or keep_failed < 0:
        raise ValueError("保留版本数量不能为负数")
    rows = list_versions(build_dir)
    ready = [row.version_id for row in rows if row.status == "ready"][:keep]
    failed = [row.version_id for row in rows if row.status == "failed"][:keep_failed]
    protected = set(ready + failed)
    removed = [row.version_id for row in rows if row.version_id not in protected
               and row.label not in ("活动", "上一个活动")]
    if apply:
        for version_id in removed:
            shutil.rmtree(_version_dir(Path(build_dir), version_id))
    return removed


def main(argv: list[str] | None = None) -> int:
    """版本管理命令行；成功返回 0，业务错误返回 1，参数错误返回 2。"""
    parser = argparse.ArgumentParser(description="元数据版本目录与切换")
    parser.add_argument("--mart", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "promote", "diff", "rollback", "prune"):
        command = commands.add_parser(name)
        command.add_argument("--mart", type=Path, default=argparse.SUPPRESS)
        if name == "promote":
            command.add_argument("version_id")
            command.add_argument("--accept", action="store_true")
        elif name == "diff":
            command.add_argument("baseline")
            command.add_argument("version_id")
        elif name == "prune":
            command.add_argument("--keep", type=int, default=KEEP_VERSIONS)
            command.add_argument("--yes", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    if args.mart is None:
        import config  # 只有未指定资料目录时才读取默认配置

        args.mart = Path(config.MART_DIR)
    build_dir = args.mart / importer.BUILD_DIR_NAME
    try:
        if args.command == "list":
            rows = list_versions(build_dir)
            if not rows:
                print("还没有版本")
            else:
                print("版本 | 导入时间 | 状态 | 表 | 字段 | 字段向量")
                for row in rows:
                    print(f"{row.version_id} | {row.created_at} | {row.label} | "
                          f"{row.counts.get('表', 0)} | {row.counts.get('字段', 0)} | {row.counts.get('字段向量', 0)}")
        elif args.command == "promote":
            previous = promote(build_dir, args.version_id, accept=args.accept)
            print(f"已切换到 {args.version_id}（上一个：{previous or '无'}）")
        elif args.command == "diff":
            stores = []
            for version_id in (args.baseline, args.version_id):
                directory = _version_dir(build_dir, version_id)
                if _read_json(directory / VERSION_FILE_NAME).get("status") != "ready":
                    raise ValueError(f"版本 {version_id} 的状态不是 ready，不能比较")
                stores.append(MetadataStore(directory / importer.DB_FILE_NAME))
            difference = diff_stores(*stores)
            print(render_diff(difference, header=[f"- 版本：{args.version_id}", f"- 基线：{args.baseline}"],
                              confirm=confirm_items(difference)))
        elif args.command == "rollback":
            print(f"已回退到 {rollback(build_dir)}")
        else:
            removed = prune(build_dir, keep=args.keep, apply=args.yes)
            if args.yes:
                print(f"已删除 {len(removed)} 个版本")
            else:
                print("\n".join(removed) if removed else "没有需要清理的版本")
                print("加 --yes 才会删除")
    except (ValueError, RuntimeError, FileNotFoundError) as error:
        print(f"失败: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
