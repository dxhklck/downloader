# Agent 能力增强计划

## 目标

把当前“发链接/一句话下载”的助手，升级为能围绕下载任务和本地资产库进行多步协作的轻量 Agent：

- 能理解并执行现有下载、登录、状态查询工具；
- 能在信息不全时向用户提出有限选项；
- 能把复杂请求拆成可见的执行计划；
- 能创建资产目录、扫描资产、按条件查找和定位视频；
- 能在资产库内移动/重命名、复制、删除到回收站和按规则批量整理视频文件；
- 能记住短期会话上下文，并在失败后有限恢复；
- 所有高风险操作保留硬约束，不让模型获得任意路径、任意命令或敏感信息访问能力。

本计划只借鉴 cc-haha 的架构思想，不复制其源码。cc-haha 使用自定义非商业 source-available 许可证，直接移植代码会带来许可和署名义务。

## 现状

- `agent.py`：OpenAI 兼容 function calling 循环，最多 8 轮；工具有平台硬路由、Schema 参数白名单和工具结果截断。
- `main.py`：FIFO 单 worker 任务队列、任务取消、重试入口、登录/配置/更新接口。
- `tools.py`：B站/抖音/通用链接下载、登录、状态、清理临时文件。
- `assets.py`：资产扫描缓存、编码探测、打开/定位视频文件。
- `web/app.js`：消息、工具状态卡、失败重试、队列显示、历史记录。

缺口：服务端没有结构化会话历史；没有计划/澄清交互；Agent 不能直接查资产或创建文件夹；工具事件缺少稳定 id；失败恢复只有简单错误提示。

## 总体原则

1. **保守能力边界**：Agent 只能调用白名单工具；不能执行命令、访问任意路径、读取 Cookie、导出 Key；删除仅限应用内回收站。
2. **路径安全**：所有资产写操作限制在 `config.ROOT` 内；拒绝 `..`、绝对路径逃逸、符号链接逃逸和非法文件名。
3. **敏感信息隔离**：API Key、Cookie、代理密码不进入模型上下文，也不出现在导出配置。
4. **用户可见**：计划、澄清、工具调用、失败原因、重试入口都在对话区展示。
5. **可测试**：每个新工具都有纯逻辑或 monkeypatch 测试；前端事件协议保持向后兼容。

## 阶段 1：短期会话记忆与动态上下文

改动文件：`agent.py`、`tests/test_agent.py`。

### 内容

- 在 `Agent` 内维护结构化 `messages`，只保存 role/content/tool_calls/tool_call_id，不保存前端 HTML。
- 每次 `chat()` 注入动态上下文：
  - 当前任务状态（来自 `tools.get_state()`）；
  - 当前排队任务概要；
  - 登录状态摘要（只含已登录/未登录/未知）；
  - 资产根目录与可用分类摘要；
  - 当前日期。
- 系统提示词拆成“静态规则 + 动态状态”两段，静态规则保持稳定。
- 当历史超过阈值时生成短摘要，保留最近若干轮原文；摘要失败则退回简单裁剪，不阻断对话。
- 历史仅存进程内，不落盘；前端 localStorage 继续只负责展示恢复。

### 验证

- `python -m pytest -q tests/test_agent.py`
- 新测试断言：动态状态进入 messages；超长历史被裁剪/摘要；Cookie/API Key 不进入消息。

## 阶段 2：计划与澄清工具

改动文件：`agent.py`、`main.py`、`web/app.js`、`web/index.html`、`web/style-graphite.css`、`tests/test_agent.py`、`tests/test_main.py`。

### 新工具

- `make_plan(steps)`：把 2–6 步计划写入会话状态并推送 `plan` 事件；前端渲染步骤卡。
- `ask_choice(question, options)`：推送 `ask_choice` 事件，前端渲染 2–4 个选项；后端用 `threading.Event` 等待用户在 120 秒内选择，超时返回“用户未选择”。答案作为 tool result 回灌模型。
- `Api.answer_choice(answer_id, value)`：接收用户选择，唤醒等待中的工具调用。

### 约束

- `ask_choice` 不允许询问 Key、Cookie、密码等敏感信息。
- 计划工具只展示，不自行改变任务状态；真正的动作仍由下载/资产工具完成。
- B站/抖音平台硬路由优先级高于模型和计划。

### 验证

- `node --check web/app.js`
- `python -m pytest -q tests/test_agent.py tests/test_main.py`
- 测试断言：选项事件带唯一 id；重复/超时答案不会误唤醒；模型收到用户选择文本。

## 阶段 3：资产管理与文件夹工具

改动文件：`tools.py`、`assets.py`、`agent.py`、`main.py`、`tests/test_tools.py`、`tests/test_agent.py`。

