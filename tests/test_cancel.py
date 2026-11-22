"""任务取消与每集状态上报测试。"""
import tools


def test_cancel_task_requires_running_task():
    tools.finish()
    result = tools.cancel_task()
    assert result["success"] is False
    assert "没有运行中的任务" in result["message"]


def test_cancel_task_sets_flag():
    assert tools.try_start("测试任务") is True
    try:
        assert tools._cancelled() is False
        result = tools.cancel_task()
        assert result["success"] is True
        assert tools._cancelled() is True
    finally:
        tools.finish()


def test_try_start_resets_cancel_and_episodes():
    assert tools.try_start("任务一") is True
    tools.cancel_task()
    tools.set_episode(3, "ok")
    tools.finish()
    assert tools.try_start("任务二") is True
    try:
        assert tools._cancelled() is False
        assert tools.get_state()["episodes"] == {}
    finally:
        tools.finish()


def test_set_episode_statuses():
    assert tools.try_start("剧集") is True
    try:
        tools.set_episode(1, "running")
        tools.set_episode(2, "ok")
        tools.set_episode(3, "fail")
        eps = tools.get_state()["episodes"]
        assert eps == {"1": "running", "2": "ok", "3": "fail"}
    finally:
        tools.finish()
