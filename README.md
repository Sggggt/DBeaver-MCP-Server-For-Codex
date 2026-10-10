# DBeaver Database MCP

本地 STDIO MCP 服务，复用 DBeaver 保存的连接配置与凭据。模型只提交结构化 JSON；数据库适配器校验对象、生成参数化 SELECT，并在只读事务中执行。服务不接受自由 SQL、SQL 片段、临时地址、账号、密码或文件路径。

## 数据库支持

| 数据库 | 范围 |
| --- | --- |
| PostgreSQL | 目录、结构化查询、只读执行计划、配置及脱敏日志摘要 |
| Oracle 12.1+ | Thin 驱动、基本 Service Name/SID 直连、目录、结构化查询、NLS 会话设置 |

实际连接由 Python 数据库驱动建立；DBeaver 提供保存的配置，不充当查询执行引擎。SSH、代理和其他网络处理器不支持。Oracle 的 TNS 描述符、OCI、钱包及自定义 TLS 配置会明确拒绝，避免忽略保存的安全配置。

Oracle 的 database 参数表示当前保存连接的服务或 SID，不能用于切换服务或 PDB。访问其他服务应使用另一条 DBeaver 连接。Oracle 12.1 的对象名限制为 30 字节；12.2+ 使用 128 字节上限，数据库自身的兼容性设置仍然生效。

## 安全边界

- 条件值全部使用驱动参数绑定。标识符经目录校验并安全引用；结构、函数、运算符和类型来自固定白名单。
- database_type 必须与保存连接的数据库类型一致。未知类型、额外参数和未实现能力均明确拒绝。
- 每次调用独立建立只读事务，保留数据库账号的原生权限，结束时回滚并关闭，不切换角色或提升权限。
- 查询执行前检查可执行依赖。Oracle 拒绝远程数据库链接、外部表、虚拟列、VPD SELECT 策略、函数/域索引、对象类型及无法验证的视图依赖。
- 表达式中的日期与数值显式绑定；Oracle NUMBER 结果使用十进制字符串保持精度。
- 限制请求复杂度、执行时间、返回行数、单元格及响应大小。Oracle 同时使用驱动调用超时和请求取消期限；文本在服务端截断，二进制内容不返回。
- 业务数据、凭据和日志不由 MCP 落盘。错误与诊断不回显 SQL、参数值、地址、凭据或数据库原始消息。

只读事务并不能证明所有 SELECT 依赖都没有外部或自治事务副作用，依赖检查不能跳过。对象定义和安全策略应由可信的数据库管理流程维护；目录审查不是针对恶意并发 DDL 的隔离机制。

## 安装与接入

需要 Python 3.11+、DBeaver，以及已保存账号凭据的受支持连接。Oracle 使用 Thin 模式，不需要安装 Oracle Client。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

在 MCP 客户端中注册虚拟环境 Python，并传入以下启动参数：

```text
-m dbeaver_database_mcp.mcp_server
```

现有启动入口保持兼容。更新安装后重新启动 MCP 服务，使工具定义和已加载的模块生效。

服务自动发现当前用户 DBeaver 工作区内的项目，读取项目 `.project` 中的名称（缺失或无效时使用目录名），并检查各项目 `.dbeaver` 内的连接配置文件。默认工作区：

| 系统 | 位置 |
| --- | --- |
| Windows | %APPDATA%\DBeaverData\workspace6 |
| macOS | ~/Library/DBeaverData/workspace6 |
| Linux | ~/.local/share/DBeaverData/workspace6 |

不依赖 `General` 项目，也不通过 MCP 参数接受任意文件路径。项目 ID 是工作区内的项目目录名；显示名称可以与 ID 不同。项目发现只返回名称、ID 和 `data-sources.json` / `credentials-config.json` 是否存在，不读取连接正文、解密凭据或连接数据库。空项目和配置缺失项目仍可被发现，`missing_files` 指明缺失项。

凭据只在本地服务进程中用于建立连接。不要将工作区文件、个人连接、运行日志或数据集加入仓库。

## 调用方式

首次调用 `list_dbeaver_projects`，使用返回的精确 `project_id` 调用 `select_dbeaver_project`。例如：

```json
{"project": "已发现的项目 ID"}
```

