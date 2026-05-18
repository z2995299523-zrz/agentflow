"""
AgentFlow 统一测试入口
运行所有测试套件并生成汇总报告
用法: python run_tests.py [--quick]
  --quick  跳过需要 LLM 的测试（仅跑单元测试）
"""
import sys
import os
import time
import subprocess
from pathlib import Path

os.chdir(Path(__file__).parent)

# 加载环境变量
env_file = Path("D:/hermes-agent-home/.env")
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v.strip())

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


def run_suite(name: str, cmd: list[str], timeout: int = 300) -> dict:
    """运行一个测试套件，返回结果"""
    print(f"\n{BOLD}━━━ {name} ━━━{RESET}")
    start = time.time()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=Path(__file__).parent)
        elapsed = time.time() - start
        passed = "PASSED" in result.stdout or "全部测试通过" in result.stdout or "passed" in result.stdout.lower()
        # Count individual tests
        import re
        match = re.search(r"(\d+)/(\d+)\s*(?:通过|passed)", result.stdout)
        if not match:
            match = re.search(r"(\d+) passed", result.stdout)

        total = int(match.group(1)) if match else 0
        failed = 0
        if match and match.lastindex and match.lastindex >= 2:
            total = int(match.group(2)) if "通过" in result.stdout else int(match.group(1))
            passed_count = int(match.group(1))
            failed = total - passed_count if total > passed_count else 0
        else:
            # Check for FAILED count
            fail_match = re.search(r"(\d+) failed", result.stdout)
            failed = int(fail_match.group(1)) if fail_match else 0
            if not match and not fail_match:
                total = "?"
                failed = "?"

        return {
            "name": name,
            "passed": total != "?" and failed == 0,
            "tests": f"{total - failed if isinstance(total, int) and isinstance(failed, int) else '?'}/{total}",
            "time": elapsed,
            "error": None,
        }
    except subprocess.TimeoutExpired:
        return {"name": name, "passed": False, "tests": "TIMEOUT", "time": timeout, "error": "超时"}
    except Exception as e:
        return {"name": name, "passed": False, "tests": "ERROR", "time": time.time() - start, "error": str(e)}


def main():
    quick = "--quick" in sys.argv

    suites = [
        ("SQL 模块 (18项)", ["python", "test_sql.py"], 300),
        ("RAG 模块 (5项)", ["python", "test_rag.py"], 300),
        ("FC 工具 (9项)", ["python", "test_sql_tools.py"], 180),
        ("LangGraph (17项)", ["python", "-m", "pytest", "test_graph.py", "-v", "--tb=short"], 300),
    ]

    if not quick:
        suites.append(("LangGraph 评估", ["python", "-m", "pytest", "eval/langgraph_eval.py", "-v", "--tb=short"], 300))

    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  AgentFlow 测试套件{' (快速模式)' if quick else ''}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")

    results = []
    total_start = time.time()

    for name, cmd, timeout in suites:
        r = run_suite(name, cmd, timeout)
        results.append(r)
        status = f"{GREEN}✅{RESET}" if r["passed"] else f"{RED}❌{RESET}"
        print(f"  {status} {name}: {r['tests']} tests ({r['time']:.1f}s)")

    total_time = time.time() - total_start
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])

    print(f"\n{BOLD}{'='*60}{RESET}")
    if failed == 0:
        print(f"{GREEN}{BOLD}  🎉 全部通过！{passed}/{len(results)} 套件 ({total_time:.0f}s){RESET}")
    else:
        print(f"{YELLOW}{BOLD}  ⚠️ {passed}/{len(results)} 套件通过，{failed} 失败 ({total_time:.0f}s){RESET}")
        for r in results:
            if not r["passed"] and r["error"]:
                print(f"    ❌ {r['name']}: {r['error']}")
    print(f"{BOLD}{'='*60}{RESET}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
