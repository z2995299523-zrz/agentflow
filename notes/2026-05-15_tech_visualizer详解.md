# QueryVisualizer 图表可视化详解

> 日期：2026-05-15 | 类别：tech | 源文件：`sql/visualizer.py`

---

## 一、整体架构

```
DataFrame 结果
    │
    ▼
analyze() — 分析数据类型
    ├── 占比数据(0~1之间小数) → 饼图 (pie)    [优先级最高]
    ├── 日期列 + 数值列 → 折线图 (line)
    ├── 分类列 + 数值列 → 柱状图 (bar)        [默认兜底]
    └── ID列自动排除（id/_id/序号/编号）
    │
    ▼
matplotlib 画图 → BytesIO (内存缓冲区) → base64 编码
    │
    ▼
返回 {"chart_type", "image_base64", "title", "success", "error"}
```

### 四个设计决策

| 决策 | 理由 | 面试关键词 |
|------|------|-----------|
| Agg 后端 | 服务器无 GUI，Agg 纯内存渲染 | "无头渲染、服务端图表" |
| Base64 而非文件 | 无状态、可嵌入 JSON | "微服务无状态、RESTful 响应" |
| 自动选图 | 降低使用门槛 | "智能图表推荐、类比 BI" |
| 日期在 bar 之前 | 日期列是字符串类型会被误判为分类列 | "数据类型推断、优先级排序" |

### 选型：matplotlib vs 其他

| 库 | 优点 | 缺点 |
|------|------|------|
| matplotlib | 稳定、中文支持好、零额外依赖 | 样式不如新库现代 |
| plotly | 交互式、漂亮 | 依赖重、Base64 编码复杂 |
| pyecharts | 中文生态好 | 需额外学习 ECharts 配置 |

---

## 二、matplotlib 三件套（必配！）

```python
import matplotlib
matplotlib.use('Agg')          # ← 必须在 import pyplot 之前！
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False
plt.style.use('seaborn-v0_8-darkgrid')
```

### 1. Agg 后端 — 解决无 GUI 崩溃

matplotlib 默认用 `TkAgg` 后端——弹 GUI 窗口显示图表。服务器没显示器，直接崩溃。

`Agg`（Anti-Grain Geometry）是**纯内存渲染器**——图表画在内存里，不依赖任何图形界面。这就是为什么能输出到 `BytesIO`（内存缓冲区）而不是弹窗。

**ETL 类比：** 好比 DataStage Server Job（需要 GUI 编辑）vs Parallel Job（命令行纯引擎运行）。生产环境不允许依赖 GUI。

**面试话术：** "matplotlib 在服务端渲染必须用 Agg 后端。Agg 是纯内存矢量渲染引擎，不依赖图形界面，画出的图直接写入内存缓冲区然后 Base64 编码返回。如果忘了这一步，部署到 Linux 服务器会直接报 `TclError: no display name`。"

### 2. 中文字体 — 解决方框

matplotlib 默认字体 DejaVu Sans 只有拉丁字符，中文字形缺失。图表里的"销售额"变方框 `□□□`。

`SimHei`（黑体）是 Windows 自带，99% 的机器都有。`Microsoft YaHei` 备选。最后的 `DejaVu Sans` 兜底——至少英文数字正常。

### 3. 负号修复 — 最隐蔽的坑

默认 matplotlib 用 Unicode 减号 `−` (U+2212)，但中文字体通常没有这个字符，负号变方框。设为 `False` 后改用 ASCII 连字符 `-`，所有字体都支持。

**面试话术：** "matplotlib 在服务端有三个必配项。Agg 后端解决无 GUI 环境渲染，中文字体解决图表标签乱码，负号修复是中文环境特有坑——matplotlib 默认用 Unicode 减号，中文字体没有这个字符。三个配置少一个都出问题，固化为模块顶部配置。"

---

## 三、analyze() — 图表类型自动判断

```python
@staticmethod
def analyze(df: pd.DataFrame) -> str:
    usable = [c for c in df.columns if not _is_excluded(c)]
    cat_cols, num_cols = [], []
    for c in usable:
        if pd.api.types.is_numeric_dtype(df[c]):
            num_cols.append(c)
        else:
            cat_cols.append(c)
    
    # 优先级：pie > line > bar
    if is_cat_first and is_num_second:
        if _is_proportion(df[col1]):    return "pie"
        if _is_datetime_like(df[col0]): return "line"
        return "bar"
```

### 判断优先级（为什么是这个顺序）

- **pie 优先级最高**：占比数据（0~1）用柱状图难看，确认占比必须饼图
- **line 次之**：日期列是字符串类型，会被归入 `cat_cols`。不先检测就永远返回 bar
- **bar 兜底**：一个分类列+一个数值列就能画，90% 的查询结果符合

### ⚠️ 关键坑：日期检测顺序

