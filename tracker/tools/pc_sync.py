#!/usr/bin/env python3
"""Учёт работы с Claude Code на этом компьютере для трекера.

Читает локальные журналы Claude Code (~/.claude/projects/**/*.jsonl) и считает
по дням и часам (время московское):
  * время — по реальным сообщениям: промежутки между соседними сообщениями
    одной сессии складываются, если они не длиннее 15 минут;
  * токены — по каждому ответу модели (input, output, cache read, cache write).
Итог шифруется ключом трекера и записывается в tracker/pc.enc рядом со страницей,
затем делается commit и push (если не указан --no-push).

Ключ: переменная окружения TRACKER_KEY или файл ~/.claude-tracker-key.

    python tracker/tools/pc_sync.py             # посчитать, зашифровать, отправить
    python tracker/tools/pc_sync.py --dry-run   # только показать итоги
    python tracker/tools/pc_sync.py --no-push   # записать pc.enc без commit/push

Claude Code по умолчанию хранит журналы около 30 дней, поэтому история
с компьютера начинается с самого старого сохранённого журнала.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

GAP = timedelta(minutes=15)
MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tracker" / "pc.enc"


def _key() -> bytes:
    raw = os.environ.get("TRACKER_KEY", "").strip()
    if not raw:
        path = Path.home() / ".claude-tracker-key"
        if path.exists():
            raw = path.read_text().strip()
    if not raw:
        sys.exit("Нет ключа: задайте TRACKER_KEY или положите ключ в ~/.claude-tracker-key")
    key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if len(key) != 32:
        sys.exit("Ключ должен быть 43 символа из ссылки трекера (после #k=)")
    return key


def _projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _prompt_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        text = ""
    text = " ".join(text.split())
    return "" if not text or text.startswith("<") else text[:90]


def collect(projects: Path):
    sessions: dict[str, dict] = {}
    activity: dict[str, list[datetime]] = defaultdict(list)
    replies: dict[str, tuple] = {}
    for path in projects.rglob("*.jsonl"):
        try:
            lines = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get("type")
                if kind not in ("user", "assistant"):
                    continue
                moment = _ts(row.get("timestamp"))
                sid = row.get("sessionId")
                if not moment or not sid:
                    continue
                info = sessions.setdefault(sid, {"first": moment, "last": moment, "project": "", "title": ""})
                info["first"] = min(info["first"], moment)
                info["last"] = max(info["last"], moment)
                if row.get("cwd") and not info["project"]:
                    info["project"] = Path(row["cwd"]).name
                message = row.get("message") or {}
                if kind == "user" and not row.get("isSidechain") and not row.get("isMeta") and not info["title"]:
                    info["title"] = _prompt_text(message)
                activity[sid].append(moment)
                if kind == "assistant" and isinstance(message.get("usage"), dict):
                    mid = message.get("id") or row.get("uuid")
                    replies[mid] = (sid, moment, message["usage"])
    return sessions, activity, replies


def aggregate(sessions, activity, replies):
    buckets: dict[tuple, dict] = defaultdict(lambda: {"m": 0.0, "i": 0, "o": 0, "cr": 0, "cw": 0, "n": 0})

    def slot(sid: str, moment: datetime) -> dict:
        local = moment.astimezone(MSK)
        return buckets[(sid, local.strftime("%Y-%m-%d"), local.hour)]

    for sid, moments in activity.items():
        moments.sort()
        for a, b in zip(moments, moments[1:]):
            if b - a <= GAP:
                slot(sid, a)["m"] += (b - a).total_seconds() / 60
    for sid, moment, usage in replies.values():
        rec = slot(sid, moment)
        rec["i"] += int(usage.get("input_tokens") or 0)
        rec["o"] += int(usage.get("output_tokens") or 0)
        rec["cr"] += int(usage.get("cache_read_input_tokens") or 0)
        rec["cw"] += int(usage.get("cache_creation_input_tokens") or 0)
        rec["n"] += 1
    records = []
    for (sid, day, hour), rec in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][2])):
        if rec["m"] < 0.05 and not rec["n"]:
            continue
        records.append({"s": sid, "d": day, "h": hour, "m": round(rec["m"], 1),
                        "i": rec["i"], "o": rec["o"], "cr": rec["cr"], "cw": rec["cw"], "n": rec["n"]})
    used = {r["s"] for r in records}
    info = {sid: {"first": s["first"].strftime("%Y-%m-%dT%H:%M:%SZ"), "last": s["last"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "project": s["project"], "title": s["title"]}
            for sid, s in sessions.items() if sid in used}
    return records, info


def encrypt(key: bytes, data: dict) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    plain = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plain, None)).decode() + "\n"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def main() -> None:
    dry = "--dry-run" in sys.argv
    push = "--no-push" not in sys.argv and not dry
    projects = _projects_dir()
    if not projects.exists():
        sys.exit(f"Не нашёл журналы Claude Code: {projects}")
    key = None if dry else _key()
    if push:
        git("pull", "-q", "--rebase", "--autostash", "origin", "main")
    records, info = aggregate(*collect(projects))
    minutes = sum(r["m"] for r in records)
    tokens = sum(r["i"] + r["o"] + r["cr"] + r["cw"] for r in records)
    days = sorted({r["d"] for r in records})
    span = f"{days[0]} — {days[-1]}" if days else "нет данных"
    print(f"сессий {len(info)}, дней {len(days)} ({span}), время {minutes / 60:.1f} ч, токенов {tokens:,}".replace(",", " "))
    if dry:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.write_text(encrypt(key, {"generated_at": now, "records": records, "sessions": info}))
    if not push:
        print(f"записано: {OUT}")
        return
    git("add", str(OUT.relative_to(ROOT)))
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("изменений нет")
        return
    day = datetime.now(MSK).strftime("%Y-%m-%d")
    git("commit", "-q", "-m", f"Трекер: данные с компьютера за {day}")
    result = git("push", "-q", "origin", "main")
    print("отправлено" if result.returncode == 0 else "не удалось отправить: " + result.stderr.strip())


if __name__ == "__main__":
    main()
