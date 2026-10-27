"""元数据导入的测试"""
import sqlite3
from contextlib import closing

from pathlib import Path

from metadata.checks import ERROR, Issue, ReportSection
from metadata.importer import ImportReport, build_mart, default_db_path, load_mart, main, run_import
from metadata.loader import load_raw
from metadata.store import MetadataStore
from tests.helpers import write_mart


def dump(db_path) -> list[str]:
    """导出元数据库的全部内容，用来比较两次导入的结果"""
    with closing(sqlite3.connect(db_path)) as conn:
        return list(conn.iterdump())


def test_build_mart_converts_rows_to_models(tiny_mart):
    mart = build_mart(load_raw(tiny_mart))

    assert [t.table_id for t in mart.tables] == ["ods.acct", "ads.cust_bal", "ods.cust"]
    assert mart.tables[0].grain_columns == ["data_dt", "acct_no"]
    assert mart.tables[0].date_column == "data_dt"
    assert mart.tables[2].date_column is None
    acct_columns = [c for c in mart.columns if c.table_id == "ods.acct"]
    assert [(c.column_name, c.ordinal, c.is_pk) for c in acct_columns] == [
        ("data_dt", 1, True), ("acct_no", 2, True), ("cust_no", 3, False), ("bal", 4, False),
    ]
    assert acct_columns[3].description == "原币"
    assert all(not edge.source_external for edge in mart.lineage)
    assert mart.lineage[0].script_name == "load_cust_bal.sql"
    relation = mart.relations[0]
    assert (relation.left_columns, relation.right_columns) == (["cust_no"], ["cust_no"])
    assert (relation.cardinality, relation.origin) == ("N:1", "manual")


def test_external_source_is_flagged_and_still_imported(tmp_path, tiny_sheets):
    tiny_sheets["血缘清单"].append(["ods.cust", "cust_nm", "src.cust_info", "cust_nm", "", ""])

    mart, issues = load_mart(write_mart(tmp_path / "m", tiny_sheets))

    assert [issue.rule for issue in issues] == ["W01"]      # 只有警告，可以入库
    assert [edge.source_external for edge in mart.lineage] == [False, False, False, True]


def test_standard_column_is_optional_and_no_marks_a_non_standard_table(tmp_path, tiny_sheets, tiny_mart):
    assert [t.standard for t in build_mart(load_raw(tiny_mart)).tables] == [True, True, True]   # 没填就是标准表
    tiny_sheets["表清单"][2].append("否")
    tiny_sheets["表清单"][1].append("是")

    mart, issues = load_mart(write_mart(tmp_path / "m", tiny_sheets))

    assert issues == []
    assert [(t.table_id, t.standard) for t in mart.tables] == [
        ("ods.acct", True), ("ads.cust_bal", True), ("ods.cust", False)]


def test_load_mart_returns_none_on_error(tmp_path, tiny_sheets):
    tiny_sheets["表清单"][0][0] = "明细层"

    mart, issues = load_mart(write_mart(tmp_path / "m", tiny_sheets))

    assert mart is None
    assert [issue.rule for issue in issues] == ["M05"]


def test_successful_import(tiny_mart):
    report = run_import(tiny_mart)

    assert report.succeeded and report.issues == []
    assert report.db_path == default_db_path(tiny_mart) == tiny_mart / "build" / "metadata.db"
    # 关联关系、向量等补全结果的数量随推导规则变化，这里只钉住清单本身的数量
    assert {name: report.counts[name] for name in ("表", "字段", "血缘")} == {"表": 3, "字段": 9, "血缘": 3}
    assert MetadataStore(report.db_path).counts() == report.counts
    text = (tiny_mart / "build" / "import_report.md").read_text(encoding="utf-8")
    assert "结果：成功" in text and "| 表 | 3 |" in text


def test_custom_database_path(tiny_mart, tmp_path):
    report = run_import(tiny_mart, db_path=tmp_path / "elsewhere" / "meta.db")

    assert report.db_path == tmp_path / "elsewhere" / "meta.db"
    assert MetadataStore(report.db_path).counts()["表"] == 3


