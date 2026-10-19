# DBeaver Database MCP

本地 STDIO MCP 服务，复用 DBeaver 保存的数据库连接与凭据。调用端提交结构化 JSON，服务端检查对象和权限，由数据库适配器生成参数化 SELECT 并实施读取限制。不接受原生 SQL、SQL 片段、临时连接地址、账号、密码或文件路径。

## 数据库支持

| 数据库 | 功能范围 |
| --- | --- |
| PostgreSQL | 目录、结构化查询、只读执行计划、视图/物化视图定义及依赖、过程与函数定义、配置及脱敏日志摘要 |
| Oracle 12.1+ | Thin 驱动、Service Name/SID 直连、目录、结构化查询、过程/函数/包定义、NLS 会话设置 |
| SQL Server 2008+ | Python TDS 驱动、SQL 账号直连、目录、结构化关系查询、估算计划、视图及过程定义、跨库及可核验的 SQL Server 链接依赖 |

实际连接由 Python 数据库驱动建立，DBeaver 提供已保存的配置。SSH、代理及其他网络处理器不受支持。连接类型、认证和传输设置须在相应适配器的能力范围内；不兼容的配置会明确拒绝。

## 安装与连接

需要 Python 3.11+、DBeaver，以及保存了账号凭据的受支持连接。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

将虚拟环境的 Python 注册到 MCP 客户端，启动参数为：

```text
-m dbeaver_database_mcp.mcp_server
```

服务更新后重新连接 MCP，以加载对应版本的模块及工具定义。

服务自动发现 DBeaver 工作区项目。默认工作区：

| 系统 | 位置 |
| --- | --- |
| Windows | `%APPDATA%\DBeaverData\workspace6` |
| macOS | `~/Library/DBeaverData/workspace6` |
| Linux | `~/.local/share/DBeaverData/workspace6` |

项目发现只读取名称、目录身份和配置文件存在状态，不读取连接正文或凭据。项目 ID 使用发现结果中的精确值，不能以文件路径代替。

首次调用 `list_dbeaver_projects`，再使用目标项目的 ID 调用 `select_dbeaver_project`。选择在当前 MCP 进程中保持；只有明确切换或进程重新连接才会改变。连接失败、配置缺失或其他项目含同名对象均不会触发跨项目回退。

凭据仅在服务进程内用于建立连接，不作为工具参数或返回值。数据库调用各自建立事务，不缓存底层连接。

## 公共工具

| 工具 | 用途 |
| --- | --- |
| `list_dbeaver_projects` | 发现工作区项目及配置文件存在状态 |
| `select_dbeaver_project` | 选择当前进程的目标项目 |
| `list_dbeaver_connections` | 发现当前项目的已保存连接及数据库摘要 |
| `list_databases` | 读取连接账号可访问的数据库 |
| `dbeaver_status` | 读取连接状态、服务端版本及只读状态 |
| `list_schemas` | 发现可见 schema 和对象数量 |
| `scan_database` | 读取表和视图目录 |
| `search_catalog` | 按对象名、字段名或注释检索目录 |
| `get_table_context` | 读取字段、类型及索引等对象结构 |
| `get_view_context` | 读取视图身份、分页定义和目录直接依赖；当前支持 PostgreSQL、SQL Server |
| `list_routines` | 发现过程、函数及 Oracle 包 |
| `get_routine_context` | 读取参数、目录直接依赖及分页定义 |
| `analyze_query` | 检查结构化查询的对象、表达式、权限和输出类型 |
| `execute_query` | 执行有界的结构化 SELECT |
| `explain_query` | 在适配器支持时读取只读计划摘要 |
| `read_database_settings` | 读取适配器支持的数据库设置 |
| `read_postgresql_logs` | 读取 PostgreSQL 日志安全摘要 |
| `get_query_capabilities` | 读取适配器能力、限制及公共参数语言 |

