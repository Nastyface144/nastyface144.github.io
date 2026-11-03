#!/usr/bin/env python3
"""Обновляет зашифрованные данные трекера (tracker/data.enc).

Ключ берётся только из переменной окружения TRACKER_KEY (base64url, 32 байта)
и никогда не хранится в репозитории.

    TRACKER_KEY=... python3 tracker/tools/update.py patch.json
    TRACKER_KEY=... python3 tracker/tools/update.py --info   # когда было обновление и сколько сессий

patch.json — либо список сессий, либо объект
{"sessions": [...], "usage": [...], "digest": {"day": "YYYY-MM-DD", "text": "..."}}.

sessions — как их отдаёт list_sessions: {id, title, created_at, updated_at, origin,
session_status, status_bucket, unread, recent_action, needs_action, area?}.
Новые сессии добавляются, известные обновляются; area сохраняется, если её нет.

usage — расход токенов по ходам работы (из событий result):
{u: uuid события, s: id сессии, t: время события ISO, i: input, o: output,
 cr: cache read, cw: cache write, c: стоимость в $ по ценам API, n: число ходов}.
Записи с уже известным u не добавляются повторно.

digest — короткая сводка дня, перезаписывает прошлую за тот же день.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

DATA = Path(__file__).resolve().parent.parent / "data.enc"

AREAS = {
    "Telegram-боты": "create",
    "Сайты и лендинги": "create",
    "Программы и автоматизация": "create",
    "Учёба": "other",
    "Вопросы и идеи": "other",
}
STATUS = {
    "SESSION_STATUS_BUCKET_WORKING": "working",
    "SESSION_STATUS_BUCKET_BLOCKED": "blocked",
    "SESSION_STATUS_BUCKET_REVIEW_READY": "review",
    "SESSION_STATUS_BUCKET_COMPLETED": "completed",
    "SESSION_STATUS_BUCKET_FAILED": "failed",
}
LABEL = {
    "working": "Идёт сейчас",
    "blocked": "Ждёт вашего ответа",
    "review": "Готово к проверке",
    "completed": "Завершена",
    "failed": "Ошибка",
}


def _key() -> bytes:
    raw = os.environ.get("TRACKER_KEY", "").strip()
    if not raw:
        sys.exit("TRACKER_KEY не задан")
    key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if len(key) != 32:
        sys.exit("TRACKER_KEY должен быть 32 байта в base64url")
    return key


def _iso(value: str) -> str:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(key: bytes) -> dict:
    if not DATA.exists():
        return {"generated_at": None, "sessions": []}
    blob = base64.b64decode(DATA.read_text().strip())
    plain = AESGCM(key).decrypt(blob[:12], blob[12:], None)
    return json.loads(plain)


def save(key: bytes, data: dict) -> None:
    nonce = os.urandom(12)
    plain = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    DATA.write_text(base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plain, None)).decode() + "\n")


def normalize(raw: dict, old: dict | None) -> dict:
    status = STATUS.get(raw.get("status_bucket", ""), (old or {}).get("status", "completed"))
    area = raw.get("area") or (old or {}).get("area") or "Вопросы и идеи"
    if area not in AREAS:
        area = "Вопросы и идеи"
    return {
        "id": raw["id"],
        "title": raw.get("title") or (old or {}).get("title") or "Без названия",
        "area": area,
        "group": AREAS[area],
        "started": _iso(raw["created_at"]),
        "last_active": _iso(raw["updated_at"]),
        "origin": raw.get("origin", ""),
        "status": status,
        "status_label": LABEL[status],
        "archived": raw.get("session_status") == "SESSION_STATUS_ARCHIVED",
        "unread": bool(raw.get("unread")),
        "summary": raw.get("recent_action") or "",
        "todo": raw.get("needs_action") or "",
        "url": "https://claude.ai/code/" + raw["id"],
    }


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    key = _key()
    data = load(key)
    if sys.argv[1] == "--info":
        last = data.get("usage", [])[-1]["t"] if data.get("usage") else "—"
        print(f"обновлено {data['generated_at']}, сессий {len(data['sessions'])}, "
              f"последний учтённый ход {last}")
        return
    known = {s["id"]: s for s in data["sessions"]}
    patch = json.loads(Path(sys.argv[1]).read_text())
    if isinstance(patch, list):
        patch = {"sessions": patch}
    usage = {u["u"]: u for u in data.get("usage", [])}
    new_usage = 0
    for rec in patch.get("usage", []):
        if rec["u"] in usage:
            continue
        rec = {k: rec[k] for k in ("u", "s", "t", "i", "o", "cr", "cw", "c", "n") if k in rec}
        rec["t"] = _iso(rec["t"])
        usage[rec["u"]] = rec
        new_usage += 1
    digests = dict(data.get("digests", {}))
    if patch.get("digest"):
        digests[patch["digest"]["day"]] = patch["digest"]["text"].strip()
    added = 0
    for raw in patch.get("sessions", []):
        if raw["id"] not in known:
            added += 1
        known[raw["id"]] = normalize(raw, known.get(raw["id"]))
    sessions = sorted(known.values(), key=lambda s: s["last_active"], reverse=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = sorted(usage.values(), key=lambda r: r["t"])
    save(key, {
        "generated_at": now,
        "tokens_since": data.get("tokens_since") or (records[0]["t"][:10] if records else None),
        "sessions": sessions,
        "usage": records,
        "digests": digests,
    })
    waiting = sum(1 for s in sessions if s["status"] == "blocked")
    print(f"всего {len(sessions)}, ждут ответа {waiting}, новых {added}, "
          f"ходов с токенами +{new_usage}, обновлено {now}")


if __name__ == "__main__":
    main()
