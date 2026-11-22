"""Api 层 FIFO 任务队列测试。"""
import threading
import time

import main


def test_chat_jobs_are_queued_and_run_in_order(monkeypatch):
    api = main.Api()
    calls = []
    gate = threading.Event()
    release = threading.Event()

    def fake_chat(text):
        calls.append("start:" + text)
        if text == "first":
            gate.set()
            release.wait(3)
        calls.append("end:" + text)

    monkeypatch.setattr(api.agent, "chat", fake_chat)

    id1 = api.chat("first")["id"]
    assert gate.wait(2)
    assert api._chat_busy.locked() is True
    id2 = api.chat("second")["id"]
    assert id2 == id1 + 1
    assert api._current_job["id"] == id1

    release.set()
    deadline = time.time() + 3
    while time.time() < deadline and calls.count("end:second") == 0:
        time.sleep(0.02)

    assert calls == ["start:first", "end:first", "start:second", "end:second"]
    assert api._current_job is None
