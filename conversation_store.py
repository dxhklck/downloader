"""会话历史持久化：每个会话一个 JSON 文件，只存模型上下文消息。"""
import json
import os
import re
import time

import config


def _dir():
    path = config.CONVERSATIONS_DIR
    os.makedirs(path, exist_ok=True)
    return path


def _path(cid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", str(cid or "")):
        return None
    return os.path.join(_dir(), cid + ".json")


def new_id():
    return "c%s" % int(time.time() * 1000)


def load(cid):
    path = _path(cid)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("messages", []) if isinstance(data, dict) else []
    except (OSError, ValueError, TypeError):
        return []


def save(cid, messages, title=""):
    path = _path(cid)
    if not path:
        return False
    data = {"id": cid, "title": str(title or "")[:60],
            "updated_at": time.time(), "messages": messages or []}
    tmp = path + ".tmp"
    try:
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
                        "title": data.get("title") or "未命名对话",
                        "updated_at": data.get("updated_at") or 0,
                        "messages": len(data.get("messages") or [])})
        except (OSError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return out[:30]