选定项目在当前 MCP 进程会话内保持。后续直接调用 `list_dbeaver_connections`、`list_databases`、`list_schemas`、`search_catalog` 和 `get_table_context`，无需重复发现项目或传递项目参数。连接列表及连接上下文携带 `project_id` / `project_name`，同名连接和相同连接 ID 只在当前项目中解析。

只有用户明确要求切换项目时才再次调用 `select_dbeaver_project`。项目列表不会改变选择；连接失败、配置缺失或连接未找到也不会自动改用其他项目。失败的项目选择保留原选择。MCP 重启后需重新选择；若首次调用连接工具且只有一个项目有 `data-sources.json`，服务会自动选定它，否则返回 `PROJECT_REQUIRED`。

项目会话保持选择，不缓存底层数据库连接或事务；每次数据库调用仍独立建立只读事务并清理连接。Python 调用者仍可通过 `Settings.from_dbeaver(config_dir)` 使用明确的本地项目配置目录。

所有工具接受可选的 database_type 枚举。新调用建议显式传 postgresql 或 oracle；旧调用省略时从所选连接识别。get_query_capabilities 不连接数据库，省略类型时返回 PostgreSQL 能力以兼容旧客户端。

```json
{
  "database_type": "oracle",
  "connection": "已发现的连接 ID",
  "database": "已发现的服务标识",
  "schema": "已发现的 schema",
  "query": {
    "table": "已发现的表",
    "alias": "t",
    "columns": [{"source": "t", "column": "已发现的字段"}],
    "limit": 50
  }
}
```

analyze_query 校验并描述查询，不读取业务行；execute_query 执行有界查询。explain_query 按数据库能力提供计划。Oracle 暂不提供计划接口，因为 EXPLAIN PLAN 会写计划表。read_postgresql_logs 仅适用于 PostgreSQL，read_database_settings 在 Oracle 中仅返回白名单 NLS 会话设置。

Oracle 支持 ON 关联、分组、HAVING、CASE、非递归 CTE、窗口函数、标量/存在性子查询、集合运算和分页；各函数、类型与条件的精确范围以 get_query_capabilities 为准。结构规范见 [query-schema.json](docs/query-schema.json)，PostgreSQL 结构示例见 [query-examples.json](docs/query-examples.json)。共享语言的合法结构不代表所有数据库都实现该能力。

## 代码结构与扩展

```text
src/dbeaver_database_mcp/
  core/
    adapter.py          # 公共适配接口与连接参数模型
    registry.py         # 数据库类型、DBeaver provider 与适配器注册
    connection.py       # DBeaver 配置与凭据读取
    projects.py         # 项目元数据发现与进程会话选择
    service.py          # 类型核对与公共调度
    mcp_server.py       # MCP 协议、工具定义与请求/响应限制
    validation.py       # 结构和复杂度校验
    query_schema.py     # 封闭的公共查询语言
    catalog.py          # 公共关系模型与依赖收集
    errors.py           # 脱敏错误与诊断
    config.py           # 服务配置与硬限制
  adapters/
    postgresql/         # PostgreSQL 执行、编译、目录、安全与日志
    oracle/             # Oracle 执行、编译、目录、安全与会话
  mcp_server.py         # 兼容原启动入口
```

新增数据库时：

1. 在 adapters/<database>/ 实现 core.adapter.DatabaseAdapter，返回 ConnectionParameters 和准确的 capabilities。数据库驱动、SQL 方言、值绑定、目录检查、依赖审查及事务清理均留在该目录。
2. 在 core.registry 注册固定模块和 DBeaver provider。不得把 MCP 参数转为模块路径、连接字符串或自由 SQL。工具的 database_type 枚举自动取注册列表。
3. 注册只返回固定安全信息的错误翻译器。所有工具方法必须遵守接口中的权限、绑定、只读、结果限制与清理要求。新增查询原子如需扩展公共语言，应更新 query_schema，并同步各适配器的能力或明确拒绝逻辑。

公共核心不导入数据库驱动。适配器不调用其他数据库的实现，也不通过字符串替换转换已有 SQL。

## 开发检查

```text
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m pytest tests
```

仓库只保留生产源码、接口说明和无需数据库连接的自动化测试源码。连接文件、测试数据集、临时服务、审计记录、构建产物与本地验证材料应放在仓库外。

## 许可证

[Apache License 2.0](LICENSE)。DBeaver、PostgreSQL、Oracle 及相关产品名称属于各自权利人。本项目并非这些产品的官方发布。
