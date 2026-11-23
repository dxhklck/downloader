"""下载工具集：把 B站/抖音的下载能力封装成标准函数，供 LLM 和界面调用。

每个函数返回结构化 dict（success/message/data），不抛异常到调用层。
进度通过可选的 progress 回调推送（GUI 用它更新界面）。
"""
import os, sys, json, re, time, random, sqlite3, subprocess, urllib.request, threading, itertools
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import config
import logutil

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"

# 任务状态（全局，界面轮询用）
STATE = {"running": False, "task": "", "done": 0, "total": 0, "logs": [],
         "log_gen": 0, "cancelled": False, "episodes": {}, "attempts": {},
         "queue": [], "current_job": None, "recent_results": []}
STATE_LOCK = threading.Lock()

# 通用站点登录：后台线程持有浏览器，前端点"完成登录"置 done 标志
LOGIN = {"active": False, "done": False}
LOGIN_LOCK = threading.Lock()

# 全局 os.remove 曾被执行期猴补丁置空（绕过 WorkBuddy safe-delete 钩子），
# 后果是全进程所有删除静默失效。改为"受控挂起"：锁保护 + 必定恢复，
# 且只覆盖被复用的下载脚本调用期间，其它时刻 os.remove 保持正常。
_REMOVE_GUARD = threading.RLock()


def _suspend_remove():
    """短暂挂起全局 os.remove，返回原函数。调用方必须在 finally 中恢复。"""
    _REMOVE_GUARD.acquire()
    return os.remove


def _restore_remove(original):
    os.remove = original
    _REMOVE_GUARD.release()


def try_start(task):
    """原子地检查并占用任务槽：成功 True，已有任务 False。消除 TOCTOU。"""
    with STATE_LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, task=task, done=0, total=0, logs=[],
                     cancelled=False, episodes={}, attempts={})
        return True


def cancel_task():
    """请求取消当前任务。任务在下一个检查点安全退出。"""
    with STATE_LOCK:
        if not STATE["running"]:
            return _fail("当前没有运行中的任务")
        STATE["cancelled"] = True
    _log("已收到取消请求，将在当前步骤完成后停止")
    return _ok("已请求取消，任务将在当前步骤完成后停止")


def _cancelled():
    with STATE_LOCK:
        return STATE["cancelled"]


def set_episode(ep, status, attempt=None):
    """上报单集状态（pending/running/retrying/ok/skip/fail/cancel）。"""
    with STATE_LOCK:
        STATE["episodes"][str(ep)] = status
        if attempt is not None:
            STATE["attempts"][str(ep)] = attempt


def finish():
    """释放任务槽。"""
    with STATE_LOCK:
        STATE["running"] = False


def _log(msg):
    with STATE_LOCK:
        STATE["logs"].append(msg)
        if len(STATE["logs"]) > 200:
            STATE["logs"] = STATE["logs"][-200:]
    logutil.info(msg)


def get_state():
    with STATE_LOCK:
        return dict(STATE, logs=list(STATE["logs"]),
                    recent_results=list(STATE["recent_results"]))


def remember_result(result, tool_name=""):
    """记录最近工具结果，供下一轮 Agent 主动汇报；只存轻量摘要。"""
    if not isinstance(result, dict):
        return
    message = str(result.get("message") or "")[:240]
    if not message:
        return
    item = {
        "tool": str(tool_name or "")[:60],
        "success": bool(result.get("success")),
        "cancelled": bool(result.get("cancelled")),
        "message": message,
        "time": time.time(),
    }
    with STATE_LOCK:
        STATE["recent_results"].append(item)
        STATE["recent_results"] = STATE["recent_results"][-8:]


def clear_logs():
    """清空日志并递增代际号，供前端丢弃在途 poll 响应中的旧日志。"""
    with STATE_LOCK:
        STATE["logs"] = []
        STATE["log_gen"] += 1
        return {"ok": True, "log_gen": STATE["log_gen"]}


def set_queue_snapshot(queued, current=None):
    """由 Api 层调用：同步队列快照到 state，供前端展示。"""
    with STATE_LOCK:
        STATE["queue"] = [dict(x) for x in queued]
        STATE["current_job"] = dict(current) if current else None


def _set(**kw):
    with STATE_LOCK:
        STATE.update(kw)


def _ok(msg, **data):
    return {"success": True, "message": msg, "data": data}


def _fail(msg, error_code="tool_error", **data):
    payload = data.pop("data", {})
    payload.update(data)
    return {"success": False, "message": msg, "error_code": error_code,
            "retryable": True, "data": payload}


def _cookie_file_domains(path):
    """只读 Cookie 文件域名，不返回 Cookie 值。"""
    if not os.path.isfile(path):
        return []
    found = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    found.add(parts[0].lstrip(".").lower())
    except OSError:
        return []
    return sorted(found)


def _browser_cookies(profile):
    """从 Chromium 持久化 Profile 读取 (域名, 名称, 过期时间)，不读取 Cookie 值。"""
    cookies = []
    for db_name in ("Network/Cookies", "Cookies"):
        db = Path(profile) / db_name
        if not db.is_file():
            continue
        try:
            src = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
            try:
                rows = src.execute(
                    "select host_key, name, expires_utc from cookies where length(encrypted_value) > 0 or length(value) > 0"
                ).fetchall()
                cookies.extend(rows)
            finally:
                src.close()
            if cookies:
                break
        except (sqlite3.Error, OSError):
            continue
    return cookies


def _live_cookie_check(profile, domains, names):
    """按域名和关键 Cookie 名判断浏览器 Profile 是否仍有有效登录态。"""
    now_chromium = int((time.time() + 11644473600) * 1_000_000)
    for host, name, expires in _browser_cookies(profile):
        if expires and expires < now_chromium:
            continue
        h = str(host or "").lstrip(".").lower()
        n = str(name or "").lower()
        if any(h == d or h.endswith("." + d) for d in domains) and n in names:
            return True
    return False


def get_login_status():
    """返回真实登录态；只读 Cookie 元信息，不读取或回传 Cookie 值。"""
    checks = (
        ("bili", "B站", (config.COOKIE_BILI, config.COOKIES_FILE),
         ("bilibili.com",), {"sessdata", "bili_jct", "dedeuserid"},
         (config.PROFILE_SITE,)),
        ("douyin", "抖音", (config.COOKIE_DOUYIN, config.COOKIES_FILE),
         ("douyin.com",), {"sessionid", "sessionid_ss", "sid_tt", "passport_csrf_token"},
         (config.PROFILE_DOUYIN, config.PROFILE_SITE)),
        ("xhs", "小红书", (config.XHS_COOKIE_FILE, config.COOKIES_FILE),
         ("xiaohongshu.com", "xhslink.com"), {"web_session", "a1", "gid"},
         (config.PROFILE_XHS, config.PROFILE_SITE)),
    )
    result = {}
    for key, label, files, domains, names, profiles in checks:
        has_key = _live_cookie_check(profiles[0], domains, names) if profiles else False
        if not has_key:
            has_key = any(
                any(d in dom for dom in _cookie_file_domains(path) for d in domains)
                for path in files
            )
        result[key] = {"status": "ok" if has_key else "none", "label": label}
    site_domains = _cookie_file_domains(config.COOKIES_FILE)
    result["site"] = {"status": "ok" if site_domains else "none",
                      "label": "通用站点", "domains": site_domains[:8]}
    return result


