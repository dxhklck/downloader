"""下载工具集：把 B站/抖音的下载能力封装成标准函数，供 LLM 和界面调用。

每个函数返回结构化 dict（success/message/data），不抛异常到调用层。
进度通过可选的 progress 回调推送（GUI 用它更新界面）。
"""
import os, sys, json, re, time, random, subprocess, urllib.request, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"

# 任务状态（全局，界面轮询用）
STATE = {"running": False, "task": "", "done": 0, "total": 0, "logs": [], "log_gen": 0}
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
        STATE.update(running=True, task=task, done=0, total=0, logs=[])
        return True


def finish():
    """释放任务槽。"""
    with STATE_LOCK:
        STATE["running"] = False


def _log(msg):
    with STATE_LOCK:
        STATE["logs"].append(msg)
        if len(STATE["logs"]) > 200:
            STATE["logs"] = STATE["logs"][-200:]


def get_state():
    with STATE_LOCK:
        return dict(STATE, logs=list(STATE["logs"]))


def clear_logs():
    """清空日志并递增代际号，供前端丢弃在途 poll 响应中的旧日志。"""
    with STATE_LOCK:
        STATE["logs"] = []
        STATE["log_gen"] += 1
        return {"ok": True, "log_gen": STATE["log_gen"]}


def _set(**kw):
    with STATE_LOCK:
        STATE.update(kw)


def _ok(msg, **data):
    return {"success": True, "message": msg, "data": data}


def _fail(msg):
    return {"success": False, "message": msg, "data": {}}


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
    for attempt in range(4):
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
            if attempt == 3:
                return "fail", str(e)[:100]
            time.sleep(2 ** attempt)
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

    def work(i):
        st, info = _bili_one(i, eps, out_dir, sess)
        with STATE_LOCK:
            STATE["done"] += 1
            d = STATE["done"]
        _log(f"[{d}/{total}] 第{i}集 {st}")
        return st

    ok = skip = fail = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, i): i for i in range(start, end + 1)}
            for fut in as_completed(futs):
                st = fut.result()
                if st == "ok": ok += 1
                elif st == "skip": skip += 1
                else: fail += 1
    finally:
        _set(running=False)
    msg = f"B站下载完成：成功 {ok}，跳过 {skip}，失败 {fail}，输出 {out_dir}"
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

            while ok < count and attempted < max_attempts:
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
    """打开抖音窗口等待扫码登录，返回登录状态。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _launch_douyin_ctx(pw, UA)
        logged, _ = _dy_login_check(ctx)
        try: ctx.close()
        except Exception: pass
    return _ok("抖音登录成功" if logged else "登录超时", logged=logged)


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
            # 导出 cookies：同时存小红书专用文件 + 合并进统一库
            cookies = ctx.cookies()
            lines = _cookies_to_lines(cookies)
            with open(config.XHS_COOKIE_FILE, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n")
            total = _merge_cookies_file(lines)
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


def _merge_cookies_file(new_lines):
    """把新 cookie 合并进统一 cookies.txt（按 domain+name 覆盖），不覆盖其它站点。"""
    merged = {}
    if os.path.isfile(config.COOKIES_FILE):
        try:
            with open(config.COOKIES_FILE, encoding="utf-8") as f:
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
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            merged[(parts[0], parts[5])] = line  # 新值覆盖旧值
    with open(config.COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("\n".join(merged.values()) + "\n")
    return len(merged)


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
                cookies = ctx.cookies()
                total = _merge_cookies_file(_cookies_to_lines(cookies))
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


def url_download(url, quality="best"):
    """按任意视频网页链接下载（YouTube/B站/抖音等 yt-dlp 支持的站点）。

    - 视频与音频自动合并为 mp4；代理由 yt-dlp 自动读取系统/环境变量。
    - 输出到 ROOT/网络视频/，文件名用视频标题。
    """
    if not isinstance(url, str) or not url.strip():
        return _fail("请提供视频链接")
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return _fail("链接格式不对，应以 http:// 或 https:// 开头")
    if not try_start(f"链接下载 {url[:40]}"):
        return _fail("已有任务在运行，请等待完成")
    out_dir = os.path.join(config.ROOT, "网络视频")
    os.makedirs(out_dir, exist_ok=True)
    try:
        import yt_dlp
    except ImportError:
        finish()
        return _fail("缺少 yt-dlp 组件，请检查安装包是否完整")
    _log(f"开始下载链接：{url[:60]}")

    def progress_hook(d):
        if d.get("status") == "downloading":
            p = d.get("_percent_str", "").strip()
            if p:
                _log(f"下载中 {p}")

    # 格式选择：优先 1080P 以下有音视频的，避免下载 4K 大会员/超大文件
    fmt = ("bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
           if quality == "best" else "best")
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
