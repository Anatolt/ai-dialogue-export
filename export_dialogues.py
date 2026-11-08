#!/usr/bin/env python3
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


INJECTED_BLOCK_RE = re.compile(
    r"<(?P<tag>system_instructions|system-reminder|skill-loaded)\b[^>]*>.*?</(?P=tag)\s*>",
    re.DOTALL,
)
SKILL_NOTICE_RE = re.compile(
    r"(?:^|\n)Skill tool loaded instructions for this request\. Follow them\.\s*",
    re.IGNORECASE,
)


@dataclass
class Message:
    role: str
    text: str
    timestamp_ms: int | None = None

    def __post_init__(self):
        self.text = SKILL_NOTICE_RE.sub("", INJECTED_BLOCK_RE.sub("", self.text)).strip()


@dataclass
class Dialogue:
    source: str
    id: str
    title: str = ""
    created_ms: int | None = None
    updated_ms: int | None = None
    project: str = ""
    source_path: str = ""
    provider: str = ""
    messages: list[Message] = field(default_factory=list)

    @property
    def bb_thread_id(self):
        if self.source == "bb":
            return self.id
        match = re.search(r"thr_[a-z0-9]+", f"{self.project} {self.source_path}")
        return match.group(0) if match else None


def parse_ms(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value * 1000) if value < 100_000_000_000 else int(value)
    try:
        return parse_ms(float(value))
    except ValueError:
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def iso_ms(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def file_times(path):
    stat = path.stat()
    created = getattr(stat, "st_birthtime", stat.st_ctime)
    return int(created * 1000), int(stat.st_mtime * 1000)


def text_content(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"].strip())
    return "\n\n".join(text for text in texts if text).strip()


def fallback_title(dialogue):
    if dialogue.title.strip():
        return dialogue.title.strip()
    for message in dialogue.messages:
        if message.role == "user":
            title = re.sub(r"\s+", " ", message.text).strip()
            return title[:119] + "…" if len(title) > 120 else title
    return dialogue.id


def open_db(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_codex(home):
    history_path = home / ".codex/thread_history_1.sqlite"
    state_path = home / ".codex/state_5.sqlite"
    if not history_path.exists():
        return []
    dialogues = {}
    if state_path.exists():
        con = open_db(state_path)
        con.row_factory = sqlite3.Row
        for row in con.execute("""
            SELECT id, title, cwd, rollout_path, created_at, created_at_ms,
                   updated_at, updated_at_ms, recency_at_ms
            FROM threads
        """):
            created = row["created_at_ms"] or parse_ms(row["created_at"])
            updated = row["updated_at_ms"] or row["recency_at_ms"] or parse_ms(row["updated_at"])
            dialogues[row["id"]] = Dialogue(
                "codex", row["id"], row["title"] or "", parse_ms(created), parse_ms(updated),
                row["cwd"] or "", row["rollout_path"] or "",
            )
        con.close()
    con = open_db(history_path)
    con.row_factory = sqlite3.Row
    seen = set()
    for table in ("thread_items", "thread_realtime_items"):
        for row in con.execute(f"""
            SELECT thread_id, item_id, item_type, item_json, rollout_ordinal, created_at_ms
            FROM {table}
            WHERE item_type IN ('userMessage', 'agentMessage')
        """):
            key = (row["thread_id"], row["item_id"])
            if key in seen:
                continue
            seen.add(key)
            dialogue = dialogues.setdefault(row["thread_id"], Dialogue("codex", row["thread_id"]))
            try:
                item = json.loads(row["item_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if row["item_type"] == "userMessage":
                role, text = "user", text_content(item.get("content"))
            else:
                role, text = "assistant", str(item.get("text") or "").strip()
            if text:
                dialogue.messages.append(Message(role, text, parse_ms(row["created_at_ms"])))
                dialogue._order = getattr(dialogue, "_order", [])
                dialogue._order.append((row["rollout_ordinal"] or 0, parse_ms(row["created_at_ms"]) or 0, len(dialogue.messages)))
            timestamp = parse_ms(row["created_at_ms"])
            if timestamp:
                dialogue.created_ms = min(filter(None, [dialogue.created_ms, timestamp]), default=None)
                dialogue.updated_ms = max(filter(None, [dialogue.updated_ms, timestamp]), default=None)
    con.close()
    for dialogue in dialogues.values():
        order = getattr(dialogue, "_order", [])
        if order:
            dialogue.messages = [dialogue.messages[index - 1] for _, _, index in sorted(order)]
            del dialogue._order
    return list(dialogues.values())


def load_jsonl_sessions(home, source, root):
    dialogues = []
    if not root.exists():
        return dialogues
    for path in root.glob("*/*.jsonl"):
        created_file, updated_file = file_times(path)
        dialogue = Dialogue(source, path.stem, created_ms=created_file, updated_ms=updated_file, source_path=str(path))
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                event_time = parse_ms(event.get("timestamp")) or parse_ms(event.get("time"))
                if event_type == "session":
                    dialogue.id = event.get("id") or dialogue.id
                    dialogue.project = event.get("cwd") or ""
                    dialogue.created_ms = event_time or dialogue.created_ms
                elif event_type in ("title", "title_change") and event.get("title"):
                    dialogue.title = event["title"]
                elif event_type == "message":
                    raw = event.get("message") or {}
                    role = raw.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    text = text_content(raw.get("content"))
                    timestamp = parse_ms(raw.get("timestamp")) or event_time
                    if text:
                        dialogue.messages.append(Message(role, text, timestamp))
                if event_time:
                    dialogue.updated_ms = max(filter(None, [dialogue.updated_ms, event_time]), default=None)
        dialogues.append(dialogue)
    return dialogues


def load_kimi(home):
    root = home / ".kimi-code/sessions"
    dialogues = []
    if not root.exists():
        return dialogues
    for path in root.glob("*/session_*/agents/main/wire.jsonl"):
        created_file, updated_file = file_times(path)
        dialogue = Dialogue(
            "kimi", path.parents[2].name, created_ms=created_file, updated_ms=updated_file,
            project=path.parents[3].name, source_path=str(path),
        )
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_time = parse_ms(event.get("time")) or parse_ms(event.get("created_at"))
                if event_time:
                    dialogue.created_ms = min(filter(None, [dialogue.created_ms, event_time]), default=None)
                    dialogue.updated_ms = max(filter(None, [dialogue.updated_ms, event_time]), default=None)
                if event.get("type") != "agent.message.appended":
                    continue
                raw = (event.get("message") or {}).get("message") or {}
                role = raw.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = text_content(raw.get("content"))
                if text:
                    dialogue.messages.append(Message(role, text, event_time))
        dialogues.append(dialogue)
    return dialogues


def load_bb(home):
    path = home / ".bb/bb.db"
    if not path.exists():
        return []
    con = open_db(path)
    con.row_factory = sqlite3.Row
    dialogues = {}
    for row in con.execute("""
        SELECT t.id, t.title, t.title_fallback, t.provider_id, t.created_at, t.updated_at,
               e.path AS environment_path, p.name AS project_name
        FROM threads t
        LEFT JOIN environments e ON e.id = t.environment_id
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.deleted_at IS NULL AND t.storage_deleted_at IS NULL
    """):
        dialogues[row["id"]] = Dialogue(
            "bb", row["id"], row["title"] or row["title_fallback"] or "",
            parse_ms(row["created_at"]), parse_ms(row["updated_at"]),
            row["environment_path"] or row["project_name"] or "", str(path), row["provider_id"] or "",
        )
    for row in con.execute("""
        SELECT thread_id, source_kind, text, source_seq, created_at
        FROM thread_search_segments
        WHERE source_kind IN ('user_message', 'assistant_message')
        ORDER BY thread_id, source_seq, created_at
    """):
        dialogue = dialogues.get(row["thread_id"])
        if dialogue is not None and row["text"]:
            role = "user" if row["source_kind"] == "user_message" else "assistant"
            dialogue.messages.append(Message(role, row["text"].strip(), parse_ms(row["created_at"])))
    con.close()
    return list(dialogues.values())


def load_opencode(home):
    path = home / ".local/share/opencode/opencode.db"
    if not path.exists():
        return []
    con = open_db(path)
    con.row_factory = sqlite3.Row
    dialogues = {}
    for row in con.execute("SELECT id, title, directory, time_created, time_updated FROM session"):
        dialogues[row["id"]] = Dialogue(
            "opencode", row["id"], row["title"] or "", parse_ms(row["time_created"]),
            parse_ms(row["time_updated"]), row["directory"] or "", str(path),
        )
    messages = {}
    for row in con.execute("SELECT id, session_id, data, time_created FROM message"):
        if row["session_id"] not in dialogues:
            continue
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        role = data.get("role")
        if role in ("user", "assistant"):
            messages[row["id"]] = (row["session_id"], role, parse_ms(row["time_created"]))
    for row in con.execute("SELECT message_id, data, time_created FROM part ORDER BY time_created"):
        if row["message_id"] not in messages:
            continue
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        if data.get("type") != "text" or not data.get("text"):
            continue
        session_id, role, message_time = messages[row["message_id"]]
        dialogues[session_id].messages.append(Message(
            role, data["text"].strip(), parse_ms(row["time_created"]) or message_time,
        ))
    con.close()
    return list(dialogues.values())


def read_varint(data, offset):
    value = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
        shift += 7
        if shift > 70:
            raise ValueError("invalid varint")
    raise ValueError("truncated varint")


def proto_fields(data):
    fields = []
    offset = 0
    while offset < len(data):
        key, offset = read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if not number or wire not in (0, 1, 2, 5):
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value, offset = read_varint(data, offset)
        elif wire == 1:
            value, offset = data[offset:offset + 8], offset + 8
        elif wire == 5:
            value, offset = data[offset:offset + 4], offset + 4
        else:
            size, offset = read_varint(data, offset)
            value, offset = data[offset:offset + size], offset + size
        if offset > len(data):
            raise ValueError("truncated protobuf field")
        fields.append((number, wire, value))
    return fields


def proto_values(data, path):
    values = [data]
    for wanted in path:
        next_values = []
        for value in values:
            try:
                fields = proto_fields(value)
            except ValueError:
                continue
            next_values.extend(raw for number, wire, raw in fields if number == wanted and wire == 2)
        values = next_values
    return values


def load_antigravity(home):
    root = home / ".gemini/antigravity/conversations"
    if not root.exists():
        return []
    dialogues = []
    for path in root.glob("*.db"):
        created, updated = file_times(path)
        dialogue = Dialogue("antigravity", path.stem, created_ms=created, updated_ms=updated, source_path=str(path))
        con = open_db(path)
        for step_type, payload in con.execute("SELECT step_type, step_payload FROM steps ORDER BY idx"):
            if step_type == 14:
                role, values = "user", proto_values(payload, (19, 2))
            elif step_type == 15:
                role, values = "assistant", proto_values(payload, (20, 1))
            else:
                continue
            texts = []
            for value in values:
                try:
                    text = value.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if text:
                    texts.append(text)
            if texts:
                dialogue.messages.append(Message(role, "\n\n".join(texts)))
        con.close()
        dialogues.append(dialogue)
    return dialogues


def markdown(dialogue):
    lines = [f"# {fallback_title(dialogue)}", ""]
    lines.append(f"- **Источник:** {dialogue.source}")
    if dialogue.provider:
        lines.append(f"- **Провайдер BB:** {dialogue.provider}")
    lines.append(f"- **ID:** `{dialogue.id}`")
    lines.append(f"- **Создан:** {iso_ms(dialogue.created_ms) or 'неизвестно'}")
    lines.append(f"- **Обновлён:** {iso_ms(dialogue.updated_ms) or 'неизвестно'}")
    if dialogue.project:
        lines.append(f"- **Проект/каталог:** `{dialogue.project}`")
    lines.extend(["", "---", ""])
    for message in dialogue.messages:
        lines.extend([f"## {'Пользователь' if message.role == 'user' else 'Ассистент'}", "", message.text, ""])
    return "\n".join(lines).rstrip() + "\n"


def safe_name(value):
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE).strip().replace(" ", "-")
    return value[:70] or "dialogue"


def main():
    parser = argparse.ArgumentParser(description="Экспорт текстов user/assistant из локальных AI-харнесов без логов инструментов")
    parser.add_argument("--output", type=Path, default=Path("dialogue-export"))
    parser.add_argument("--limit", type=int, default=100, help="Число диалогов; 0 — все")
    parser.add_argument("--order", choices=("newest", "oldest"), default="newest")
    parser.add_argument("--source", action="append", choices=("codex", "pi", "omp", "antigravity", "bb", "opencode", "kimi"))
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit должен быть неотрицательным")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"Каталог {output} не пуст; укажите новый или пустой --output")
    output.mkdir(parents=True, exist_ok=True)
    home = Path.home()
    loaders = {
        "codex": load_codex,
        "pi": lambda home: load_jsonl_sessions(home, "pi", home / ".pi/agent/sessions"),
        "omp": lambda home: load_jsonl_sessions(home, "omp", home / ".omp/agent/sessions"),
        "antigravity": load_antigravity,
        "bb": load_bb,
        "opencode": load_opencode,
        "kimi": load_kimi,
    }
    selected_sources = args.source or list(loaders)
    all_dialogues = []
    errors = {}
    for source in selected_sources:
        try:
            all_dialogues.extend(loaders[source](home))
        except Exception as error:
            errors[source] = str(error)
            print(f"{source}: {error}", file=sys.stderr)
    for dialogue in all_dialogues:
        if re.match(r"^<(?:system_instructions|system-reminder|skill-loaded)\b", dialogue.title):
            dialogue.title = ""
        else:
            dialogue.title = SKILL_NOTICE_RE.sub("", INJECTED_BLOCK_RE.sub("", dialogue.title)).strip()
        dialogue.messages = [message for message in dialogue.messages if message.text]
    bb_ids = {dialogue.id for dialogue in all_dialogues if dialogue.source == "bb" and dialogue.messages}
    stats = {}
    for source in selected_sources:
        records = [dialogue for dialogue in all_dialogues if dialogue.source == source]
        stats[source] = {"discovered": len(records), "nonempty": sum(bool(d.messages) for d in records), "selected": 0, "markdown_bytes": 0}
    candidates = [dialogue for dialogue in all_dialogues if dialogue.messages]
    candidates.sort(key=lambda d: d.updated_ms or d.created_ms or 0, reverse=args.order == "newest")
    selected = candidates[:args.limit] if args.limit else candidates
    index_entries = []
    for number, dialogue in enumerate(selected, 1):
        dialogue.title = fallback_title(dialogue)
        relative = Path(dialogue.source) / f"{number:03d}-{safe_name(dialogue.title)}-{safe_name(dialogue.id)[:12]}.md"
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(markdown(dialogue), encoding="utf-8")
        size = destination.stat().st_size
        source_stats = stats[dialogue.source]
        source_stats["selected"] += 1
        source_stats["markdown_bytes"] += size
        bb_thread_id = dialogue.bb_thread_id
        index_entries.append({
            "number": number,
            "source": dialogue.source,
            "provider": dialogue.provider or None,
            "id": dialogue.id,
            "title": dialogue.title,
            "created_at": iso_ms(dialogue.created_ms),
            "updated_at": iso_ms(dialogue.updated_ms),
            "project": dialogue.project or None,
            "source_path": dialogue.source_path or None,
            "bb_thread_id": bb_thread_id,
            "possible_bb_duplicate": dialogue.source != "bb" and bb_thread_id in bb_ids,
            "message_count": len(dialogue.messages),
            "user_messages": sum(message.role == "user" for message in dialogue.messages),
            "assistant_messages": sum(message.role == "assistant" for message in dialogue.messages),
            "characters": sum(len(message.text) for message in dialogue.messages),
            "markdown_bytes": size,
            "file": str(relative),
        })
    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    index = {
        "generated_at": generated,
        "order": args.order,
        "limit": args.limit,
        "selected_count": len(selected),
        "discovered_count": len(all_dialogues),
        "nonempty_count": len(candidates),
        "sources": stats,
        "errors": errors,
        "dialogues": index_entries,
    }
    (output / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Экспорт диалогов", "",
        f"- Сформирован: {generated}",
        f"- Порядок: {'сначала новые' if args.order == 'newest' else 'сначала старые'}",
        f"- Выгружено: {len(selected)} из {len(candidates)} непустых диалогов",
        "- Содержимое: только текстовые сообщения пользователя и ассистента; инструменты, команды, reasoning и служебные события исключены",
        "", "| № | Источник | Обновлён | Сообщений | Байт | Диалог |",
        "|---:|---|---|---:|---:|---|",
    ]
    for entry in index_entries:
        title = entry["title"].replace("|", "\\|")
        lines.append(f"| {entry['number']} | {entry['source']} | {entry['updated_at'] or ''} | {entry['message_count']} | {entry['markdown_bytes']} | [{title}]({quote(entry['file'])}) |")
    (output / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Выгружено {len(selected)} диалогов в {output}")
    for source in selected_sources:
        item = stats[source]
        print(f"{source}: обнаружено {item['discovered']}, непустых {item['nonempty']}, выгружено {item['selected']}, Markdown {item['markdown_bytes']} байт")
    total_bytes = sum(entry["markdown_bytes"] for entry in index_entries)
    print(f"Всего Markdown: {total_bytes} байт")


if __name__ == "__main__":
    main()
