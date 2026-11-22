"""抖音随机下载核心（收编自根目录 douyin_random.py）。
从推荐流抓 ID 池 → route 拦截下载 → ffprobe 校验视频流。
路径由调用方注入（OUT_DIR/COOKIE_FILE），默认值取自 config。
"""
import os, re, time, subprocess

from playwright.sync_api import sync_playwright

import config

COOKIE_FILE = config.COOKIE_DOUYIN
OUT_DIR = os.path.join(config.DIR_DOUYIN, "随机")
POOL_FILE = os.path.join(config.ROOT, "douyin_pool.json")
FEED_URL = "https://www.douyin.com/jingxuan"
WANT = 3
SCROLLS = 8
MAX_BYTES = 50 * 1024 * 1024
CHUNK = 1024 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def load_cookies():
    out = []
    with open(COOKIE_FILE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 7 or not p[5].strip():
                continue
            try:
                exp = int(p[4])
            except ValueError:
                exp = -1
            out.append({
                "name": p[5].strip(), "value": p[6].strip(), "domain": p[0].strip(),
                "path": p[2].strip(), "expires": exp if exp > 0 else -1,
                "secure": p[3].strip().upper() == "TRUE",
                "httpOnly": p[1].strip().upper() == "TRUE",
            })
    return out


def safe_name(s, fallback):
    s = re.sub(r"[\\/:*?\"<>|\r\n\t#]", "", (s or "")).strip()
    s = re.sub(r"\s+", " ", s)[:50]
    return s or fallback


def ffprobe_has_video(path):
    try:
        r = subprocess.run(
            [config.ffprobe_exe(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def harvest(ctx, pool):
    page = ctx.new_page()

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
                    try:
                        dur = int(dur)
                    except Exception:
                        dur = 0
                    if aid not in pool:
                        pool[aid] = {"desc": o.get("desc") or "", "duration": dur,
                                     "author": ((o.get("author") or {}).get("nickname") or "")}
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(body)

    page.on("response", on_resp)
    print(f"[harvest] open {FEED_URL}", flush=True)
    try:
        page.goto(FEED_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[harvest] nav warn {str(e)[:70]}", flush=True)
    page.wait_for_timeout(5000)
    print(f"[harvest] title={page.title()}", flush=True)

    for i in range(SCROLLS):
        try:
            page.mouse.wheel(0, 2400)
        except Exception:
            pass
        page.wait_for_timeout(2200)
        print(f"[harvest] scroll {i+1} pool={len(pool)}", flush=True)
        if len(pool) >= 40:
            break
    page.close()
    return pool


def download_one(ctx, aid, pool):
    meta = pool.get(aid, {})
    streams = {}

    def handle_route(route):
        url = route.request.url
        if "douyinvod.com" not in url:
            route.continue_()
            return
        m = re.search(r"[?&]br=(\d+)", url)
        br = m.group(1) if m else "0"
        try:
            resp = route.fetch(timeout=90000)
            body = resp.body()
            cr = resp.headers.get("content-range", "")
            tm = re.search(r"/(\d+)", cr)
            s = streams.setdefault(br, {"url": url, "total": 0, "segs": {}})
            if tm:
                s["url"] = url
                s["total"] = int(tm.group(1))
            if resp.status == 206 and body:
                sm = re.search(r"bytes\s+(\d+)-", cr)
                if sm:
                    s["segs"][int(sm.group(1))] = body
            route.fulfill(response=resp)
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    page = ctx.new_page()
    page.route("**/*douyinvod.com/**", handle_route)
    for attempt in range(2):
        try:
            page.goto(f"https://www.douyin.com/video/{aid}",
                      wait_until="domcontentloaded", timeout=40000)
        except Exception:
            pass
        page.wait_for_timeout(4500)
        for _ in range(4):
            if any(v["total"] > 0 for v in streams.values()):
                break
            for sel in ["[data-e2e='play']", ".xg-icon-play", "video"]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        el.click(timeout=1500)
                        break
                except Exception:
                    pass
            page.wait_for_timeout(6000)
        if any(v["total"] > 0 for v in streams.values()):
            break

    total_segs = {k: len(v["segs"]) for k, v in streams.items()}
    print(f"[stream] {aid} " + ", ".join(
        f"br={k} total={v['total']} segs={total_segs[k]}" for k, v in streams.items()), flush=True)

    cands = [k for k, v in streams.items() if 0 < v["total"] <= MAX_BYTES]
    cands.sort(key=lambda k: streams[k]["total"], reverse=True)
    if not cands:
        page.close()
        return None, "no valid stream"

    js = """async (args) => {
        const {url, ranges} = args;
        await Promise.all(ranges.map(([s,e]) =>
            fetch(url, {headers:{Range:`bytes=${s}-${e}`}}).then(r=>r.status).catch(()=>0)
        ));
        return ranges.length;
    }"""

    for br in cands[:3]:
        info = streams[br]
        url, total = info["url"], info["total"]
        pos = 0
        while pos < total:
            ranges = []
            for _ in range(6):
                if pos >= total:
                    break
                end = min(pos + CHUNK - 1, total - 1)
                ranges.append([pos, end])
                pos = end + 1
            try:
                page.evaluate(js, {"url": url, "ranges": ranges})
            except Exception:
                pass
            if pos >= total:
                break
        segs = info["segs"]
        if not segs:
            continue
        data = b"".join(segs[k] for k in sorted(segs))
        name = safe_name(meta.get("desc"), aid)
        out = os.path.join(OUT_DIR, f"{name}_{aid[-6:]}.mp4")
        with open(out, "wb") as f:
            f.write(data)
        ok = ffprobe_has_video(out)
        print(f"[check] br={br} size={len(data)} video={ok}", flush=True)
        if ok:
            page.close()
            return out, f"{len(data)/1048576:.1f}MB"
        # 不直接 os.remove：由调用方 tools 受控管理，避免全进程删文件失效
        try:
            os.remove(out)
        except Exception:
            pass

    page.close()
    return None, "all candidates audio-only"


def main():
    import json
    import random
    cookies = load_cookies()
    print(f"[cookies] {len(cookies)}", flush=True)
    pool = {}
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=[
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled", "--no-sandbox",
            "--mute-audio",
        ])
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        ctx.add_cookies(cookies)

        harvest(ctx, pool)
        print(f"[pool] {len(pool)} videos", flush=True)
        with open(POOL_FILE, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=1)

        usable = [a for a, m in pool.items() if 5000 <= (m.get("duration") or 0) <= 180000]
        if len(usable) < WANT:
            usable = list(pool.keys())
        random.shuffle(usable)
        picks = usable[: WANT * 2]

        ok = 0
        for aid in picks:
            if ok >= WANT:
                break
            t0 = time.time()
            try:
                path, info = download_one(ctx, aid, pool)
            except Exception as e:
                path, info = None, f"error {str(e)[:60]}"
            if path:
                ok += 1
                results.append((path, info, time.time() - t0))
        browser.close()

    print(f"\n池子 {len(pool)} 个 / 目标 {WANT} 个 / 成功 {ok} 个", flush=True)


if __name__ == "__main__":
    main()
