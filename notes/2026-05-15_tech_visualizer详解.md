# QueryVisualizer 代码详解

> 日期：2026-05-15 | 类别：tech | 源文件：`sql/visualizer.py`

---

## 一、整体架构

```
DataFrame 结果
    │
    ▼
analyze() — 分析数据类型
    ├── 1个分类列 + 1个数值列 → 柱状图 (bar)
    ├── 日期列 + 数值列 → 折线图 (line)
    ├── 1个分类 + 1个占比(0~1) → 饼图 (pie)
    └── 都不匹配 → 柱状图（兜底）
    │
    ▼
matplotlib 画图 → BytesIO → base64 编码
    │
    ▼
返回 {"chart_type", "image_base64", "title", "success", "error"}
```

---

## 二、解决什么问题？

**没有 visualizer 之前：**
```
用户问：销售额最高的5个产品
Agent：MacBook Pro 14 (97万), iPad Air (14万), iPhone 15 (12万)...

用户想看图 → 打开 Excel → 复制数据 → 插入图表 → 截图
```

**有了 visualizer 之后：**
```
用户问：销售额最高的5个产品
Agent：[柱状图] + 自然语言解释
一气呵成，3秒出图
```

---

## 三、Base64 方案选型

| 方案 | 优点 | 缺点 |
|------|------|------|
| 保存文件 | 简单直观 | 需要清理、并发冲突、路径管理 |
| Base64 | 无状态、可嵌入 JSON、RESTful | 数据量比文件大约 33% |

选择 Base64 是因为项目要部署成 Web 服务——FastAPI 返回 JSON，Streamlit 直接用 `<img>` 渲染。没有文件系统的中间状态。

---

## 四、图表类型自动判断

```
优先级从高到低：
1. 占比检测（0~1之间的小数）→ pie
2. 日期检测（字符串可解析为日期）→ line
3. 分类+数值 → bar（默认兜底）
```

### 关键坑：日期检测必须在 bar 之前

```python
# ❌ 错误顺序
if is_cat_first and is_num_second:
    return "bar"  # 日期列被当成分类列，直接返回 bar

# ✅ 正确顺序
if is_cat_first and is_num_second:
    if _is_proportion(df[col1]):   # 先判占比
        return "pie"
    if _is_datetime_like(df[col0]): # 再判日期
        return "line"
    return "bar"                    # 最后兜底
```

### ID 列排除

```python
_EXCLUDE_KEYWORDS = ['id', '_id', '序号', '编号']
# product_id, customer_id, 编号 → 不做数值轴或分类轴
```

避免把 `product_id: 1,2,3,4,5` 当成分类标签画柱状图。

---

## 五、matplotlib 三个关键配置

```python
# 1. Agg 后端（无 GUI，服务器必备）
import matplotlib
matplotlib.use('Agg')  # 必须在 import pyplot 之前！

# 2. 中文字体（Windows 上 SimHei 最可靠）
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']

# 3. 负号显示
plt.rcParams['axes.unicode_minus'] = False  # 否则负号显示为方块
```

**面试话术：** "matplotlib 在服务端必须用 Agg 后端，因为服务器没有图形界面。Agg 是一个纯内存渲染器，把图表直接画到内存缓冲区，然后编码成 Base64 返回。三个配置缺一不可：Agg 后端、中文字体、负号修复。"

---

## 六、三种图表实现

### 柱状图 (bar)
- `ax.bar()` + 数值标注在柱子上方
- `rotation=30` 防止长标签重叠

### 折线图 (line)
- `ax.plot()` + 半透明填充区域 (`fill_between`)
- 数据点标注 + 连线

### 饼图 (pie)
- `ax.pie()` + `autopct='%1.1f%%'`
- 白边分隔 (`wedgeprops`)

### 共同点
- 每次 `plt.close(fig)` 防止内存泄漏
- `bbox_inches='tight'` 防止标签被裁切
- 深色配色方案 (`COLORS`)

---

## 七、设计决策总结

| 决策 | 理由 | 面试关键词 |
|------|------|-----------|
| Base64 而非文件 | 无状态、RESTful | "微服务架构、无状态设计" |
| 自动选图而非手动指定 | 降低使用门槛 | "智能图表推荐、类比 BI 工具" |
| matplotlib 而非 plotly | 依赖轻、稳定 | "最小可行方案、不引入额外复杂度" |
| 排除 ID 列 | 避免无意义图表 | "数据清洗意识、防御性编程" |

---

## 相关笔记

- [[2026-05-15_tech_text-to-sql-agent详解|Text-to-SQL Agent 详解]]
- [[README|笔记索引]]
- 源文件：`sql/visualizer.py`
