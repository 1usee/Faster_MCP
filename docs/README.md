# 文档索引

| 文档 | 读者 | 什么时候看 |
|---|---|---|
| [../README.md](../README.md) | 使用者 | 第一次接触项目：它是什么、怎么装、怎么接到客户端 |
| [EXTENDING.md](EXTENDING.md) | 加功能的人 | **要加新功能时先看这份**。三步流程、写作规范、常见坑、检查清单 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 改架构的人 | 想动 loader/registry/server 时，先看它的图解与设计理由 |
| [design-notes.md](design-notes.md) | 想理解取舍的人 | 想知道"为什么不用 eval""为什么要白名单"等具体决策 |
| [../examples/](../examples) | 接入的人 | 各类 MCP 客户端的配置片段 |

## 三十秒速览

- **一个功能 = `faster_mcp/features/` 下的一个子包**。加功能就是加目录，不改既有文件。
- **注册在 import 阶段自动完成**（`@feature_tool` 装饰器 + loader 扫描）。
- **工具描述是写给模型的**：必须回答"做什么 / 何时用 / 返回什么"。
- **错误要抛 `ToolInputError` 并在消息里列出合法取值**，这样模型能自我纠正。
- **stdio 传输下不许 print 到 stdout**，日志一律走 stderr。
- 加完跑 `python -m faster_mcp --list` 和 `python scripts\smoke_test.py`。
