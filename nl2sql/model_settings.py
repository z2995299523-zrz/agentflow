"""服务端模型配置，供所有浏览器共用。"""
import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import config
from nl2sql.model_connection import normalize_api_key, normalize_base_url

DEFAULT_QWEN = "qwen3.8-flash"
DEFAULT_MODEL_KEY = "default_model_id"


def _legacy_path() -> Path:
    return Path(config.MART_DIR) / "build" / "model_settings.json"


def _history_path() -> Path:
    """会话库可能是指向可写位置的链接，读写其实际路径。"""
    return (Path(config.MART_DIR) / "build" / "history.db").resolve()


def _read_value(table: str, query: str, arguments: tuple = ()) -> str | bytes | None:
    """只读取得已保存的配置；旧版会话库可能还没有配置表。"""
    path = _history_path()
    if not path.exists():
        return None
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        row = conn.execute(query, arguments).fetchone() if exists else None
    return row[0] if row else None


def dashscope_label(model: str) -> str:
    """根据实际型号显示内置百炼入口。"""
    return "千问" if model.lower().startswith("qwen") else "百炼"


def get_qwen() -> str:
    """读取内置百炼入口的型号，兼容旧千问配置。"""
    # 旧版本仅接受 qwen 前缀，保留旧键的值以支持回退。
    stored = _read_value("model_settings", "SELECT value FROM model_settings WHERE key='dashscope_model'")
    if stored is None:
        stored = _read_value("model_settings", "SELECT value FROM model_settings WHERE key='qwen_model'")
    if stored is not None:
        if not config.valid_model_code(stored):
            raise ValueError("百炼模型配置无效，请重新保存")
        return stored
    path = _legacy_path()
    if not path.exists():
        return DEFAULT_QWEN
    data = json.loads(path.read_text(encoding="utf-8"))
    name = data.get("qwen_model")
    if not isinstance(name, str) or not config.valid_model_code(name):
        raise ValueError("百炼模型配置无效，请重新保存")
    return name


def set_qwen(name: str) -> str:
    """将内置百炼入口的型号写入会话库。"""
    name = name.strip()
    if not config.valid_model_code(name):
        raise ValueError("请填写有效的百炼模型代码")
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS model_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO model_settings (key, value) VALUES ('dashscope_model', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (name,))
    return name


def get_conversation_model(conversation_id: str) -> str | None:
    """读取会话独立的模型入口；未设置时返回空值。"""
    stored = _read_value("conversation_models", "SELECT model FROM conversation_models WHERE conversation_id = ?",
                         (conversation_id,))
    if stored is not None:
        return stored
    path = Path(config.MART_DIR) / "build" / "conversation_models.db"
    if not path.exists():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
        row = conn.execute("SELECT model FROM choices WHERE conversation_id = ?", (conversation_id,)).fetchone()
    return row[0] if row else None


def set_conversation_model(conversation_id: str, model: str) -> None:
    """保存会话独立的模型入口，不更改历史轮次。"""
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS conversation_models (conversation_id TEXT PRIMARY KEY, model TEXT NOT NULL)")
        conn.execute("INSERT INTO conversation_models (conversation_id, model) VALUES (?, ?) "
                     "ON CONFLICT(conversation_id) DO UPDATE SET model = excluded.model", (conversation_id, model))


def delete_conversation_model(conversation_id: str) -> None:
    """删除会话时清理对应模型选择。"""
    path = _history_path()
    if path.exists():
        with sqlite3.connect(path, timeout=10) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS conversation_models (conversation_id TEXT PRIMARY KEY, model TEXT NOT NULL)")
            conn.execute("DELETE FROM conversation_models WHERE conversation_id = ?", (conversation_id,))
    legacy = Path(config.MART_DIR) / "build" / "conversation_models.db"
    if legacy.exists():
        with sqlite3.connect(legacy, timeout=10) as conn:
            conn.execute("DELETE FROM choices WHERE conversation_id = ?", (conversation_id,))


def list_custom_models() -> list[dict]:
    """读取用户配置的模型，不返回密钥。"""
    path = _history_path()
    if not path.exists():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_models'").fetchone():
            return []
        has_connections = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_connections'").fetchone()
        if has_connections:
            rows = conn.execute("SELECT m.id, m.label, m.provider, m.model, m.enabled, c.base_url, c.revision "
                                "FROM custom_models m LEFT JOIN model_connections c ON m.id = c.model_id ORDER BY m.rowid").fetchall()
        else:
            rows = conn.execute("SELECT id, label, provider, model, enabled FROM custom_models ORDER BY rowid").fetchall()
    return [dict(row) | {"enabled": bool(row["enabled"])} for row in rows]


def builtin_id(choice: config.ModelChoice) -> str:
    """返回内置连接使用的稳定 ID。"""
    return 'qwen' if choice.provider == 'dashscope' else choice.name


