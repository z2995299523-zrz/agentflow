"""向量索引的测试：哈希向量器、建向量、全量检索、向量经元数据库往返、BGE 的加载策略

BGE 的加载策略用假的 sentence_transformers 顶替真库来测，整个文件不加载任何模型。
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import types
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from metadata import index as index_module
from metadata.importer import default_db_path, load_mart, main, run_import
from metadata.index import (
    EMBEDDER_ENV,
    BgeEmbedder,
    HashEmbedder,
    VectorIndex,
    build_vectors,
    default_embedder,
    example_text,
    field_ref_id,
    field_text,
)
from metadata.models import Example, Mart
from metadata.store import MetadataStore, VectorBundle, VectorSet, read_vectors, write_store

ROOT = Path(__file__).resolve().parents[1]
MOCK_MART_DIR = ROOT / "data" / "mock_mart"
SAMPLE_TEXTS = ["贷款余额", "不良贷款余额 npl_bal", "Org_No 机构号 ＡＢＣ１２３"]


def dump(db_path) -> list[str]:
    """导出元数据库的全部内容，用来比较两次导入的结果"""
    with closing(sqlite3.connect(db_path)) as conn:
        return list(conn.iterdump())


def make_example(number: int, requirement: str, caliber_note: str = "") -> Example:
    """造一条样例，只有需求和口径对向量有意义"""
    return Example(example_id=f"01_demo.sql#{number}", requirement=requirement, caliber_note=caliber_note,
                   sql_text="SELECT 1", tables=[], source_file="01_demo.sql", line_no=number)


def make_set(rows: list[list[float]], ref_ids: list[str]) -> VectorSet:
    """手工造一组向量，检索结果可以心算"""
    return VectorSet(kind="field", ref_ids=ref_ids, texts=list(ref_ids),
                     matrix=np.array(rows, dtype=np.float32))


class RecordingEmbedder:
    """向量由哈希向量器给出，同时记录每次调用的文本条数和全部文本"""

    def __init__(self) -> None:
        self._inner = HashEmbedder()
        self.name = self._inner.name
        self.dim = self._inner.dim
        self.batches: list[int] = []
        self.texts: list[str] = []

    def embed(self, texts):
        self.batches.append(len(texts))
        self.texts.extend(texts)
        return self._inner.embed(texts)


class RefusingHashEmbedder(HashEmbedder):
    """名字、维度和哈希向量器相同，但一调用就失败：证明向量全部复用了，没有调用向量器"""

    def embed(self, texts):
        raise AssertionError("向量应该全部复用，不该调用向量器")


class ExplodingEmbedder:
    """一用就失败的向量器，证明某条路径根本没有用到向量器"""

    name = "boom"
    dim = 512

    def embed(self, texts):
        raise AssertionError("这条路径不应该调用向量器")


@pytest.fixture(scope="module")
def probe() -> dict:
    """在全新的解释器里（Python 的字符串哈希种子也不同）算哈希向量，并看有没有顺带加载模型相关的库"""
    code = (
        "import hashlib, json, sys\n"
        "from metadata.index import HashEmbedder\n"
        f"texts = {SAMPLE_TEXTS!r}\n"
        "digest = hashlib.sha256(HashEmbedder().embed(texts).tobytes()).hexdigest()\n"
        "heavy = [name for name in ('sentence_transformers', 'torch') if name in sys.modules]\n"
        "print(json.dumps({'digest': digest, 'heavy': heavy}))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, "PYTHONHASHSEED": "31337"}, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------- 哈希向量器 ----------


def test_hash_vectors_are_deterministic_and_normalized():
    first = HashEmbedder().embed(SAMPLE_TEXTS)
    second = HashEmbedder().embed(SAMPLE_TEXTS)

    assert first.shape == (3, 512) and first.dtype == np.float32
    assert np.array_equal(first, second)
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0, atol=1e-6)


def test_hash_vectors_do_not_depend_on_the_python_hash_seed(probe):
    expected = hashlib.sha256(HashEmbedder().embed(SAMPLE_TEXTS).tobytes()).hexdigest()

    assert probe["digest"] == expected


def test_importing_the_index_module_does_not_load_the_model_libraries(probe):
    assert probe["heavy"] == []


def test_empty_text_gives_a_zero_vector():
    vectors = HashEmbedder().embed(["", "，。 ！", "机构号"])

    assert not vectors[0].any()
    assert not vectors[1].any()            # 只有标点：没有任何特征
    assert vectors[2].any()
    assert HashEmbedder().embed([]).shape == (0, 512)


def test_similarity_follows_shared_wording():
    loan, npl_loan, org = HashEmbedder().embed(["贷款余额", "不良贷款余额", "机构号"])

    assert loan @ npl_loan > 0.7           # 共用了二元组「贷款」「款余」「余额」
    assert abs(loan @ org) < 0.3           # 没有任何共同字词
    assert loan @ npl_loan > loan @ org


def test_features_are_chinese_chars_bigrams_and_ascii_words_with_their_weights():
    embedder = HashEmbedder()

    two_chars = embedder.embed(["贷款"])[0]            # 两个单字（0.5）加一个二元组（1.0）
    weights = np.sort(np.abs(two_chars[two_chars != 0]))
    assert np.allclose(weights, np.array([0.5, 0.5, 1.0]) / np.sqrt(1.5), atol=1e-6)

    word = embedder.embed(["Org_No"])[0]               # ASCII 整词只有一个特征
    assert np.count_nonzero(word) == 1
    assert np.abs(word).max() == pytest.approx(1.0)

    assert np.count_nonzero(embedder.embed(["贷 款"])[0]) == 2        # 中间隔了空格：只有两个单字，没有二元组
    assert np.count_nonzero(embedder.embed(["a贷"])[0]) == 2          # 中英文交界处不凑二元组


def test_text_is_normalized_before_hashing():
    full_width, plain = HashEmbedder().embed(["Org_No　ＡＢＣ１２３", "org_no abc123"])

    assert np.array_equal(full_width, plain)           # 全角转半角、英文转小写


def test_hash_embedder_dimension_and_name():
    small = HashEmbedder(64)

    assert (HashEmbedder().name, HashEmbedder().dim) == ("hash-bigram-512", 512)
    assert (small.name, small.dim) == ("hash-bigram-64", 64)
    assert small.embed(["贷款余额"]).shape == (1, 64)
    with pytest.raises(ValueError):
        HashEmbedder(0)


# ---------- 字段与样例的文本、编号 ----------


def test_field_ref_id_and_text_formats(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    table = next(t for t in mart.tables if t.table_id == "ods.acct")
    columns = {c.column_name: c for c in mart.columns if c.table_id == "ods.acct"}

    assert field_ref_id("ods.acct", "bal") == "ods.acct.bal"
    assert field_text(table, columns["bal"]) == "账户表 余额"           # 表中文名 字段中文名；口径说明「原币」不放进去
    assert field_text(table, columns["acct_no"]) == "账户表 账号"


def test_field_text_leaves_out_the_caliber_note(tiny_mart):
    """口径说明在真实库里多是加工过程的描述，混进向量文本会稀释相似度，所以只用表中文名和字段中文名"""
    mart, _ = load_mart(tiny_mart)
    table = next(t for t in mart.tables if t.table_id == "ods.acct")
    bal = next(c for c in mart.columns if c.table_id == "ods.acct" and c.column_name == "bal")
    process = bal.model_copy(update={"description": "【指标】折人民币余额；加工：SUM(CASE WHEN a.flag = '1' THEN a.bal END)"})

    assert bal.description == "原币"
    assert field_text(table, process) == field_text(table, bal) == "账户表 余额"      # 只有说明不同的两个字段，文本相同


def test_example_text_joins_requirement_and_caliber():
    with_note = make_example(1, "查询各机构的对公存款余额", "折人民币")

    assert example_text(with_note) == "查询各机构的对公存款余额 折人民币"
    assert example_text(make_example(2, "查询各机构的对公存款余额")) == "查询各机构的对公存款余额"


# ---------- 建向量 ----------


def test_build_vectors_covers_every_field_in_mart_order(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    tables = {t.table_id: t for t in mart.tables}

    bundle, _ = build_vectors(mart, HashEmbedder())

    field = bundle.sets["field"]
    assert (bundle.embedder, bundle.dim) == ("hash-bigram-512", 512)
    assert field.kind == "field"
    assert field.ref_ids == [f"{c.table_id}.{c.column_name}" for c in mart.columns]
    assert field.texts == [field_text(tables[c.table_id], c) for c in mart.columns]
    assert "原币" not in " ".join(field.texts)           # 口径说明不进向量文本
    assert field.matrix.shape == (9, 512) and field.matrix.dtype == np.float32
    assert np.array_equal(field.matrix, HashEmbedder().embed(field.texts))
    assert "example" not in bundle.sets                # 没有样例就不建样例集


def test_build_vectors_adds_example_vectors_when_there_are_examples(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    mart = mart.model_copy(update={"examples": [make_example(1, "查询各客户的账户余额", "原币"),
                                                make_example(2, "查询客户数")]})

    bundle, _ = build_vectors(mart, HashEmbedder())

    example = bundle.sets["example"]
    assert example.kind == "example"
    assert example.ref_ids == ["01_demo.sql#1", "01_demo.sql#2"]
    assert example.texts == ["查询各客户的账户余额 原币", "查询客户数"]
    assert np.array_equal(example.matrix, HashEmbedder().embed(example.texts))


def test_build_vectors_embeds_in_batches_without_changing_the_result():
    mart, _ = load_mart(MOCK_MART_DIR)
    embedder = RecordingEmbedder()

    batched, _ = build_vectors(mart, embedder, batch_size=40)
    whole, _ = build_vectors(mart, HashEmbedder(), batch_size=1000)

    assert embedder.batches == [40, 40, 13]            # 93 个字段
    assert np.array_equal(batched.sets["field"].matrix, whole.sets["field"].matrix)
    with pytest.raises(ValueError):
        build_vectors(mart, HashEmbedder(), batch_size=0)


def test_build_vectors_is_reproducible():
    mart, _ = load_mart(MOCK_MART_DIR)

    first, _ = build_vectors(mart, HashEmbedder())
    second, _ = build_vectors(mart, HashEmbedder())

    assert first.sets["field"].ref_ids == second.sets["field"].ref_ids
    assert first.sets["field"].texts == second.sets["field"].texts
    assert np.array_equal(first.sets["field"].matrix, second.sets["field"].matrix)


def test_build_vectors_report_section(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    mart = mart.model_copy(update={"examples": [make_example(1, "查询客户数")]})

    _, section = build_vectors(mart, HashEmbedder())

    assert section.title == "向量"
    text = "\n".join(section.lines)
    assert "向量器：hash-bigram-512" in text
    assert "维度：512" in text
    assert "字段向量：9 条" in text
    assert "样例向量：1 条" in text
    assert "耗时：" in text


def test_build_vectors_rejects_an_embedder_that_returns_the_wrong_shape(tiny_mart):
    class Broken:
        name = "broken"
        dim = 512

        def embed(self, texts):
            return np.zeros((len(texts), 8), dtype=np.float32)

    mart, _ = load_mart(tiny_mart)

    with pytest.raises(ValueError, match="维度"):
        build_vectors(mart, Broken())


def test_build_vectors_normalizes_vectors_from_a_careless_embedder(tiny_mart):
    class Careless:
        name = "careless"
        dim = 4

        def embed(self, texts):
            matrix = np.full((len(texts), 4), 2.0, dtype=np.float32)     # 长度是 4，没有归一化
            matrix[[i for i, text in enumerate(texts) if "账号" in text]] = 0.0
            return matrix

    mart, _ = load_mart(tiny_mart)

    bundle, _ = build_vectors(mart, Careless())

    matrix = bundle.sets["field"].matrix
    norms = np.linalg.norm(matrix, axis=1)
    zero_rows = [i for i, text in enumerate(bundle.sets["field"].texts) if "账号" in text]
    assert zero_rows and np.all(norms[zero_rows] == 0.0)               # 零向量保持为零
    assert np.allclose(np.delete(norms, zero_rows), 1.0)


# ---------- 存进元数据库 ----------


def test_vectors_survive_the_store_round_trip(tiny_mart, tmp_path):
    mart, _ = load_mart(tiny_mart)
    mart = mart.model_copy(update={"examples": [make_example(1, "查询客户数", "按客户号去重")]})
    bundle, _ = build_vectors(mart, HashEmbedder())

    write_store(tmp_path / "m.db", mart, bundle)
    store = MetadataStore(tmp_path / "m.db")

    for kind in ("field", "example"):
        original, restored = bundle.sets[kind], store.vectors(kind)
        assert restored.ref_ids == original.ref_ids
        assert restored.texts == original.texts
        assert restored.matrix.dtype == np.float32
        assert np.array_equal(restored.matrix, original.matrix)       # 逐位相同
    assert store.embedder_name == "hash-bigram-512"


def test_embedder_name_and_dim_are_recorded_in_meta_info(tiny_mart):
    report = run_import(tiny_mart, embedder=HashEmbedder(256))

    with closing(sqlite3.connect(report.db_path)) as conn:
        info = dict(conn.execute("SELECT key, value FROM meta_info"))

    assert info == {"embedder": "hash-bigram-256", "dim": "256"}
    assert MetadataStore(report.db_path).vectors("field").matrix.shape == (9, 256)


def test_import_uses_the_embedder_from_the_environment_by_default(tiny_mart):
    report = run_import(tiny_mart)                     # tests/conftest.py 把环境变量设成了 hash

    assert MetadataStore(report.db_path).embedder_name == "hash-bigram-512"
    assert report.counts["字段向量"] == 9
    text = (tiny_mart / "build" / "import_report.md").read_text(encoding="utf-8")
    assert "## 向量" in text and "字段向量：9 条" in text


def test_no_index_import_writes_no_vectors_and_never_calls_the_embedder(tiny_mart):
    report = run_import(tiny_mart, build_index=False, embedder=ExplodingEmbedder())

    with closing(sqlite3.connect(report.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meta_vector").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM meta_info").fetchone() == (0,)
    store = MetadataStore(report.db_path)
    assert store.vectors("field") is None and store.embedder_name is None
    assert report.counts["字段向量"] == 0


def test_reimport_with_another_embedder_replaces_every_vector(tiny_mart):
    run_import(tiny_mart, embedder=HashEmbedder(256))

    report = run_import(tiny_mart, embedder=HashEmbedder(512))

    store = MetadataStore(report.db_path)
    assert store.embedder_name == "hash-bigram-512"
    assert store.vectors("field").matrix.shape == (9, 512)
    with closing(sqlite3.connect(report.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meta_vector").fetchone() == (9,)         # 没有旧向量残留
        assert dict(conn.execute("SELECT key, value FROM meta_info")) == {"embedder": "hash-bigram-512", "dim": "512"}


def test_reimport_with_no_index_drops_the_old_vectors(tiny_mart):
    run_import(tiny_mart)
    assert MetadataStore(default_db_path(tiny_mart)).vectors("field") is not None

    report = run_import(tiny_mart, build_index=False)

    store = MetadataStore(report.db_path)
    assert store.vectors("field") is None and store.embedder_name is None


def test_command_line_no_index_flag_never_asks_for_the_default_embedder(tiny_mart, monkeypatch):
    def refuse():
        raise AssertionError("--no-index 不应该去取向量器")

    monkeypatch.setattr("metadata.importer.default_embedder", refuse)

    assert main([str(tiny_mart), "--no-index"]) == 0
    assert MetadataStore(default_db_path(tiny_mart)).embedder_name is None


def test_importing_the_mock_mart_twice_gives_identical_databases(tmp_path):
    mart_dir = tmp_path / "mock_mart"
    shutil.copytree(MOCK_MART_DIR, mart_dir, ignore=shutil.ignore_patterns("build", "__pycache__", "*.py"))

    first = run_import(mart_dir, embedder=HashEmbedder())
    first_dump = dump(first.db_path)
    second = run_import(mart_dir, embedder=HashEmbedder())

    assert second.counts["字段向量"] == 93
    assert dump(second.db_path) == first_dump          # 包括向量的每一个字节


def test_read_vectors_reads_only_the_vectors(tiny_mart, tmp_path):
    report = run_import(tiny_mart)

    bundle = read_vectors(report.db_path)

    store = MetadataStore(report.db_path)
    assert (bundle.embedder, bundle.dim) == (store.embedder_name, 512)
    assert bundle.sets["field"].texts == store.vectors("field").texts
    assert np.array_equal(bundle.sets["field"].matrix, store.vectors("field").matrix)
    assert read_vectors(run_import(tiny_mart, tmp_path / "plain.db", build_index=False).db_path) is None
    with pytest.raises(FileNotFoundError):
        read_vectors(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()      # 没有顺手建出一个空库
    (tmp_path / "junk.db").write_bytes(b"not a database " * 100)
    with pytest.raises(RuntimeError, match="读不了元数据库"):
        read_vectors(tmp_path / "junk.db")


# ---------- 复用上一次的向量 ----------


def rename_column(mart: Mart, table_id: str, column_name: str, column_cn: str) -> Mart:
    """改一个字段的中文名，这个字段的向量文本就变了"""
    columns = [column.model_copy(update={"column_cn": column_cn})
               if (column.table_id, column.column_name) == (table_id, column_name) else column
               for column in mart.columns]
    return mart.model_copy(update={"columns": columns})


def nudged(bundle: VectorBundle) -> VectorBundle:
    """每个分量乘一个很接近 1 的数：余弦几乎不变，逐位却和重算的不同，用来分辨向量是复用的还是重算的"""
    return VectorBundle(embedder=bundle.embedder, dim=bundle.dim, sets={
        kind: VectorSet(kind=kind, ref_ids=vector_set.ref_ids, texts=vector_set.texts,
                        matrix=vector_set.matrix * np.float32(1 + 1e-6))
        for kind, vector_set in bundle.sets.items()})


def test_unchanged_texts_reuse_previous_vectors_without_calling_the_embedder(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    mart = mart.model_copy(update={"examples": [make_example(1, "查询客户数")]})
    previous = nudged(build_vectors(mart, HashEmbedder())[0])
    hints: list[int] = []

    bundle, section = build_vectors(mart, RefusingHashEmbedder(), previous=previous, before_embed=hints.append)

    for kind in ("field", "example"):
        assert np.array_equal(bundle.sets[kind].matrix, previous.sets[kind].matrix)   # 逐位就是上一版的
    assert hints == []                                 # 不用加载模型，也就不提示
    text = "\n".join(section.lines)
    assert "不同的文本 10 条：复用上一版 10 条，新算 0 条" in text
    assert "全部复用，没有调用向量器" in text


def test_only_new_texts_are_embedded_and_a_sample_of_reused_ones_is_checked():
    mart, _ = load_mart(MOCK_MART_DIR)
    previous = nudged(build_vectors(mart, HashEmbedder())[0])
    changed = rename_column(mart, "dim.org", "org_nm", "机构全称")
    embedder = RecordingEmbedder()
    hints: list[int] = []

    bundle, section = build_vectors(changed, embedder, previous=previous, before_embed=hints.append)

    field = bundle.sets["field"]
    renamed = field.ref_ids.index("dim.org.org_nm")
    assert field.texts[renamed].endswith("机构全称")
    assert embedder.batches == hints == [33]           # 1 条新文本，加上从 92 条复用的文本里抽查的 32 条
    assert embedder.texts[0] == field.texts[renamed]
    assert len(set(embedder.texts[1:])) == 32 and field.texts[renamed] not in embedder.texts[1:]
    assert np.array_equal(field.matrix[renamed], HashEmbedder().embed([field.texts[renamed]])[0])
    others = [row for row in range(len(field.ref_ids)) if row != renamed]
    assert np.array_equal(field.matrix[others], previous.sets["field"].matrix[others])     # 抽查过的也用上一版的
    text = "\n".join(section.lines)
    assert "不同的文本 93 条：复用上一版 92 条，新算 1 条" in text
    assert "抽查复用的 32 条，与重算的余弦最低 1.0000" in text


def test_reused_vectors_that_disagree_with_the_model_are_all_recomputed():
    """向量模型换了、名字没变时，抽查发现对不上，一条旧向量也不留"""
    mart, _ = load_mart(MOCK_MART_DIR)
    honest = build_vectors(mart, HashEmbedder())[0].sets["field"]
    stale = np.random.default_rng(7).normal(size=honest.matrix.shape).astype(np.float32)
    stale /= np.linalg.norm(stale, axis=1, keepdims=True)
    previous = VectorBundle(embedder="hash-bigram-512", dim=512, sets={"field": VectorSet(
        kind="field", ref_ids=honest.ref_ids, texts=honest.texts, matrix=stale)})
    embedder = RecordingEmbedder()

    bundle, section = build_vectors(rename_column(mart, "dim.org", "org_nm", "机构全称"), embedder,
                                    previous=previous)

    field = bundle.sets["field"]
    assert np.array_equal(field.matrix, HashEmbedder().embed(field.texts))
    assert embedder.batches == [33, 60]                # 抽查对不上，剩下的 60 条也重算
    text = "\n".join(section.lines)
    assert "不同的文本 93 条：全部新算（抽查复用的 32 条" in text and "向量模型可能换过" in text


def test_vectors_from_another_embedder_are_not_reused(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    previous, _ = build_vectors(mart, HashEmbedder(256))
    embedder = RecordingEmbedder()

    bundle, section = build_vectors(mart, embedder, previous=previous)

    assert embedder.batches == [9]
    assert np.array_equal(bundle.sets["field"].matrix, HashEmbedder().embed(bundle.sets["field"].texts))
    assert ("全部新算（上一版的向量器是 hash-bigram-256（256 维），这次是 hash-bigram-512（512 维））"
            in "\n".join(section.lines))


def test_identical_texts_are_embedded_once(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    acct = next(table for table in mart.tables if table.table_id == "ods.acct")
    twin = acct.model_copy(update={"table_id": "ods.acct_hist", "table_name": "acct_hist"})    # 中文名也是「账户表」
    twin_columns = [column.model_copy(update={"table_id": twin.table_id})
                    for column in mart.columns if column.table_id == "ods.acct"]
    mart = mart.model_copy(update={"tables": [*mart.tables, twin], "columns": [*mart.columns, *twin_columns]})
    embedder = RecordingEmbedder()

    bundle, section = build_vectors(mart, embedder)

    field = bundle.sets["field"]
    assert len(field.ref_ids) == 13 and embedder.batches == [9]
    assert np.array_equal(field.matrix[field.ref_ids.index("ods.acct_hist.bal")],
                          field.matrix[field.ref_ids.index("ods.acct.bal")])
    assert "不同的文本 9 条：全部新算（没有上一版的向量）" in "\n".join(section.lines)


def test_texts_without_any_feature_do_not_look_like_a_changed_model(tiny_mart):
    mart, _ = load_mart(tiny_mart)
    mart = mart.model_copy(update={"examples": [make_example(1, "，。")]})     # 只有标点，哈希向量是零向量
    previous, _ = build_vectors(mart, HashEmbedder())

    bundle, section = build_vectors(rename_column(mart, "ods.acct", "bal", "账户余额"), HashEmbedder(),
                                    previous=previous)

    text = "\n".join(section.lines)
    assert "复用上一版 9 条，新算 1 条" in text and "可能换过" not in text      # 零向量也被抽查到了
    assert not bundle.sets["example"].matrix.any()


def test_reimport_reuses_every_vector_without_calling_the_embedder(tiny_mart):
    first = dump(run_import(tiny_mart, embedder=HashEmbedder()).db_path)

    report = run_import(tiny_mart, embedder=RefusingHashEmbedder())

    assert dump(report.db_path) == first
    text = (tiny_mart / "build" / "import_report.md").read_text(encoding="utf-8")
    assert "复用上一版 9 条，新算 0 条" in text and "全部复用，没有调用向量器" in text


def test_reindex_recomputes_every_vector(tiny_mart):
    run_import(tiny_mart, embedder=HashEmbedder())
    embedder = RecordingEmbedder()

    report = run_import(tiny_mart, embedder=embedder, reindex=True)

    assert embedder.batches == [9]
    assert "全部新算（加了 --reindex）" in report.to_markdown()


def test_import_says_why_nothing_could_be_reused(tiny_mart):
    assert "全部新算（元数据库还不存在）" in run_import(tiny_mart).to_markdown()

    run_import(tiny_mart, build_index=False)
    assert "全部新算（上一次导入没有算向量）" in run_import(tiny_mart).to_markdown()

    default_db_path(tiny_mart).write_bytes(b"not a database " * 100)
    report = run_import(tiny_mart)
    assert report.succeeded and "全部新算（读不了元数据库" in report.to_markdown()
    assert MetadataStore(report.db_path).counts()["字段向量"] == 9


def test_command_line_reindex_flag(tiny_mart, capsys):
    assert main([str(tiny_mart)]) == 0
    capsys.readouterr()

    assert main([str(tiny_mart), "--reindex"]) == 0
    assert "全部新算（加了 --reindex）" in capsys.readouterr().out
    assert main([str(tiny_mart), "--reindex", "--no-index"]) == 2      # 不算向量就谈不上重算
    assert "用法" in capsys.readouterr().out


def test_import_merges_compatible_vector_sources_in_order(tiny_mart, tiny_sheets, tmp_path, monkeypatch):
    """坏库、无向量库和不兼容库被跳过，候选库补充的新文本仍能复用。"""
    from metadata import importer
    from tests.helpers import write_mart

    base = run_import(tiny_mart, tmp_path / "base.db", embedder=HashEmbedder()).db_path
    no_vectors = run_import(tiny_mart, tmp_path / "plain.db", build_index=False).db_path
    incompatible = run_import(tiny_mart, tmp_path / "other.db", embedder=HashEmbedder(256)).db_path
    tiny_sheets["字段清单"][3][2] = "账户余额"
    write_mart(tiny_mart, tiny_sheets)
    candidate = run_import(tiny_mart, tmp_path / "candidate.db", embedder=HashEmbedder()).db_path
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"broken sqlite database")
    captured = []
    original = importer.build_vectors

    def capture(*args, **kwargs):
        captured.append(kwargs["previous"])
        return original(*args, **kwargs)

    monkeypatch.setattr(importer, "build_vectors", capture)
    report = run_import(tiny_mart, tmp_path / "merged.db", embedder=RefusingHashEmbedder(),
                        reuse_from=[tmp_path / "missing.db", broken, no_vectors, base, incompatible, candidate])

    assert report.succeeded and "全部复用，没有调用向量器" in report.to_markdown()
    merged = captured[0]
    first = read_vectors(base).sets["field"]
    second = read_vectors(candidate).sets["field"]
    assert merged.embedder == "hash-bigram-512" and merged.dim == 512
    assert merged.sets["field"].ref_ids == first.ref_ids + second.ref_ids
    assert merged.sets["field"].texts == first.texts + second.texts
    assert np.array_equal(merged.sets["field"].matrix, np.concatenate((first.matrix, second.matrix)))


def test_import_uses_first_unusable_source_reason_and_empty_sources(tiny_mart, tmp_path):
    plain = run_import(tiny_mart, tmp_path / "plain.db", build_index=False).db_path
    report = run_import(tiny_mart, tmp_path / "new.db", reuse_from=[plain, tmp_path / "missing.db"])
    assert "全部新算（上一次导入没有算向量）" in report.to_markdown()
    report = run_import(tiny_mart, tmp_path / "new.db", reuse_from=[])
    assert "全部新算（元数据库还不存在）" in report.to_markdown()


# ---------- 全量检索 ----------


def test_search_returns_the_top_k_by_descending_score():
    index = VectorIndex(make_set([[1, 0], [0.6, 0.8], [0, 1], [-1, 0]], ["a", "b", "c", "d"]))

    result = index.search(np.array([1, 0], dtype=np.float32), k=3)

    assert [ref_id for ref_id, _ in result] == ["a", "b", "c"]
    assert [score for _, score in result] == pytest.approx([1.0, 0.6, 0.0])
    assert all(type(score) is float for _, score in result)


def test_equal_scores_are_ordered_by_ref_id():
    ids = ["f.c", "f.a", "f.d", "f.b"]                 # 入库顺序故意打乱
    index = VectorIndex(make_set([[1, 0], [1, 0], [0, 1], [1, 0]], ids))

    result = index.search([1, 0], k=4)

    assert [ref_id for ref_id, _ in result] == ["f.a", "f.b", "f.c", "f.d"]
    assert [ref_id for ref_id, _ in index.search([1, 0], k=2)] == ["f.a", "f.b"]   # 平局压在第 k 名的边界上，也按编号取


def test_identical_vectors_get_bit_identical_scores_in_high_dimensions():
    """矩阵乘向量走 BLAS 时，相同的两行在不同位置上可能差最后一位，「同分」就会失效，所以检索不能直接用 @"""
    rng = np.random.default_rng(7)
    base = rng.normal(size=(3, 512)).astype(np.float32)
    base /= np.linalg.norm(base, axis=1, keepdims=True)
    picks = [0, 1, 2, 0, 1, 0, 2, 1, 0, 2, 1]          # 每个向量重复三四次，散落在不同位置
    ids = [f"id{20 - i:02d}" for i in range(len(picks))]   # 编号和入库顺序相反
    index = VectorIndex(make_set(base[picks].tolist(), ids))

    result = index.search(base[0], k=len(picks))

    assert len({score for _, score in result}) == 3     # 只有三个不同的得分
    assert result == sorted(result, key=lambda item: (-item[1], item[0]))


def test_allowed_filters_before_taking_the_top_k():
    index = VectorIndex(make_set([[1, 0], [0.6, 0.8], [0, 1], [-1, 0]], ["a", "b", "c", "d"]))

    assert [ref_id for ref_id, _ in index.search([1, 0], k=1, allowed={"b", "c"})] == ["b"]   # 不是先取第一名再过滤
    assert index.search([1, 0], k=3, allowed=["d", "no.such.id", "d"]) == [("d", -1.0)]     # 不认识的编号忽略，重复的只算一次
    assert index.search([1, 0], k=3, allowed=[]) == []
    assert index.search([1, 0], k=3, allowed=None) == index.search([1, 0], k=3)


def test_k_larger_than_the_index_returns_everything_and_k_zero_returns_nothing():
    index = VectorIndex(make_set([[1, 0], [0, 1]], ["a", "b"]))

    assert [ref_id for ref_id, _ in index.search([1, 0], k=50)] == ["a", "b"]
    assert index.search([1, 0], k=0) == []
    assert index.search([1, 0], k=-1) == []


def test_search_on_an_empty_set_returns_nothing():
    empty = VectorSet(kind="field", ref_ids=[], texts=[], matrix=np.empty((0, 4), dtype=np.float32))

    assert VectorIndex(empty).search(np.zeros(4, dtype=np.float32), k=5) == []


def test_query_from_another_embedder_is_rejected():
    index = VectorIndex(make_set([[1, 0], [0, 1]], ["a", "b"]))

    with pytest.raises(ValueError, match="维度"):
        index.search(np.zeros(3, dtype=np.float32), k=1)
    with pytest.raises(ValueError):
        index.search(np.zeros((1, 2), dtype=np.float32), k=1)         # 要的是一维的查询向量


def test_vector_set_with_mismatched_rows_is_rejected():
    broken = VectorSet(kind="field", ref_ids=["a"], texts=["a"], matrix=np.zeros((2, 3), dtype=np.float32))

    with pytest.raises(ValueError):
        VectorIndex(broken)


def test_field_search_finds_the_column_by_its_chinese_name():
    mart, _ = load_mart(MOCK_MART_DIR)
    bundle, _ = build_vectors(mart, HashEmbedder())
    index = VectorIndex(bundle.sets["field"])

    top = index.search(HashEmbedder().embed(["不良贷款余额"])[0], k=3)
    assert top[0][0] == "ads.corp_cust_loan_bal_d.npl_bal_rmb"

    org_fields = {ref_id for ref_id in bundle.sets["field"].ref_ids if ref_id.startswith("dim.org.")}
    in_org = index.search(HashEmbedder().embed(["机构名称"])[0], k=2, allowed=org_fields)
    assert in_org[0][0] == "dim.org.org_nm"
    assert all(ref_id in org_fields for ref_id, _ in in_org)


# ---------- 向量器的选择与 BGE 的加载策略 ----------


def test_default_embedder_reads_the_environment_on_every_call(monkeypatch):
    monkeypatch.setenv(EMBEDDER_ENV, "hash")
    assert isinstance(default_embedder(), HashEmbedder)
    monkeypatch.setenv(EMBEDDER_ENV, " Hash ")           # 大小写和首尾空格不影响
    assert isinstance(default_embedder(), HashEmbedder)

    monkeypatch.setenv(EMBEDDER_ENV, "bge")
    bge = default_embedder()
    assert isinstance(bge, BgeEmbedder)

    monkeypatch.delenv(EMBEDDER_ENV)
    assert default_embedder() is bge                   # 没设置时也是 BGE，而且是同一个对象

    monkeypatch.setenv(EMBEDDER_ENV, "hash")
    assert isinstance(default_embedder(), HashEmbedder)     # 切回去立刻生效


def test_default_embedder_rejects_an_unknown_value(monkeypatch):
    monkeypatch.setenv(EMBEDDER_ENV, "openai")

    with pytest.raises(ValueError, match=EMBEDDER_ENV):
        default_embedder()


@pytest.fixture
def fake_model_library(monkeypatch):
    """用假的 sentence_transformers 顶替真库，BGE 的加载策略就能在不加载模型的情况下测"""
    state = types.SimpleNamespace(constructed=[], failures=[], encoded=[], events=[])

    class FakeSentenceTransformer:
        def __init__(self, name, **kwargs):
            state.constructed.append((name, kwargs))
            if state.failures:
                outcome = state.failures.pop(0)
                if outcome is not None:
                    raise outcome

        def encode(self, sentences, **kwargs):
            state.encoded.append((list(sentences), kwargs))
            return np.full((len(sentences), 512), 0.5, dtype=np.float64)    # 故意给 float64

    class FakeLibrary(types.ModuleType):
        def __getattr__(self, name):         # from sentence_transformers import SentenceTransformer 时才会走到这里
            if name != "SentenceTransformer":
                raise AttributeError(name)
            state.events.append("import")
            return FakeSentenceTransformer

    def detect_device():
        state.events.append("detect")
        return "cuda"

    monkeypatch.setitem(sys.modules, "sentence_transformers", FakeLibrary("sentence_transformers"))
    monkeypatch.setattr(index_module, "_detect_device", detect_device)
    monkeypatch.setattr(BgeEmbedder(), "_model", None)
    monkeypatch.setenv("HF_ENDPOINT", "")
    return state


def test_bge_embedder_is_a_lazy_singleton(fake_model_library):
    embedder = BgeEmbedder()
    empty = embedder.embed([])

    assert embedder is BgeEmbedder()
    assert (embedder.name, embedder.dim) == ("bge-small-zh-v1.5", 512)
    assert empty.shape == (0, 512) and empty.dtype == np.float32
    assert fake_model_library.constructed == []        # 创建对象、空输入都不会加载模型


def test_bge_loads_once_from_the_local_cache_and_uses_cuda(fake_model_library):
    embedder = BgeEmbedder()

    vectors = embedder.embed(["贷款余额", "机构号"])
    embedder.embed(["账号"])

    assert fake_model_library.constructed == [
        ("BAAI/bge-small-zh-v1.5", {"device": "cuda", "local_files_only": True})]
    assert all(kwargs["normalize_embeddings"] is True for _, kwargs in fake_model_library.encoded)
    assert vectors.dtype == np.float32 and vectors.shape == (2, 512)


def test_bge_imports_the_model_library_before_touching_cuda(fake_model_library):
    """顺序反过来，在 Windows 上已经导入 openpyxl 的进程里，导入 pyarrow 时会内存访问冲突、整个进程崩溃"""
    BgeEmbedder().embed(["贷款余额"])

    assert fake_model_library.events == ["import", "detect"]


def test_bge_downloads_only_when_the_local_cache_misses(fake_model_library):
    fake_model_library.failures[:] = [OSError("缓存里没有"), None]

    BgeEmbedder().embed(["贷款余额"])

    assert [kwargs.get("local_files_only", False) for _, kwargs in fake_model_library.constructed] == [True, False]


def test_bge_load_failure_tells_how_to_skip_the_index(fake_model_library):
    fake_model_library.failures[:] = [OSError("缓存里没有"), OSError("连不上网")]

    with pytest.raises(RuntimeError, match="--no-index") as error:
        BgeEmbedder().embed(["贷款余额"])

    assert "缓存里没有" in str(error.value) and "连不上网" in str(error.value)     # 两次失败的原因都要给出


def test_missing_model_library_tells_how_to_skip_the_index(fake_model_library, monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)    # 让 import 失败

    with pytest.raises(RuntimeError, match="--no-index"):
        BgeEmbedder().embed(["贷款余额"])


def test_bge_download_goes_through_the_mirror_unless_an_endpoint_is_configured(fake_model_library, monkeypatch):
    BgeEmbedder().embed(["贷款余额"])
    assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"

    monkeypatch.setenv("HF_ENDPOINT", "https://example.invalid")
    monkeypatch.setattr(BgeEmbedder(), "_model", None)
    BgeEmbedder().embed(["贷款余额"])
    assert os.environ["HF_ENDPOINT"] == "https://example.invalid"      # 配置过的不动
