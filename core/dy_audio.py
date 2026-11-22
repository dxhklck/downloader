"""抖音无声视频补音轨（收编自根目录 douyin_fix_audio.py）。
route 抓 media-audio 流 → ffmpeg -c copy 合成到已有文件，不重下视频轨。
路径由 config 派生；os.remove 由调用方 tools 受控管理。
"""
import os, re, subprocess, time, argparse

from playwright.sync_api import sync_playwright

import config

VIDEO_DIR = os.path.join(config.DIR_DOUYIN, "随机")
COOKIE = config.COOKIE_DOUYIN
MAP = os.path.join(config.ROOT, "silent_id_map.json")
CHUNK = 512 * 1024
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
JS = """async (args) => {
    const {url, ranges} = args;
    await Promise.all(ranges.map(([s,e]) =>
        fetch(url, {headers:{Range:`bytes=${s}-${e}`}}).then(r=>r.status).catch(()=>0)));
    return ranges.length;
}"""


def load_cookies():
    out = []
    with open(COOKIE, encoding="utf-8") as f:
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
            out.append({"name": p[5].strip(), "value": p[6].strip(), "domain": p[0].strip(),
                        "path": p[2].strip(), "expires": exp if exp > 0 else -1,
                        "secure": p[3].strip().upper() == "TRUE",
                        "httpOnly": p[1].strip().upper() == "TRUE"})
    return out


def has_audio(path):
    r = subprocess.run([config.ffprobe_exe(), "-v", "error", "-show_entries", "stream=codec_type",
                        "-of", "csv=p=0", path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    return "audio" in r.stdout


def grab_audio(ctx, aid):
    """返回音轨 bytes，失败返回 (None, 原因)。"""
    streams = {}
    audio_urls = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = k.lower()
                if kl in ("play_addr", "download_addr") and isinstance(v, dict):
                    for u in v.get("url_list", []):
                        if "media-audio" in u and u not in audio_urls:
                            audio_urls.append(u)
                elif kl == "bit_rate" and isinstance(v, list):
                    for br in v:
                        if isinstance(br, dict):
                            for u in br.get("play_addr", {}).get("url_list", []):
                                if "media-audio" in u and u not in audio_urls:
                                    audio_urls.append(u)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    def on_resp(resp):
        ct = resp.headers.get("content-type", "")
        if "json" not in ct and "javascript" not in ct:
            return
        try:
            body = resp.json()
        except Exception:
            return
        walk(body)

    def handle(route):
        url = route.request.url
        if "douyinvod.com" not in url:
            route.continue_()
            return
        try:
            resp = route.fetch(timeout=60000)
            body = resp.body()
            cr = resp.headers.get("content-range", "") or ""
            s = streams.setdefault(url, {"total": 0, "segs": {}})
            tm = re.search(r"/(\d+)", cr)
            if tm and not s["total"]:
                s["total"] = int(tm.group(1))
            if resp.status == 206 and body:
                sm = re.search(r"bytes\s+(\d+)-", cr)
                if sm:
                    s["segs"].setdefault(int(sm.group(1)), body)
            elif resp.status == 200 and body:
                s["segs"].setdefault(0, body)
                if not s["total"]:
                    s["total"] = len(body)
            route.fulfill(response=resp)
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    page = ctx.new_page()
    page.route("**/*douyinvod.com/**", handle)
    page.on("response", on_resp)
    for attempt in range(2):
        try:
            page.goto(f"https://www.douyin.com/video/{aid}", wait_until="domcontentloaded", timeout=35000)
        except Exception:
            pass
        page.wait_for_timeout(5000)
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
            page.wait_for_timeout(5000)
        if any(v["total"] > 0 for v in streams.values()):
            break

    if not any("media-audio" in u and v["total"] > 0 for u, v in streams.items()) and audio_urls:
        for au in audio_urls[:2]:
            try:
                page.evaluate(JS, {"url": au, "ranges": [[0, min(CHUNK - 1, 2 * 1024 * 1024)]]})
                page.wait_for_timeout(1500)
            except Exception:
                pass

    ac = None
    for u, v in streams.items():
        if "media-audio" in u and v["total"] > 0:
            ac = (u, v)
            break
    if not ac:
        au_info = f"audio_urls={len(audio_urls)}"
        page.close()
        return None, f"no_audio_stream(streams={len(streams)},{au_info})"

    u, info = ac
    total = info["total"]
    segs = info["segs"]

    def assemble():
        got = sum(len(b) for b in segs.values())
        if got < total * 0.85:
            return None, got
        buf = bytearray(total)
        for start, b in segs.items():
            if start < total:
                buf[start:start + len(b)] = b
        return bytes(buf), got

    data, got = assemble()
    if data is not None:
        page.close()
        return data, f"{total//1024}KB"

    for _round in range(3):
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
                page.evaluate(JS, {"url": u, "ranges": ranges})
            except Exception:
                break
        data, got = assemble()
        if data:
            page.close()
            return data, f"{total//1024}KB-ranged{_round+1}"
    page.close()
    return (None, f"incomplete {got//1024}/{total//1024}KB")


def video_duration(path):
    """读视频流时长（秒），失败返回 0。"""
    try:
        r = subprocess.run([config.ffprobe_exe(), "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line and line != "N/A":
                return float(line)
        return 0
    except Exception:
        return 0


def mux(video_path, audio_bytes):
    apath = video_path + ".fixaudio.m4a"
    mixed = video_path + ".fixmix.mp4"
    try:
        with open(apath, "wb") as f:
            f.write(audio_bytes)
        # 抖音音轨流常比视频轨长（实测 33s 视频配 258s 音频），不截断会导致
        # 画面放完后音频继续"重复播放"。用 -t 精确截到视频时长 + -shortest 双保险。
        vdur = video_duration(video_path)
        cmd = [config.ffmpeg_exe(), "-y", "-v", "error", "-i", video_path, "-i", apath]
        if vdur > 0:
            cmd += ["-t", f"{vdur:.3f}"]
        cmd += ["-c", "copy", "-shortest", "-movflags", "+faststart", mixed]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        if r.returncode == 0 and os.path.exists(mixed) and has_audio(mixed):
            os.replace(mixed, video_path)
            return True, ""
        return False, r.stderr.strip()[:100]
    finally:
        for p in (apath, mixed):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def main():
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--login", action="store_true")
    args = ap.parse_args()

    mp = json.load(open(MAP, encoding="utf-8"))
    items = list(mp.items())[args.offset:]
    if args.limit > 0:
        items = items[:args.limit]
    ok = fail = skip = 0
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=False, args=[
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled", "--no-sandbox", "--mute-audio"])
        ctx = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        ctx.add_cookies(load_cookies())
        for i, (fname, aid) in enumerate(items, 1):
            path = os.path.join(VIDEO_DIR, fname)
            if not os.path.exists(path) or has_audio(path):
                skip += 1
                continue
            data, info = grab_audio(ctx, aid)
            if not data:
                fail += 1
                continue
            good, _ = mux(path, data)
            ok += 1 if good else 0
            fail += 0 if good else 1
        b.close()
    print(f"DONE ok={ok} fail={fail} skip={skip}", flush=True)


if __name__ == "__main__":
    main()
