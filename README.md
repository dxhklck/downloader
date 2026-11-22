# 视频下载助手

对话式下载工具。用一句话告诉它下载什么，它调用后台能力完成。

## 启动

双击 `启动.bat`。弹出"视频下载助手"窗口即可用。

## 首次配置（一次）

1. 点右上角 ⚙ 打开设置
2. 供应商选"智谱 GLM"（glm-4-flash 永久免费），去 open.bigmodel.cn 手机号注册，创建 API Key
3. 粘贴 Key，点"测试连接"，通过后点"保存"
4. Key 只存本机 `config.json`，不上传

## 怎么用

在对话框输入，回车发送：

| 你说 | 它做什么 |
|---|---|
| 下载柯南第1到100集 | B站番剧按集下载，1080P，自动合并 |
| 查询柯南下完了吗 | 统计已下集数、缺哪些集 |
| 随机刷20个视频 | 抖音推荐流随机下载 |
| 下20个唱歌的 | 抖音筛选唱歌/翻唱类 |
| **粘贴一个视频链接** | **按链接下载（YouTube/B站/抖音等），自动合并音视频** |
| 抖音登录 | 弹出窗口扫码登录 |
| 清理临时文件 | 删除 B站目录的 .v/.a 碎片 |

> - **YouTube 等墙外站点**：打开你的代理软件（开启系统代理）即可，程序会自动探测并使用，**不用在设置里填**。只有自动探测不到时，才在设置里手动填代理地址（如 `http://127.0.0.1:7890`）。国内站点无需代理。
> - **需要登录的站点（小红书等）**：在设置里用「网站登录」——填入网站首页 → 打开登录窗口 → 登录后回来点「完成登录」。登录态自动记住，之后发该站链接即可下载。小红书另有专用「扫码登录」按钮（自动识别登录，无需点完成）。
> - 一次登录一个站点，Cookie 合并保存，下次换别的站再登录即可，互不覆盖。

右侧显示当前任务进度和运行日志。

## 架构

```
downloader/
├── main.py          pywebview 窗口 + JS 桥接
├── agent.py         LLM function calling 循环（智谱等）
├── tools.py         下载能力（B站 / 抖音 / 清理）
├── config.py        配置与供应商模板
├── web/             界面（HTML/CSS/JS）
├── config.json      本地配置（填 key 后生成）
└── 启动.bat
```

- 单进程：Python312 + WebView2，不起服务、不占端口
- B站：playurl API 拿 1080P DASH，ffmpeg 合并（沿用已验证脚本的经验）
- 抖音：Playwright route 拦截 + 持久化 profile（登录一次长期用）
- 平台改版时只改 `tools.py` 一处

## 已知限制

- 抖音付费短剧下不了（平台服务端鉴权）
- 部分抖音视频只抓得到音轨，会自动跳过
- B站集中下载时临时碎片会累积，用"清理临时文件"收尾
- 需要 Python3.12 + pywebview + openai + playwright + ffmpeg 环境（本机已就绪）

## 给别人用

复制整个 `downloader` 文件夹到对方机器，装好同样的 Python 环境和依赖，各自填自己的 API Key 即可。

## 安装包（免 Python 环境）

三大平台的安装包由 GitHub Actions 自动构建，打 tag（如 `git tag v1.0.0 && git push --tags`）即触发：

| 平台 | 安装包 | 说明 |
|---|---|---|
| Windows | `视频下载助手-x.y.z-setup.exe` | Inno Setup 安装器，桌面快捷方式，可卸载 |
| macOS | `视频下载助手-x.y.z-macos.dmg` | 拖入 Applications；未签名，首次打开需右键→打开 |
| Linux | `视频下载助手-x.y.z-linux-amd64.deb` | Debian/Ubuntu 系安装，含桌面入口 |

产物在仓库 Actions 页的 Artifacts 下载；打 tag 的版本会自动发布到 Releases。安装包内已带 Python 运行时、ffmpeg 和预置模型配置，开箱即用。

## 自己构建

```bash
pip install -r requirements.txt pyinstaller
python make_icon.py        # 生成三平台图标（已生成，可跳过）
python build.py            # 本机打包（Windows 出绿色文件夹）
```

打包细节见 `打包部署说明.md`；站点支持情况见 `站点兼容性测试_20261122.md`。

## 开发约定

- 代码在 `downloader/` 单目录，核心模块五件套 + `core/`（抖音抓取内核）+ `web/`（界面）
- 平台差异统一收敛在：`tools._browser_channel_kwargs()`（浏览器）、`assets._os_open/_os_reveal`（文件操作）、`config.system_proxy_url()`（代理探测）
- 修复 bug 前先读 `代码审计报告_20261122.md` 里的历史坑（如 WorkBuddy 沙箱的 safe-delete hook）

