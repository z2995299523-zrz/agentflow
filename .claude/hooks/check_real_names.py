"""提交、推送前的保密检查：入库内容里不能出现真实专题库的名称。

真实资料在 data/real_mart/（不入库）。本脚本只从那里读名称，自己不含任何真实名称，
所以可以入库。git worktree 里没有这个目录，就读主目录的；都没有的机器上什么也不查，直接放行。

名称有三种来源，都在 data/real_mart/ 下：
- 表清单.csv 的模式名、表英文名，血缘清单.csv 的目标表、源表：按完整标识符比对，不区分大小写，
  短于 4 个字符的不查（太容易误报）
- 保密词.txt：每行一个，# 开头是注释，按子串比对，放单位名称、表名前缀这类关键词
- 保密词_例外.txt：确认是误报的名称，每行一个，从上面两种里去掉

用法：
    作为 Claude Code 的 PreToolUse 钩子（配置在 .claude/settings.json），从标准输入读工具调用：
      命令里有 git commit：查工作区相对 HEAD 的改动、未跟踪的文件、命令本身（含提交说明）
      命令里有 git push：查本地分支上还没推到远端的提交（改动和提交说明）
      命中就以退出码 2 拦下这次调用，命中的位置写到标准错误
    python .claude/hooks/check_real_names.py --all     查全部入库文件，命中时退出码 1
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# 一条 git 命令里出现 commit 或 push 子命令（允许前面有 -C 目录、-c 配置这类选项）
GIT_ACTION = re.compile(r"\bgit\b[^;&|\n]*?\b(commit|push)\b")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
MIN_LEN = 4
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_SHOW = 20

Item = tuple[str, str]  # （位置, 要检查的文本）


def find_mart() -> Path:
    """名单所在的 data/real_mart/。git worktree 里没有这个目录（不入库），就用主目录里的。"""
    mart = ROOT / "data" / "real_mart"
    if mart.is_dir():
        return mart
    try:  # 主目录是各 worktree 共用的 .git 目录的上一级
        common = Path(git("rev-parse", "--git-common-dir").strip())
    except RuntimeError:
        return mart
    return (ROOT / common).parent / "data" / "real_mart"


def load_patterns() -> tuple[set[str], list[str]]:
    """读本机的真实名称，返回（按标识符比对的名称集合, 按子串比对的保密词）。"""
    mart = find_mart()
    names: set[str] = set()
    sources = [("表清单.csv", ("模式名", "表英文名")), ("血缘清单.csv", ("目标表", "源表"))]
    for file_name, columns in sources:
        path = mart / file_name
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                for col in columns:
                    for part in re.split(r"[.\s]+", (row.get(col) or "").strip()):
                        if len(part) >= MIN_LEN:
                            names.add(part.lower())
    words = [w.lower() for w in _read_word_list(mart / "保密词.txt")]
    allowed = {w.lower() for w in _read_word_list(mart / "保密词_例外.txt")}
    return names - allowed, [w for w in words if w not in allowed]


def _read_word_list(path: Path) -> list[str]:
    """读一个词表文件：每行一个，跳过空行和 # 注释；文件不存在时返回空列表。"""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [s.strip() for s in lines if s.strip() and not s.strip().startswith("#")]


def git(*args: str) -> str:
    """在仓库根目录跑一条 git 命令，返回标准输出（路径里的中文不转义）。"""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout


def diff_items(diff_text: str, prefix: str = "") -> list[Item]:
    """从 -U0 的差异里取出新增的行和新增、改名后的文件路径；删掉的行和文件不查。"""
    items: list[Item] = []
    path, line_no, in_header = "", 0, False

    def add_name(name: str) -> None:
        items.append((f"{prefix}{name}（文件名）", name))

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            path, in_header = "", True
        elif in_header and line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
            if path:
                add_name(path)
        elif in_header and line.startswith("rename to "):
            add_name(line[len("rename to "):])
        elif in_header and line.startswith("Binary files ") and " and b/" in line:
            add_name(line.rsplit(" and b/", 1)[1].removesuffix(" differ"))
        elif line.startswith("@@"):
            in_header = False
            m = HUNK.match(line)
            line_no = int(m.group(1)) if m else 0
        elif not in_header and path and line.startswith("+"):
            items.append((f"{prefix}{path}:{line_no}", line[1:]))
            line_no += 1
    return items


def file_items(rel_path: str) -> list[Item]:
    """一个文件的路径和每一行；太大的文件只查路径。"""
    items: list[Item] = [(f"{rel_path}（文件名）", rel_path)]
    path = ROOT / rel_path
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return items
    text = path.read_bytes().decode("utf-8", errors="ignore")
    items.extend((f"{rel_path}:{i}", s) for i, s in enumerate(text.splitlines(), 1))
    return items