def _safe_media_file(path):
    """校验完整路径是 ROOT 内的真实视频文件。"""
    if not isinstance(path, str) or not path.strip():
        return None
    try:
        root = os.path.realpath(config.ROOT)
        full = os.path.realpath(path)
        if os.path.commonpath([full, root]) != root or full == root:
            return None
        if not os.path.isfile(full):
            return None
        if not full.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm")):
            return None
        return full
    except (ValueError, OSError):
        return None


def _safe_dest_file(path):
    """校验目标文件位置必须在 ROOT 内，且不允许覆盖。"""
    if not isinstance(path, str) or not path.strip():
        return None
    try:
        root = os.path.realpath(config.ROOT)
        full = os.path.realpath(path)
        if os.path.commonpath([full, root]) != root or full == root:
            return None
        if not full.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm")):
            return None
        if os.path.exists(full):
            return None
        parent = os.path.dirname(full)
        if os.path.commonpath([parent, root]) != root:
            return None
        return full
    except (ValueError, OSError):
        return None


def _trash_dir():
    return os.path.join(config._APP_DIR, "asset_trash")


def _safe_agent_dir(rel):
    """把相对资产根目录的用户路径解析为安全目录，拒绝越界和非法字符。"""
    if not isinstance(rel, str) or not rel.strip():
        return None
    rel = rel.strip().replace("\\", "/")
    if os.path.isabs(rel) or ":" in rel or "\x00" in rel or ".." in rel.split("/"):
        return None
    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts or len(parts) > 8:
        return None
    for part in parts:
        if len(part) > 80:
            return None
        upper = part.upper()
        if upper in ("CON", "PRN", "AUX", "NUL"):
            return None
        if upper.rstrip("0123456789") in ("COM", "LPT") and upper.rstrip("0123456789") != upper:
            return None
        if any(ch in part for ch in '<>"|?*'):
            return None
    root = os.path.realpath(config.ROOT)
    full = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath([full, root]) != root or full == root:
        return None
    return full


def create_folder(path):
    """在资产根目录下创建文件夹；只允许相对安全路径，不做删除/移动。"""
    full = _safe_agent_dir(path)
    if not full:
        return _fail("路径无效：请给出资产根目录下的相对路径，不能包含 '..' 或盘符")
    try:
        existed = os.path.isdir(full)
        os.makedirs(full, exist_ok=True)
        return _ok("文件夹已存在" if existed else "文件夹已创建", path=full)
    except OSError as e:
        return _fail("创建文件夹失败：" + str(e)[:200], error_code="mkdir_failed")


def asset_stats():
    """返回资产统计摘要，不向模型暴露完整文件列表。"""
    import assets
    try:
        result = assets.scan(include_duration=False)
    except Exception as e:
        return _fail("资产扫描失败：" + str(e)[:200], error_code="asset_scan_failed")
    data = result.get("data", {})
    stats = data.get("stats", {})
    return _ok(result.get("message", "资产统计完成"),
               total=stats.get("total", 0), total_gb=stats.get("total_gb", 0),
               categories=stats.get("cats", {}), scanned_at=data.get("scanned_at"))


def asset_search(keyword=None, ext=None, category=None, min_size_mb=0, limit=20):
    """按条件筛选资产，结果最多20条，且只返回轻量字段。"""
    import assets
    try:
        result = assets.scan(include_duration=False)
        items = result.get("data", {}).get("items", [])
    except Exception as e:
        return _fail("资产扫描失败：" + str(e)[:200], error_code="asset_scan_failed")
    kw = str(keyword or "").strip().lower()
    ex = str(ext or "").strip().lower()
    cat = str(category or "").strip()
    try:
        min_size = max(0, float(min_size_mb)) * 1024 * 1024
    except (TypeError, ValueError):
        min_size = 0
    try:
        cap = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        cap = 20
    matched = []
    for it in items:
        if not str(it.get("path", "")).lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm")):
            continue
        if kw and kw not in it.get("name", "").lower() and kw not in it.get("rel", "").lower():
            continue
        if ex and not it.get("name", "").lower().endswith(ex if ex.startswith(".") else "." + ex):
            continue
        if cat and it.get("cat") != cat:
            continue
        if it.get("size", 0) < min_size:
            continue
        matched.append({
            "name": it.get("name"), "path": it.get("path"), "rel": it.get("rel"),
            "cat": it.get("cat"), "size": it.get("size"), "mtime": it.get("mtime"),
        })
        if len(matched) >= cap:
            break
    return _ok("找到 %d 个匹配视频" % len(matched), items=matched, limit=cap,
               total_scanned=len(items))


def scan_assets(force=False):
    """刷新资产库索引，只回灌统计摘要。"""
    import assets
    try:
        result = assets.scan(include_duration=True, force=bool(force))
    except Exception as e:
        return _fail("资产扫描失败：" + str(e)[:200], error_code="asset_scan_failed")
    stats = result.get("data", {}).get("stats", {})
    return _ok(result.get("message", "资产扫描完成"),
               total=stats.get("total", 0), total_gb=stats.get("total_gb", 0),
               categories=stats.get("cats", {}))


def reveal_asset(path):
    """在文件管理器中定位资产库内视频。"""
    import assets
    return assets.locate(path)


def move_asset(src, dest):
    """移动/重命名资产库内视频；目标必须仍在 ROOT 内且不覆盖。"""
    source = _safe_media_file(src)
    target = _safe_dest_file(dest)
    if not source:
        return _fail("源文件无效或不在资产目录内", error_code="bad_source")
    if not target:
        return _fail("目标无效、不在资产目录内或已存在（禁止覆盖）", error_code="bad_destination")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(source, target)
        return _ok("已移动", src=source, dest=target)
    except OSError as e:
        return _fail("移动失败：" + str(e)[:200], error_code="move_failed")


def copy_asset(src, dest):
    """复制资产库内视频；目标必须仍在 ROOT 内且不覆盖。"""
    import shutil
    source = _safe_media_file(src)
    target = _safe_dest_file(dest)
    if not source:
        return _fail("源文件无效或不在资产目录内", error_code="bad_source")
    if not target:
        return _fail("目标无效、不在资产目录内或已存在（禁止覆盖）", error_code="bad_destination")
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        return _ok("已复制", src=source, dest=target)
    except OSError as e:
        return _fail("复制失败：" + str(e)[:200], error_code="copy_failed")