def _builtin_models() -> dict[str, dict]:
    default = config.default_model()
    presets = config.model_choices()
    label = next((choice.label for choice in presets if choice.name == default and choice.provider == config.LLM_PROVIDER),
                 default)
    first = config.ModelChoice(default, label, config.LLM_PROVIDER)
    choices = [first, *(choice for choice in presets if builtin_id(choice) != builtin_id(first))]
    return {builtin_id(choice): {'id': builtin_id(choice), 'provider': choice.provider,
                                'label': choice.label, 'model': choice.name, 'enabled': True, 'builtin': True}
            for choice in choices}


def model_catalog(*, selectable: bool = False) -> list[dict]:
    """合并内置、编辑后的连接和自定义模型，排除已删除的内置项。"""
    builtins = _builtin_models()
    visible = {builtin_id(choice) for choice in config.available_models()} if selectable else builtins.keys()
    saved = {item['id']: item for item in list_custom_models()}
    deleted = _deleted_builtins()
    catalog = []
    for model_id, original in builtins.items():
        if model_id in deleted:
            continue
        if model_id in saved:
            item = saved.pop(model_id) | {'builtin': True}
            if not selectable or model_route(item):
                catalog.append(item)
        elif model_id in visible:
            if model_id == 'qwen':
                model = get_qwen()
                original.update(model=model, label=dashscope_label(model))
            catalog.append(original | {'original': True})
    catalog.extend(item | {'builtin': False} for item in saved.values()
                   if item['id'] not in deleted and (not selectable or model_route(item)))
    return catalog


def _deleted_builtins() -> set[str]:
    path = _history_path()
    if not path.exists():
        return set()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_builtin_models'").fetchone():
            return set()
        return {row[0] for row in conn.execute("SELECT model_id FROM deleted_builtin_models")}


def default_model_id() -> str:
    """返回原有服务端默认模型入口 ID。"""
    return 'qwen' if config.LLM_PROVIDER == 'dashscope' else config.default_model()


def query_models() -> list[dict]:
    """列出当前可用于查数的模型，受控模式只列批准的入口。"""
    items = model_catalog(selectable=True)
    if config.ACCESS_CONTROL_ENABLED:
        return [item for item in items if item["id"] in config.ACCESS_MODEL_IDS]
    return items