def test_custom_report_path_and_version_id(tiny_mart, tmp_path, tiny_sheets):
    report_path = tmp_path / "candidate" / "report.md"
    db_path = tmp_path / "candidate" / "meta.db"
    report = run_import(tiny_mart, db_path, report_path=report_path, version_id="test-version")
    assert report.version_id == MetadataStore(db_path).version_id == "test-version"
    assert "- 版本：test-version" in report_path.read_text(encoding="utf-8")
    assert not (tiny_mart / "build" / "import_report.md").exists()
    tiny_sheets["表清单"][0][0] = "明细层"
    write_mart(tiny_mart, tiny_sheets)
    report = run_import(tiny_mart, db_path, report_path=report_path, version_id="failed-version")
    assert not report.succeeded and report.version_id == "failed-version"
    assert "- 版本：failed-version" in report_path.read_text(encoding="utf-8")
    assert MetadataStore(db_path).version_id == "test-version"


def test_failed_import_keeps_previous_database(tmp_path, tiny_sheets):
    mart_dir = write_mart(tmp_path / "m", tiny_sheets)
    previous = dump(run_import(mart_dir).db_path)
    tiny_sheets["表清单"][0][0] = "明细层"
    tiny_sheets["关联关系"].clear()
    write_mart(mart_dir, tiny_sheets)

    report = run_import(mart_dir)

    assert not report.succeeded
    assert [issue.rule for issue in report.errors] == ["M05"]
    assert dump(report.db_path) == previous                       # 仍是上一次成功导入的内容
    text = (mart_dir / "build" / "import_report.md").read_text(encoding="utf-8")
    assert "失败，元数据库未更新" in text
    assert "| 错误 | M05 | 表清单 | 2 |" in text


def test_import_is_idempotent(tiny_mart):
    report = run_import(tiny_mart)
    first = dump(report.db_path)

    run_import(tiny_mart)

    assert dump(report.db_path) == first


def test_main_exit_codes(tmp_path, tiny_sheets, capsys):
    good = write_mart(tmp_path / "good", tiny_sheets)
    assert main([str(good)]) == 0
    assert "结果：成功" in capsys.readouterr().out

    tiny_sheets["表清单"][0][0] = "明细层"
    bad = write_mart(tmp_path / "bad", tiny_sheets)
    assert main([str(bad)]) == 1
    assert "M05" in capsys.readouterr().out

    assert main([str(tmp_path / "nope")]) == 1
    assert "资料目录不存在" in capsys.readouterr().out


def test_mart_directory_with_chinese_and_spaces(tmp_path, tiny_sheets):
    mart_dir = write_mart(tmp_path / "专题库 资料", tiny_sheets)

    report = run_import(mart_dir)

    assert report.succeeded
    assert MetadataStore(report.db_path).table("ads.cust_bal").table_cn == "客户余额汇总"


def test_main_without_argument_uses_configured_mart_dir(tiny_mart, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config, "MART_DIR", str(tiny_mart))

    assert main([]) == 0
    assert "结果：成功" in capsys.readouterr().out
    assert (tiny_mart / "build" / "metadata.db").exists()


def test_main_reports_unreadable_file_in_one_line(tmp_path, tiny_sheets, capsys):
    mart_dir = write_mart(tmp_path / "m", tiny_sheets)
    (mart_dir / "表清单.csv").write_bytes(bytes([0xFF, 0xFE, 0xFF]))

    assert main([str(mart_dir)]) == 1
    assert "导入失败: 无法识别 表清单.csv 的编码" in capsys.readouterr().out


def test_incomplete_but_usable_mart_is_imported_with_warnings(tmp_path, tiny_sheets):
    tiny_sheets["表清单"][2][4:6] = ["全量表", ""]        # 全量表，粒度不清楚
    tiny_sheets["字段清单"][8][3] = ""                    # 有一个字段不知道类型
    mart_dir = write_mart(tmp_path / "m", tiny_sheets)

    report = run_import(mart_dir)

    assert report.succeeded
    assert sorted(issue.rule for issue in report.warnings) == ["W04", "W05"]
    store = MetadataStore(report.db_path)
    assert store.table("ods.cust").table_type == "全量表"
    assert store.table("ods.cust").grain_columns == []