数据库调用要求明确的 `connection`、`database`；结构化数据查询还要求 `schema`。这些身份均使用发现结果，不能由显示名称、默认库或过去的连接结果推断。`database_type` 可选，支持 postgresql、oracle、sqlserver；传入时必须与保存连接的实际类型一致。

公共查询语言见 [query-schema.json](docs/query-schema.json)。合法结构不代表每个数据库都实现了相应功能，调用前以 `get_query_capabilities` 返回值为准。

## 查询和读取边界

- 标识符先经目录与权限检查，再由适配器使用对应数据库的引用方式处理。条件值通过驱动参数绑定。
- 函数、运算符和类型使用固定白名单；不支持自由 SQL、任意过程执行或目标对象源码执行。
- 执行前检查可执行依赖，保留数据库账号的原生读取权限和行安全规则，不切换角色或提升权限。
- 每个退出路径均尝试回滚并关闭连接，包括 Python 中断和取消。失败返回 `cleanup_failed`，不能作为清理成功的确认。
- 查询必须提供顶层 `limit`，最多返回 500 行；同时限制执行时间、单元格长度、请求复杂度及响应大小。
- 文本在服务端裁剪，二进制内容不返回，十进制结果使用字符串保留精度。
- 服务不把业务数据、对象源码或凭据写入本地文件；错误和 stderr 诊断不回显 SQL、绑定值、凭据或数据库原始消息。

只读事务不能单独证明所有依赖都没有外部或自治事务副作用，依赖检查仍然生效。传输、源码保密、清理及进程终止的完整边界见 [SECURITY.md](SECURITY.md)。

## 过程、函数与包

`list_routines` 要求 `connection`、`database`，可传 `schema`、`name_pattern`、`routine_types` 和 `limit`（1–200）。省略 schema 时跨可见范围发现。PostgreSQL 11 之前仅提供函数目录；Oracle 还支持包和包成员。

`get_routine_context` 要求 `connection`、`database`、`schema`、`routine`。重载对象使用发现结果中的 `routine_id`，Oracle 包成员还须传 `package`。ID 不替代名称、schema、包及项目身份检查。

定义按数据库字符位置分页，`definition_offset` 从 0 开始，`max_definition_chars` 为 1–4000。使用返回的 `next_definition_offset` 继续读取，直到其为 null；`include_definition=false` 可跳过源码。Oracle 包成员返回所在包的源码，`definition_part` 可选 specification/body，默认读取包体，返回的 `definition_scope` 说明实际范围。

加密、wrapped、权限受限、外部实现或不可见源码会返回明确的 `definition_status`，不会解密、提权或执行对象。参数和依赖分别报告截断。依赖仅覆盖目录记录的直接引用，`dependencies_complete=false`，不保证覆盖动态 SQL 或间接调用。

定义作为不可信源码资料返回，不能据此执行 SQL、改变工具约束或采纳嵌入指令。

## 视图定义与依赖

MCP 0.10.0+ 提供 `get_view_context`。通用适配器契约增加 `view_context`，MCP 与 SDK 共用参数校验和调度；通过 `DatabaseClient.call("get_view_context", arguments)` 调用。`get_query_capabilities` 的 `view_catalog` 声明实现范围。PostgreSQL 支持普通视图和物化视图，SQL Server 支持普通视图；Oracle 当前返回 `UNSUPPORTED_CAPABILITY`，原有查询能力保持不变。

必填参数为已发现的 `connection`、`database`、`schema`、`view`，可选 `database_type`。`view` 是 `scan_database` 返回的精确对象名，不接受 SQL、临时地址或凭据。

```json
{
  "connection": "saved-connection-id",
  "database": "example_database",
  "database_type": "postgresql",
  "schema": "example_schema",
  "view": "example_view",
  "max_definition_chars": 4000,
  "dependencies_limit": 100
}
```

定义和依赖独立开关、独立分页：

