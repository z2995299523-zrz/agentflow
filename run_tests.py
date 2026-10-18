"""
AgentFlow 统一测试入口
运行测试套件并生成汇总报告
用法: python run_tests.py [--quick]
  单元测试一律不调模型、不加载 BGE，--quick 只为兼容旧用法，效果相同。
  要用模型跑评估，用 python run_eval.py
"""
import sys
import os
import time
import subprocess
from pathlib import Path

os.chdir(Path(__file__).parent)

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
            # pytest 的汇总行：失败和出错（例如夹具出错）都算没通过，总数 = 通过 + 失败 + 出错
            fail_match = re.search(r"(\d+) failed", result.stdout)
            error_match = re.search(r"(\d+) errors?\b", result.stdout)
            failed = sum(int(m.group(1)) for m in (fail_match, error_match) if m)
            total += failed
            if not match and not fail_match and not error_match:
                total = "?"
                failed = "?"

        return {
            "name": name,
            # 以退出码为准：输出里数得出通过数，不代表整个套件通过了
            "passed": result.returncode == 0 and total != "?" and failed == 0,
            "tests": f"{total - failed if isinstance(total, int) and isinstance(failed, int) else '?'}/{total}",
            "time": elapsed,
            "error": None if result.returncode == 0 else f"退出码 {result.returncode}",
        }
    except subprocess.TimeoutExpired:
        return {"name": name, "passed": False, "tests": "TIMEOUT", "time": timeout, "error": "超时"}
    except Exception as e:
        return {"name": name, "passed": False, "tests": "ERROR", "time": time.time() - start, "error": str(e)}


def main():
    # Windows 控制台是 GBK 时打印不了 ✅ 这类字符，换成问号，不让汇总报错
    sys.stdout.reconfigure(errors="replace")
    quick = "--quick" in sys.argv

    suites = [
        ("NL2SQL 单元测试", ["python", "-m", "pytest", "tests", "-q", "--tb=short"], 600),
    ]

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
