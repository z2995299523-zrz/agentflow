"""AgentFlow RAG 模块端到端测试"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

def main():
    # 1. 创建测试文档
    os.makedirs("data/test_docs", exist_ok=True)
    with open("data/test_docs/AgentFlow产品介绍.txt", "w", encoding="utf-8") as f:
        f.write("""AgentFlow 企业级AI智能助手平台

产品概述
AgentFlow 是一个开箱即用的 AI 助手平台，支持文档知识库问答（RAG）和自然语言数据分析（Text-to-SQL）。

核心功能
1. 文档知识库问答：上传PDF/Word/TXT文档，用自然语言提问，AI基于文档内容回答
2. 自然语言数据分析：不用写SQL也能做数据分析，结果自动可视化
3. 统一API接口：RAG和SQL分析通过统一的FastAPI接口调用
4. 一键部署：Docker Compose启动，改个配置就能跑

技术架构
- LLM层：兼容OpenAI/DeepSeek/通义千问
- 框架层：LangChain + LangGraph
- 存储层：ChromaDB向量数据库 + SQLite/PostgreSQL
- 后端层：FastAPI异步框架
- 前端层：Streamlit Web界面

产品优势
- 多模型兼容：一套接口，切换DeepSeek/OpenAI/Qwen只需改配置
- 中文友好：深度优化的中文文档理解和问答能力
- 数据安全：支持私有化部署，数据不出企业内网
- 开箱即用：Docker一键启动，无需复杂配置
""")
    print("✅ 1/5 测试文档已创建")

    # 2. 文档加载 + 分块
    from rag.loader import load_and_split
    chunks = load_and_split(["data/test_docs/AgentFlow产品介绍.txt"])
    print(f"✅ 2/5 文档加载: {len(chunks)} 个文本块")
    for i, c in enumerate(chunks):
        print(f"   块{i+1}: {len(c.page_content)}字 [{c.metadata.get('source')}]")

    # 3. 创建向量库
    from rag.vectorstore import create_vectorstore, get_document_count
    store = create_vectorstore(chunks)
    count = get_document_count(store)
    print(f"✅ 3/5 向量库创建: {count} 条记录")

    # 4. 相似度检索
    from rag.vectorstore import similarity_search
    results = similarity_search("技术架构是什么", store)
    print(f"✅ 4/5 相似度检索: 找到 {len(results)} 条相关内容")
    print(f"   最相关: {results[0].page_content[:80]}...")

    # 5. RAG 问答
    from rag.chain import ask
    result = ask("AgentFlow有哪些核心功能？", vectorstore=store)
    print(f"✅ 5/5 RAG 问答完成!")
    print(f"   回答: {result['answer'][:200]}...")
    print(f"   来源: {result['sources']}")

    print("\n" + "="*60)
    print("🎉 全部测试通过！RAG 模块正常工作")
    print("="*60)

if __name__ == "__main__":
    main()