def trash_asset(path):
    """移入应用内回收站，记录原路径；不做永久删除。"""
    import shutil, json, time
    source = _safe_media_file(path)
    if not source:
        return _fail("文件无效或不在资产目录内", error_code="bad_source")
    trash = _trash_dir()
    try:
        os.makedirs(trash, exist_ok=True)
        name = "%d_%s" % (int(time.time() * 1000), os.path.basename(source))
        dest = os.path.join(trash, name)
        meta = {"name": name, "src": source, "trashed_at": time.time()}
        with open(dest + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        shutil.move(source, dest)
        return _ok("已移入回收站，可恢复", name=name, src=source, trash=dest)
    except OSError as e:
        return _fail("移入回收站失败：" + str(e)[:200], error_code="trash_failed")


def list_trashed():
    """列出应用内回收站项目，只返回项目名、原路径和删除时间。"""
    trash = _trash_dir()
    if not os.path.isdir(trash):
        return _ok("回收站为空", items=[])
    items = []
    for name in sorted(os.listdir(trash)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(trash, name), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        items.append({"name": meta.get("name") or name[:-5],
                      "src": meta.get("src", ""),
                      "trashed_at": meta.get("trashed_at", 0)})
    return _ok("回收站共 %d 项" % len(items), items=items)


def restore_asset(name):
    """从应用内回收站恢复到原路径；目标被占用时拒绝覆盖。"""
    import shutil, json
    if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
        return _fail("回收站项目名无效", error_code="bad_name")
    trash = _trash_dir()
    item = os.path.join(trash, name)
    meta_path = item + ".json"
    if not os.path.isfile(item) or not os.path.isfile(meta_path):
        return _fail("回收站中找不到该项目", error_code="not_found")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        original = meta.get("src")
        if not isinstance(original, str) or os.path.exists(original):
            return _fail("原路径已不存在或已被占用，未覆盖", error_code="restore_blocked")
        root = os.path.realpath(config.ROOT)
        if os.path.commonpath([os.path.realpath(original), root]) != root:
            return _fail("原路径不在资产目录内，未恢复", error_code="restore_blocked")
        os.makedirs(os.path.dirname(original), exist_ok=True)
        shutil.move(item, original)
        os.remove(meta_path)
        return _ok("已从回收站恢复", path=original)
    except (OSError, ValueError) as e:
        return _fail("恢复失败：" + str(e)[:200], error_code="restore_failed")


_ORGANIZE_PREVIEWS = {}
_ORGANIZE_LOCK = threading.Lock()
_ORGANIZE_SEQ = itertools.count(1)


def organize_preview(rule, category=None, value=None, limit=200):
    """生成批量整理预览，不改动文件。"""
    import assets
    rule = str(rule or "").strip().lower()
    if rule not in ("ext", "date", "size", "category"):
        return _fail("规则只能是 ext/date/size/category", error_code="bad_rule")
    try:
        scan = assets.scan(include_duration=False)
        items = scan.get("data", {}).get("items", [])
    except Exception as e:
        return _fail("资产扫描失败：" + str(e)[:200], error_code="asset_scan_failed")
    try:
        cap = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        cap = 200
    if rule == "ext" and category:
        wanted = "." + str(category).lstrip(".")
        items = [i for i in items if str(i.get("name", "")).lower().endswith(wanted.lower())]
    if rule == "category" and category:
        items = [i for i in items if i.get("cat") == category]
    changes = []
    seen = set()
    root = os.path.realpath(config.ROOT)
    for it in items[:cap]:
        src = _safe_media_file(it.get("path", ""))
        if not src or src in seen:
            continue
        seen.add(src)
        name = os.path.basename(src)
        if rule == "ext":
            folder = str(category or "按格式").lstrip(".").upper() or "按格式"
        elif rule == "date":
            import datetime
            folder = datetime.datetime.fromtimestamp(it.get("mtime", 0)).strftime("%Y-%m")
        elif rule == "size":
            size = it.get("size", 0)
            folder = "大于1GB" if size >= 2**30 else ("100MB-1GB" if size >= 100 * 2**20 else "小于100MB")
        else:
            folder = str(it.get("cat") or "未分类")
        if value:
            folder = os.path.join(str(value).strip("/\\"), folder)
        dest = os.path.join(root, folder, name)
        parent = os.path.realpath(os.path.dirname(dest))
        if os.path.commonpath([parent, root]) != root or os.path.exists(dest):
            continue
        changes.append({"src": src, "dest": dest, "folder": folder})
    if not changes:
        return _fail("没有可整理的视频，可能目标已存在或规则无匹配", error_code="no_changes")
    preview_id = "p%s" % next(_ORGANIZE_SEQ)
    with _ORGANIZE_LOCK:
        _ORGANIZE_PREVIEWS[preview_id] = {"changes": changes, "expires": time.time() + 300}
        expired = [k for k, v in _ORGANIZE_PREVIEWS.items() if v["expires"] < time.time()]
        for k in expired:
            _ORGANIZE_PREVIEWS.pop(k, None)
    return _ok("已生成整理预览，共 %d 项" % len(changes), preview_id=preview_id,
               changes=[{"src": c["src"], "dest": c["dest"]} for c in changes[:20]],
               total=len(changes))


def organize_apply(preview_id):
    """按一次性预览批量执行；不做覆盖。"""
    with _ORGANIZE_LOCK:
        preview = _ORGANIZE_PREVIEWS.pop(str(preview_id), None)
    if not preview or preview["expires"] < time.time():
        return _fail("预览已过期或不存在，请重新生成", error_code="preview_expired")
    moved = []
    failed = []
    for change in preview["changes"]:
        try:
            parent = os.path.realpath(os.path.dirname(change["dest"]))
            root = os.path.realpath(config.ROOT)
            if os.path.commonpath([parent, root]) != root or os.path.exists(change["dest"]):
                failed.append({"src": change["src"], "error": "目标无效或已存在"})
                continue
            os.makedirs(parent, exist_ok=True)
            os.replace(change["src"], change["dest"])
            moved.append(change["dest"])
        except OSError as e:
            failed.append({"src": change["src"], "error": str(e)[:120]})
    return _ok("批量整理完成：成功 %d，失败 %d" % (len(moved), len(failed)),
               moved=moved[:20], failed=failed[:20], success_count=len(moved), fail_count=len(failed))


# ============ B站下载 ============

def _bili_sessdata():
    with open(config.COOKIE_BILI, encoding="utf-8") as f:
        for line in f:
            p = line.strip().split("\t")
            if len(p) >= 7 and p[5] == "SESSDATA":
                return "SESSDATA=" + p[6]
    raise RuntimeError("cookie 无 SESSDATA，请重新导出")


def _bili_eps():
    with open(config.EP_LIST, encoding="utf-8") as f:
        return json.load(f)["result"]["episodes"]


def _safe_name(s):
    return "".join(c for c in s if c not in '\\/:*?"<>|').strip()


def _bili_get_dash(ep, sess):
    url = (f'https://api.bilibili.com/pgc/player/web/playurl?bvid={ep["bvid"]}'
           f'&cid={ep["cid"]}&qn=112&fnval=16&fourk=1&fnver=0')
    dash = {}
    for attempt in range(5):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Referer": "https://www.bilibili.com", "Cookie": sess})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("code") != 0:
            raise RuntimeError(f"API code={d.get('code')}")
        dash = ((d.get("result") or {}).get("dash") or {})
        if dash.get("video") and dash.get("audio"):
            break
        time.sleep(1.5 * (attempt + 1))
    vs, aus = dash.get("video", []), dash.get("audio", [])
    if not vs or not aus:
        raise RuntimeError("无 DASH 流")
    # 严格选 1080P avc，避开大会员 4K（600MB+/集）
    cands = [x for x in vs if x.get("height", 0) == 1080 and "avc" in x.get("codecs", "")]
    if not cands:
        cands = [x for x in vs if x.get("height", 0) == 1080]
    if not cands:
        cands = [x for x in vs if x.get("height", 0) <= 1080 and "avc" in x.get("codecs", "")]
    if not cands:
        cands = vs
    v = max(cands, key=lambda x: x.get("bandwidth", 0))
    a = max(aus, key=lambda x: x.get("bandwidth", 0))
    return (v.get("baseUrl") or v.get("base_url"), a.get("baseUrl") or a.get("base_url"))


def _bili_fetch(url, path, sess):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.bilibili.com", "Cookie": sess})
    with urllib.request.urlopen(req, timeout=90) as r, open(path, "wb") as out:
        while True:
            c = r.read(1024 * 1024)
            if not c:
                break
            out.write(c)


def _bili_one(idx, eps, out_dir, sess):
    ep = eps[idx - 1]
    title = _safe_name(ep.get("title") or str(idx))
    path = os.path.join(out_dir, f"{idx:04d}_{title}.mp4")
    if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
        return "skip", 0
    vtmp = os.path.join(out_dir, f"{idx:04d}_{title}.v.mp4")
    atmp = os.path.join(out_dir, f"{idx:04d}_{title}.a.m4a")
    for attempt in range(1, 5):
        if _cancelled():
            return "cancel", "用户取消"
        set_episode(idx, "running" if attempt == 1 else "retrying", attempt)
        try:
            vu, au = _bili_get_dash(ep, sess)
            _bili_fetch(vu, vtmp, sess)
            _bili_fetch(au, atmp, sess)
            r = subprocess.run(
                [config.ffmpeg_exe(), "-y", "-loglevel", "error", "-i", vtmp, "-i", atmp,
                 "-c", "copy", "-movflags", "+faststart", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
            if r.returncode != 0:
                raise RuntimeError("ffmpeg: " + r.stderr[-150:])
            sz = os.path.getsize(path)
            if sz < 5_000_000:
                raise RuntimeError(f"文件过小 {sz}")
            return "ok", sz
        except Exception as e:
            if attempt == 4:
                return "fail", str(e)[:100]
            _log(f"第{idx}集第{attempt}次失败，准备重试：{str(e)[:100]}")
            time.sleep(2 ** (attempt - 1))
    return "fail", "unknown"


def bili_download(start, end, out_dir=None, workers=4, ep_path=None):
    """下载 B站番剧 [start,end] 集。返回结构化结果。"""
    try:
        start, end, workers = int(start), int(end), int(workers)
    except (TypeError, ValueError):
        return _fail("集数/并发数必须是整数")
    if start < 1 or end < start:
        return _fail(f"集数范围无效：{start}-{end}")
    workers = max(1, min(workers, 16))
    out_dir = out_dir or config.DIR_BILI
    try:
        sess = _bili_sessdata()
        eps = _bili_eps() if ep_path is None else json.load(
            open(ep_path, encoding="utf-8"))["result"]["episodes"]
    except Exception as e:
        return _fail(f"初始化失败：{e}")
    if end > len(eps):
        return _fail(f"结束集 {end} 超过总集数 {len(eps)}")
    if not try_start(f"B站 {start}-{end}"):
        return _fail("已有任务在运行，请等待完成")
    os.makedirs(out_dir, exist_ok=True)
    total = end - start + 1
    _set(total=total)
    _log(f"开始下载 B站 {start}-{end}，共 {total} 集，{workers} 并发")

    for i in range(start, end + 1):
        set_episode(i, "pending")

    def work(i):
        if _cancelled():
            set_episode(i, "cancel")
            return "cancel"
        st, info = _bili_one(i, eps, out_dir, sess)
        with STATE_LOCK:
            STATE["done"] += 1
            d = STATE["done"]
        set_episode(i, "cancel" if st == "cancel" else st)
        _log(f"[{d}/{total}] 第{i}集 {st}")
        return st

    ok = skip = fail = cancelled_count = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, i): i for i in range(start, end + 1)}
            for fut in as_completed(futs):
                st = fut.result()
                if st == "ok": ok += 1
                elif st == "skip": skip += 1
                elif st == "cancel": cancelled_count += 1
                else: fail += 1
    finally:
        finish()
    msg = f"B站下载{'已取消' if cancelled_count else '完成'}：成功 {ok}，跳过 {skip}，失败 {fail}" + (
        f"，取消 {cancelled_count}" if cancelled_count else "") + f"，输出 {out_dir}"
    _log(msg)
    return _ok(msg, ok=ok, skip=skip, fail=fail, out_dir=out_dir)


def bili_status():
    """查询番剧列表状态：总集数、已下集数、缺失区间。"""
    try:
        eps = _bili_eps()
    except Exception as e:
        return _fail(f"读取剧集列表失败：{e}")
    # 从默认目录数正片
    have = set()
    try:
        names = os.listdir(config.DIR_BILI)
    except OSError:
        return _fail(f"下载目录不存在：{config.DIR_BILI}")
    for f in names:
        m = re.match(r"^(\d{4})_", f)
        if m and f.endswith(".mp4") and not f.endswith(".v.mp4"):
            have.add(int(m.group(1)))
    n = len(eps)
    missing = [i for i in range(1, n + 1) if i not in have]
    rng = []
    if missing:
        s = p = missing[0]
        for i in missing[1:]:
            if i != p + 1:
                rng.append([s, p]); s = i
            p = i
        rng.append([s, p])
    return _ok(f"共 {n} 集，已下 {len(have)}，缺 {len(missing)}",
               total=n, have=len(have), missing=len(missing), ranges=rng)


# ============ 抖音下载 ============

def _dy_core():
    """返回自包含的抖音核心模块（core 包，随应用分发，不再依赖根目录脚本）。"""
    from core import dy_random as dr
    return dr


def _browser_channel_kwargs():
    """按平台返回浏览器启动参数列表（依次尝试）。

    Windows 优先系统 Edge（免打包 417MB 浏览器）；macOS/Linux 优先 Chrome，
    最后回退 Playwright 自带 Chromium（需 playwright install chromium）。
    """
    import sys
    if sys.platform == "win32":
        return [{"channel": "msedge"}, {}]
    return [{"channel": "chrome"}, {}]


def _launch_douyin_ctx(pw, user_agent):
    """启动抖音持久化上下文：优先系统 Edge（免打包 417MB 浏览器），失败回退 Playwright Chromium。"""
    common = dict(
        headless=False, user_agent=user_agent,
        viewport={"width": 1280, "height": 800},
        args=["--autoplay-policy=no-user-gesture-required",
              "--disable-blink-features=AutomationControlled", "--no-sandbox", "--mute-audio"])
    for kwargs in _browser_channel_kwargs():
        label = "系统 Edge" if kwargs.get("channel") == "msedge" else (
            "系统 Chrome" if kwargs.get("channel") == "chrome" else "Playwright Chromium")
        try:
            ctx = pw.chromium.launch_persistent_context(
                config.PROFILE_DOUYIN, **common, **kwargs)
            _log("浏览器：" + label)
            return ctx
        except Exception as e:
            _log(f"浏览器启动失败({label})：{str(e)[:60]}")
    raise RuntimeError("无法启动浏览器：系统浏览器与 Playwright Chromium 均不可用")


def _dy_login_check(ctx):
    """检测登录态；未登录时等待扫码。返回 (logged, page)。"""
    import time
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    body = page.locator("body").inner_text()[:300]
    if "登录" in body:
        _log("抖音未登录，请在弹出窗口扫码（最长5分钟）")
        deadline = time.time() + 300
        while time.time() < deadline:
            page.wait_for_timeout(5000)
            if "登录" not in page.locator("body").inner_text()[:300]:
                break
    logged = "登录" not in page.locator("body").inner_text()[:300]
    _log("抖音已登录" if logged else "抖音登录超时")
    return logged, page


def _dy_harvest(page, ctx, pool, scrolls=80, target=150):
    """首页推荐流滚轮扩池。"""
    def on_resp(resp):
        u = resp.url
        if "aweme" not in u and "feed" not in u:
            return
        try:
            body = resp.json()
        except Exception:
            return
        def walk(o):
            if isinstance(o, dict):
                aid = o.get("aweme_id")
                if aid and isinstance(o.get("desc"), str):
                    vid = o.get("video") or {}
                    dur = vid.get("duration") or o.get("duration") or 0
                    try: dur = int(dur)
                    except Exception: dur = 0
                    if aid not in pool:
                        pool[aid] = {"desc": o.get("desc") or "", "duration": dur,
                                     "author": ((o.get("author") or {}).get("nickname") or "")}
                for v in o.values(): walk(v)
            elif isinstance(o, list):
                for v in o: walk(v)
        walk(body)
    page.on("response", on_resp)
    page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    for i in range(scrolls):
        if len(pool) >= target:
            break
        try:
            page.mouse.wheel(0, 2400)
        except Exception:
            pass
        page.wait_for_timeout(1400)
        if (i + 1) % 10 == 0:
            _log(f"扩池 {i+1} 次，候选 {len(pool)}")
    return pool


def _dy_ensure_audio(ctx, path, aid):
    """下载后补音轨。抓 media-audio 流 + ffmpeg 合成。多轮重试提高成功率。"""
    from core import dy_audio as dfa
    if dfa.has_audio(path):
        return True, "已有音轨"
    orig = None
    last = "未尝试"
    try:
        # mux 的 finally 会用 os.remove 清理临时文件，WorkBuddy hook 下可能抛 WinError 1455。
        # 受控挂起，函数返回前必定恢复，不影响 clean_temp 等其它删除操作。
        orig = _suspend_remove()
        os.remove = lambda p: None
        for attempt in range(3):  # 音轨抓取偶发失败，重试 2 次
            try:
                data, info = dfa.grab_audio(ctx, aid)
            except Exception as e:
                data, info = None, f"抓取异常({str(e)[:50]})"
            last = info
            if not data:
                _log(f"补音轨第 {attempt+1} 次未抓到流（{info}），重试")
                continue
            good, err = dfa.mux(path, data)
            if good:
                return True, f"音轨已合成(第{attempt+1}次)"
            last = f"合成失败({err})"
        return False, last
    except Exception as e:
        return False, f"补音轨异常({str(e)[:60]})"
    finally:
        if orig is not None:
            _restore_remove(orig)


def douyin_random(count, out_dir=None, keywords=None, min_sec=5, max_sec=360):
    """随机刷抖音下载。keywords 为 None 则完全随机；否则筛描述含关键词的。"""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return _fail("下载数量必须是整数")
    count = max(1, min(count, 200))  # 防 LLM 幻觉出大数
    if not try_start(f"抖音随机 {count}"):
        return _fail("已有任务在运行")
    dr = _dy_core()
    out_dir = out_dir or os.path.join(config.DIR_DOUYIN, "随机")
    os.makedirs(out_dir, exist_ok=True)
    dr.OUT_DIR = out_dir
    dr.MAX_BYTES = 200 * 1024 * 1024
    # 受控挂起 os.remove：仅覆盖本次下载，finally 必定恢复，不污染全进程
    orig_remove = _suspend_remove()
    os.remove = lambda p: None

    from playwright.sync_api import sync_playwright
    _set(total=count)  # task/done/logs 已由 try_start 设置
    ok = fail = 0
    try:
        with sync_playwright() as pw:
            def make_ctx():
                return _launch_douyin_ctx(pw, dr.UA)
            ctx = make_ctx()
            logged, page = _dy_login_check(ctx)
            if not logged:
                return _fail("抖音登录超时，请重试并扫码")
            pool = {}
            _dy_harvest(page, ctx, pool)
            _log(f"候选池 {len(pool)} 个")

            done6 = set()
            for f in os.listdir(out_dir):
                m = re.search(r"_(\d{6})\.mp4$", f)
                if m: done6.add(m.group(1))

            blacklist = set()          # 失败过的 aid，短期不再重试，防重复命中死循环
            max_attempts = count * 6 + 30
            attempted = 0

            def usable():
                out = []
                for a, m in pool.items():
                    if a in blacklist or a[-6:] in done6:
                        continue
                    d = m.get("duration") or 0
                    if not (min_sec * 1000 <= d <= max_sec * 1000):
                        continue
                    if keywords and not any(k in m.get("desc", "") for k in keywords):
                        continue
                    out.append(a)
                return out

            while ok < count and attempted < max_attempts and not _cancelled():
                attempted += 1
                cand = usable()
                if not cand:
                    _log("候选耗尽，刷新推荐流")
                    page = ctx.new_page()
                    _dy_harvest(page, ctx, pool, scrolls=40, target=len(pool) + 50)
                    try: page.close()
                    except Exception: pass
                    cand = usable()
                    if not cand:
                        _log("刷新后仍无候选，停止")
                        break
                random.shuffle(cand)
                aid = cand[0]
                path = info = None
                for attempt in range(3):
                    try:
                        path, info = dr.download_one(ctx, aid, pool)
                        break
                    except Exception as e:
                        msg = str(e).lower()
                        if "closed" in msg or "target page" in msg:
                            _log(f"浏览器崩溃，重建 ctx (#{attempt+1})")
                            try: ctx.close()
                            except Exception: pass
                            time.sleep(2)
                            ctx = make_ctx()
                            # 复检登录态并重取 page，避免 page 仍指向已关闭的旧 ctx
                            logged, page = _dy_login_check(ctx)
                            if not logged:
                                _log("重建后登录失效，停止")
                                break
                        else:
                            info = str(e)[:60]
                            break
                if path:
                    # 补音轨：抖音视频轨/音轨分离，download_one 只抓了视频轨。
                    # 关键：补音轨失败＝无声成片，必须丢弃并顺延，绝不能计入成功。
                    aud_ok, aud_info = _dy_ensure_audio(ctx, path, aid)
                    if aud_ok:
                        ok += 1
                        done6.add(aid[-6:])
                        with STATE_LOCK:
                            STATE["done"] = ok
                        _log(f"[{ok}/{count}] OK {os.path.basename(path)[:40]} [音轨:{aud_info}]")
                    else:
                        # 无声文件不留：移入 _discard 隔离目录（os.remove 此时已被挂起）
                        try:
                            discard = os.path.join(os.path.dirname(path), "_discard")
                            os.makedirs(discard, exist_ok=True)
                            os.replace(path, os.path.join(discard, os.path.basename(path)))
                        except Exception:
                            pass
                        fail += 1
                        blacklist.add(aid)
                        _log(f"无声丢弃 {aid} ({aud_info})，顺延下一个")
                else:
                    fail += 1
                    blacklist.add(aid)
                    _log(f"FAIL {aid} ({info})")
            if ok < count:
                _log(f"达到尝试上限（{attempted}/{max_attempts}），成功 {ok}/{count}，提前结束")
            try: ctx.close()
            except Exception: pass
    finally:
        _restore_remove(orig_remove)  # 必定恢复 os.remove，杜绝全进程污染
        finish()
    msg = f"抖音下载完成：成功 {ok}，失败 {fail}，输出 {out_dir}"
    _log(msg)
    return _ok(msg, ok=ok, fail=fail, out_dir=out_dir)


def douyin_login():
    """打开抖音窗口等待扫码登录，成功后保存 Cookie 文件。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _launch_douyin_ctx(pw, UA)
        try:
            logged, _ = _dy_login_check(ctx)
            total, cookies = _save_profile_cookies(ctx) if logged else (0, [])
        finally:
            try: ctx.close()
            except Exception: pass
    if not logged:
        return _fail("抖音登录超时，请重试")
    _log(f"抖音登录成功，已保存 {len(cookies)} 个 Cookie")
    return _ok(f"抖音登录成功，已记住登录（{len(cookies)} 个 Cookie）",
               logged=True, total=total)


def xhs_login():
    """打开小红书窗口扫码登录，登录成功后自动导出 Cookie 到 config.XHS_COOKIE_FILE。

    之后 url_download 下载小红书视频会自动读取该文件，无需手动导 Cookie。
    """
    from playwright.sync_api import sync_playwright
    os.makedirs(config.PROFILE_XHS, exist_ok=True)
    with sync_playwright() as pw:
        ctx = None
        try:
            common = dict(
                headless=False, user_agent=UA, viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            for kwargs in _browser_channel_kwargs():
                try:
                    ctx = pw.chromium.launch_persistent_context(
                        config.PROFILE_XHS, **common, **kwargs)
                    break
                except Exception:
                    ctx = None
            if ctx is None:
                raise RuntimeError("无法启动浏览器")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=45000)
            _log("小红书登录：请在弹出窗口扫码（最长5分钟）")
            deadline = time.time() + 300
            logged = False
            while time.time() < deadline:
                page.wait_for_timeout(5000)
                names = [c["name"] for c in ctx.cookies()]
                if "web_session" in names:
                    logged = True
                    break
            if not logged:
                return _fail("小红书登录超时，请重试")
            # 导出 cookies：专用文件 + 统一库，下次下载直接复用
            total, cookies = _save_profile_cookies(ctx)
            _log(f"小红书登录成功，已保存 {len(cookies)} 个 Cookie")
            return _ok(f"小红书登录成功，已记住登录（{len(cookies)} 个 Cookie）", total=total)
        except Exception as e:
            return _fail(f"小红书登录失败：{str(e)[:120]}")
        finally:
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass


def _cookies_to_lines(cookies):
    """Playwright cookie 列表 -> Netscape 格式行。"""
    lines = []
    for c in cookies:
        domain = c.get("domain", "")
        host_only = "FALSE" if domain.startswith(".") else "TRUE"
        expires = int(c.get("expires", -1) or -1)
        if expires < 0:
            expires = 0
        lines.append("\t".join([
            domain, host_only, c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(expires), c.get("name", ""), c.get("value", ""),
        ]))
    return lines


def _merge_cookie_lines(path, new_lines):
    """把 cookie 合并进指定 Netscape 文件（按 domain+name 覆盖）。"""
    merged = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        merged[(parts[0], parts[5])] = line
        except Exception:
            pass
    for line in new_lines:
        parts = line.split("\t") if line else []
        if len(parts) >= 7:
            merged[(parts[0], parts[5])] = line  # 新值覆盖旧值
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("\n".join(merged.values()) + "\n")
    return len(merged)


def _merge_cookies_file(new_lines):
    """把新 cookie 合并进统一 cookies.txt（按 domain+name 覆盖），不覆盖其它站点。"""
    return _merge_cookie_lines(config.COOKIES_FILE, new_lines)


def _save_profile_cookies(ctx):
    """把浏览器 Profile 的登录 Cookie 导出成文件，供下次下载直接复用。"""
    cookies = ctx.cookies()
    lines = _cookies_to_lines(cookies)
    if not lines:
        return 0, []
    total = _merge_cookies_file(lines)
    for path, domains in (
        (config.COOKIE_BILI, ("bilibili.com",)),
        (config.COOKIE_DOUYIN, ("douyin.com",)),
        (config.XHS_COOKIE_FILE, ("xiaohongshu.com", "xhslink.com")),
    ):
        site_lines = [line for line in lines
                      if any(d in line.split("\t")[0].lstrip(".").lower() for d in domains)]
        if site_lines:
            _merge_cookie_lines(path, site_lines)
    return total, cookies


def site_login(url, timeout=300):
    """通用站点登录：打开任意网址，用户登录后在前端点"完成登录"，
    程序导出该站点 cookie 合并进统一 cookies.txt，之后 url_download 全站通用。
    """
    from playwright.sync_api import sync_playwright
    if not isinstance(url, str) or not re.match(r"^https?://", url.strip(), re.I):
        return _fail("请输入以 http:// 或 https:// 开头的网址")
    url = url.strip()
    with LOGIN_LOCK:
        if LOGIN["active"]:
            return _fail("已有登录窗口在进行中")
        LOGIN.update(active=True, done=False)
    os.makedirs(config.PROFILE_SITE, exist_ok=True)
    try:
        with sync_playwright() as pw:
            common = dict(
                headless=False, user_agent=UA, viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            for kwargs in _browser_channel_kwargs():
                try:
                    ctx = pw.chromium.launch_persistent_context(
                        config.PROFILE_SITE, **common, **kwargs)
                    break
                except Exception:
                    ctx = None
            if ctx is None:
                raise RuntimeError("无法启动浏览器")
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                _log(f"登录窗口已打开：{url[:50]}。请在窗口里登录，完成后回设置页点「完成登录」")
                deadline = time.time() + timeout
                done = False
                while time.time() < deadline:
                    with LOGIN_LOCK:
                        done = LOGIN["done"]
                    if done:
                        break
                    try:
                        page.wait_for_timeout(1500)
                    except Exception:
                        break  # 窗口被关闭
                if not done:
                    return _fail("登录超时（5分钟），请重试")
                # 确认后不立即导出：等跳转落地、Cookie 写入完成。
                # 连续 3 次（每次 1 秒）Cookie 数量不变且非空，视为稳定。
                stable = 0
                last_count = -1
                for _ in range(8):
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
                    cookies = ctx.cookies()
                    if cookies and len(cookies) == last_count:
                        stable += 1
                        if stable >= 3:
                            break
                    else:
                        stable = 0
                    last_count = len(cookies)
                total, cookies = _save_profile_cookies(ctx)
                _log(f"登录成功：已保存 {len(cookies)} 个 Cookie，统一库共 {total} 个")
                return _ok(f"登录成功，已记住该站点（本次 {len(cookies)} 个 Cookie）",
                           count=len(cookies), total=total)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        return _fail(f"登录失败：{str(e)[:120]}")
    finally:
        with LOGIN_LOCK:
            LOGIN.update(active=False, done=False)


def confirm_login():
    """前端"完成登录"按钮：通知后台登录线程导出 cookie。"""
    with LOGIN_LOCK:
        if not LOGIN["active"]:
            return _fail("当前没有进行中的登录")
        LOGIN["done"] = True
        return _ok("已确认登录，正在保存 Cookie...")


def clean_temp():
    """清理 B站目录的 .v.mp4 / .a.m4a 临时碎片。仅删有对应正片的。"""
    if not try_start("清理临时文件"):
        return _fail("已有任务在运行，请等待完成")
    out_dir = config.DIR_BILI
    try:
        tmp = []
        fin = set()
        for f in os.listdir(out_dir):
            if f.endswith(".v.mp4") or f.endswith(".a.m4a"):
                m = re.match(r"^(\d{4})_", f)
                if m: tmp.append((f, int(m.group(1))))
            elif f.endswith(".mp4"):
                m = re.match(r"^(\d{4})_", f)
                if m: fin.add(int(m.group(1)))
        safe = [f for f, idx in tmp if idx in fin]  # 非数字文件名已被正则过滤
        freed = sum(os.path.getsize(os.path.join(out_dir, f)) for f in safe)
        done = 0
        for f in safe:
            try:
                os.remove(os.path.join(out_dir, f))  # 此时 os.remove 已恢复为真实实现
                done += 1
            except Exception:
                pass
    except Exception as e:
        return _fail(f"清理失败：{e}")
    finally:
        finish()
    return _ok(f"清理 {done}/{len(safe)} 个临时碎片，释放约 {freed/1073741824:.1f}GB",
               count=done, freed_gb=round(freed/1073741824, 2))


# ============ 通用链接下载（yt-dlp）============

def _write_cookie_file(cookie_str, path):
    """把用户粘贴的 cookie 写成 Netscape 格式文件给 yt-dlp。

    兼容两种输入：
    1) `name=value; name2=value2`（浏览器插件 Copy as header 常见格式）
    2) 完整 Netscape 文件（# Netscape HTTP Cookie File ...），原样写入
    """
    s = (cookie_str or "").strip()
    if not s:
        return False
    try:
        if s.startswith("# Netscape") or "\t" in s:
            with open(path, "w", encoding="utf-8") as f:
                f.write(s if s.endswith("\n") else s + "\n")
        else:
            lines = ["# Netscape HTTP Cookie File"]
            for part in s.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if not k:
                    continue
                lines.append("\t".join([".xiaohongshu.com", "TRUE", "/", "TRUE",
                                        "0", k, v]))
            if len(lines) < 2:
                return False
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        return True
    except Exception:
        return False


def _verify_merge(path):
    """校验合并产物的音画时长；音频超长（画面放完后音频还在响）则截断到视频时长。
    返回 (是否OK, 说明)。"""
    try:
        ffprobe = config.ffprobe_exe()
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                            "stream=codec_type,duration", "-of", "json", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        d = json.loads(r.stdout)
        v = [s for s in d["streams"] if s.get("codec_type") == "video"]
        a = [s for s in d["streams"] if s.get("codec_type") == "audio"]
        if not v or not a:
            return False, "缺视频流或音频流" if v or a else "无有效流"
        vd = float(v[0].get("duration") or 0)
        ad = float(a[0].get("duration") or 0)
        if not vd:
            return True, "视频时长未知，跳过校验"
        diff = abs(vd - ad)
        if diff <= 2.0:
            return True, f"音画对齐(差{diff:.1f}s)"
        # 音频明显超长/过短：重封装并截到视频时长
        _log(f"音画时长偏差 {diff:.1f}s，自动修正到视频时长 {vd:.1f}s")
        tmp = path + ".fixmerge.mp4"
        r2 = subprocess.run(
            [config.ffmpeg_exe(), "-y", "-v", "error", "-i", path,
             "-t", f"{vd:.3f}", "-c", "copy", "-shortest", "-movflags", "+faststart", tmp],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        if r2.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 100000:
            os.replace(tmp, path)
            return True, f"已修正音画(原差{diff:.1f}s)"
        # 修正失败，清理临时文件
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, f"音画偏差{diff:.1f}s且自动修正失败"
    except Exception as e:
        return False, f"校验异常({str(e)[:60]})"


def url_info(url):
    """预探测链接：只读取元信息，不下载；区分单视频/播放列表/直播/需要登录。"""
    try:
        import yt_dlp
    except ImportError:
        return _fail("缺少 yt-dlp 组件，无法探测链接", error_code="probe_failed")
    if not isinstance(url, str) or not re.match(r"^https?://", url.strip(), re.I):
        return _fail("请提供有效的 http/https 链接", error_code="bad_arguments")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": True, "socket_timeout": 30, "retries": 1}
    px = config.proxy_url() or config.system_proxy_url()
    if px:
        opts["proxy"] = px
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url.strip(), download=False) or {}
        entries = info.get("entries")
        kind = "playlist" if entries is not None else "live" if info.get("live_status") == "is_live" else "video"
        item = {
            "kind": kind, "title": str(info.get("title") or "")[:160],
            "webpage_url": str(info.get("webpage_url") or url.strip())[:500],
            "duration": int(info.get("duration") or 0),
            "count": len(list(entries)) if entries is not None else 1,
            "need_login": bool(info.get("login_required") or info.get("age_limit")),
            "extractor": str(info.get("extractor_key") or "")[:60],
        }
        next_action = ("这是合集/列表，请先问用户要下载全部还是前 N 个"
                       if kind == "playlist" else "这是单视频，可直接 url_download")
        return _ok("链接探测完成：" + (item["title"] or "未命名"), **item,
                   next_action=next_action)
    except Exception as e:
        text = str(e).lower()
        code = "login_required" if "login" in text else "probe_failed"
        return _fail("链接探测失败：" + str(e)[:150], error_code=code)


def search_videos(keyword, platform="all", count=8, out_dir=None, quality="best"):
    """按关键词搜索视频，只返回候选列表，不下载；out_dir/quality 原样回传供界面下载用。"""
    try:
        import yt_dlp
    except ImportError:
        return _fail("缺少 yt-dlp 组件，无法搜索", error_code="search_failed")
    if not isinstance(keyword, str) or not keyword.strip():
        return _fail("请提供搜索关键词", error_code="bad_arguments")
    keyword = keyword.strip()[:100]
    try:
        cap = max(1, min(12, int(count)))
    except (TypeError, ValueError):
        cap = 8
    key = str(platform or "all").strip().lower()
    # yt-dlp 的 Douyin extractor 未提供 SEARCH_KEY，不能构造 douyinsearch:N。
    # 抖音保持原有随机下载路径；这里不假装支持搜索。
    if key in ("douyin", "dy", "抖音"):
        return _fail("抖音暂不支持关键词搜索，请用“随机刷N个”或发具体链接",
                     error_code="unsupported_search_platform")
    prefix = {"bili": "bilisearch", "bilibili": "bilisearch", "b站": "bilisearch",
              "youtube": "ytsearch", "yt": "ytsearch"}.get(key, "ytsearch")
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True,
        "skip_download": True, "playlistend": cap,
        "socket_timeout": 30, "retries": 1,
    }
    px = config.proxy_url() or config.system_proxy_url()
    if px:
        opts["proxy"] = px
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("%s%d:%s" % (prefix, cap, keyword), download=False)
        items = []
        for e in ((info or {}).get("entries") or [])[:cap]:
            if not isinstance(e, dict):
                continue
            url = e.get("webpage_url") or e.get("url") or ""
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            items.append({
                "title": str(e.get("title") or "无标题")[:160],
                "url": url,
                "id": str(e.get("id") or "")[:80],
                "duration": int(e.get("duration") or 0),
                "uploader": str(e.get("uploader") or "")[:80],
            })
        if not items:
            return _fail("没有找到匹配的视频，试试更短的关键词", error_code="no_results")
        return _ok("找到 %d 个候选视频，请在界面选择" % len(items),
                   items=items, keyword=keyword, platform=key,
                   out_dir=out_dir or "", quality=quality or "best")
    except Exception as e:
        return _fail("搜索失败：" + str(e)[:150], error_code="search_failed")


def url_download(url, quality="best", out_dir=None):
    """按任意视频网页链接下载（YouTube/B站/抖音等 yt-dlp 支持的站点）。

    - 视频与音频自动合并为 mp4；代理由 yt-dlp 自动读取系统/环境变量。
    - 默认输出到 ROOT/网络视频/；out_dir 只允许资产根目录下的安全相对路径。
    """
    if not isinstance(url, str) or not url.strip():
        return _fail("请提供视频链接")
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return _fail("链接格式不对，应以 http:// 或 https:// 开头")
    if out_dir:
        safe_dir = _safe_agent_dir(out_dir)
        if not safe_dir:
            return _fail("保存目录无效：请使用资产根目录下的相对路径，不能包含 '..' 或盘符")
    else:
        safe_dir = os.path.join(os.path.realpath(config.ROOT), "网络视频")
    if not try_start(f"链接下载 {url[:40]}"):
        return _fail("已有任务在运行，请等待完成")
    out_dir = safe_dir
    os.makedirs(out_dir, exist_ok=True)
    try:
        import yt_dlp
    except ImportError:
        finish()
        return _fail("缺少 yt-dlp 组件，请检查安装包是否完整")
    _log(f"开始下载链接：{url[:60]}")

    def progress_hook(d):
        if _cancelled():
            raise RuntimeError("用户取消下载")
        if d.get("status") == "downloading":
            p = d.get("_percent_str", "").strip()
            if p:
                _log(f"下载中 {p}")

    # 格式选择：best 限 1080P，避免 4K 大会员/超大文件
    height_map = {"best": 1080, "1080": 1080, "720": 720}
    height = height_map.get(str(quality), 1080)
    fmt = (f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best")
    opts = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(out_dir, "%(title).80s [%(id)s].%(ext)s"),
        "ffmpeg_location": os.path.dirname(config.ffmpeg_exe()) or None,
        "progress_hooks": [progress_hook],
        "quiet": True, "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "concurrent_fragment_downloads": 4,
    }
    # ffmpeg_location 若为裸名（PATH 回退）就设为 None，让 yt-dlp 自己找
    if opts["ffmpeg_location"] and not os.path.isdir(opts["ffmpeg_location"]):
        opts["ffmpeg_location"] = None
    # Cookie：统一库（通用站点登录保存）优先，全站点通用；兼容旧的小红书专用库与手动粘贴。
    cookie_path = None
    temp_cookie = False
    is_xhs = "xiaohongshu.com" in url or "xhslink.com" in url
    if os.path.isfile(config.COOKIES_FILE):
        cookie_path = config.COOKIES_FILE  # 通用登录库，不删
        _log("已加载登录态（通用 Cookie 库）")
    elif is_xhs and os.path.isfile(config.XHS_COOKIE_FILE):
        cookie_path = config.XHS_COOKIE_FILE  # 旧版小红书专用库，不删
        _log("已加载小红书登录态（旧版库）")
    elif is_xhs:
        xck = config.xhs_cookie()
        if not xck:
            finish()
            return _fail("该站点需登录：请在设置页点「网站登录」完成一次登录，或手动粘贴 Cookie")
        cookie_path = os.path.join(config._APP_DIR, "_xhs_cookies.txt")
        if not _write_cookie_file(xck, cookie_path):
            finish()
            return _fail("Cookie 格式不对，请粘贴 name=value; ... 形式或完整 Netscape 文件")
        temp_cookie = True
        _log("已加载小红书 Cookie（手动粘贴）")
    if cookie_path:
        opts["cookiefile"] = cookie_path
    # 代理：优先用设置里配的；否则清掉环境里的注入代理（WorkBuddy 的 127.0.0.1 代理
    # 常返回 502，会挡住 YouTube）。用临时环境，结束后恢复，不影响同进程其它下载。
    px = config.proxy_url() or config.system_proxy_url()
    saved = {}
    try:
        if px:
            # 显式指定代理，同时清掉环境变量里可能注入的坏代理，避免冲突
            opts["proxy"] = px
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
                if k in os.environ:
                    saved[k] = os.environ.pop(k)
            _log(f"使用代理：{px}")
        else:
            # 无代理：清掉环境注入的坏代理，直连国内站点
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
                if k in os.environ:
                    saved[k] = os.environ.pop(k)
        files_before = set(os.listdir(out_dir))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = (info or {}).get("title", "")
        dur = (info or {}).get("duration") or 0
        # 定位本次下载文件并校验音画合并
        merge_note = ""
        new_files = [f for f in set(os.listdir(out_dir)) - files_before
                     if f.lower().endswith((".mp4", ".mkv", ".webm", ".flv"))]
        if new_files:
            newest = max(new_files, key=lambda f: os.path.getmtime(os.path.join(out_dir, f)))
            verify_ok, note = _verify_merge(os.path.join(out_dir, newest))
            merge_note = f" [合并校验：{note}]"
            _log(f"合并校验：{note}")
        _log(f"链接下载完成：{title[:40]}")
        return _ok(f"已下载：{title[:40]}（{int(dur)}秒）→ {out_dir}{merge_note}",
                   title=title, duration=dur, out_dir=out_dir)
    except Exception as e:
        if _cancelled():
            return _ok("下载已取消", cancelled=True)
        return _fail(f"链接下载失败：{str(e)[:150]}")
    finally:
        for k, v in saved.items():  # 恢复被临时清掉的环境代理
            os.environ[k] = v
        if cookie_path and temp_cookie:  # 只删手动粘贴生成的临时文件，登录保存的保留
            try:
                os.remove(cookie_path)
            except Exception:
                pass
        finish()
