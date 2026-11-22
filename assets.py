"""资产库：扫描本地视频文件，生成可检索的资产清单（带缓存）。

缓存 asset_cache.json 存在本目录，首次全量扫描，之后按 mtime 判断是否重扫。
"""
import os, json, time, subprocess
from concurrent.futures import ThreadPoolExecutor
import config

CACHE = os.path.join(config._APP_DIR, "asset_cache.json")
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm")


def _categories():
    """顶层视频目录 -> 分类名。自动纳入所有含视频的顶层目录。"""
    cats = []
    for d in sorted(os.listdir(config.ROOT)):
        p = os.path.join(config.ROOT, d)
        if os.path.isdir(p) and not d.startswith((".", "__")) and d not in ("downloader", "logs"):
            cats.append((d, p))
    return cats


def _duration(path):
    try:
        r = subprocess.run(
            [config.ffprobe_exe(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        return round(float(r.stdout.strip()))
    except Exception:
        return 0


def _fingerprint():
    """轻量指纹：只 os.stat，不 ffprobe。返回 {path: (size, mtime_rounded)}。
    2722 个文件约 1-2 秒，远快于逐个 ffprobe 的 155 秒。"""
    fp = {}
    for _, root in _categories():
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith(VIDEO_EXT):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                # 纳秒级 mtime + ctime 双因子，避免同 size 且 0.1s 内变更的漏检
                fp[full] = [st.st_size, st.st_mtime_ns, round(st.st_ctime, 1)]
    return fp


def _build_result(items):
    cat_stats = {}
    for it in items:
        s = cat_stats.setdefault(it["cat"], {"count": 0, "size": 0})
        s["count"] += 1
        s["size"] += it["size"]
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {
        "items": items,
        "stats": {
            "total": len(items),
            "total_gb": round(sum(i["size"] for i in items) / 2**30, 1),
            "cats": cat_stats,
        },
        "scanned_at": time.time(),
    }


def scan(include_duration=True, force=False):
    """扫描所有视频，返回 {items, stats, scanned_at}。

    缓存策略：按文件指纹（size+mtime）校验，不按时间。
    - 文件集合没变 → 直接返回缓存（含已探测的时长），秒开。
    - 文件变了 → 增量：复用未变文件的时长，只对新文件跑 ffprobe。
    - force=True → 强制全量重扫（供"重新扫描"按钮用）。
    """
    fp = _fingerprint()
    cached = None
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = None

    if not force and cached and cached.get("fingerprint") == fp:
        return {"success": True, "message": "使用缓存（文件无变化）", "data": cached}

    old_items = {i["path"]: i for i in (cached or {}).get("items", [])}
    items = []
    need_dur = []
    for cat, root in _categories():
        for dirpath, _, files in os.walk(root):
            for fn in files:
                full = os.path.join(dirpath, fn)
                if full not in fp:
                    continue
                size, mtime_ns, ctime = fp[full]
                mtime = mtime_ns / 1e9  # 供前端排序用（秒）
                prev = old_items.get(full)
                unchanged = (prev and prev.get("size") == size
                             and prev.get("mtime_ns") == mtime_ns
                             and abs(prev.get("ctime", 0) - ctime) < 0.2)
                # force=True 时强制全部重探测，不复用旧 dur
                if not force and unchanged:
                    items.append(prev)  # 未变：整条复用（含 dur）
                else:
                    it = {
                        "name": fn, "path": full,
                        "rel": os.path.relpath(full, config.ROOT), "cat": cat,
                        "size": size, "mtime": mtime, "mtime_ns": mtime_ns,
                        "ctime": ctime, "dur": 0,
                    }
                    items.append(it)
                    need_dur.append(it)

    # 只对新文件探测时长（通常 0 个）；force 时全部进 need_dur
    fail_dur = 0
    if include_duration and need_dur:
        workers = min(16, len(need_dur))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            durs = list(ex.map(lambda x: _duration(x["path"]), need_dur))
        for it, d in zip(need_dur, durs):
            it["dur"] = d
            if d == 0:
                fail_dur += 1

    result = _build_result(items)
    result["fingerprint"] = fp
    # include_duration=False 时不写缓存，避免把 dur=0 的错误时长固化
    if include_duration:
        tmp = CACHE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            os.replace(tmp, CACHE)  # 原子写，进程被杀不会截断缓存
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    changed = len(need_dur)
    extra = f"，{fail_dur} 个时长探测失败" if fail_dur else ""
    return {"success": True,
            "message": (f"增量扫描：新增/变更 {changed} 个，共 {len(items)} 个" if not force
                        else f"全量扫描完成 {len(items)} 个") + extra,
            "data": result}


def probe_codec(path):
    """读视频编码名，供前端判断能否内嵌播放。hevc/h265 → 需系统播放器。"""
    try:
        r = subprocess.run(
            [config.ffprobe_exe(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        codec = (r.stdout or "").strip().lower()
        return {"codec": codec, "embeddable": codec in ("h264", "av1", "vp9", "vp8", "mpeg4")}
    except Exception:
        return {"codec": "", "embeddable": True}  # 探测失败仍尝试内嵌


def _safe_media_path(path):
    """校验路径必须是 ROOT 下的真实视频文件，防止被诱导打开任意程序/URL。"""
    if not isinstance(path, str) or not path:
        return None
    try:
        full = os.path.abspath(path)
        root = os.path.abspath(config.ROOT)
        if os.path.commonpath([full, root]) != root:
            return None
        if not os.path.isfile(full):
            return None
        if not full.lower().endswith(VIDEO_EXT):
            return None
        return full
    except (ValueError, OSError):
        return None


def _os_open(path):
    """跨平台打开文件（系统默认播放器）。"""
    import sys
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def _os_reveal(path):
    """跨平台在文件管理器中定位文件。"""
    import sys
    if sys.platform == "win32":
        subprocess.run(["explorer", "/select," + path], check=False)
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False)


def open_file(path):
    """用系统默认播放器打开（仅允许 ROOT 下的视频文件）。"""
    safe = _safe_media_path(path)
    if not safe:
        return {"success": False, "message": "路径无效或不在资产目录内"}
    try:
        _os_open(safe)
        return {"success": True, "message": "已打开"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def locate(path):
    """在资源管理器中定位并选中文件（仅允许 ROOT 下的视频文件）。"""
    safe = _safe_media_path(path)
    if not safe:
        return {"success": False, "message": "路径无效或不在资产目录内"}
    try:
        _os_reveal(safe)
        return {"success": True, "message": "已定位"}
    except Exception as e:
        return {"success": False, "message": str(e)}
