# DBeaver Database MCP

面向 Codex 的本地 MCP 服务器。它复用 DBeaver 中已经保存的 PostgreSQL 连接，以结构化 JSON 描述查询，并由服务端生成参数化的只读 `SELECT`。服务不提供自由 SQL、写入语句、任意函数调用、临时连接参数或任意文件读取入口。

## 适用范围

- 发现 DBeaver 中已有的 PostgreSQL 连接、数据库、schema、表、视图、字段、约束、索引和注释。
- 分析、解释和执行结构化只读查询，支持关联、聚合、CTE、集合运算、窗口函数、JSON、数组和子查询等受控表达式。
- 读取当前数据库配置的非敏感值。
- 在 PostgreSQL 已启用受支持日志格式且当前账号具备权限时，返回经过脱敏的日志事件摘要。

该服务仅支持 DBeaver 本地工作区中的 PostgreSQL 直连配置。启用 SSH、代理或其他网络处理器的连接不会被使用。

## 安全边界

- 连接地址、用户名和密码只从当前用户的 DBeaver 工作区读取，不作为 MCP 参数返回或接受。
- 每个数据库会话均使用只读事务，并在结束时回滚和关闭。
- 查询值使用数据库参数绑定；表名、字段名、运算符、函数、类型和查询结构均经过封闭式校验。
- PostgreSQL 原生权限、RLS 和视图权限保持生效；服务不切换角色、不提升权限，也不修改授权。
- 行数、请求大小、响应大小、语句时间和锁等待均有上限。
- 结果与日志内容不由 MCP 服务器落盘；错误信息会移除 SQL、参数值、凭据和原始数据库消息。
- DBeaver 凭据会在本地服务进程内短暂解密以建立连接。运行该服务的操作系统账号应与 DBeaver 工作区所有者相同，并受到同等级别的保护。

只读事务无法证明所有外部数据源都没有远端副作用。未通过依赖审查的函数、外部表访问机制、自定义访问方法和扩展对象会被拒绝。

## 环境要求

- Python 3.11 或更高版本。
- DBeaver Community 或 Enterprise。
- DBeaver 中至少存在一个已保存凭据的 PostgreSQL 连接。
- MCP 进程能够读取当前用户的 DBeaver 工作区，并能够访问目标 PostgreSQL 服务。

默认工作区位置：

| 系统 | 位置 |
| --- | --- |
| Windows | `%APPDATA%\DBeaverData\workspace6\General\.dbeaver` |
| macOS | `~/Library/DBeaverData/workspace6/General/.dbeaver` |
| Linux | `~/.local/share/DBeaverData/workspace6/General/.dbeaver` |

服务需要工作区内的 `data-sources.json` 和 `credentials-config.json`。这些文件包含连接配置或凭据，不应复制到项目目录或提交到 Git。

## 安装

在项目根目录创建独立环境并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

macOS 或 Linux：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install .
```

## 接入 Codex

Codex 支持本地 STDIO MCP 服务器。可在 `~/.codex/config.toml`，或受信任项目的 `.codex/config.toml` 中添加服务器配置：

```toml
[mcp_servers.dbeaver-database]
command = "C:\\absolute\\path\\to\\dbeaver-database-mcp\\.venv\\Scripts\\python.exe"
args = ["-m", "dbeaver_database_mcp.mcp_server"]
startup_timeout_sec = 10
tool_timeout_sec = 45
enabled = true
```

macOS 或 Linux 将 `command` 改为对应虚拟环境中 Python 的绝对路径，例如 `/absolute/path/to/dbeaver-database-mcp/.venv/bin/python`。保存后重启 Codex 客户端。Codex 的 MCP 配置字段以[官方 OpenAI 文档](https://developers.openai.com/codex/mcp/)为准。

也可以在安装后通过 Codex CLI 注册控制台入口：

```text
codex mcp add dbeaver-database -- dbeaver-database-mcp
```

## 工具

| 工具 | 用途 |
| --- | --- |
| `list_dbeaver_connections` | 列出可用的 DBeaver PostgreSQL 连接摘要 |
| `list_databases` | 列出指定连接下可访问的数据库 |
| `dbeaver_status` | 检查目标数据库、只读状态、版本与限制 |
| `list_schemas` | 列出可见 schema 及对象计数 |
| `scan_database` | 扫描指定 schema 的表和视图目录 |
| `search_catalog` | 按名称或注释搜索目录 |
| `get_table_context` | 读取表或视图的字段、约束、索引和注释 |
| `analyze_query` | 校验结构化查询及其对象依赖，不读取业务行 |
| `explain_query` | 返回经过脱敏的执行计划摘要，不运行 `ANALYZE` |
| `execute_query` | 执行有界的结构化只读查询 |
| `read_database_settings` | 读取当前账号可见的非敏感数据库设置 |
| `read_postgresql_logs` | 读取经过脱敏的 PostgreSQL 当前日志摘要 |
| `get_query_capabilities` | 返回结构化查询能力与输入约束 |

调用时应先使用 `list_dbeaver_connections`，再选择精确的 `connection` 和 `database`。数据查询还需要精确的 `schema`。完整查询结构见 [`docs/query-schema.json`](docs/query-schema.json)，通用参数示例见 [`docs/query-examples.json`](docs/query-examples.json)。

## 已知限制

- 不接受 SQL 文本或 SQL 片段。
- 不支持 DBeaver 的网络处理器、临时地址或临时凭据。
- 不保证兼容所有 PostgreSQL 扩展、外部数据包装器、自定义函数或访问方法。
- PostgreSQL 日志读取依赖服务器配置、日志格式和当前连接账号权限。
- 工具的只读边界不改变同一数据库账号在其他客户端中的权限。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。DBeaver、PostgreSQL、Codex 及 OpenAI 是其各自权利人的商标或产品名称；本项目不代表这些项目或公司的官方发布。
