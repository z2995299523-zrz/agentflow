"""从干净的 Git 提交构建前端，并生成带逐文件摘要的发布包。"""
import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path


def package_frontend(root: Path, destination: Path) -> str:
    """确认前端源码没有未提交修改，构建并绑定完整 Git 提交。"""
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "web"], cwd=root, check=True)
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "web"],
                                        cwd=root, text=True)
    if untracked.strip():
        raise RuntimeError("web 下存在未入库源码，不能标记为指定提交的构建产物")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise RuntimeError("未找到 npm")
    subprocess.run([npm, "run", "build"], cwd=root / "web", check=True)
    files = sorted(path for path in (root / "web/dist").rglob("*") if path.is_file())
    if not (root / "web/dist/index.html").is_file():
        raise RuntimeError("缺少前端首页")
    manifest = {"commit": commit, "files": {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as bundle:
        for path in files:
            bundle.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        content = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        entry = tarfile.TarInfo("frontend-manifest.json")
        entry.size = len(content)
        bundle.addfile(entry, io.BytesIO(content))
    print(json.dumps({"commit": commit, "file": str(destination),
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "files": len(files)}))
    return commit


def main() -> None:
    """生成本地构建包；不包含环境配置、真实资料和会话数据库。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime-deploy/frontend.tar.gz"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    package_frontend(root, args.output.resolve())


if __name__ == "__main__":
    main()
