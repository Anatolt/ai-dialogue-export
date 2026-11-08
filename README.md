# ai-dialogue-export

Экспорт диалогов из локальных AI-харнесов в чистые читабельные логи чата (Markdown): только сообщения пользователя и ассистента, без логов запуска инструментов, результатов команд, reasoning, системных вставок и skill-документов.

Поддерживаемые источники (определяются автоматически по наличию хранилищ):

- **Codex** — `~/.codex/thread_history_1.sqlite` + `~/.codex/state_5.sqlite`
- **BB** — `~/.bb/bb.db`
- **Kimi Code** — `~/.kimi-code/sessions/*/session_*/agents/main/wire.jsonl`
- **OMP** — `~/.omp/agent/sessions/*/*.jsonl`
- **Pi** — `~/.pi/agent/sessions/*/*.jsonl`
- **OpenCode** — `~/.local/share/opencode/opencode.db`
- **Antigravity (Gemini)** — `~/.gemini/antigravity/conversations/*.db`

## Требования

- Python 3.10+, только стандартная библиотека.
- Все SQLite-базы открываются в read-only режиме; исходные данные не изменяются.

## Использование

```bash
# Пробная выгрузка 100 свежих диалогов (по умолчанию)
python3 export_dialogues.py --output dialogue-export-100

# Полная выгрузка
python3 export_dialogues.py --output dialogue-export-all --limit 0

# Только выбранные источники, сначала старые
python3 export_dialogues.py --source codex --source bb --order oldest --output out
```

Каталог `--output` должен быть новым или пустым.

## Результат

- `<output>/<source>/NNN-заголовок-id.md` — по одному файлу на диалог: метаданные (источник, ID, даты, проект) и последовательность реплик «Пользователь»/«Ассистент».
- `<output>/index.md` — общий оглавление-таблица со ссылками.
- `<output>/index.json` — машиночитаемый индекс со статистикой по каждому диалогу (число реплик, символов, байт, `possible_bb_duplicate` для вероятных нативных копий BB-диалогов в других харнесах).

## Приватность

Скрипт ничего никуда не отправляет: только локальное чтение и запись в указанный каталог. Выгруженные диалоги содержат вашу переписку — не публикуйте их.
