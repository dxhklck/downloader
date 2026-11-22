"""自包含核心模块：抖音下载与音轨修复（从根目录脚本收编）。
对外只暴露 download_one / grab_audio / mux / has_audio 等函数，
路径全部由 config 派生，不再硬编码机器路径。
"""
