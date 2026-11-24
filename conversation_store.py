"""会话历史持久化：每个会话一个 JSON 文件，只存模型上下文消息。"""
import json
import os
import re
import threading
import time
import uuid

import config

_STORE_LOCK = threading.RLock()


def _dir():
    path = config.CONVERSATIONS_DIR
    os.makedirs(path, exist_ok=True)
    return path


def _path(cid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", str(cid or "")):
        return None
    return os.path.join(_dir(), cid + ".json")


def new_id():
    return "c%s%s" % (int(time.time() * 1000), uuid.uuid4().hex[:8])


def read(cid):
    path = _path(cid)
    if not path or not os.path.isfile(path):
        return {}
    try:
        with _STORE_LOCK:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def load(cid):
    return read(cid).get("messages", [])


HISTORY_MAX_RECORDS = 300


def _write(cid, data):
    path = _path(cid)
    if not path:
        return False
    tmp = path + ".tmp"
    try:
        with _STORE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def save(cid, messages, title="", display=None, events=None):
    with _STORE_LOCK:
        old = read(cid)
        data = {"id": cid, "title": str(title or old.get("title") or "")[:60],
                "updated_at": time.time(), "messages": messages or [],
                "display": display if display is not None else old.get("display", []),
                "events": events if events is not None else old.get("events", []),
                "records": old.get("records", [])}
        return _write(cid, data)


def append_record(cid, record):
    """把一条界面记录追加到同一会话流水，和 messages 一起持久化。"""
    with _STORE_LOCK:
        data = read(cid)
        records = data.get("records") or []
        records.append(record)
        data["records"] = records[-HISTORY_MAX_RECORDS:]
        data["updated_at"] = time.time()
        return _write(cid, data)


def _record_from_message(message):
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
    text = str(content or "")
    if role == "user":
        return {"type": "user", "text": text}
    if role == "assistant":
        return {"type": "msg", "text": text} if text and text != "None" else None
    if role == "tool":
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            payload = {"message": text}
        return {"type": "result", "payload": payload}
    return None


def migrate_legacy(cid):
    """旧会话优先从模型上下文还原结构化流水，display 只保留文字内容。"""
    with _STORE_LOCK:
        data = read(cid)
        if data.get("records"):
            return data["records"]
        records = []
        for message in data.get("messages") or []:
            record = _record_from_message(message)
            if record:
                records.append(record)
        if not records:
            import re as _re
            display = data.get("display") or []
            if isinstance(display, list):
                for item in display:
                    text = _re.sub(r"<[^>]+>", " ", str(item))
                    text = _re.sub(r"\s+", " ", text).strip()
                    if text:
                        records.append({"type": "msg", "text": text[:1000]})
        if not records:
            return []
        data["records"] = records[-HISTORY_MAX_RECORDS:]
        _write(cid, data)
        return data["records"]


def _title_from(data):
    for record in data.get("records") or []:
        if isinstance(record, dict) and record.get("type") == "user":
            text = str(record.get("text") or "").strip()
            if text:
                return text[:60]
    for message in data.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "user":
            text = str(message.get("content") or "").strip()
            if text:
                return text[:60]
    return "新对话"


def delete(cid):
    path = _path(cid)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def list_all():
    out = []
    for name in os.listdir(_dir()):
        if not name.endswith(".json"):
            continue
        path = os.path.join(_dir(), name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            out.append({"id": data.get("id") or name[:-5],
                        "title": data.get("title") or _title_from(data),
                        "updated_at": data.get("updated_at") or 0})
        except (OSError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return out[:30]
