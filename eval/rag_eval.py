"""RAG 检索质量评估 — 自实现指标（ETL对账风格）"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_questions(path=None):
    """加载评估问题集"""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "rag_test_questions.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_test_docs():
    """加载测试文档到 ChromaDB（使用独立持久化目录，避免污染主知识库）"""
    from rag.loader import load_single_file, split_documents
    from rag.vectorstore import create_vectorstore

    docs_dir = os.path.join(os.path.dirname(__file__), "..", "data", "test_docs")
    eval_persist_dir = os.path.join(os.path.dirname(__file__), "..", "chroma_data_eval")

    all_chunks = []
    for fname in sorted(os.listdir(docs_dir)):
        if fname.endswith(".txt"):
            fpath = os.path.join(docs_dir, fname)
            docs = load_single_file(fpath)
            chunks = split_documents(docs)
            for c in chunks:
                c.metadata["source"] = fname
            all_chunks.extend(chunks)

    print(f"✅ 加载 {len(os.listdir(docs_dir))} 个测试文档 → {len(all_chunks)} 个文本块")
    vs = create_vectorstore(all_chunks, persist_dir=eval_persist_dir)
    return vs


def _get_source_basename(source_str):
    """从 metadata.source 提取文件名（去掉路径前缀和页面后缀）"""
    # source 可能是 "公司考勤制度.txt" 或包含路径
    basename = os.path.basename(str(source_str))
    # 去掉 " 第X页" 后缀
    if " 第" in basename:
        basename = basename.split(" 第")[0]
    return basename


def recall_at_k(q_item, vectorstore, k=5):
    """Recall@K: 命中的相关文档 / 总相关文档"""
    retriever = vectorstore.as_retriever(search_kwargs={"k": k})
    docs = retriever.invoke(q_item["question"])
    sources = set(_get_source_basename(d.metadata.get("source", "")) for d in docs)
    expected = set(q_item.get("expected_sources", []))
    if not expected:
        # no_answer 类型：返回空结果=完美，有结果=0分
        return 1.0 if not docs else 0.0
    hits = len(sources & expected)
    return hits / len(expected)


def precision_at_k(q_item, vectorstore, k=5):
    """Precision@K: 命中的相关文档 / 返回的K个结果"""
    retriever = vectorstore.as_retriever(search_kwargs={"k": k})
    docs = retriever.invoke(q_item["question"])
    sources = set(_get_source_basename(d.metadata.get("source", "")) for d in docs)
    expected = set(q_item.get("expected_sources", []))
    if not sources:
        return 1.0 if not expected else 0.0
    if not expected:
        return 0.0 if sources else 1.0
    hits = len(sources & expected)
    return hits / len(sources)


def mrr(q_item, vectorstore, k=5):
    """MRR: 1/第一个相关结果的排名"""
    retriever = vectorstore.as_retriever(search_kwargs={"k": k})
    docs = retriever.invoke(q_item["question"])
    expected = set(q_item.get("expected_sources", []))
    if not expected:
        return 1.0 if not docs else 0.0
    for i, doc in enumerate(docs):
        if _get_source_basename(doc.metadata.get("source", "")) in expected:
            return 1.0 / (i + 1)
    return 0.0


def answer_relevance_score(answer, ground_truth):
    """简化版：检查答案是否包含标准答案关键词"""
    if not answer or not ground_truth:
        return 0.0
    gt_lower = ground_truth.lower()
    ans_lower = answer.lower()
    if gt_lower in ans_lower:
        return 1.0
    # 部分匹配：关键词重叠（去除标点后分词）
    import re
    gt_words = set(re.findall(r"[\w一-鿿]+", gt_lower))
    ans_words = set(re.findall(r"[\w一-鿿]+", ans_lower))
    if not gt_words:
        return 0.0
    overlap = len(gt_words & ans_words)
    return overlap / len(gt_words)


def run_evaluation(questions=None, vectorstore=None):
    """运行完整评估，输出报告"""
    if questions is None:
        questions = load_questions()
    if vectorstore is None:
        vectorstore = load_test_docs()

    from rag.chain import ask

    recall_scores = []
    precision_scores = []
    mrr_scores = []
    relevance_scores = []
    details = []

    for q in questions:
        # 检索评估
        rec = recall_at_k(q, vectorstore)
        prec = precision_at_k(q, vectorstore)
        m = mrr(q, vectorstore)

        # 生成回答评估
        result = ask(q["question"], vectorstore=vectorstore)
        answer = result.get("answer", "")
        relevance = answer_relevance_score(answer, q["ground_truth"])

        recall_scores.append(rec)
        precision_scores.append(prec)
        mrr_scores.append(m)
        relevance_scores.append(relevance)

        details.append({
            "question": q["question"],
            "type": q["type"],
            "recall@5": rec,
            "precision@5": prec,
            "mrr": m,
            "answer_relevance": relevance,
            "answer_preview": answer[:100],
        })

    return {
        "total_questions": len(questions),
        "avg_recall@5": sum(recall_scores) / len(recall_scores) if recall_scores else 0,
        "avg_precision@5": sum(precision_scores) / len(precision_scores) if precision_scores else 0,
        "avg_mrr": sum(mrr_scores) / len(mrr_scores) if mrr_scores else 0,
        "avg_relevance": sum(relevance_scores) / len(relevance_scores) if relevance_scores else 0,
        "details": details,
    }


if __name__ == "__main__":
    print("=" * 60)
    print("RAG 检索质量评估")
    print("=" * 60)

    result = run_evaluation()

    print(f"\n📊 基线指标 (共{result['total_questions']}题):")
    print(f"   Recall@5:      {result['avg_recall@5']:.0%}  (目标 ≥80%)")
    print(f"   Precision@5:   {result['avg_precision@5']:.0%}  (目标 ≥60%)")
    print(f"   MRR:           {result['avg_mrr']:.2f}  (目标 ≥0.70)")
    print(f"   Answer Relev:  {result['avg_relevance']:.0%}  (目标 ≥80%)")

    print(f"\n📋 逐题详情:")
    for d in result["details"]:
        ok = "✅" if d["recall@5"] >= 0.8 and d["mrr"] >= 0.5 else "⚠️"
        print(f"   {ok} [{d['type']:10s}] R={d['recall@5']:.0%} P={d['precision@5']:.0%} M={d['mrr']:.2f} A={d['answer_relevance']:.0%} | {d['question'][:45]}...")

    print("\n" + "=" * 60)
