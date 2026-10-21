"""
AgentFlow 评估入口：在模拟专题库上跑 A、B、C 三组对照评估（会调用 DeepSeek）
用法: python run_eval.py [参数同 python -m eval.nl2sql_eval]
  python run_eval.py                          # 三组全跑，43 题，4 个线程
  python run_eval.py --configs C --ids B01,K02  # 只跑 C 组的两道题
报告写到 eval/reports/，逐题记录在 eval/reports/runs/（不入库）。
真实库静态评估：python run_eval.py --static 题目文件 --mart 资料目录 --out 输出目录
"""
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

from eval.nl2sql_eval import main  # noqa: E402  先切到项目目录，相对路径的默认值才对

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
