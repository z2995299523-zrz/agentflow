"""
初始化示例 SQLite 数据库
创建 products / sales / customers 三张表并插入测试数据
运行方式: python data/init_sample_db.py
"""
import sqlite3
import os
import random

DB_PATH = os.path.join(os.path.dirname(__file__), "sample.db")


def init_db():
    """创建表结构并填充示例数据"""
    # 如果数据库已存在则删除重建，保证幂等
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ========================================
    # 1. 产品表
    # ========================================
    cursor.execute("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            product_name TEXT NOT NULL,
            category TEXT NOT NULL,
            unit_price REAL NOT NULL
        )
    """)

    products = [
        (1,  "iPhone 15",         "电子产品", 6999.00),
        (2,  "MacBook Pro 14",    "电子产品", 14999.00),
        (3,  "大米 5kg",          "食品",     35.00),
        (4,  "食用油 5L",         "食品",     69.90),
        (5,  "洗衣液 3L",         "日用品",   29.90),
        (6,  "iPad Air",          "电子产品", 4999.00),
        (7,  "洗发水 500ml",      "日用品",   49.00),
        (8,  "牛奶 1L",           "食品",     8.50),
        (9,  "AirPods Pro",       "电子产品", 1899.00),
        (10, "纸巾 24卷",         "日用品",   39.90),
        (11, "全麦面包",          "食品",     12.00),
        (12, "20000mAh 充电宝",   "电子产品", 149.00),
    ]
    cursor.executemany(
        "INSERT INTO products VALUES (?, ?, ?, ?)", products
    )

    # ========================================
    # 2. 客户表
    # ========================================
    cursor.execute("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            customer_name TEXT NOT NULL,
            region TEXT NOT NULL,
            level TEXT NOT NULL
        )
    """)

    customers = [
        (1,  "张三",  "华东", "A"),
        (2,  "李四",  "华东", "B"),
        (3,  "王五",  "华南", "A"),
        (4,  "赵六",  "华北", "C"),
        (5,  "钱七",  "华中", "B"),
        (6,  "孙八",  "华南", "A"),
        (7,  "周九",  "华东", "C"),
        (8,  "吴十",  "华北", "B"),
        (9,  "郑一",  "华中", "A"),
        (10, "冯二",  "华南", "B"),
    ]
    cursor.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?)", customers
    )

    # ========================================
    # 3. 销售表
    # ========================================
    cursor.execute("""
        CREATE TABLE sales (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            sale_date TEXT NOT NULL,
            region TEXT NOT NULL
        )
    """)

    random.seed(42)
    regions = ["华东", "华南", "华北", "华中"]
    sales = []
    sale_id = 1
    # 生成 2024-01 到 2024-04 共 4 个月的销售数据
    for month in range(1, 5):
        for day in range(1, 29):  # 每月 28 天有销售
            # 每天随机 1-3 笔销售
            for _ in range(random.randint(1, 3)):
                product_id = random.randint(1, len(products))
                quantity = random.randint(1, 20)
                region = random.choice(regions)
                # 日期格式 YYYY-MM-DD
                date_str = f"2024-{month:02d}-{day:02d}"
                sales.append((sale_id, product_id, quantity, date_str, region))
                sale_id += 1
                if len(sales) >= 50:
                    break
            if len(sales) >= 50:
                break
        if len(sales) >= 50:
            break

    cursor.executemany(
        "INSERT INTO sales VALUES (?, ?, ?, ?, ?)", sales
    )

    conn.commit()
    conn.close()

    # 统计输出
    print(f"✅ 示例数据库已创建: {DB_PATH}")
    print(f"   products: {len(products)} 条")
    print(f"   customers: {len(customers)} 条")
    print(f"   sales: {len(sales)} 条（覆盖 {len(set(s[3][:7] for s in sales))} 个月）")


if __name__ == "__main__":
    init_db()
