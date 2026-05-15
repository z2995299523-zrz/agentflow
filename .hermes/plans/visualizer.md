# visualizer.py 开发计划

> 日期: 2026-05-15
> 对应升级计划: Week 3 Day 13-14
> 执行者: Claude Code (通过 Hermes 委托)

---

## 任务: 实现 sql/visualizer.py

### 核心类: QueryVisualizer

```python
class QueryVisualizer:
    """
    查询结果自动可视化
    
    根据 DataFrame 的列类型自动选择最佳图表类型，
    生成 Base64 编码的 PNG 图片，可直接嵌入 Web 响应。
    """
    
    def analyze(self, df: pd.DataFrame) -> str:
        """分析 DataFrame，返回推荐的图表类型 (bar/line/pie)"""
    
    def visualize(self, df: pd.DataFrame, chart_type: str = "auto",
                  title: str = "") -> dict:
        """生成图表，返回 Base64 图片数据
    
        Returns:
            {"chart_type": str, "image_base64": str, "title": str,
             "success": bool, "error": str|None}
        """
```

### 图表类型自动判断规则

```python
# 输入: DataFrame
# 输出: "bar" | "line" | "pie" | "bar" (兜底)

def _analyze(df):
    columns = df.columns.tolist()
    dtypes = df.dtypes

    # 规则1: 2列，第1列字符串+第2列数值 → bar (柱状图)
    # 规则2: 2列，第1列含日期/时间 → line (折线图)
    # 规则3: 2列，第2列是百分比或占比 → pie (饼图)
    # 规则4: 3+列 → bar (默认，取前两列)
    # 默认: bar
```

**排除规则:** 不做数值轴的列：
- 列名含 `id`, `_id`, `序号`, `编号`
- dtype 为 int64 但列名暗示是 ID

### 技术约束

1. **matplotlib 配置:**
```python
import matplotlib
matplotlib.use('Agg')  # 无 GUI 后端，必须！（Windows 上尤其重要）
import matplotlib.pyplot as plt

# 中文字体（Windows）
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题
```

2. **图表样式:**
```python
# 使用现代风格
plt.style.use('seaborn-v0_8-darkgrid')  # 或 'ggplot'

# 图表尺寸
fig, ax = plt.subplots(figsize=(10, 6))

# 颜色方案（深色，适合面试展示）
colors = ['#2E86AB', '#A23B72', '#F18F01', '#C73E1D', '#6A994E']
```

3. **Base64 编码:**
```python
import base64
from io import BytesIO

buffer = BytesIO()
plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight')
buffer.seek(0)
image_base64 = base64.b64encode(buffer.read()).decode()
plt.close()  # 释放内存！
```

4. **每种图表类型的实现:**

bar (柱状图):
```python
ax.bar(x_data, y_data, color=colors[:len(x_data)])
ax.set_xlabel(x_label)
ax.set_ylabel(y_label)
# 在柱子上方标注数值
for i, v in enumerate(y_data):
    ax.text(i, v + max(y_data)*0.02, f'{v:,.0f}', ha='center')
```

line (折线图):
```python
ax.plot(x_data, y_data, marker='o', linewidth=2, markersize=8, color=colors[0])
ax.fill_between(range(len(y_data)), y_data, alpha=0.1, color=colors[0])
ax.set_xlabel(x_label)
ax.set_ylabel(y_label)
```

pie (饼图):
```python
wedges, texts, autotexts = ax.pie(
    y_data, labels=x_data, autopct='%1.1f%%',
    colors=colors[:len(x_data)], startangle=90
)
```

5. **代码规范:**
- 所有公开方法有中文 docstring
- 使用 type hints
- 4 空格缩进
- UTF-8 编码
- `matplotlib.use('Agg')` 必须在 import pyplot 之前

### 文件路径
- `sql/visualizer.py` — 主文件
- 更新 `sql/__init__.py` — 导出 QueryVisualizer

---

## 扩展任务: 更新 test_sql.py

在 test_sql.py 末尾添加可视化测试：

```python
# 测试8: 图表可视化
print("\n📌 测试8: 图表可视化 — 销售额 TOP5 柱状图")
# 先用 agent 查询得到数据
result = agent.query("销售额最高的5个产品是哪些？")
# TODO: 从 result 中提取数据，转成 DataFrame，调用 visualizer
# 或者直接创建一个测试 DataFrame
```

## 验收标准

- [ ] QueryVisualizer 能正确分析 DataFrame → 推荐图表类型
- [ ] 柱状图生成成功，Base64 编码非空
- [ ] 折线图生成成功
- [ ] 饼图生成成功
- [ ] 中文字符正常显示（不出现方框）
- [ ] 图表尺寸合理、颜色协调
- [ ] __init__.py 导出正确
- [ ] 无内存泄漏（每次 plt.close()）
