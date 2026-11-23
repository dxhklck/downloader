"""Cookie 转换与合并的纯函数测试。"""
import tools


def test_cookies_to_lines_netscape_format():
    lines = tools._cookies_to_lines([
        {"domain": ".example.com", "name": "a", "value": "1", "path": "/", "secure": True, "expires": 0},
        {"domain": "site.com", "name": "b", "value": "2", "path": "/x", "secure": False, "expires": -1},
    ])
    assert lines[0].split("\t") == [".example.com", "FALSE", "/", "TRUE", "0", "a", "1"]
    assert lines[1].split("\t") == ["site.com", "TRUE", "/x", "FALSE", "0", "b", "2"]


def test_merge_cookies_overwrites_same_domain_name(tmp_path, monkeypatch):
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\nold.com\tTRUE\t/\tFALSE\t0\tsid\told\nkeep.com\tTRUE\t/\tFALSE\t0\tk\tv\n", encoding="utf-8")
    monkeypatch.setattr(tools.config, "COOKIES_FILE", str(path))

    total = tools._merge_cookies_file(["old.com\tTRUE\t/\tFALSE\t0\tsid\tnew"])
    content = path.read_text(encoding="utf-8")
    assert "old.com\tTRUE\t/\tFALSE\t0\tsid\tnew" in content
    assert "sid\told" not in content
    assert "keep.com" in content
    assert total == 2


class _FakeYdl:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=True):
        return {"title": "测试视频", "duration": 1}


def test_url_download_uses_custom_safe_directory(tmp_path, monkeypatch):
    import sys
    import types
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=_FakeYdl))
    tools.finish()
    result = tools.url_download("https://example.com/video", out_dir="B站comfyui")
    assert result["success"] is True
    assert result["data"]["out_dir"] == str(tmp_path / "B站comfyui")
    assert (tmp_path / "B站comfyui").is_dir()


def test_url_download_rejects_unsafe_directory(tmp_path, monkeypatch):
    tools.finish()
    result = tools.url_download("https://example.com/video", out_dir="../outside")
    assert result["success"] is False


def test_search_videos_returns_candidates_without_download(monkeypatch):
    import sys
    import types

    class FakeYdl:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, query, download=False):
            assert download is False
            assert query.startswith("bilisearch8:")
            return {"entries": [
                {"title": "视频A", "url": "https://www.bilibili.com/video/BV1",
                 "id": "BV1", "duration": 60, "uploader": "老师"},
                {"title": "视频B", "webpage_url": "https://www.bilibili.com/video/BV2",
                 "id": "BV2", "duration": 0, "uploader": ""},
            ]}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYdl))
    result = tools.search_videos("信息科技", platform="bili", count=8, out_dir="课程")
    assert result["success"] is True
    assert len(result["data"]["items"]) == 2
    assert result["data"]["items"][0]["url"] == "https://www.bilibili.com/video/BV1"
    assert result["data"]["out_dir"] == "课程"


def test_site_login_rejects_bad_url():
    result = tools.site_login("ftp://example.com")
    assert result["success"] is False
    assert "http" in result["message"]


def test_get_login_status_never_returns_cookie_values(tmp_path, monkeypatch):
    bili = tmp_path / "bili.txt"
    bili.write_text(".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tsecret\n", encoding="utf-8")
    monkeypatch.setattr(tools.config, "COOKIE_BILI", str(bili))
    monkeypatch.setattr(tools.config, "COOKIE_DOUYIN", str(tmp_path / "none.txt"))
    monkeypatch.setattr(tools.config, "XHS_COOKIE_FILE", str(tmp_path / "none2.txt"))
    monkeypatch.setattr(tools.config, "COOKIES_FILE", str(tmp_path / "none3.txt"))
    status = tools.get_login_status()
    assert status["bili"]["status"] == "ok"
    assert "secret" not in repr(status)


def test_create_folder_rejects_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    assert tools.create_folder("..\\outside")["success"] is False
    assert tools.create_folder("C:/Windows/Temp")["success"] is False
    assert not (tmp_path.parent / "outside").exists()


