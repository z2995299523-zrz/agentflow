"""Text-to-SQL Agent 端到端测试"""
import sys
import os
import sqlite3

# 确保从项目根目录运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def assert_test(label: str, condition: bool, detail: str = "") -> bool:
    """输出测试结果，返回是否通过"""
    status = "✅ PASS" if condition else "❌ FAIL"
    msg = f"  {status}: {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return condition


def main() -> None:
    passed = 0
    total = 0

    # ========================================
    # 前置：确保示例数据库存在
    # ========================================
    print("=" * 60)
    print("Text-to-SQL Agent 测试")
    print("=" * 60)

    if not os.path.exists("data/sample.db"):
        print("⚠ 未找到 data/sample.db，正在创建...")
        from data import init_sample_db
        init_sample_db.init_db()

    # ========================================
    # 初始化 Agent
    # ========================================
    from sql.agent import SQLQueryAgent, SafeSQLDatabase, FORBIDDEN_KEYWORDS

    print("\n📌 初始化 SQLQueryAgent...")
    agent = SQLQueryAgent()
    print(f"  数据库: {agent.db_uri}")
    schema = agent.get_schema()
    print(f"  Schema 已加载 ({len(schema)} 字符)")
    total += 1
    if assert_test("Agent 初始化成功", len(schema) > 0, f"{len(schema)} chars"):
        passed += 1

    # ========================================
    # 测试1: 基本查询 — 产品类别
    # ========================================
    print("\n📌 测试1: 基本查询 — 有哪些产品类别？")
    total += 1
    result = agent.query("有哪些产品类别？")
    print(f"  SQL: {result['sql']}")
    print(f"  回答: {result['answer']}")
    ok = result["success"] and len(result["answer"]) > 0
    if assert_test("基本查询返回结果", ok):
        passed += 1

    # ========================================
    # 测试2: 聚合查询 — 每个类别的产品数量
    # ========================================
    print("\n📌 测试2: 聚合查询 — 每个类别的产品数量是多少？")
    total += 1
    result = agent.query("每个类别的产品数量是多少？")
    print(f"  SQL: {result['sql']}")
    print(f"  回答: {result['answer']}")
    ok = result["success"] and len(result["answer"]) > 0
    if assert_test("聚合查询返回结果", ok):
        passed += 1

    # ========================================
    # 测试3: 排序查询 — 销售额最高的5个产品
    # ========================================
    print("\n📌 测试3: 排序查询 — 销售额最高的5个产品是哪些？")
    total += 1
    result = agent.query("销售额最高的5个产品是哪些？")
    print(f"  SQL: {result['sql']}")
    print(f"  回答: {result['answer']}")
    ok = result["success"] and len(result["answer"]) > 0
    if assert_test("排序查询返回结果", ok):
        passed += 1

    # ========================================
    # 测试4: 时间查询 — 2024年1月的销售额
    # ========================================
    print("\n📌 测试4: 时间查询 — 2024年1月的销售额是多少？")
    total += 1
    result = agent.query("2024年1月的销售额是多少？")
    print(f"  SQL: {result['sql']}")
    print(f"  回答: {result['answer']}")
    ok = result["success"] and len(result["answer"]) > 0
    if assert_test("时间查询返回结果", ok):
        passed += 1

    # ========================================
    # 测试5: 多表关联 — 华东地区客户买了哪些产品
    # ========================================
    print("\n📌 测试5: 多表关联 — 华东地区的客户买了哪些产品？")
    total += 1
    result = agent.query("华东地区的客户买了哪些产品？")
    print(f"  SQL: {result['sql']}")
    print(f"  回答: {result['answer']}")
    ok = result["success"] and len(result["answer"]) > 0
    if assert_test("多表关联查询返回结果", ok):
        passed += 1

    # ========================================
    # 测试6: 安全检查 — 拒绝 DROP TABLE
    # ========================================
    print("\n📌 测试6: 安全检查 — 尝试执行 DROP TABLE（应被拒绝）")
    total += 1
    try:
        # 直接调用 SafeSQLDatabase 的安全校验，模拟危险 SQL
        SafeSQLDatabase._check_safe("DROP TABLE products")
        assert_test("安全检查拦截危险操作", False, "未抛出异常")
    except ValueError as e:
        assert_test("安全检查拦截危险操作", True, str(e)[:80])
        passed += 1

    # 验证真实 query 也不会执行危险操作
    total += 1
    # 尝试让 Agent 生成危险 SQL（Agent 的 prefix 已禁止，这里再确认）
    result = agent.query("删除所有产品数据")
    print(f"  SQL: {result['sql']}")
    print(f"  回答: {result['answer']}")
    # 如果生成的 SQL 不包含危险操作就 OK
    sql_upper = result["sql"].upper()
    dangerous = any(kw in sql_upper.split() for kw in FORBIDDEN_KEYWORDS)
    if assert_test("Agent 拒绝生成写操作 SQL", not dangerous and result["success"]):
        passed += 1

    # ========================================
    # 测试7: 空结果处理
    # ========================================
    print("\n📌 测试7: 空结果处理 — 查询不存在的数据")
    total += 1
    result = agent.query("2050年1月的销售额是多少？")
    print(f"  SQL: {result['sql']}")
    print(f"  回答: {result['answer']}")
    ok = result["success"] or "2050" in result.get("error", "")
    if assert_test("空结果正确处理", ok):
        passed += 1

    # ========================================
    # 总结
    # ========================================
    print("\n" + "=" * 60)
    print(f"🎯 测试结果: {passed}/{total} 通过")
    if passed == total:
        print("🎉 全部测试通过！Text-to-SQL Agent 正常工作")
    else:
        print(f"⚠ {total - passed} 个测试失败，请检查上方输出")
    print("=" * 60)


if __name__ == "__main__":
    main()