def test_report_escapes_pipes_and_line_breaks_in_messages():
    report = ImportReport(mart_dir=Path("m"), db_path=Path("m/build/metadata.db"), succeeded=True,
                          issues=[Issue(ERROR, "W06", "加工脚本", None, "a || b\n第二行")])

    assert "| a \\|\\| b 第二行 |" in report.to_markdown()


def test_report_renders_sections_with_content_before_issues():
    report = ImportReport(
        mart_dir=Path("m"), db_path=Path("m/build/metadata.db"), succeeded=True,
        issues=[Issue(ERROR, "W06", "加工脚本", None, "说明")],
        sections=[ReportSection("加工脚本", ("- 7 个文件",)), ReportSection("没有内容的节", ())],
    )

    text = report.to_markdown()

    assert "## 加工脚本\n\n- 7 个文件" in text
    assert "没有内容的节" not in text
    assert text.index("## 加工脚本") < text.index("## 问题清单")


def test_main_no_index_skips_vectors(tmp_path, tiny_sheets):
    with_index = write_mart(tmp_path / "with", tiny_sheets)
    without_index = write_mart(tmp_path / "without", tiny_sheets)

    assert main([str(with_index)]) == 0
    assert main([str(without_index), "--no-index"]) == 0

    assert MetadataStore(default_db_path(with_index)).embedder_name is not None
    assert MetadataStore(default_db_path(without_index)).embedder_name is None


def test_main_rejects_unknown_option(capsys):
    assert main(["--nope"]) == 2
    assert "用法" in capsys.readouterr().out


def test_main_says_so_before_loading_the_model(tiny_mart, monkeypatch, capsys):
    """提示挂在 before_embed 上：真要算向量时才说（全部复用时 build_vectors 不调它，见 test_index）"""
    import metadata.importer as importer

    original = importer.run_import

    def fake_run_import(mart_dir, db_path, **kwargs):
        from metadata.index import HashEmbedder

        before_embed = kwargs["before_embed"]
        if before_embed is not None:
            before_embed(5)                    # 像真的导入一样：调向量器之前先报要算几条
        return original(mart_dir, db_path, **{**kwargs, "embedder": HashEmbedder(), "before_embed": None})

    monkeypatch.setenv("METADATA_EMBEDDER", "bge")
    monkeypatch.setattr(importer, "run_import", fake_run_import)

    assert main([str(tiny_mart)]) == 0
    assert "有 5 条文本要算向量。第一次加载向量模型可能较慢" in capsys.readouterr().out

    assert main([str(tiny_mart), "--no-index"]) == 0
    assert "向量模型" not in capsys.readouterr().out

    monkeypatch.setenv("METADATA_EMBEDDER", "hash")             # 哈希向量器不用加载模型，也不提示
    assert main([str(tiny_mart)]) == 0
    assert "向量模型" not in capsys.readouterr().out


def test_cli_diff_summary_pending_and_replay_flag(tiny_mart, tiny_sheets, capsys):
    from metadata import versions

    assert main([str(tiny_mart), "--no-index", "--replay"]) == 0
    assert "差异：" not in capsys.readouterr().out
    tiny_sheets["表清单"][0][-1] = "更新说明"
    write_mart(tiny_mart, tiny_sheets)
    assert main([str(tiny_mart), "--no-index", "--replay"]) == 0
    output = capsys.readouterr().out
    assert "差异：没有需确认项（报告：" in output
    assert output.index("# 元数据导入报告") < output.index("差异：") < output.index("已切换为活动版本")
    tiny_sheets["字段清单"] = [r for r in tiny_sheets["字段清单"] if r[:2] != ["ods.cust", "cust_nm"]]
    write_mart(tiny_mart, tiny_sheets)
    assert main([str(tiny_mart), "--no-index"]) == 0
    output = capsys.readouterr().out
    assert "删了 0 张表、1 个字段" in output and "差异报告：" in output
    assert "项需要确认，没有切换；看过差异报告后运行 python -m metadata.versions promote" in output
    assert "--accept" in output
    candidate = versions.list_versions(tiny_mart / "build")[0]
    assert candidate.label == "候选"
