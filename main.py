"""视频下载助手 · 桌面应用入口（pywebview 单进程）。

双击运行或：python main.py
界面用 WebView2 渲染，JS 通过 js_api 直接调用本文件的 Api 类。
"""
import os, sys, json, threading, queue, webview

import config
import tools
import assets
from agent import Agent


class Api:
    """暴露给 JavaScript 的 Python 接口。"""

    def __init__(self):
        self.q = queue.Queue()           # 事件队列 -> 界面轮询
        self.agent = Agent(self._emit)
        self._chat_busy = threading.Lock()   # 只串行化对话，防连发导致多 agent 线程并发
        self._login_busy = threading.Lock()  # 登录独立占用，不再阻塞对话

    @staticmethod
    def _err(e):
        """把异常转成结构化错误：白话 message + 技术 detail。"""
        code = getattr(e, "status_code", None)
        if code == 401:
            msg = "API Key 无效或过期，请在设置里重新填写"
        elif code == 429:
            msg = "请求太频繁被限流，请稍后再试"
        elif code and 500 <= code < 600:
            msg = "服务方临时故障，请稍后重试"
        else:
            msg = "操作失败，请展开详情查看原因"
        return {"ok": False, "code": code or "error", "message": msg, "detail": str(e)[:300]}

    def _emit(self, event, data):
        self.q.put({"event": event, "data": data})

    # ---- 界面轮询 ----
    def poll(self):
        """前端定时调用，取走所有待处理事件。"""
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        out.append({"event": "state", "data": tools.get_state()})
        return out

    # ---- 对话 ----
    def chat(self, text):
        """用户发言。下载是长任务，放后台线程，不阻塞界面。"""
        if not self._chat_busy.acquire(blocking=False):
            return {"ok": False, "message": "上一条指令还在处理中，请稍候"}
        def run():
            try:
                self.agent.chat(text)
            finally:
                self._emit("done", {})  # 唯一解锁信号，前端据此结束本轮
                self._chat_busy.release()
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    # ---- 主题 ----
    def get_theme(self):
        return {"theme": config.load().get("theme", "dark")}

    def set_theme(self, theme):
        theme = "light" if theme == "light" else "dark"
        try:
            config.save({"theme": theme})
            return {"ok": True, "theme": theme}
        except Exception as e:
            return self._err(e)

    # ---- 设置 ----
    def get_config(self):
        cfg = config.load()
        key = cfg.get("api_key", "")
        # 不回传真实 Key；前端通过 configured 判断是否已配置，留空即不修改。
        cfg.pop("api_key", None)
        cfg["providers"] = config.PROVIDERS
        cfg["configured"] = bool(key)
        return cfg

    def save_config(self, cfg_json):
        try:
            cfg = json.loads(cfg_json) if isinstance(cfg_json, str) else cfg_json
            # 前端 api_key 留空即不修改；任何非空值都视为用户显式填写的新 Key。
            if not str(cfg.get("api_key", "")).strip():
                cfg.pop("api_key", None)
            config.save(cfg)
            self.agent._refresh_client()
            return {"ok": True, "message": "配置已保存"}
        except Exception as e:
            return self._err(e)

    # ---- 资产库 ----
    def asset_scan(self, force=False):
        return assets.scan(include_duration=True, force=bool(force))

    def asset_open(self, path):
        return assets.open_file(path)

    def asset_codec(self, path):
        return assets.probe_codec(path)

    def asset_locate(self, path):
        return assets.locate(path)

    def check_llm(self):
        """测试 LLM 是否可用（发一条最小请求）。"""
        if not self.agent.configured():
            return {"ok": False, "message": "未配置 API key，请先填写并保存", "detail": ""}
        try:
            resp = self.agent.client.chat.completions.create(
                model=self.agent.model,
                messages=[{"role": "user", "content": "回复OK两个字"}],
                max_tokens=200,
            )
            return {"ok": True, "message": "连接正常：" + (resp.choices[0].message.content or "").strip()}
        except Exception as e:
            return self._err(e)

    def _bg_task(self, fn, done_event, lock=None):
        """后台执行长任务（登录/下载），避免阻塞桥线程；done 事件必定推送。"""
        lock = lock or self._chat_busy
        if not lock.acquire(blocking=False):
            return {"ok": False, "message": "有任务正在进行，请稍候"}
        def run():
            try:
                r = fn()
            except Exception as e:
                r = self._err(e)
            finally:
                # done 在 finally 中统一推送：即使工具抛异常，前端也能解锁
                self._emit(done_event, r)
                lock.release()
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def xhs_login(self):
        """打开小红书窗口扫码登录（后台执行）。"""
        r = self._bg_task(tools.xhs_login, "xhs_login_done", lock=self._login_busy)
        if r.get("ok"):
            r["message"] = "已打开小红书窗口，请扫码"
        return r

    def site_login(self, url):
        """通用站点登录（后台执行）：打开网址，登录后点"完成登录"。"""
        r = self._bg_task(lambda: tools.site_login(url), "site_login_done", lock=self._login_busy)
        if r.get("ok"):
            r["message"] = "已打开登录窗口，登录后在设置页点「完成登录」"
        return r

    def confirm_login(self):
        """前端"完成登录"：通知后台导出 Cookie。"""
        return tools.confirm_login()

    def clear_logs(self):
        """清空运行日志（前端"清空"按钮）。"""
        return tools.clear_logs()

    def get_paths(self):
        """返回当前数据目录，供设置页展示/确认。"""
        return {"ok": True, "root": config.ROOT, "dir_bili": config.DIR_BILI,
                "dir_douyin": config.DIR_DOUYIN}


def main():
    api = Api()
    web_dir = os.path.join(config._RES_DIR, "web")
    window = webview.create_window(
        "视频下载助手",
        os.path.join(web_dir, "index.html"),
        js_api=api,
        width=1180, height=760,
        min_size=(900, 600),
        background_color="#F5F5F5",
    )
    icon = os.path.join(config._RES_DIR, "assets", "app.ico")
    webview.start(gui="edgechromium", debug=False,
                  icon=icon if os.path.isfile(icon) else None)


if __name__ == "__main__":
    main()
