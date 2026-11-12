"""读取仅用于界面展示的参考问法，不将其作为 SQL 样例或模型证据。"""
import json
from pathlib import Path

from metadata.store import MetadataStore


def question_suggestions(mart_dir: str, store: MetadataStore,
                         allowed: frozenset[str] | None = None) -> list[dict]:
    """从资料目录读取问法，只保留关联表均在同一授权业务域的项目。"""
    path = Path(mart_dir) / 'build' / 'suggested_questions.json'
    try:
        if not path.is_file() or path.stat().st_size > 262144:
            return []
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return []
    if not isinstance(payload, list):
        return []
    domains = {table.table_id: table.domain_code for table in store.tables()
               if allowed is None or table.table_id in allowed}
    seen: set[str] = set()
    result = []
    for item in payload[:200]:
        if not isinstance(item, dict):
            continue
        ident, text, tables = item.get('id'), item.get('question'), item.get('tables')
        if (not isinstance(ident, str) or not ident or len(ident) > 80 or ident in seen
                or not isinstance(text, str) or not text.strip() or len(text) > 200
                or not isinstance(tables, list) or not tables
                or not all(isinstance(table, str) and table in domains for table in tables)):
            continue
        table_domains = {domains[table] for table in tables}
        if len(table_domains) != 1 or not next(iter(table_domains)):
            continue
        seen.add(ident)
        result.append({'example_id': 'suggestion:' + ident, 'requirement': text.strip(),
                       'domain': next(iter(table_domains))})
    return result
