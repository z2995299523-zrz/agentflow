"""LangGraph 工作流评估 — 路由准确率 + 端到端成功率 + LLM-as-Judge"""

import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_questions(path: str = None):
    """加载测试问题集"""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "test_questions.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def evaluate_routing(questions=None):
    """评估 Supervisor 路由准确率

    Returns:
        {"total": N, "correct": N, "accuracy": 0.0, "details": [...]}
    """
    if questions is None:
        questions = load_questions()

    from graph.supervisor import supervisor_node
    from graph.state import AgentState

    correct = 0
    details = []

    for q in questions:
        state: AgentState = {
            "messages": [],
            "question": q["question"],
            "intent": "",
            "rag_result": "",
            "rag_sources": [],
            "sql_result": "",
            "sql_query": "",
            "sql_chart": "",
            "final_answer": "",
        }
        result = supervisor_node(state)
        actual = result["intent"]
        expected = q["expected_intent"]
        is_correct = actual == expected
        if is_correct:
            correct += 1
        details.append({
            "question": q["question"],
            "expected": expected,
            "actual": actual,
            "correct": is_correct,
        })

    return {
        "total": len(questions),
        "correct": correct,
        "accuracy": correct / len(questions) if questions else 0,
        "details": details,
    }


def evaluate_e2e(questions=None, skip_slow=True):
    """评估端到端成功率

    Args:
        skip_slow: 是否跳过 RAG/mixed 类问题（需要 BGE 模型，太慢）

    Returns:
        {"total": N, "success": N, "rate": 0.0, "details": [...]}
    """
    if questions is None:
        questions = load_questions()

    from graph.workflow import build_workflow

    # 跳过需要 BGE 模型的问题
    if skip_slow:
        questions = [q for q in questions if q["expected_intent"] not in ("rag", "mixed")]
        if not questions:
            return {
                "total": 0, "success": 0, "rate": 0, "details": [],
                "note": "所有问题被跳过（BGE 模型未预热）",
            }

    wf = build_workflow(enable_memory=False)

    success = 0
    details = []

    for q in questions:
        try:
            result = wf.invoke({"question": q["question"]})
            answer_len = len(result.get("final_answer", ""))
            is_success = answer_len >= q.get("min_length", 1)
            if is_success:
                success += 1
            details.append({
                "question": q["question"],
                "answer_length": answer_len,
                "success": is_success,
                "answer_preview": result.get("final_answer", "")[:100],
            })
        except Exception as e:
            details.append({
                "question": q["question"],
                "error": str(e),
                "success": False,
            })

    return {
        "total": len(questions),
        "success": success,
        "rate": success / len(questions) if questions else 0,
        "details": details,
    }


def llm_judge(questions=None):
    """LLM-as-Judge：用 DeepSeek 评估回答质量（1-5分）

    Returns:
        {"avg_score": 0.0, "details": [...]}
    """
    if questions is None:
        questions = load_questions()

    from graph.workflow import build_workflow
    from config import get_llm_kwargs
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import SystemMessage, HumanMessage

    wf = build_workflow(enable_memory=False)
    llm = ChatOpenAI(**get_llm_kwargs())

    JUDGE_PROMPT = (
        "你是评估专家。给以下 AI 回答打分（1-5分）：\n"
        "5=完美回答，准确完整\n"
        "4=良好，有小瑕疵\n"
        "3=基本可用\n"
        "2=有错误或遗漏\n"
        "1=完全错误或无关\n"
        "只返回数字分数，不要解释。"
    )

    scores = []
    details = []

    for q in questions:
        if q["expected_intent"] in ("rag", "mixed"):
            continue  # 跳过 RAG（BGE 太慢）
        try:
            result = wf.invoke({"question": q["question"]})
            answer = result.get("final_answer", "")
            response = llm.invoke([
                SystemMessage(content=JUDGE_PROMPT),
                HumanMessage(content=f"问题：{q['question']}\n回答：{answer}"),
            ])
            score = int(response.content.strip()[0]) if response.content.strip() else 3
            scores.append(score)
            details.append({
                "question": q["question"], "score": score, "answer": answer[:100],
            })
        except Exception as e:
            details.append({"question": q["question"], "error": str(e)})

    return {
        "avg_score": sum(scores) / len(scores) if scores else 0,
        "details": details,
    }


if __name__ == "__main__":
    print("=" * 60)
    print("LangGraph 工作流评估")
    print("=" * 60)

    print("\n📊 1. 路由准确率评估")
    routing = evaluate_routing()
    print(f"   正确: {routing['correct']}/{routing['total']} = {routing['accuracy']:.0%}")
    for d in routing["details"]:
        mark = "✅" if d["correct"] else "❌"
        print(f"   {mark} {d['question'][:40]}... (预期:{d['expected']}, 实际:{d['actual']})")

    print("\n📊 2. 端到端成功率评估")
    e2e = evaluate_e2e(skip_slow=True)
    if e2e.get("note"):
        print(f"   ⚠️ {e2e['note']}")
    else:
        print(f"   成功: {e2e['success']}/{e2e['total']} = {e2e['rate']:.0%}")

    print("\n📊 3. LLM-as-Judge 评分")
    judge = llm_judge()
    if judge["avg_score"] > 0:
        print(f"   平均分: {judge['avg_score']:.1f}/5.0")

    print("\n" + "=" * 60)
    print("评估完成")