def commit_items(command: str) -> list[Item]:
    """git commit 可能带进去的内容：相对 HEAD 的改动、未跟踪的文件、命令本身。"""
    items: list[Item] = [("命令（含提交说明）", command)]
    items += diff_items(git("diff", "HEAD", "-U0", "--no-color", "--no-ext-diff"))
    for rel in git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if rel:
            items += file_items(rel)
    return items


def push_items() -> list[Item]:
    """本地分支上还没推到任何远端的提交：每个提交的说明和新增的行。"""
    log = git("log", "-p", "-U0", "--no-color", "--no-ext-diff",
              "--format=%x1e%h%n%B%x1f", "--branches", "--not", "--remotes")
    items: list[Item] = []
    for chunk in log.split("\x1e")[1:]:
        sha, _, rest = chunk.partition("\n")
        message, _, diff_text = rest.partition("\x1f")
        items.append((f"提交 {sha} 的说明", message))
        items += diff_items(diff_text, prefix=f"提交 {sha} ")
    return items


def all_tracked_items() -> list[Item]:
    """全部入库文件的路径和内容。"""
    items: list[Item] = []
    for rel in git("ls-files", "-z").split("\0"):
        if rel:
            items += file_items(rel)
    return items


def find_hits(items: list[Item], names: set[str], words: list[str]) -> list[tuple[str, str]]:
    """逐项比对，返回（位置, 命中的名称）列表。"""
    hits: dict[tuple[str, str], None] = {}  # 用字典去重并保持顺序
    for where, text in items:
        low = text.lower()
        found = {w for w in words if w in low}
        found |= {tok for tok in IDENT.findall(low) if tok in names}
        hits.update(((where, name), None) for name in sorted(found))
    return list(hits)


def format_hits(hits: list[tuple[str, str]]) -> str:
    """把命中列表排成几行，太多时只列前面一部分。"""
    lines = [f"  {where}  →  {name}" for where, name in hits[:MAX_SHOW]]
    if len(hits) > MAX_SHOW:
        lines.append(f"  ……共 {len(hits)} 处，只列前 {MAX_SHOW} 处")
    return "\n".join(lines)


def run_hook() -> int:
    """钩子模式：只在命令里有 git commit / git push 时检查，命中返回 2。"""
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    command = str((payload.get("tool_input") or {}).get("command") or "")
    actions = {m.group(1) for m in GIT_ACTION.finditer(command)}
    if not actions:
        return 0
    names, words = load_patterns()
    if not names and not words:
        return 0  # 这台机器上没有真实资料，无从检查
    messages: list[str] = []
    if "commit" in actions:
        hits = find_hits(commit_items(command), names, words)
        if hits:
            messages.append(
                "提交被拦下：下面的内容里有真实专题库的名称（名单来自本机 data/real_mart/）：\n"
                f"{format_hits(hits)}\n"
                "改成虚构的名称、或把文件挪进 data/real_mart/ 后再提交。确认是误报，"
                "就把这个词写进 data/real_mart/保密词_例外.txt。"
            )
    if "push" in actions:
        hits = find_hits(push_items(), names, words)
        if hits:
            messages.append(
                "推送被拦下：还没推送的提交里有真实专题库的名称：\n"
                f"{format_hits(hits)}\n"
                "这些名称已经在本地提交历史里，要改写历史才能去掉。先停下来告诉用户，不要自己改写。"
            )
    if messages:
        print("\n\n".join(messages), file=sys.stderr)
        return 2
    return 0


def run_audit() -> int:
    """自查模式：查全部入库文件，打印结果，命中返回 1。"""
    names, words = load_patterns()
    if not names and not words:
        print("没有 data/real_mart/ 下的清单和保密词，无从检查。")
        return 0
    items = all_tracked_items()
    hits = find_hits(items, names, words)
    files = sum(1 for where, _ in items if where.endswith("（文件名）"))
    print(f"查了 {files} 个入库文件；名称 {len(names)} 个，保密词 {len(words)} 个；命中 {len(hits)} 处")
    if hits:
        print(format_hits(hits))
        return 1
    return 0


def main() -> int:
    """入口：带 --all 是自查，否则当作钩子从标准输入读工具调用。"""
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="提交、推送前检查真实专题库名称有没有进入入库内容")
    parser.add_argument("--all", action="store_true", help="查全部入库文件")
    args = parser.parse_args()
    try:
        return run_audit() if args.all else run_hook()
    except Exception as exc:  # 检查不了就别放行，免得漏查
        print(f"保密检查脚本出错，这次先拦下：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