```python
# ❌ 错误（Claude Code 初版）
if is_cat_first and is_num_second:
    if _is_proportion(df[col1]):    return "pie"
    return "bar"  # 日期列直接被当成分类列

# ✅ 修复
if is_cat_first and is_num_second:
    if _is_proportion(df[col1]):    return "pie"
    if _is_datetime_like(df[col0]): return "line"  # ← bar 之前
    return "bar"
```

日期 `"2024-01-01"` 是字符串 → 归入 `cat_cols` → `is_cat_first` 为 True → 错误返回 bar。

### 三个辅助函数

**`_is_excluded()` — ID 列排除**
```python
_EXCLUDE_KEYWORDS = ['id', '_id', '序号', '编号']
# product_id: 1,2,3,4,5 → 排除，不做分类轴
```
类比数据治理：ID 列不该进 BI 报表维度。

**`_is_datetime_like()` — 日期检测**
```python
# 方法1: datetime64 类型 → 直接返回 True
# 方法2: 字符串 → 尝试解析前5个值
pd.Timestamp(str(val))  # 能解析就是日期
```
SQLite 无原生日期类型，日期存为字符串，必须尝试解析。

**`_is_proportion()` — 占比检测**
```python
in_range = values.between(0, 1).all()       # 全在 0~1
has_fraction = values.apply(lambda x: x != int(x)).any()  # 至少一个小数
return in_range and has_fraction
```
两个条件缺一不可。`[0, 1, 0, 1]` 是整数标志位，不是占比。

### 面试话术

> "图表类型的自动判断分三级优先级。占比数据用饼图，时序数据用折线图，其他默认柱状图。有一个很容易踩的坑——日期列在数据库里是字符串类型，pandas 归类为分类列。如果先判断 bar 再判断 line，日期数据永远走不到折线图。所以我把日期检测放在 bar 之前，反映一个设计原则：特定类型优先于通用类型。"

---

## 四、三种图表绘制

### 柱状图 `_draw_bar()`

```python
ax.bar(range(len(x_data)), y_data, color=colors, edgecolor='white')
# 数值标注在柱子上方
ax.text(bar_val.get_x() + bar_val.get_width()/2,
        bar_val.get_height() + offset,
        f'{val:,.0f}' if val != int(val) else f'{val:,.1f}')
```

- `range(len(x_data))` 而非直接传 `x_data` — 确保柱子等距排列
- `edgecolor='white'` — 柱子间加白边分隔
- 整数显示 `97万`，小数显示 `97.5`。`,` 是千位分隔符

### 折线图 `_draw_line()`

```python
ax.plot(indices, y_data, marker='o', linewidth=2.5, markersize=8)
ax.fill_between(indices, y_data, alpha=0.12, color=colors[0])
```

`fill_between` 在折线下方加半透明色块（透明度 12%）— 视觉上让折线"立"起来，面试展示效果更好。

### 饼图 `_draw_pie()`

```python
ax.pie(y_data, labels=x_data,
       autopct='%1.1f%%',           # 45.0%
       startangle=90,               # 从12点钟方向开始
       pctdistance=0.6,
       wedgeprops={'edgecolor': 'white', 'linewidth': 1})
```

`startangle=90` — 最大扇区从正上方开始，饼图视觉惯例。

### 共同设计

```python
plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight')
plt.close(fig)  # 必须！防止内存泄漏
```

- `dpi=100` — 清晰度与大小平衡点
- `bbox_inches='tight'` — 裁掉空白边距
- `plt.close(fig)` — 100次查询不释放 = 100个 Figure 对象泄漏

---

## 五、visualize() 主流程 — Base64 编码

```
DataFrame → matplotlib Figure → BytesIO (内存缓冲区)
                                    ↓
                              savefig (PNG 格式)
                                    ↓
                              base64.b64encode()
                                    ↓
                              .decode() → 字符串
                                    ↓
                     "iVBORw0KGgoAAAANS..."
                     → 嵌入 JSON 响应
                     → 前端 <img src="data:image/png;base64,...">
```

### 为什么 PNG 不是 SVG？

PNG 是位图，所有浏览器原生支持。SVG 矢量图在 `<img>` 标签中 Base64 显示不稳定。面试展示优先兼容性。

### 异常处理

```python
except Exception as e:
    return {"success": False, "image_base64": "",
            "error": f"{e}\n{traceback.format_exc()}"}
```

和 agent.py 一致的结构化错误返回。`traceback.format_exc()` 带完整堆栈，调试有用。

### 面试话术

> "visualize 方法遵循和前端解耦的设计。不管底层用什么可视化库，返回的永远是一个包含 chart_type 和 image_base64 的字典。前端不需要 Python 环境，拿到 Base64 直接放 `<img>` 标签。这种设计让后端可以随时换库 — 比如以后换 plotly 做交互式图表 — 前端一行代码不用改。"

---

## 相关笔记

- [[2026-05-15_tech_text-to-sql-agent详解|Text-to-SQL Agent 详解]]
- [[README|笔记索引]]
- 源文件：`sql/visualizer.py`
