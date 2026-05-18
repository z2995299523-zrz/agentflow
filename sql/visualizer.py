"""
查询结果自动可视化

根据 DataFrame 的列类型自动选择最佳图表类型，
生成 Base64 编码的 PNG 图片，可直接嵌入 Web 响应。
"""
import base64
from io import BytesIO
from typing import Any

import pandas as pd

# matplotlib 必须用 Agg 后端，必须在 import pyplot 之前设置
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ⚠️ style.use() 会覆盖 font.sans-serif，所以字体设置必须在 style 之后
plt.style.use('seaborn-v0_8-darkgrid')

# Windows 中文字体 + 负号显示
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# 深色配色方案
COLORS = ['#2E86AB', '#A23B72', '#F18F01', '#C73E1D', '#6A994E',
          '#3B8EA5', '#D81159', '#8F3985', '#218380', '#FBB13C']

# 被排除不做数值轴的列名关键词
_EXCLUDE_KEYWORDS = ['id', '_id', '序号', '编号']


def _is_excluded(col_name: str) -> bool:
    """判断列名是否应被排除（如 ID 列）"""
    col_lower = col_name.lower()
    return any(kw in col_lower for kw in _EXCLUDE_KEYWORDS)


def _is_datetime_like(series: pd.Series) -> bool:
    """判断列是否包含日期/时间信息"""
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if pd.api.types.is_string_dtype(series) or pd.api.types.is_object_dtype(series):
        # 尝试解析前几个非空值
        sample = series.dropna().head(5)
        if len(sample) == 0:
            return False
        for val in sample:
            try:
                pd.Timestamp(str(val))
                return True
            except (ValueError, TypeError):
                continue
    return False


def _is_proportion(series: pd.Series) -> bool:
    """判断数值列是否像占比数据（值在 0~1 之间，且至少有一个非整数）"""
    if not pd.api.types.is_numeric_dtype(series):
        return False
    values = series.dropna()
    if len(values) == 0:
        return False
    in_range = values.between(0, 1).all()
    has_fraction = values.apply(lambda x: x != int(x)).any()
    return bool(in_range and has_fraction)


