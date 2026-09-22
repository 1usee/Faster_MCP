"""支持 `python -m faster_mcp` 启动服务。

这个文件存在的意义：让"不带任何参数运行"成为最简单的启动方式，
模型调用与人工调试都用得上。真正的逻辑在 server.py，本文件不做任何加工。
"""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
