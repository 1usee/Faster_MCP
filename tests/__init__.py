"""让 tests/ 目录可被 pytest 发现。

有了这个文件，测试可以直接 `import faster_mcp`（只要项目根在 sys.path 上，
pytest 的 rootdir 机制会自动处理）。同时把它标记为包，避免同名测试文件冲突。
"""