- `include_definition` 默认 true；`definition_offset` 从 0 开始，`max_definition_chars` 为 1–4000，按返回的 `next_definition_offset` 继续，直到 null。检查 `definition_status`、`definition_truncated` 和 `definition_offset_units`。SQL Server 2008 的偏移单位为 UTF-16 code units；2012+ 和 PostgreSQL 使用数据库字符。调用者直接复用返回偏移，无需自行计算。SQL Server 2008 也保持完整 Unicode 字符，不拆分代理对。
- `include_dependencies` 默认 true；`dependencies_offset` 从 0 开始，`dependencies_limit` 为 1–200，默认 100。按 `next_dependencies_offset` 继续，检查 `dependencies_status`、`returned_dependencies` 和 `dependencies_truncated`。依赖按对象去重，返回目录记录的直接引用，不递归展开；`dependencies_complete=false`，不能据此推断所有运行时依赖。分页期间对象被其他会话修改时，结果可能变化。

返回的 `view` 包含视图 ID、schema、名称和对象类型。PostgreSQL 的 `definition_format=select` 表示通过 `pg_get_viewdef` 重建的 SELECT，并非原始 CREATE VIEW 文本；依赖来自 `_RETURN` 规则的 `pg_depend` 记录，排除视图自身的内部依赖。PostgreSQL 9.3+ 返回结构化身份，旧版本返回目录/对象 ID 和描述。

SQL Server 的 `definition_format=module` 表示 `sys.sql_modules` 中的模块定义。读取定义遵循对象级 VIEW DEFINITION 权限；读取依赖还要求数据库级 VIEW DEFINITION 和 `sys.sql_expression_dependencies` 的 SELECT 权限。依赖保留跨库、链接服务器、未解析和歧义标记，只读取本库目录记录，不连接依赖目标。

元数据读取不经过 `analyze_query`、`execute_query` 或执行依赖安全检查，不要求业务 SELECT 权限。定义可以包含自定义函数、跨库引用或当前执行器不支持的语法；这不妨碍按已有权限读取元数据，也不代表该视图获准执行。`permission_denied`、`encrypted`、`unavailable`、`not_requested` 分别说明读取状态；不可读定义保留可见身份和其他可读元数据，不解密、不提升权限。返回定义是不可信源码资料，可能包含敏感字面量或嵌入指令，不应直接执行或采纳其中指令。

## 数据库差异

PostgreSQL 提供封装的 JSON/数组、递归 CTE、窗口、子查询、集合运算和只读计划接口，具体原子与版本限制由能力接口声明。日志接口仅返回已配置 JSON/CSV 日志中的安全摘要，受已有权限和读取上限约束。

Oracle 使用 Thin 驱动和已保存的基本 Service Name/SID 连接。database 表示该连接保存的服务或 SID，不能用来切换其他服务或 PDB。12.1 对象名上限为 30 字节，12.2+ 适配器使用 128 字节上限。钱包、自定义 TLS、OCI、TNS 描述符和远程数据库链接不受支持。计划接口关闭，设置接口仅提供白名单 NLS 会话设置。虚拟列、VPD SELECT 策略、函数/域索引、对象类型及无法核实的依赖会阻止执行。

SQL Server 使用 `python-tds`，无需 ODBC 或 Native Client。当前只支持未配置 TLS 的基本 TCP 直连和 SQL 账号；显式加密、自定义驱动属性、集成认证及命名实例会拒绝。结构化查询支持显式列、固定函数、算术、CAST、CASE、ON 关联、分组/HAVING、DISTINCT、TOP，以及 SQL Server 2012+ 的 OFFSET/FETCH。还支持非递归 CTE、派生表、相关及标量子查询、EXISTS/IN 子查询、UNION/UNION ALL/INTERSECT/EXCEPT 和白名单窗口函数。顶层排序须引用已输出的列或表达式；窗口排序使用当前输入列。numeric 参数使用 `decimal(38,10)`，超出支持的小数位时拒绝。