### 新工具

- `asset_stats()`：返回资产总数、总大小、分类统计、最近扫描时间。
- `asset_search(keyword, ext, category, min_size_mb, limit)`：按文件名/相对路径/扩展名/分类/大小筛选，默认最多 20 条，防止结果过大。
- `create_folder(path)`：在 `config.ROOT` 下创建单层或多层目录，返回安全路径；目录已存在时返回 success 并说明。
- `reveal_asset(path)`：在文件管理器中定位视频；复用 `assets.locate()` 的 ROOT 白名单。
- `scan_assets(force=False)`：触发资产扫描；复用现有缓存逻辑，只返回统计摘要，不把全量列表回灌模型。

### 受限文件操作（用户确认后新增）

- `move_asset(src, dest)`：移动或重命名资产库内视频；目标必须仍在 ROOT 内；目标已存在时拒绝覆盖。
- `copy_asset(src, dest)`：复制视频到资产库内其他目录；目标已存在时拒绝覆盖。
- `trash_asset(path)`：把视频移入应用内回收站目录并记录原路径，不做永久删除。
- `restore_asset(name)`：从回收站恢复到原路径；目标已被占用时拒绝覆盖。
- `organize_preview(rule, category, value)`：按分类、日期、扩展名或大小生成批量整理预览，不实际改动文件。
- `organize_apply(preview_id)`：按已预览的变更批量执行；预览 id 一次性使用，避免过期误操作。

### 明确不做

- 不做永久删除、资产根目录外文件操作、任意命令执行和同名覆盖。
- 单个移动/重命名/复制直接执行；批量整理先预览变更，用户确认后再执行。
- 不把 Cookie 文件、profile、配置文件、缓存文件作为资产暴露给 Agent。
- 不向模型返回完整资产列表，只返回筛选后的小结果。

### 路径安全

- `create_folder` 使用 `os.path.realpath` 校验目标必须在 `config.ROOT` 内。
- 拒绝绝对路径、盘符路径、UNC 路径、`..` 穿越、NUL 字符和保留设备名。
- 目录名长度、层级深度设上限；创建失败返回结构化错误。

### 验证

- `python -m pytest -q tests/test_tools.py`
- 新测试断言：合法目录创建成功；`..`/绝对路径被拒绝；搜索结果有上限；资产结果不含 Cookie 路径。

## 阶段 4：工具事件与失败恢复

改动文件：`agent.py`、`main.py`、`web/app.js`、`web/style-graphite.css`、`tests/test_agent.py`。

### 内容

- 事件统一携带 `tool_call_id`、`name`、`args`、`status`、`elapsed_ms`。
- 工具结果统一字段：`success/message/data/error_code/retryable`。
- 模型 API 失败时限量重试一次；重试提示明确“不要重复道歉，直接继续未完成任务”。
- 工具参数错误、未知工具、执行异常都转成结构化 tool result 回灌模型，不中断整轮对话。
- 大工具结果落盘为本地 JSON，回灌短预览和路径；不在对话里塞满日志。
- 前端工具卡支持状态：调用中/等待选择/完成/失败/取消，并显示耗时和错误码。

### 验证

- `python -m pytest -q tests/test_agent.py tests/test_main.py`
- `node --check web/app.js`
- 测试断言：一次 API 失败后能继续；重复错误不会无限重试；大结果被裁剪且有预览路径。

## 阶段 5：全量验证与交付

命令：

```bash
python -m py_compile main.py tools.py config.py assets.py agent.py notify.py logutil.py build.py updater.py
node --check web/app.js
python -m pytest -q tests/
git diff --check
```

手工验证：

1. 连续发送两条任务，队列与状态卡正确。
2. 对 B站请求误选抖音工具仍被硬拦截。
3. 信息不全时 Agent 能发起选择题，用户选项能回灌。
4. 让 Agent 创建资产目录：合法路径成功，`..` 被拒绝。
5. 让 Agent 查找资产：结果有上限、可定位文件、无 Cookie 路径。
6. 失败工具能显示原因并重试；取消任务后不会继续重试。
7. 窄窗口下计划卡、选项卡、工具卡不溢出。

## 不在本计划内

- 多 Agent 团队、MCP、插件市场、Skills、Computer Use。
- 永久删除、绕过回收站的删除、任意路径移动复制和自动覆盖同名文件。
- 自动安装更新、任意命令执行、任意路径读写。
- 各站点专用画质解析器和登录自动化扩展。
- 直接复制 cc-haha 源码或前端组件。

## 交付标准

计划文档写入仓库；按阶段完成代码和测试；全量验证通过；不主动 commit/push。若某阶段需要扩大到删除/移动用户文件或改动外部共享状态，先停下询问用户。
