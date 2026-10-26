"""读取服务器运行配置，核对资料契约；不输出密钥或真实模型名称。"""
import json
import os
import re
import shlex
import sqlite3
from contextlib import closing
from pathlib import Path

ENV_PATH = Path("/etc/agentflow/app.env")
COUNT_TABLES = {"表": "meta_table", "字段": "meta_column", "血缘": "meta_lineage",
                "关联关系": "meta_relation", "加工语句": "meta_script", "样例": "meta_example"}


def read_environment(path: Path = ENV_PATH) -> dict[str, str]:
    """读取 systemd 环境文件中的简单键值，保留引号内的空格和井号。"""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        for entry in shlex.split(line, comments=True, posix=True):
            key, separator, value = entry.partition("=")
            if not separator or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
                raise ValueError("运行配置必须使用 KEY=value 格式")
            values[key] = value
    return values


def metadata_contract(path: Path) -> dict:
    """以只读方式检核元数据库，返回数量与向量器，不读取业务记录。"""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("元数据库完整性检查失败")
        counts = {label: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for label, table in COUNT_TABLES.items()}
        counts["字段向量"] = connection.execute("SELECT COUNT(*) FROM meta_vector WHERE kind='field'").fetchone()[0]
        info = dict(connection.execute("SELECT key, value FROM meta_info"))
    if counts["表"] < 1 or counts["字段"] < 1:
        raise ValueError("元数据库没有可用的表和字段")
    if info.get("embedder") != "bge-small-zh-v1.5" or counts["字段向量"] != counts["字段"]:
        raise ValueError("正式发布必须具有完整 BGE 字段向量")
    return {"counts": counts, "vector_model": info["embedder"], "version_id": info.get("version_id")}


def make_profile(mart_dir: str, executor: str, release: Path) -> dict:
    """绑定资料、执行层和稳定会话路径，阻止真实资料误连模拟执行库。"""
    if executor not in ("none", "duckdb") or "\n" in mart_dir or "\r" in mart_dir:
        raise ValueError("资料目录或执行层无效")
    mock = mart_dir in ("./data/mock_mart", "data/mock_mart")
    if not mock and (not Path(mart_dir).is_absolute() or executor != "none"):
        raise ValueError("外部资料必须使用绝对目录，且 EXECUTOR=none")
    root = (release / mart_dir if mock else Path(mart_dir)).resolve()
    contract = metadata_contract(root / "build/metadata.db")
    return {"mart_dir": mart_dir, "executor": executor, "external": not mock,
            "history_path": str((root / "build/history.db").resolve()), **contract}


def write_profile(path: Path, profile: dict) -> None:
    """原子更新资料相关配置，其余密钥和设置保留原文、属主与权限。"""
    changes = {"MART_DIR": profile["mart_dir"], "EXECUTOR": profile["executor"],
               "AGENTFLOW_HISTORY_DB": profile["history_path"]}
    stat = path.stat()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if not re.match(r"\s*(?:" + "|".join(changes) + r")\s*=", line)]
    lines.extend(key + "=" + json.dumps(value, ensure_ascii=False) for key, value in changes.items())
    temporary = path.with_name(path.name + ".next")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        os.chmod(temporary, stat.st_mode & 0o777)
        output.write("\n".join(lines) + "\n")
    if hasattr(os, "chown"):
        os.chown(temporary, stat.st_uid, stat.st_gid)
    temporary.replace(path)
