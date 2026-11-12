"""验证轻量 BGE 的批处理、兼容规则及运行时选择。"""
from types import SimpleNamespace

import numpy as np
import pytest

from metadata.index import BGE_NAME, BgeEmbedder, OnnxBgeEmbedder
from nl2sql.service import embedder_for


class Tokens:
    """记录分词前文本并返回简化的分词结果。"""

    def __init__(self):
        self.batches = []

    def encode_batch(self, texts):
        self.batches.append(texts)
        return [SimpleNamespace(ids=[1, 2], attention_mask=[1, 1], type_ids=[0, 0]) for _ in texts]


class Session:
    """返回固定向量，并核对模型输入。"""

    def run(self, names, inputs):
        assert names == ['embedding']
        assert set(inputs) == {'input_ids', 'attention_mask', 'token_type_ids'}
        return [np.ones((len(inputs['input_ids']), 512), dtype=np.float32)]


def test_onnx_batches_and_matches_original_preprocessing(tmp_path):
    """保留原分词前处理、批次大小和空输入行为。"""
    model = OnnxBgeEmbedder(str(tmp_path))
    tokens = Tokens()
    model._tokenizer, model._session = tokens, Session()
    assert model.embed([]).shape == (0, 512)
    out = model.embed(['  ABC 中文  '] * 129)
    assert out.shape == (129, 512) and out.dtype == np.float32
    assert [len(batch) for batch in tokens.batches] == [128, 1]
    assert tokens.batches[0][0] == 'abc 中文'


def test_onnx_model_missing_or_invalid_fails_closed(tmp_path):
    """候选文件不存在或输出不合规时，不悄悄切换向量空间。"""
    model = OnnxBgeEmbedder(str(tmp_path))
    with pytest.raises(RuntimeError, match='模型文件不完整'):
        model.embed(['test'])
    model._tokenizer = Tokens()
    model._session = SimpleNamespace(run=lambda *_: [np.full((1, 512), np.nan, dtype=np.float32)])
    with pytest.raises(RuntimeError, match='返回的向量无效'):
        model.embed(['test'])


def test_embedder_choice_is_opt_in_and_reuses_runtime(tmp_path, monkeypatch):
    """未配置时仍用原模型，配置后同一目录复用轻量模型。"""
    store = SimpleNamespace(embedder_name=BGE_NAME)
    monkeypatch.delenv('AGENTFLOW_BGE_ONNX_DIR', raising=False)
    assert isinstance(embedder_for(store), BgeEmbedder)
    monkeypatch.setenv('AGENTFLOW_BGE_ONNX_DIR', str(tmp_path))
    first = embedder_for(store)
    assert isinstance(first, OnnxBgeEmbedder) and embedder_for(store) is first
