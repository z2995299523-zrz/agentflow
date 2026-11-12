"""千问服务端配置及请求解析。"""
import json
import sqlite3
import pytest

import config
import main
from nl2sql import model_settings


def test_qwen_setting_is_persistent_and_applies_to_next_request(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ACCESS_MODEL_IDS", frozenset({"qwen"}))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(config, "available_models", lambda: [config.ModelChoice("qwen3.8-flash", "千问", "dashscope")])
    assert model_settings.get_qwen() == "qwen3.8-flash"
    assert main._resolved_model("qwen") == "@dashscope:qwen3.8-flash"
    assert main.update_qwen_settings(main.QwenSettings(model="qwen3.8-27b")).model == "qwen3.8-27b"
    assert model_settings.get_qwen() == "qwen3.8-27b"
    assert main._resolved_model("qwen") == "@dashscope:qwen3.8-27b"
    with pytest.raises(ValueError):
        model_settings.set_qwen("invalid name")
    assert model_settings.get_qwen() == "qwen3.8-27b"


def test_conversation_models_are_independent_and_can_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MART_DIR", str(tmp_path))
    assert model_settings.get_conversation_model("a") is None
    model_settings.set_conversation_model("a", "qwen")
    model_settings.set_conversation_model("b", "deepseek-flash")
    assert model_settings.get_conversation_model("a") == "qwen"
    assert model_settings.get_conversation_model("b") == "deepseek-flash"
    model_settings.set_conversation_model("a", "deepseek-flash")
    model_settings.delete_conversation_model("b")
    assert model_settings.get_conversation_model("a") == "deepseek-flash"
    assert model_settings.get_conversation_model("b") is None


def test_settings_in_history_are_included_in_backup_and_legacy_values_remain_readable(tmp_path, monkeypatch):
    from deploy.backup_history import backup_history

    monkeypatch.setattr(config, "MART_DIR", str(tmp_path))
    build = tmp_path / "build"
    build.mkdir()
    (build / "model_settings.json").write_text(json.dumps({"qwen_model": "qwen-old"}), encoding="utf-8")
    with sqlite3.connect(build / "conversation_models.db") as legacy:
        legacy.execute("CREATE TABLE choices (conversation_id TEXT PRIMARY KEY, model TEXT NOT NULL)")
        legacy.execute("INSERT INTO choices VALUES (?, ?)", ("old", "qwen"))
    assert model_settings.get_qwen() == "qwen-old"
    assert model_settings.get_conversation_model("old") == "qwen"
    model_settings.set_qwen("qwen-new")
    model_settings.set_conversation_model("old", "deepseek-flash")
    assert model_settings.get_qwen() == "qwen-new"
    assert model_settings.get_conversation_model("old") == "deepseek-flash"
    evidence = backup_history(build / "history.db", tmp_path / "backup" / "history.db")
    assert evidence["restore_verified"]
    assert evidence["counts"]["model_settings"] == 1
    assert evidence["counts"]["conversation_models"] == 1