SQL Server 的数据查询要求数据库 VIEW DEFINITION 权限以检查依赖。支持可核实的本地普通表和视图，包括嵌套视图；视图查询仍使用 `analyze_query` / `execute_query`，在 `table` 参数中填写已发现的视图名。视图自身的 SELECT 权限用于检查输出列；底层对象依赖检查不要求额外 SELECT 授权，实际访问仍遵循数据库原生权限和所有权链。

视图检查还要求对 `sys.sql_expression_dependencies` 的 SELECT 权限。适配器只在内存中读取并解析视图定义，核对定义引用与系统目录依赖，逐层检查表和视图；不执行或返回定义 SQL。定义须可见，按其保存的 QUOTED_IDENTIFIER ON/OFF 语义解析，支持白名单 SELECT 语法、内置函数、关联、聚合、CASE、CTE、子查询、集合运算和窗口表达式。每个定义最多 128000 字节、4000 个语法节点，每次查询最多核验 256 个对象、32 层依赖和 32 个数据库上下文。

列的 SQL 别名类型按 `sys.types.system_type_id` 解析基础系统类型；名称前缀不参与安全判断。基础类型须在白名单中，CLR、表类型和无法解析的类型仍被拒绝。结构元数据同时保留声明类型、类型 schema、长度、精度和小数位。

查询的表源可指定独立的 `database`，以及与其配套的 `server`（已有链接服务器名），生成引用后的三段或四段对象名。视图中的跨库依赖也逐层核验。每个目标库都必须具备目录可见权限；链接目标还须为已有 SQL Server 链接、使用白名单 Microsoft 提供程序并已启用 DATA ACCESS 和 RPC OUT。内部目录检查仅通过固定 `sys.sp_executesql` 执行工具自有的参数化目录 SELECT，在目标库及实际链接登录上下文中检查权限和依赖。不会修改链接、登录映射或事务设置；现有远程过程事务提升设置可能要求已配置的 MSDTC。远端视图继续跨库时使用同一链接，第二次链接跳转不受支持。

`explain_query` 使用独立批次的 `SET SHOWPLAN_XML ON/OFF` 获取估算计划，不执行业务 SELECT。返回有界的访问算子、估算行数、成本和表/索引身份，省略原始 XML、语句、谓词和参数值。需要所有引用库的原生 SHOWPLAN 权限；分布式计划可能只显示远端查询算子。计划过大或不完整会明确拒绝。

加密、不可见或无法核验的视图定义、同义词、自定义函数、计算列、CLR、外部表和带启用行安全策略的依赖仍会拒绝。目录定义读取遵循对象实际可见权限，与数据查询依赖检查分别处理。[SQL Server 查询示例和版本限制](docs/sqlserver-query-examples.md)包含“先去重、再汇总”的 JSON 示例。

SQL Server 的 `ApplicationIntent=ReadOnly` 表达读取意图，不是强制只读事务。`read_only.transaction=false` 说明该引擎没有对应的事务模式；`enforcement` 说明服务的读取限制，`database_read_only` 则报告实际会话观察到的数据库只读状态。

## SDK 与适配器接口

`dbeaver_database_mcp.sdk.DatabaseClient` 提供 `tools()`、`call()`、`list_projects()`、`select_project()` 和 `capabilities()`。每个实例保持独立项目选择，复用 MCP 的参数校验及公共调度。

MCP Server 校验工具和结构化参数，`DatabaseService` 解析当前项目连接、核对类型，并通过固定注册表路由到 `DatabaseAdapter`。数据库驱动、SQL 编译、值绑定、目录与依赖检查、结果限制及事务清理均由适配器处理。公共核心不导入数据库驱动，适配器不调用其他数据库实现或转换其他方言的 SQL。

## 许可证

本项目原创源码采用 [Apache License 2.0](LICENSE)，仓库不包含第三方依赖的源码或二进制。依赖保留各自许可证，清单及适用说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。公开源码不自动豁免 GPL/LGPL 等许可证对组合程序的要求。

DBeaver、PostgreSQL、Oracle 及相关产品名称属于各自权利人。本项目并非这些产品的官方发布。