def set_default_model(model_id: str) -> None:
    """保存当前可用的模型入口为默认值。"""
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not any(item["id"] == model_id for item in query_models()):
            raise ValueError("请选择已启用且可用于查数的模型")
        conn.execute("CREATE TABLE IF NOT EXISTS model_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO model_settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (DEFAULT_MODEL_KEY, model_id))


def delete_model(model_id: str) -> None:
    """删除配置和凭据，已删除的内置项不再重新生成。"""
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    builtin = model_id in _builtin_models()
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _create_model_tables(conn)
        if conn.execute("SELECT 1 FROM deleted_builtin_models WHERE model_id=?", (model_id,)).fetchone():
            raise LookupError("模型配置不存在")
        deleted = conn.execute("DELETE FROM custom_models WHERE id=?", (model_id,)).rowcount
        if not builtin and not deleted:
            raise LookupError("模型配置不存在")
        conn.execute("DELETE FROM model_connections WHERE model_id=?", (model_id,))
        if builtin:
            conn.execute("INSERT INTO deleted_builtin_models VALUES (?)", (model_id,))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_settings'").fetchone():
            conn.execute("DELETE FROM model_settings WHERE key=? AND value=?", (DEFAULT_MODEL_KEY, model_id))


def _create_model_tables(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS custom_models (id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, enabled INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS model_connections (model_id TEXT PRIMARY KEY, base_url TEXT NOT NULL, credential BLOB NOT NULL, revision TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS deleted_builtin_models (model_id TEXT PRIMARY KEY)")


def model_route(item: dict) -> str | None:
    """返回可用模型的内部路由。"""
    if not item['enabled']:
        return None
    if item['provider'] == 'custom':
        if item.get('revision') and _credential_file().is_file():
            return f"@custom:{item['id']}:{item['revision']}:{item['model']}"
    elif config._api_key(item['provider']):
        return f"@{item['provider']}:{item['model']}"
    return None


def default_choice(items: list[dict]) -> dict | None:
    """优先使用保存的默认入口，不可用时沿用原有选择规则。"""
    saved = _read_value("model_settings", "SELECT value FROM model_settings WHERE key=?", (DEFAULT_MODEL_KEY,))
    by_id = {item["id"]: item for item in items}
    for model_id in (saved, default_model_id()):
        if model_id in by_id:
            return by_id[model_id]
    return next((item for item in items if item['provider'] == 'custom'), items[0] if items else None)


def _credential_file() -> Path:
    return _history_path().with_suffix('.model-credentials.key')


def _cipher(*, create: bool = False):
    from cryptography.fernet import Fernet

    path = _credential_file()
    if create and not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            temporary = Path(tmp.name)
            tmp.write(Fernet.generate_key())
        try:
            # 原子发布完整密钥，多个工作进程同时创建时只采用第一份。
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    return Fernet(path.read_bytes())


def _decrypt_api_key(credential: bytes) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _cipher().decrypt(credential).decode('utf-8')
    except (OSError, ValueError, InvalidToken):
        raise ValueError("模型密钥无法读取，请重新填写 API Key") from None


def saved_test_api_key(model_id: str | None, base_url: str) -> str:
    """测试已有自定义配置时，只向原地址使用已保存的密钥。"""
    if model_id:
        credential = _read_value("model_connections", "SELECT c.credential FROM model_connections c "
                                 "JOIN custom_models m ON m.id=c.model_id WHERE m.id=? AND m.provider='custom' "
                                 "AND c.base_url=?", (model_id, base_url))
        if isinstance(credential, bytes):
            return _decrypt_api_key(credential)
    raise ValueError("新增模型或更换接口地址时，请填写 API Key")


def custom_model_connection(model_id: str, revision: str, model: str) -> dict[str, str]:
    """只在服务端解密本轮模型连接，校验配置版本与查询一致。"""
    path = _history_path()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10) as conn:
        row = conn.execute("SELECT c.base_url, c.credential FROM model_connections c JOIN custom_models m ON m.id=c.model_id "
                           "WHERE m.id=? AND c.revision=? AND m.model=? AND m.provider='custom' AND m.enabled=1",
                           (model_id, revision, model)).fetchone()
    if row is None:
        raise ValueError("模型配置已变化，请重新提交")
    return {'base_url': row[0], 'api_key': _decrypt_api_key(row[1])}


def save_custom_model(label: str, provider: str, model: str, enabled: bool, model_id: str | None = None,
                      *, base_url: str | None = None, api_key: str | None = None) -> dict:
    """新增或更新自定义模型，服务商连接和密钥由服务端配置。"""
    model = model.strip()
    label = label.strip()
    if model_id == 'qwen' and provider == 'dashscope' and label in {'千问', '百炼'}:
        label = dashscope_label(model)
    label = label or model[:60]
    if not label or len(label) > 60:
        raise ValueError("模型名称不能为空且不能超过 60 个字符")
    if provider not in config.PROVIDERS and provider != 'custom':
        raise ValueError("不支持的服务商")
    if not config.valid_model_code(model):
        raise ValueError("请填写服务商提供的有效模型名称")
    if provider == 'custom':
        base_url = normalize_base_url(base_url or '')
        api_key = normalize_api_key(api_key or '')
    elif base_url or api_key:
        raise ValueError("请选择自定义接口以填写 Base URL 和 API Key")
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _create_model_tables(conn)
        if model_id is None:
            model_id = 'custom-' + uuid.uuid4().hex
            conn.execute("INSERT INTO custom_models VALUES (?, ?, ?, ?, ?)", (model_id, label, provider, model, int(enabled)))
        else:
            cursor = conn.execute("UPDATE custom_models SET label=?, provider=?, model=?, enabled=? WHERE id=?", (label, provider, model, int(enabled), model_id))
            if cursor.rowcount != 1:
                if model_id not in _builtin_models() or conn.execute(
                    "SELECT 1 FROM deleted_builtin_models WHERE model_id=?", (model_id,)
                ).fetchone():
                    raise LookupError("模型配置不存在")
                conn.execute("INSERT INTO custom_models VALUES (?, ?, ?, ?, ?)",
                             (model_id, label, provider, model, int(enabled)))
        if provider == 'custom':
            previous = conn.execute("SELECT base_url, credential FROM model_connections WHERE model_id=?", (model_id,)).fetchone()
            if api_key:
                credential = _cipher(create=True).encrypt(api_key.encode('utf-8'))
            elif previous and previous[0] == base_url and _credential_file().is_file():
                credential = previous[1]
            else:
                raise ValueError("新增模型或更换接口地址时，请填写 API Key")
            conn.execute("INSERT INTO model_connections VALUES (?, ?, ?, ?) ON CONFLICT(model_id) DO UPDATE SET "
                         "base_url=excluded.base_url, credential=excluded.credential, revision=excluded.revision",
                         (model_id, base_url, credential, uuid.uuid4().hex))
        else:
            conn.execute("DELETE FROM model_connections WHERE model_id=?", (model_id,))
    return {"id": model_id, "label": label, "provider": provider, "model": model, "enabled": enabled,
            "base_url": base_url if provider == 'custom' else None, "key_configured": provider == 'custom',
            "builtin": model_id in _builtin_models()}
