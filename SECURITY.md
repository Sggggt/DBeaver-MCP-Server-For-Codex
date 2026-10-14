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

SQL Server view execution requires a visible QUOTED_IDENTIFIER ON definition,
an allowlisted SELECT syntax tree, and a complete match between parsed relation
references and catalog dependency IDs. Dependencies are recursively checked as
local tables or views. Encrypted or unverifiable definitions, cross-database or
remote references, synonyms, user-defined functions, computed table columns,
CLR/alias types, external tables, and enabled row-security policies are rejected.
Database VIEW DEFINITION is required; views additionally require SELECT on
sys.sql_expression_dependencies. Definitions are read in bounded chunks, audited
in memory and never submitted for execution or included in diagnostics. These
checks do not prevent another database session from concurrently altering objects
between inspection and execution; the account and database DDL boundary remains
the administrator's responsibility.

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
