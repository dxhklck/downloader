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


def test_download_batch_enqueues_downloads_and_agent_report():
    api = main.Api()
    result = api.download_batch([
        {"title": "视频A", "url": "https://www.bilibili.com/video/BV1"},
        {"title": "无效", "url": "ftp://bad"},
    ], out_dir="课程")
    assert result["ok"] is True
    assert len(result["ids"]) == 2  # 1 个下载 + 1 个 Agent 汇报
    jobs = list(api._jobs.queue)
    assert jobs[0]["name"] == "url_download"
    assert jobs[0]["args"]["out_dir"] == "课程"
    assert jobs[1]["kind"] == "chat"
    assert "用户已选择" in jobs[1]["text"]


def test_retry_tool_blocks_planning_tools():
    api = main.Api()
    assert api.retry_tool("ask_choice", {})["ok"] is False
    assert api.retry_tool("make_plan", {})["ok"] is False
    assert api.retry_tool("get_state", {})["ok"] is False
