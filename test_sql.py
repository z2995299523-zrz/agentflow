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
    # 测试8: 图表可视化
    # ========================================
    print("\n📌 测试8: 图表可视化 — QueryVisualizer")

    import pandas as pd
    from sql.visualizer import QueryVisualizer

    visualizer = QueryVisualizer()

    # 8a: analyze() — 柱状图识别
    total += 1
    df_bar = pd.DataFrame({
        "产品": ["iPhone", "MacBook", "iPad", "AirPods", "充电宝"],
        "销售额": [69990, 149990, 49990, 18990, 1490],
    })
    chart = visualizer.analyze(df_bar)
    ok = chart == "bar"
    if assert_test("analyze 识别柱状图", ok, f"结果={chart}"):
        passed += 1

    # 8b: analyze() — 饼图识别（占比数据）
    total += 1
    df_pie = pd.DataFrame({
        "类别": ["电子产品", "食品", "日用品"],
        "占比": [0.45, 0.30, 0.25],
    })
    chart = visualizer.analyze(df_pie)
    ok = chart == "pie"
    if assert_test("analyze 识别饼图", ok, f"结果={chart}"):
        passed += 1

    # 8c: analyze() — 折线图识别（日期列）
    total += 1
    df_line = pd.DataFrame({
        "日期": ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"],
        "销售额": [15000, 23000, 18000, 29000],
    })
    chart = visualizer.analyze(df_line)
    ok = chart == "line"
    if assert_test("analyze 识别折线图", ok, f"结果={chart}"):
        passed += 1

    # 8d: visualize() — 柱状图生成
    total += 1
    result = visualizer.visualize(df_bar, chart_type="bar", title="产品销售额")
    ok = (result["success"] and len(result["image_base64"]) > 0
          and result["chart_type"] == "bar")
    if assert_test("柱状图 Base64 生成", ok,
                   f"base64_len={len(result['image_base64'])}"):
        passed += 1

    # 8e: visualize() — 折线图生成
    total += 1
    result = visualizer.visualize(df_line, chart_type="line", title="月度销售趋势")
    ok = (result["success"] and len(result["image_base64"]) > 0
          and result["chart_type"] == "line")
    if assert_test("折线图 Base64 生成", ok,
                   f"base64_len={len(result['image_base64'])}"):
        passed += 1

    # 8f: visualize() — 饼图生成
    total += 1
    result = visualizer.visualize(df_pie, chart_type="pie", title="品类占比")
    ok = (result["success"] and len(result["image_base64"]) > 0
          and result["chart_type"] == "pie")
    if assert_test("饼图 Base64 生成", ok,
                   f"base64_len={len(result['image_base64'])}"):
        passed += 1

    # 8g: visualize() — auto 模式
    total += 1
    result = visualizer.visualize(df_bar, chart_type="auto")
    ok = (result["success"] and len(result["image_base64"]) > 0
          and result["chart_type"] == "bar")
    if assert_test("auto 模式自动选择图表类型", ok,
                   f"type={result['chart_type']}"):
        passed += 1

    # 8h: visualize() — 带 ID 列的 DataFrame（ID 应被排除）
    total += 1
    df_with_id = pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "名称": ["iPhone", "MacBook", "iPad", "AirPods", "充电宝"],
        "产品编号": ["P001", "P002", "P003", "P004", "P005"],
        "销售额": [69990, 149990, 49990, 18990, 1490],
    })
    result = visualizer.visualize(df_with_id, chart_type="auto",
                                  title="排除ID列测试")
    ok = result["success"] and len(result["image_base64"]) > 0
    if assert_test("自动排除 ID/编号 列", ok,
                   f"type={result['chart_type']}"):
        passed += 1

    # 8i: 与真实查询联动的可视化测试
    total += 1
    # 用 Agent 查询数据，拿 SQL 直接执行获取 DataFrame
    agent_result = agent.query("每个类别的产品数量是多少？")
    sql = agent_result["sql"]
    print(f"  SQL: {sql}")
    if sql:
        import sqlite3
        conn = sqlite3.connect("data/sample.db")
        df_real = pd.read_sql_query(sql, conn)
        conn.close()
        result = visualizer.visualize(df_real, chart_type="auto",
                                      title="每类别产品数量")
        ok = result["success"] and len(result["image_base64"]) > 0
        if assert_test("真实查询结果可视化", ok,
                       f"rows={len(df_real)}, type={result['chart_type']}"):
            passed += 1
    else:
        assert_test("真实查询结果可视化", False, "SQL 为空，无法测试")

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
