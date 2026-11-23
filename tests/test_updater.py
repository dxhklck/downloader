"""更新检查：版本比较与各平台安装包选择。"""
import updater


def test_version_compare():
    assert updater.is_newer("v1.0.7", "1.0.6") is True
    assert updater.is_newer("1.0.6.0", "1.0.6") is False
    assert updater.is_newer("v1.0.5", "1.0.6") is False


def test_select_asset_by_platform():
    assets = [
        {"name": "video_download-1.0.6.0-setup.exe"},
        {"name": "video_download-1.0.6.0-macos.dmg"},
        {"name": "video_download-1.0.6.0-linux-amd64.deb"},
        {"name": "Source code (zip)"},
    ]
    assert updater.select_asset(assets, "win32", "AMD64").endswith(".exe")
    assert updater.select_asset(assets, "darwin", "arm64").endswith(".dmg")
    assert updater.select_asset(assets, "linux", "x86_64").endswith(".deb")
    assert updater.select_asset(assets, "linux", "aarch64") is None