class QueryVisualizer:
    """查询结果自动可视化

    根据 DataFrame 的列类型自动选择最佳图表类型（柱状图/折线图/饼图），
    生成 Base64 编码的 PNG 图片，可直接嵌入 Web 响应。
    """

    @staticmethod
    def analyze(df: pd.DataFrame) -> str:
        """分析 DataFrame，返回推荐的图表类型

        Args:
            df: 查询结果 DataFrame

        Returns:
            "bar" | "line" | "pie"
        """
        usable = [c for c in df.columns if not _is_excluded(c)]
        if len(usable) == 0:
            return "bar"

        # 找出分类列和数值列
        cat_cols = []
        num_cols = []
        for c in usable:
            if pd.api.types.is_numeric_dtype(df[c]):
                num_cols.append(c)
            else:
                cat_cols.append(c)

        # 规则1: 2列，第1列分类/字符串 + 第2列数值
        if len(usable) == 2:
            col0, col1 = usable[0], usable[1]
            is_cat_first = col0 in cat_cols or not pd.api.types.is_numeric_dtype(df[col0])
            is_num_second = col1 in num_cols

            if is_cat_first and is_num_second:
                # 规则3: 第2列是占比 → pie
                if _is_proportion(df[col1]):
                    return "pie"
                # 规则2: 第1列含日期/时间 → line（必须在 bar 之前判断）
                if _is_datetime_like(df[col0]):
                    return "line"
                return "bar"

            return "bar"

        # 规则4: 3+列 → 取前两列，柱状图
        return "bar"

    @staticmethod
    def visualize(
        df: pd.DataFrame,
        chart_type: str = "auto",
        title: str = "",
    ) -> dict[str, Any]:
        """生成图表，返回 Base64 编码的 PNG 图片

        Args:
            df: 查询结果 DataFrame
            chart_type: 图表类型 "bar"/"line"/"pie"/"auto"，默认 "auto"
            title: 图表标题，为空则自动生成

        Returns:
            {"chart_type": str, "image_base64": str, "title": str,
             "success": bool, "error": str|None}
        """
        try:
            if chart_type == "auto":
                chart_type = QueryVisualizer.analyze(df)

            if chart_type not in ("bar", "line", "pie"):
                chart_type = "bar"

            # 准备数据：找到分类列和数值列
            usable = [c for c in df.columns if not _is_excluded(c)]
            if len(usable) == 0:
                usable = list(df.columns)

            cat_col = None
            num_col = None
            for c in usable:
                if cat_col is None and not pd.api.types.is_numeric_dtype(df[c]):
                    cat_col = c
                elif num_col is None and pd.api.types.is_numeric_dtype(df[c]):
                    num_col = c
            if cat_col is None:
                cat_col = usable[0]
            if num_col is None:
                # 没有数值列则取第一列
                num_col = usable[-1] if len(usable) > 1 else usable[0]

            x_data = df[cat_col].astype(str).tolist()
            y_data = df[num_col].astype(float).tolist()

            if not title:
                title = f"{num_col} 按 {cat_col}"

            # 创建图表
            fig, ax = plt.subplots(figsize=(10, 6))
            n = len(x_data)
            chart_colors = COLORS * (n // len(COLORS) + 1)

            if chart_type == "bar":
                _draw_bar(ax, x_data, y_data, cat_col, num_col, title, chart_colors[:n])
            elif chart_type == "line":
                _draw_line(ax, x_data, y_data, cat_col, num_col, title, chart_colors[:n])
            elif chart_type == "pie":
                _draw_pie(ax, x_data, y_data, title, chart_colors[:n])

            # Base64 编码
            buffer = BytesIO()
            plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight')
            buffer.seek(0)
            image_base64 = base64.b64encode(buffer.read()).decode()
            plt.close(fig)

            return {
                "chart_type": chart_type,
                "image_base64": image_base64,
                "title": title,
                "success": True,
                "error": None,
            }
        except Exception as e:
            import traceback
            return {
                "chart_type": chart_type if chart_type != "auto" else "bar",
                "image_base64": "",
                "title": title,
                "success": False,
                "error": f"{e}\n{traceback.format_exc()}",
            }


def _draw_bar(
    ax: plt.Axes,
    x_data: list[str],
    y_data: list[float],
    x_label: str,
    y_label: str,
    title: str,
    colors: list[str],
) -> None:
    """绘制柱状图"""
    bars = ax.bar(range(len(x_data)), y_data, color=colors, edgecolor='white', linewidth=0.5)
    ax.set_xticks(range(len(x_data)))
    ax.set_xticklabels(x_data, rotation=30, ha='right', fontsize=9)
    ax.set_xlabel(x_label, fontsize=11)
    ax.set_ylabel(y_label, fontsize=11)
    ax.set_title(title, fontsize=14, fontweight='bold', pad=15)

    # 在柱子上方标注数值
    max_y = max(y_data) if y_data else 1
    for bar_val, val in zip(bars, y_data):
        offset = max_y * 0.02
        ax.text(
            bar_val.get_x() + bar_val.get_width() / 2,
            bar_val.get_height() + offset,
            f'{val:,.1f}' if val != int(val) else f'{val:,.0f}',
            ha='center', va='bottom', fontsize=8, fontweight='bold',
        )


def _draw_line(
    ax: plt.Axes,
    x_data: list[str],
    y_data: list[float],
    x_label: str,
    y_label: str,
    title: str,
    colors: list[str],
) -> None:
    """绘制折线图"""
    indices = range(len(y_data))
    ax.plot(indices, y_data, marker='o', linewidth=2.5, markersize=8,
            color=colors[0], markerfacecolor=colors[0], markeredgecolor='white')
    ax.fill_between(indices, y_data, alpha=0.12, color=colors[0])
    ax.set_xticks(indices)
    ax.set_xticklabels(x_data, rotation=30, ha='right', fontsize=9)
    ax.set_xlabel(x_label, fontsize=11)
    ax.set_ylabel(y_label, fontsize=11)
    ax.set_title(title, fontsize=14, fontweight='bold', pad=15)

    for i, v in enumerate(y_data):
        ax.text(i, v + max(y_data) * 0.03 if y_data else 0,
                f'{v:,.1f}' if v != int(v) else f'{v:,.0f}',
                ha='center', fontsize=8, fontweight='bold')


def _draw_pie(
    ax: plt.Axes,
    x_data: list[str],
    y_data: list[float],
    title: str,
    colors: list[str],
) -> None:
    """绘制饼图"""
    wedges, texts, autotexts = ax.pie(
        y_data,
        labels=x_data,
        autopct='%1.1f%%',
        colors=colors,
        startangle=90,
        pctdistance=0.6,
        labeldistance=1.1,
        wedgeprops={'edgecolor': 'white', 'linewidth': 1},
    )
    for t in autotexts:
        t.set_fontsize(9)
        t.set_fontweight('bold')
    for t in texts:
        t.set_fontsize(10)
    ax.set_title(title, fontsize=14, fontweight='bold', pad=15)
