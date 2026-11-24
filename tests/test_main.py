"""Api 后台任务测试：登录锁分离与 done 事件必定推送。"""
import time

import main


def test_login_task_does_not_block_chat():
    """登录占用 _login_busy，不应阻塞对话用的 _chat_busy。"""
    api = main.Api()
    assert api._login_busy.acquire(blocking=False) is True
    # 登录锁被占用时，对话锁仍可用
    assert api._chat_busy.acquire(blocking=False) is True
    api._chat_busy.release()
    api._login_busy.release()


def test_bg_task_emits_done_on_exception():
    """后台任务抛异常时，done 事件必须推送，前端才能解锁。"""
    api = main.Api()

    def boom():
        raise RuntimeError("模拟异常")

    result = api._bg_task(boom, "site_login_done")
    assert result["ok"] is True
    for _ in range(50):
        events = api.poll()
        done = [e for e in events if e["event"] == "site_login_done"]
        if done:
            assert done[0]["data"]["ok"] is False
            return
        time.sleep(0.02)
    raise AssertionError("未收到 site_login_done 事件")


def test_record_events_persist_to_current_conversation(tmp_path, monkeypatch):
    import conversation_store
    monkeypatch.setattr(conversation_store.config, "CONVERSATIONS_DIR", str(tmp_path))
    api = main.Api()
    cid = api.agent.conversation_id
    api._current_job = {"kind": "chat", "id": 0}
    api._emit("record", {"type": "user", "text": "测试记录"})
    api._current_job = None
    records = conversation_store.read(cid).get("records") or []
    assert records and records[-1]["text"] == "测试记录"
    switched = api.switch_conversation(cid)
    assert switched["records"][-1]["text"] == "测试记录"


def test_download_batch_enqueues_downloads_and_agent_report():
    api = main.Api()
    result = api.download_batch([
        {"title": "视频A", "url": "https://www.bilibili.com/video/BV1"},
        {"title": "无效", "url": "ftp://bad"},
    ], out_dir="课程")
    assert result["ok"] is True
    assert len(result["ids"]) == 2  # 1 个下载 + 1 个 Agent 汇报
    jobs = list(api._jobs.queue)
    download_jobs = [j for j in jobs if j.get("name") == "url_download"]
    assert len(download_jobs) == 1
    assert all(j["args"]["url"].startswith("http") for j in download_jobs)
    assert download_jobs[0]["args"]["out_dir"] == "课程"
    assert jobs[1]["kind"] == "chat"
    assert "用户已选择" in jobs[1]["text"]


def test_list_conversations_shows_title_without_message_count(tmp_path, monkeypatch):
    import conversation_store
    monkeypatch.setattr(conversation_store.config, "CONVERSATIONS_DIR", str(tmp_path))
    cid = conversation_store.new_id()
    conversation_store.save(cid, [{"role": "user", "content": "测试标题"}], title="")
    api = main.Api()
    items = api.list_conversations()["items"]
    assert items[0]["title"] == "测试标题"
    assert "messages" not in items[0]


def test_switch_conversation_migrates_legacy_display(tmp_path, monkeypatch):
    import conversation_store
    monkeypatch.setattr(conversation_store.config, "CONVERSATIONS_DIR", str(tmp_path))
    cid = conversation_store.new_id()
    conversation_store.save(cid, [], display=['<div class="msg msg-user">旧消息</div>'])
    data = conversation_store.read(cid)
    data.pop("records", None)
    (tmp_path / (cid + ".json")).write_text(__import__("json").dumps(data, ensure_ascii=False),
                                            encoding="utf-8")
    api = main.Api()
    switched = api.switch_conversation(cid)
    assert switched["records"][0]["type"] == "msg"
    assert "旧消息" in switched["records"][0]["text"]


def test_asset_trash_batch_summarizes_failures(monkeypatch):
    api = main.Api()
    monkeypatch.setattr(main.tools, "trash_asset", lambda path: (
        {"success": True} if path == "ok.mp4" else {"success": False, "message": "占用中"}))
    result = api.asset_trash_batch(["ok.mp4", "bad.mp4"])
    assert result["success"] is True
    assert result["data"]["ok"] == 1
    assert result["data"]["failed"][0]["path"] == "bad.mp4"


def test_retry_tool_allows_whitelisted_tool():
    api = main.Api()
    result = api.retry_tool("url_download", {"url": "https://example.com/v", "evil": 1})
    assert result["ok"] is True
    job = list(api._jobs.queue)[-1]
    assert job["args"]["url"] == "https://example.com/v"


def test_retry_tool_blocks_planning_tools():
    api = main.Api()
    assert api.retry_tool("ask_choice", {})["ok"] is False
    assert api.retry_tool("make_plan", {})["ok"] is False
    assert api.retry_tool("get_state", {})["ok"] is False
