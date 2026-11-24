"""视频下载助手 · 桌面应用入口（pywebview 单进程）。

双击运行或：python main.py
界面用 WebView2 渲染，JS 通过 js_api 直接调用本文件的 Api 类。
"""
import os, sys, json, re, threading, queue, itertools, webview

import config
import tools
import assets
import notify
import updater
from agent import Agent, FUNCS


class Api:
    """暴露给 JavaScript 的 Python 接口。"""

    def __init__(self):
        self.q = queue.Queue()           # 事件队列 -> 界面轮询
        self.agent = Agent(self._emit)
        self._chat_busy = threading.Lock()   # 兼容既有测试：表示 worker 正占用
        self._login_busy = threading.Lock()  # 登录独立占用，不再阻塞对话
        self._jobs = queue.Queue()
        self._job_ids = itertools.count(1)
        self._job_lock = threading.Lock()
        self._current_job = None
        self._allowed_tools = set(FUNCS)
        threading.Thread(target=self._job_worker, daemon=True).start()

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
        payload = dict(data) if isinstance(data, dict) else data
        if isinstance(payload, dict) and self._current_job:
            payload.setdefault("conversation_id", self._current_job.get("cid") or self.agent.conversation_id)
        self.q.put({"event": event, "data": payload})
        if event == "record" and self._current_job:
            import conversation_store
            cid = payload.get("conversation_id") or self.agent.conversation_id
            record = {k: v for k, v in payload.items() if k != "conversation_id"}
            conversation_store.append_record(cid, record)

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

    # ---- 对话队列 ----
    def _job_worker(self):
        while True:
            job = self._jobs.get()
            with self._job_lock:
                self._current_job = job
                self._sync_queue()
            try:
                if job["kind"] == "chat":
                    self._emit("record", {"type": "user", "text": job.get("text", ""),
                                          "conversation_id": job.get("cid")})
                elif job["kind"] == "tool":
                    self._emit("record", {"type": "tool_start", "name": job.get("name", ""),
                                          "args": job.get("args", {}), "conversation_id": job.get("cid")})
                self._emit("job_start", {"id": job["id"], "kind": job["kind"]})
                try:
                    self._chat_busy.acquire()
                    try:
                        if job["kind"] == "chat":
                            self.agent.chat(job["text"])
                        else:
                            self._run_tool(job["name"], job["args"])
                    finally:
                        self._chat_busy.release()
                except Exception as e:
                    self._emit("result", {"success": False, "message": "任务执行失败",
                                          "detail": str(e)[:300]})
            finally:
                with self._job_lock:
                    self._current_job = None
                    self._sync_queue()
                self._emit("done", {"id": job["id"], "kind": job["kind"]})
                self._jobs.task_done()

    def _sync_queue(self):
        queued = []
        for job in list(self._jobs.queue):
            queued.append({"id": job["id"], "kind": job["kind"], "name": job.get("name") or job.get("text", "")})
        tools.set_queue_snapshot(queued, self._current_job)

    def _enqueue(self, kind, **data):
        job = {"id": next(self._job_ids), "kind": kind,
               "cid": self.agent._record_conversation_id, **data}
        with self._job_lock:
            self._jobs.put(job)
            self._sync_queue()
        return job["id"]

    def _run_tool(self, name, args):
        """只执行 Agent 已声明的工具：重试入口也不绕过白名单。"""
        import time
        job_id = self._current_job["id"]
        tool_call_id = "retry_%s_%s" % (job_id, name)
        started = time.monotonic()
        self._emit("tool", {"name": name, "args": args, "job_id": job_id,
                            "tool_call_id": tool_call_id, "status": "running",
                            "elapsed_ms": 0})
        fn = FUNCS.get(name)
        if not callable(fn):
            result = {"success": False, "message": f"未知工具：{name}"}
        elif not isinstance(args, dict):
            result = {"success": False, "message": "工具参数格式不正确"}
        else:
            from agent import _SCHEMA_PARAMS
            import inspect
            accepted = _SCHEMA_PARAMS.get(name, set()) & set(inspect.signature(fn).parameters)
            call_args = {k: v for k, v in args.items() if k in accepted}
            try:
                result = fn(**call_args)
            except Exception as e:
                result = self._err(e)
        elapsed = int((time.monotonic() - started) * 1000)
        self._emit("tool_done", {"name": name, "args": args,
                                 "status": "ok" if result.get("success") else "error",
                                 "tool_call_id": tool_call_id, "elapsed_ms": elapsed,
                                 "error_code": result.get("error_code"),
                                 "retryable": result.get("retryable", not result.get("success"))})
        self._emit("result", result)
        tools.remember_result(result, name)
        if ("download" in name or "random" in name) and (result.get("success") or result.get("cancelled")):
            enabled = config.load().get("notify_on_complete", False)
            if enabled:
                notify.notify("视频下载助手", result.get("message", "下载已完成")[:200])

    def chat(self, text):
        """用户发言：进入 FIFO 队列，由单 worker 顺序执行。"""
        job_id = self._enqueue("chat", text=text)
        return {"ok": True, "id": job_id, "message": "已加入队列"}

    def save_conversation_display(self, html_items):
        """兼容旧前端：现在会话记录由服务端 record 事件统一维护。"""
        return {"ok": True}

    def answer_choice(self, answer_id, value):
        """前端选择题回调：把用户选择送回等待中的 Agent 工具。"""
        return self.agent.answer_choice(answer_id, value)

    # ---- 会话管理 ----
    def list_conversations(self):
        return self.agent.list_conversations()

    def new_conversation(self):
        return self.agent.new_conversation()

    def switch_conversation(self, cid):
        return self.agent.load_conversation(cid)

    def delete_conversation(self, cid):
        return self.agent.delete_conversation(cid)

    def confirm_organize(self, preview_id, confirmed=False):
        """批量整理确认：未确认时不执行；确认后走队列重放预览 id。"""
        if not confirmed:
            return {"ok": False, "message": "用户未确认，未执行整理"}
        return self.retry_tool("organize_apply", {"preview_id": preview_id})

    def retry_tool(self, name, args):
        """失败工具重试：与对话共用同一队列，禁止越权重放任意函数。"""
        from agent import NO_RETRY_TOOLS
        base = str(name).split(".")[-1]
        if base not in self._allowed_tools or base in NO_RETRY_TOOLS:
            return {"ok": False, "message": f"该工具不支持重试：{name}"}
        if not isinstance(args, dict):
            return {"ok": False, "message": "重试参数格式不正确"}
        job_id = self._enqueue("tool", name=base, args=args)
        return {"ok": True, "id": job_id, "message": "重试已加入队列"}

    def download_batch(self, items, out_dir=None, quality="best"):
        """把用户从搜索结果点选的多个视频按序入队，最后让 Agent 汇报。"""
        if not isinstance(items, list) or not items:
            return {"ok": False, "message": "请先选择要下载的视频"}
        if len(items) > 12:
            return {"ok": False, "message": "一次最多下载 12 个视频"}
        jobs = []
        selected = []
        for it in items:
            if not isinstance(it, dict):
                continue
            url = str(it.get("url") or "").strip()
            if not re.match(r"^https?://", url, re.I):
                continue
            args = {"url": url, "quality": quality or "best"}
            if out_dir:
                args["out_dir"] = out_dir
            jobs.append(self._enqueue("tool", name="url_download", args=args))
            selected.append({"title": str(it.get("title") or "未命名")[:120], "url": url})
        if not jobs:
            return {"ok": False, "message": "所选视频没有有效链接"}
        titles = "；".join(x["title"] for x in selected[:8])
        chat_id = self._enqueue(
            "chat",
            text="用户已选择 %d 个搜索结果并加入下载队列：%s。下载结果以上述工具结果为准，请只简短确认，不要重复下载或再次搜索。" % (
                len(selected), titles),
        )
        return {"ok": True, "ids": jobs + [chat_id], "chat_id": chat_id,
                "message": "已加入队列 %d 个下载任务" % len(jobs)}

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
        cfg["notify_on_complete"] = bool(cfg.get("notify_on_complete", False))
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

    def asset_trash(self, path):
        """移入应用内回收站，可恢复；不做永久删除。"""
        return tools.trash_asset(path)

    def asset_trash_batch(self, paths):
        """批量移入回收站，逐个处理并汇总成功/失败。"""
        if not isinstance(paths, list) or not paths:
            return {"success": False, "message": "请先选择视频", "data": {"ok": 0, "failed": 0}}
        ok, failed = 0, []
        for path in paths:
            result = tools.trash_asset(path)
            if result.get("success"):
                ok += 1
            else:
                failed.append({"path": path, "message": result.get("message", "删除失败")})
        msg = "已移入回收站 %d 个" % ok
        if failed:
            msg += "，失败 %d 个" % len(failed)
        return {"success": ok > 0, "message": msg,
                "data": {"ok": ok, "failed": failed}}

    def asset_restore(self, name):
        return tools.restore_asset(name)

    def asset_trashed(self):
        return tools.list_trashed()

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

    def cancel_task(self):
        """请求取消当前下载任务。"""
        return tools.cancel_task()

    def get_login_status(self):
        return tools.get_login_status()

    def export_config(self):
        return config.export_config()

    def import_config(self, payload):
        return config.import_config(payload)

    def get_paths(self):
        """返回当前数据目录，供设置页展示/确认。"""
        return {"ok": True, "root": config.ROOT, "dir_bili": config.DIR_BILI,
                "dir_douyin": config.DIR_DOUYIN}

    def get_version_info(self):
        """返回当前版本与运行环境，供设置页“关于”展示。"""
        import platform
        info = updater.build_info()
        return {
            "ok": True,
            "version": updater.current_version(),
            "author": info.get("author", ""),
            "author_url": info.get("author_url", ""),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "root": config.ROOT,
        }

    def check_update(self):
        """检查 GitHub Release 是否有新版本。"""
        return updater.check_update()

    def download_update(self, asset, page):
        """下载更新包到用户下载目录（不自动安装）。"""
        return updater.download_update(asset, page)

    def open_external(self, url):
        """在浏览器打开发布页（麒麟 ARM64 等无预编译包的场景）。"""
        import webbrowser
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return self._err(e)

    def install_update(self, path):
        """运行已下载的安装包；只处理下载目录里的更新产物。"""
        import os
        downloads = os.path.realpath(os.path.join(os.path.expanduser("~"), "Downloads"))
        candidate = os.path.realpath(str(path or ""))
        if not os.path.isfile(candidate):
            return {"ok": False, "message": "安装包不存在，请重新下载"}
        if os.path.dirname(candidate) != downloads:
            return {"ok": False, "message": "安装包路径无效，请重新下载"}
        ext = os.path.splitext(candidate)[1].lower()
        if ext not in (".exe", ".dmg", ".deb"):
            return {"ok": False, "message": "不支持的安装包格式"}
        try:
            if sys.platform == "win32":
                os.startfile(candidate)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", candidate])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", candidate])
            return {"ok": True, "message": "已启动安装包，请按提示完成更新"}
        except Exception as e:
            return self._err(e)


def main():
    api = Api()
    web_dir = os.path.join(config._RES_DIR, "web")
    window = webview.create_window(
        "视频下载助手",
        os.path.join(web_dir, "index.html"),
        js_api=api,
        width=1180, height=760,
        min_size=(760, 520),
        background_color="#F5F5F5",
    )
    icon = os.path.join(config._RES_DIR, "assets", "app.ico")
    # 不硬编码 gui：Windows 用 EdgeChromium，macOS 用 Cocoa，Linux 用 GTK，
    # 由 pywebview 按平台自动选择，否则 mac/Linux 安装包启动即失败。
    webview.start(debug=False, icon=icon if os.path.isfile(icon) else None)


if __name__ == "__main__":
    main()