def test_create_folder_rejects_too_long_name(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    assert tools.create_folder("a" * 81)["success"] is False


def test_create_folder_allows_safe_relative_path(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    result = tools.create_folder("动漫/2026/TEST")
    assert result["success"] is True
    assert (tmp_path / "动漫" / "2026" / "TEST").is_dir()


def test_asset_search_never_exposes_cookie_files(tmp_path, monkeypatch):
    import assets
    root = tmp_path / "videos"
    root.mkdir()
    items = [
        {"name": "clip.mp4", "path": str(root / "clip.mp4"), "rel": "videos/clip.mp4",
         "cat": "videos", "size": 1, "mtime": 0},
        {"name": "cookies.txt", "path": str(tmp_path / "cookies.txt"),
         "rel": "cookies.txt", "cat": "root", "size": 1, "mtime": 0},
    ]
    monkeypatch.setattr(assets, "scan", lambda **kw: {"success": True, "message": "ok",
        "data": {"items": items, "stats": {}, "scanned_at": 0}})
    result = tools.asset_search()
    assert result["success"] is True
    assert [it["name"] for it in result["data"]["items"]] == ["clip.mp4"]


def test_asset_search_caps_results(tmp_path, monkeypatch):
    import assets
    root = tmp_path / "videos"
    root.mkdir()
    for i in range(30):
        (root / ("v%02d.mp4" % i)).write_bytes(b"x")
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    monkeypatch.setattr(assets, "scan", lambda **kw: {"success": True, "message": "ok", "data": {
        "items": [{"name": "v%02d.mp4" % i, "path": str(root / ("v%02d.mp4" % i)),
                   "rel": "videos/v%02d.mp4" % i, "cat": "videos", "size": 1, "mtime": 0}
                  for i in range(30)], "stats": {}, "scanned_at": 0}})
    result = tools.asset_search(ext=".mp4", limit=100)
    assert result["success"] is True
    assert len(result["data"]["items"]) == 20
    assert result["data"]["limit"] == 20


def test_move_and_copy_asset_inside_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    src = tmp_path / "a.mp4"
    src.write_bytes(b"video")
    assert tools.copy_asset(str(src), str(tmp_path / "sub" / "copy.mp4"))["success"] is True
    assert (tmp_path / "sub" / "copy.mp4").read_bytes() == b"video"
    assert tools.move_asset(str(src), str(tmp_path / "sub" / "moved.mp4"))["success"] is True
    assert not src.exists()


def test_file_operations_reject_escape_and_overwrite(tmp_path, monkeypatch):
    other = tmp_path.parent / "outside.mp4"
    other.write_bytes(b"x")
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    media = tmp_path / "one.mp4"
    media.write_bytes(b"x")
    assert tools.move_asset(str(media), str(other))["success"] is False
    target = tmp_path / "two.mp4"
    target.write_bytes(b"x")
    assert tools.copy_asset(str(media), str(target))["success"] is False


def test_trash_and_restore_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    monkeypatch.setattr(tools.config, "_APP_DIR", str(tmp_path / "app"))
    media = tmp_path / "one.mp4"
    media.write_bytes(b"x")
    trashed = tools.trash_asset(str(media))
    assert trashed["success"] is True
    assert not media.exists()
    restored = tools.restore_asset(trashed["data"]["name"])
    assert restored["success"] is True
    assert media.exists()


def test_list_trashed_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    monkeypatch.setattr(tools.config, "_APP_DIR", str(tmp_path / "app"))
    assert tools.list_trashed()["data"]["items"] == []
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    trashed = tools.trash_asset(str(media))
    listed = tools.list_trashed()
    assert listed["success"] is True
    assert [it["name"] for it in listed["data"]["items"]] == [trashed["data"]["name"]]


def test_organize_preview_apply(tmp_path, monkeypatch):
    import assets
    monkeypatch.setattr(tools.config, "ROOT", str(tmp_path))
    src = tmp_path / "a.mp4"
    src.write_bytes(b"x")
    monkeypatch.setattr(assets, "scan", lambda **kw: {"success": True, "message": "ok", "data": {
        "items": [{"name": "a.mp4", "path": str(src), "rel": "a.mp4", "cat": "root",
                   "size": 1, "mtime": 0, "dur": 0}], "stats": {}, "scanned_at": 0}})
    preview = tools.organize_preview("ext", category="mp4")
    assert preview["success"] is True
    pid = preview["data"]["preview_id"]
    assert src.exists()
    applied = tools.organize_apply(pid)
    assert applied["success"] is True
    assert not src.exists()
    assert (tmp_path / "MP4" / "a.mp4").exists()
    assert tools.organize_apply(pid)["success"] is False
