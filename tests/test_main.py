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
