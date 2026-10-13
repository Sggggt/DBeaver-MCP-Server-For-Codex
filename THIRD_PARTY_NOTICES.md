# Third-party dependencies

This repository contains this project's source code. It does not vendor
third-party implementation code or dependency binaries. The project's original
files are licensed under [Apache-2.0](LICENSE); dependencies keep their own terms.

## Runtime dependencies

| Component | License | Upstream |
| --- | --- | --- |
| cryptography | Apache-2.0 OR BSD-3-Clause | [source](https://github.com/pyca/cryptography) |
| psycopg / psycopg-binary | LGPL-3.0-only | [source and terms](https://github.com/psycopg/psycopg/blob/3.3.4/LICENSE.txt) |
| jsonschema | MIT | [source](https://github.com/python-jsonschema/jsonschema) |
| tzdata | Apache-2.0 | [source](https://github.com/python/tzdata) |
| pglast | GPL-3.0-or-later | [source and terms](https://github.com/lelit/pglast/blob/v7/README.rst) |
| oracledb | UPL-1.0 OR Apache-2.0 | [source](https://github.com/oracle/python-oracledb) |
| sqlglot | MIT | [source](https://github.com/tobymao/sqlglot) |
| python-tds | MIT | [source](https://github.com/denisenkom/pytds) |

Transitive dependencies include attrs, jsonschema-specifications, referencing
and rpds-py (MIT), cffi (MIT-0), pycparser (BSD-3-Clause), and typing_extensions
(PSF-2.0). Upstream `OR` expressions provide a license choice. Dependency version
constraints are defined in `pyproject.toml`; license terms must be checked when
those versions change.

## License scope

The PostgreSQL adapter uses pglast in-process for dependency safety checks.
Its GPL terms remain applicable; publishing source rather than binaries does
not by itself exempt a combined work from GPL obligations. Likewise, Psycopg
retains its LGPL terms. This project's Apache-2.0 declaration does not relicense
these dependencies or establish that a combined program is exclusively Apache-2.0.

This inventory identifies dependencies and their upstream terms; it is not a
replacement for a required license copy or attribution notice. If third-party
code or dependency binaries are incorporated into a distribution, preserve the
applicable license texts, copyright and NOTICE statements, and satisfy any
corresponding-source or library-replacement requirements.
