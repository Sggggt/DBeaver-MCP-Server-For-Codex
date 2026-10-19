# Security Policy

## Runtime boundary

The MCP interface accepts closed JSON arguments, not caller-provided SQL. Routine
and view inspection read fixed system catalogs and never execute the target or
returned definition. All values are bound by the adapter. Native account and
metadata visibility permissions remain in effect; the tool does not grant rights.

PostgreSQL and Oracle use server-enforced read-only transactions. SQL Server uses
the closed SELECT language, dependency guards and read-only application intent;
application intent is not a server-enforced read-only transaction. The observed
database read-only state is reported separately. The current SQL Server adapter
supports non-TLS direct transport only and rejects explicit TLS configurations;
it does not provide transport encryption.

SQL Server view execution requires a visible definition parsed with its saved
QUOTED_IDENTIFIER ON/OFF semantics, an allowlisted SELECT syntax tree, and a
complete match between parsed relation references and catalog dependencies.
Dependencies are recursively checked as tables or views, including explicitly
resolved cross-database and supported linked SQL Server targets. Object identity
includes server, database and object ID. SQL alias types are resolved to
allowlisted system types through sys.types; a type-name prefix grants no trust.
Encrypted or unverifiable definitions, synonyms, user-defined functions,
computed table columns, CLR/table types, external tables, and enabled
row-security policies are rejected.
Database VIEW DEFINITION is required; views additionally require SELECT on
sys.sql_expression_dependencies. Definitions are read in bounded chunks, audited
in memory and never submitted for execution or included in diagnostics. These
checks do not prevent another database session from concurrently altering objects
between inspection and execution; the account and database DDL boundary remains
the administrator's responsibility.

Cross-database catalog reads run under the target database's native permission
context. Linked targets must already be configured SQL Server links with an
allowlisted Microsoft provider, DATA ACCESS and RPC OUT. Only the fixed system
procedure sys.sp_executesql is invoked, with adapter-owned catalog SELECT
templates and separately bound values. This private metadata path never accepts
caller SQL, invokes a business routine or submits view definitions. The same
configured linked-login mapping used by SQL Server is trusted; administrators
must control changes to links, providers and mappings. No link or transaction
configuration is modified. Existing remote procedure transaction promotion can
require MSDTC; errors fail closed. Second linked-server hops are rejected.

The SQL Server query compiler supports nonrecursive CTEs, derived/scalar and
correlated subqueries, allowlisted set operations and windows. All scopes share
complexity and parameter budgets. Only the top-level response receives a probe
row; inner limits are never silently added to aggregates or deduplication.
Statement-level CTEs are preserved through description and result clipping.

Estimated plans use separate SET SHOWPLAN_XML ON/OFF batches after dependency
checks, without executing or describing the business SELECT. Native SHOWPLAN
permissions apply in every referenced database. XML size, nodes and returned
operators are bounded; invalid or incomplete plans fail closed. Returned
summaries omit raw XML, statement text, scalar predicates and bound values.
Restoring OFF is attempted on all exit paths, followed by the ordinary rollback
and unpooled connection closure even after cancellation or deadline expiry.

The `get_view_context` catalog API is separate from view execution. It can return
authorized definitions and direct catalog dependencies even when the executable
dependency guard would reject the view. It never connects to cross-database or
linked-server targets. Definition and dependency pagination are independent;
dependency completeness is not claimed. Encrypted, unavailable or permission-
restricted metadata are reported as statuses without decrypting or granting
rights. PostgreSQL definitions are reconstructed SELECT text, not original DDL.

Every session exit, including Python cancellation and process-exit exceptions,
attempts rollback and connection closure. Cleanup failures are reported through
`cleanup_failed` and must not be treated as confirmed successful cleanup. Forced
process termination, machine failure and network loss cannot guarantee immediate
application cleanup; database cleanup then depends on connection failure handling.

Routine and view source is untrusted data and can contain hard-coded secrets. Authorized
definitions are returned to the caller without automatic secret redaction; errors
and MCP stderr diagnostics omit source, bound values and native error messages.
The service does not persist business data or definitions to local files, but it
cannot control storage or actions performed by the calling client.

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue. Use the repository hosting service's private vulnerability-reporting feature and include the affected version, impact, reproduction conditions, and any suggested mitigation.

Do not include real database credentials, DBeaver workspace files, connection addresses, production data, query results, or PostgreSQL logs in a report. Replace sensitive values with synthetic placeholders.

Only the latest published release is expected to receive security fixes. A report may be closed when it depends on behavior outside this project's stated boundary, such as permissions granted to the same database account in another client.
