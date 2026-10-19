# SQL Server structured queries (0.11.0+)

These examples use synthetic names. Resolve the saved connection, database,
schema and objects with the discovery tools first. Pass each JSON below as
`query` to `analyze_query`, `execute_query` or `explain_query`, alongside the
normal outer `connection`, `database`, `schema` and optional `database_type`.
No field accepts SQL text. Data values remain separate driver parameters.

## Deduplicate, then aggregate

The inner query intentionally has no `limit`. Applying the response limit to the
deduplication phase would change the aggregate.

```json
{
  "with": [{
    "name": "deduplicated",
    "query": {
      "table": "records",
      "alias": "e",
      "columns": [
        {"source": "e", "column": "group_id"},
        {"source": "e", "column": "record_id"}
      ],
      "distinct": true
    }
  }],
  "from": {"cte": "deduplicated", "alias": "d"},
  "columns": [{"source": "d", "column": "group_id"}],
  "aggregates": [{"function": "count", "as": "record_count"}],
  "group_by": [{"source": "d", "column": "group_id"}],
  "order_by": [{"field": "record_count", "direction": "desc"}],
  "limit": 100
}
```

Derived tables use `"from": {"query": {...}, "alias": "d"}`. Scalar subqueries
use `{"subquery": {...}}` and return one column; SQL Server enforces the usual
single-row rule. Conditions support `{"exists": {...}, "not": false}` and
`{"left": {...}, "op": "in_query", "query": {...}}` (also `not_in_query`).
Scalar and condition subqueries can reference visible outer aliases; derived
tables and CTE definitions cannot. CTEs can reference earlier CTEs. Recursive,
forward and duplicate names are rejected. Nested CTEs are hoisted under private
names so description and result clipping retain valid statement-level WITH.

## Cross-database and linked SQL Server sources

```json
{
  "from": {
    "server": "reporting_link",
    "database": "archive",
    "schema": "dbo",
    "table": "records",
    "alias": "e"
  },
  "columns": [{"source": "e", "column": "record_id"}],
  "where": {
    "left": {"source": "e", "column": "record_id"},
    "op": "eq",
    "value": "example-record"
  },
  "limit": 50
}
```

Omit `server` for another database on the same instance. A `server` always
requires an explicit `database`. Qualifiers identify one source and are not
inherited into independent nested queries. They also work on joined table
sources and on the shorthand `table`/`alias` query shape. PostgreSQL and Oracle
reject these qualifiers instead of silently querying another object.

Each dependency is checked under the target database's native permissions.
Links must already exist in `sys.servers`, use MSOLEDBSQL/MSOLEDBSQL19,
SQLNCLI/SQLNCLI10/SQLNCLI11 or SQLOLEDB, and permit DATA ACCESS and RPC OUT.
The adapter verifies the remote version and uses the linked login for fixed,
parameterized catalog SELECTs through `sys.sp_executesql`. It never submits view
source or caller SQL through that procedure. Remote procedure transaction
promotion may require an existing MSDTC configuration; the tool does not change
it. Remote views may refer to another database through the same link; another
linked-server hop is rejected. Arbitrary hosts, credentials and non-SQL Server
providers are not supported.

## Window ranking

```json
{
  "table": "records",
  "alias": "e",
  "select": [
    {"as": "record_id", "expr": {"source": "e", "column": "record_id"}},
    {"as": "position", "expr": {
      "window": "row_number",
      "partition_by": [{"source": "e", "column": "group_id"}],
      "order_by": [{
        "expr": {"source": "e", "column": "record_id"},
        "direction": "desc"
      }]
    }}
  ],
  "limit": 100
}
```

To filter the rank, put this query inside a derived table or CTE and filter
`position` in the outer query. Remove its inner limit when ranking the full set.

| Capability | Minimum version / limits |
| --- | --- |
| Nonrecursive CTE, derived/scalar/correlated subqueries, EXISTS/IN | SQL Server 2008; 16 nested query levels |
| UNION, UNION ALL, INTERSECT, EXCEPT | 2008; equal column counts, matching by position; no INTERSECT ALL/EXCEPT ALL |
| ROW_NUMBER, RANK, DENSE_RANK, NTILE, window COUNT/SUM/AVG/MIN/MAX | 2008 |
| LAG, LEAD, FIRST_VALUE, LAST_VALUE, PERCENT_RANK, CUME_DIST | 2012 |
| Explicit ROWS/RANGE frame | 2012; aggregate/value functions only; RANGE has no numeric offsets |
| OFFSET/FETCH | 2012; explicit projected output ordering and limit |
| Estimated plan | 2008; native SHOWPLAN permission in every referenced database |
| Alias column types | Allowlisted underlying system type; CLR/table types rejected |
| Saved views | Visible ON/OFF definition, allowlisted syntax and matched catalog dependencies |

Nested queries may have explicit limits up to the public schema's nested-query
bound. These are respected exactly; the extra truncation probe row applies only
to the top-level response. Nested ORDER BY requires a limit. All scopes share
2,000 bound parameters and 4,000 expression/condition nodes. The outer limit is
at most 500. Recursive CTEs, aggregate FILTER, arbitrary functions, table
functions, lateral joins and free SQL remain unsupported.

## Estimated plans and security

Use the same JSON with `explain_query`. `plan.operators` reports physical/logical
operators, estimated rows, subtree costs and available object/index identities.
`plan.executed=false` and `estimated=true` distinguish it from execution
statistics. Original XML, statement text, scalar predicates and parameter values
are omitted. Oversized, truncated or invalid XML produces a structured error.
Distributed plans can have opaque Remote Query operators rather than the remote
engine's full plan.

SQL Server has no server-enforced read-only transaction equivalent. The closed
SELECT compiler, dependency guard, native account privileges, deadline, result
limits and rollback/close behavior enforce the interface's boundary. An alias
type or QUOTED_IDENTIFIER OFF does not exempt a dependency from these checks.
See [SECURITY.md](../SECURITY.md) for concurrency and cleanup limits.

SQL Server reference: [sys.types](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-types-transact-sql),
[QUOTED_IDENTIFIER](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-quoted-identifier-transact-sql),
[sp_executesql](https://learn.microsoft.com/en-us/sql/relational-databases/system-stored-procedures/sp-executesql-transact-sql),
[SHOWPLAN_XML](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-showplan-xml-transact-sql).
