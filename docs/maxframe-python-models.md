# MaxFrame Python Models

> **Status: Generally Available (`1.11.3`).** Supported for production workloads
> within the scope, limitations, and operational guidance below.

Release owners should also run the
[production readiness gates](maxframe-production-readiness.md) against their
own quota, runtime image, dependencies, and data volume.

`dbt-maxcompute` can execute dbt Python models as MaxFrame DataFrame jobs and
materialize their results as MaxCompute tables.

This guide covers installation, model authoring, partitioning, incremental
strategies, operational behavior, and the known differences from
`dbt-bigquery` Python models.

## Supported scope

MaxFrame Python models support:

- `table` and `incremental` materializations;
- `dbt.ref`, `dbt.source`, `dbt.config`, `dbt.config.get`, `dbt.this`, and
  `dbt.is_incremental`;
- regular MaxCompute partitions, including multiple partition columns;
- automatic time partitions based on `date`, `datetime`, `timestamp`, or
  `timestamp_ntz` columns;
- `merge`, `append`, `delete+insert`, `insert_overwrite`, and `microbatch`
  incremental strategies;
- `on_schema_change`, lifecycle, table properties, SQL hints, and MaxFrame
  quota selection;
- bounded retries for transient MaxFrame DAG transport failures;
- MaxFrame session IDs in dbt run results.

The model must return exactly one MaxFrame DataFrame. Pandas DataFrames,
MaxFrame Series objects, and arbitrary Python values are not supported model
results.

## Prerequisites

- a Python version supported by the installed dbt Core and MaxFrame SDK;
- dbt Core 1.11.2 or later;
- a MaxCompute project and credentials that can create, alter, read, write,
  and delete tables in the target schema.

Install the adapter with its optional MaxFrame runtime:

```bash
pip install "dbt-maxcompute[maxframe]"
```

The MaxFrame extra currently installs `maxframe>=2.7.1,<3.0.0`.

Check the interpreter before running a Python model:

```bash
python --version
python -m dbt --version
```

The adapter does not restrict MaxFrame installation or submission to Python
3.11. Python 3.11 is recommended for models that serialize custom functions
through `DataFrame.apply`, `Series.apply`, or `with_python_requirements`,
because the default MaxCompute execution image uses CPython 3.11. A different
client minor version can work for built-in DataFrame operations but may be
incompatible when Python functions are serialized into the remote worker.

## Profile configuration

The standard MaxCompute profile is also used by MaxFrame:

```yaml
my_maxcompute_project:
  target: prod
  outputs:
    prod:
      type: maxcompute
      project: my_project
      schema: analytics
      endpoint: https://service.cn-shanghai.maxcompute.aliyun.com/api
      auth_type: access_key
      access_key_id: "{{ env_var('MAXCOMPUTE_ACCESS_KEY_ID') }}"
      access_key_secret: "{{ env_var('MAXCOMPUTE_ACCESS_KEY_SECRET') }}"

      # Optional MaxFrame settings
      submission_method: maxframe
      maxframe_quota_name: my_maxframe_quota
      maxframe_retries: 2
      maxframe_python_version_check: warn
      maxframe_pythonpack_production: true
      timezone: Asia/Shanghai
```

Do not store credentials in the dbt project. Use environment variables, STS,
RAM roles, OIDC, or another supported credential provider.

To use the Alibaba Cloud default credential provider chain, replace the three
access-key lines above with:

```yaml
      auth_type: chain
```

The same chain identity is used for dbt SQL and MaxFrame Python models. No
`sitecustomize.py` or `CredentialProviderAccount` monkey patch is required.

`submission_method: maxframe` is optional because MaxFrame is the default
Python submission method. `maxframe_retries: 2` means the adapter may create
up to two retry sessions after the initial session when waiting for a DAG fails
with a transport error. `maxframe_pythonpack_production: true` keeps successful
PythonPack dependency builds in MaxFrame's production cache for reuse by
periodic jobs. A UDF can still request a rebuild with
`with_python_requirements(..., force_rebuild=True)`.

`maxframe_python_version_check` controls only the compatibility notice for
custom-function serialization. `warn` (the default) logs once per dbt process
and continues; `error` is an opt-in strict policy for controlled production
targets; `off` suppresses the check. It never changes package installation.
Setting `maxframe_pythonpack_production: false` explicitly forwards the
non-production PythonPack mode, so it also overrides a process-level MaxFrame
option inherited by the dbt invocation.

## Your first MaxFrame model

Create `models/orders_positive.py`:

```python
def model(dbt, session):
    dbt.config(
        materialized="table",
        submission_method="maxframe",
        lifecycle=30,
        timeout=3600,
    )

    orders = dbt.ref("stg_orders")
    return orders[orders["amount"] > 0][
        ["order_id", "customer_id", "amount", "ds"]
    ]
```

Run it like any other dbt model:

```bash
dbt parse
dbt run --select orders_positive
```

The second argument to `model` is the MaxFrame session created for that dbt
node. `dbt.ref` and `dbt.source` return lazy MaxFrame DataFrames, so normal
MaxFrame filtering, projection, joins, aggregations, and UDF operations can be
used before returning the final DataFrame.

Use pandas-style boolean indexing for row filters:

```python
orders = orders[orders["amount"] > 0]
```

Do not port `df.filter(boolean_expression)` literally from another DataFrame
API. In MaxFrame, `DataFrame.filter` selects labels; it is not the row-filter
operation. Also prefer `df["id"]` over `df.id` for a column named `id`, because
MaxFrame reserves `DataFrame.id` for its internal tileable identifier.

## Reading refs and sources

```python
def model(dbt, session):
    orders = dbt.ref("stg_orders")
    customers = dbt.source("raw", "customers")

    return orders.merge(customers, on="customer_id", how="left")
```

Dependencies are still recorded in the dbt DAG. `dbt run --select +my_model`
therefore builds upstream dbt models before the MaxFrame model.

For a partitioned ref or source, MaxFrame exposes partition columns in the
returned DataFrame. They can be filtered, joined, grouped, and selected like
regular columns even though MaxCompute stores them separately from data
columns.

## Partitioned tables

### Regular partitions

The partition column must be present in the returned DataFrame:

```python
def model(dbt, session):
    dbt.config(
        materialized="table",
        partition_by={"field": "ds", "data_type": "string"},
        lifecycle=30,
    )
    return dbt.ref("stg_orders")
```

BigQuery-style singular keys and MaxCompute-style plural keys are both
accepted. These two configurations are equivalent:

```python
partition_by={"field": "ds", "data_type": "string"}
```

```python
partition_by={"fields": "ds", "data_types": "string"}
```

Use lists or comma-separated strings for multiple MaxCompute partition
columns:

```python
partition_by={
    "fields": ["region", "ds"],
    "data_types": ["string", "string"],
}
```

### Automatic time partitions

When `data_type` is a time type, the adapter creates a MaxCompute automatic
partitioned table through a lifecycle-1 staging table:

```python
def model(dbt, session):
    dbt.config(
        materialized="table",
        partition_by={
            "field": "event_time",
            "data_type": "timestamp",
            "granularity": "day",
            "generate_column_name": "ds",
        },
        lifecycle=30,
    )
    return dbt.ref("events")
```

The returned DataFrame must contain `event_time`. It must not add the generated
`ds` column itself. MaxCompute derives `ds` from `event_time`.

## Incremental models

Incremental targets are created as transactional MaxCompute tables because
MaxCompute `MERGE` requires a transactional target.

If an incremental target was created by an older adapter version as a
non-transactional table, recreate it once before enabling MaxFrame incremental
runs:

```bash
dbt run --select my_incremental_model --full-refresh
```

### Merge

```python
def model(dbt, session):
    dbt.config(
        materialized="incremental",
        incremental_strategy="merge",
        unique_key="order_id",
        partition_by={"field": "ds", "data_type": "string"},
        on_schema_change="sync_all_columns",
        lifecycle=30,
    )

    orders = dbt.ref("stg_orders")
    if dbt.is_incremental:
        # Replace this predicate with the project's watermark policy.
        orders = orders[orders["order_id"] > 1_000_000]
    return orders
```

Use `unique_key` for update/insert semantics. Without a unique key, the merge
SQL falls back to inserting the temporary DataFrame.

### Append

`append` inserts every row returned during an incremental run. Do not configure
a `unique_key` with this strategy.

```python
dbt.config(
    materialized="incremental",
    incremental_strategy="append",
    partition_by={"field": "ds", "data_type": "string"},
)
```

### Delete and insert

`delete+insert` deletes matching keys from the target and then inserts the
returned rows:

```python
dbt.config(
    materialized="incremental",
    incremental_strategy="delete+insert",
    unique_key="order_id",
    partition_by={"field": "ds", "data_type": "string"},
)
```

### Insert overwrite

`insert_overwrite` requires `partition_by`. On incremental runs, return only
the partitions that should be replaced:

```python
def model(dbt, session):
    dbt.config(
        materialized="incremental",
        incremental_strategy="insert_overwrite",
        partition_by={"field": "ds", "data_type": "string"},
        lifecycle=30,
    )

    orders = dbt.ref("stg_orders")
    if dbt.is_incremental:
        orders = orders[orders["ds"].isin(["2026-08-08", "2026-08-09"])]
    return orders
```

Dynamic overwrite replaces the partition values present in the returned
DataFrame and preserves other target partitions.

### Microbatch

Microbatch requires `unique_key`, `event_time`, `batch_size`, `begin`, and a
time `partition_by` configuration whose `granularity` matches `batch_size`:

```python
def model(dbt, session):
    dbt.config(
        materialized="incremental",
        incremental_strategy="microbatch",
        unique_key="event_id",
        event_time="event_time",
        batch_size="day",
        begin="2026-08-01",
        partition_by={
            "field": "event_time",
            "data_type": "timestamp",
            "granularity": "day",
            "generate_column_name": "ds",
        },
        lifecycle=30,
    )
    return dbt.ref("events")
```

Use a literal string for `begin`; calls such as `datetime.datetime(...)` are
not accepted inside `dbt.config` in a Python model. For each batch, the adapter
applies the half-open interval
`event_time >= batch_start and event_time < batch_end` to the returned
DataFrame before writing it.

## Schema changes

Python incremental models support dbt's `on_schema_change` behavior. For
pipelines that intentionally add and remove columns, use:

```python
dbt.config(on_schema_change="sync_all_columns")
```

As with SQL incremental models, test schema changes in a non-production schema
before enabling automatic synchronization in production.

## MaxFrame dependencies and UDFs

Model-level dbt `packages` are not dynamically installed for MaxFrame models.
Packages imported while building the lazy DataFrame graph must already be
installed in the same Python environment as dbt.

For dependencies executed remotely by a MaxFrame UDF, use MaxFrame's native
decorator:

```python
from maxframe.udf import with_python_requirements


@with_python_requirements("numpy")
def normalize(value):
    import numpy as np

    return np.log1p(value)
```

This is different from dbt's persistent `functions:` resource. Persistent
Python scalar and aggregate UDFs are documented in the
[Python UDF guide](python-udfs.md); they use MaxCompute catalog functions and
do not run inside the MaxFrame model DAG.

## Runtime and observability settings

| Setting | Location | Default | Meaning |
|---|---|---:|---|
| `submission_method` | profile or model | `maxframe` | Python job backend. |
| `maxframe_quota_name` | profile or model | project default | MaxFrame session quota. Model value wins. |
| `maxframe_retries` | profile or model | `2` | New-session retries after a DAG transport failure. |
| `maxframe_python_version_check` | profile or model | `warn` | CP311 custom-UDF compatibility policy: `warn`, `error`, or `off`. |
| `maxframe_pythonpack_production` | profile or model | `true` | Use MaxFrame's durable production cache for successful PythonPack builds. |
| `timeout` | model | `600` | MaxFrame session timeout in seconds. Increase it for queued or large DAGs. |
| `lifecycle` | model | project behavior | Target table lifecycle in days. |
| `sql_hints` | model | adapter defaults | MaxCompute SQL settings forwarded to MaxFrame. |

The dbt result message and `query_id` contain the final MaxFrame session ID.
The adapter checks whether LogView is available but never writes a signed
LogView URL to logs or dbt artifacts because that URL contains a temporary
access token.

## Failure and full-refresh behavior

- Table models build an intermediate table and rename it only after the
  MaxFrame DAG succeeds.
- Python incremental `--full-refresh` follows the same intermediate/backup
  swap, so a model-computation failure does not delete the existing target.
- MaxFrame writes a lifecycle-1 staging table with a type-preserving internal
  sentinel row, then MaxCompute SQL filters the sentinel before creating the
  final or incremental temporary relation. This keeps empty table and empty
  incremental outputs valid without collecting model data into the dbt process.
- Normal incremental runs create a lifecycle-1 temporary table from that stage
  before executing the configured MaxCompute incremental SQL.
- Failed MaxFrame DAG output cleanup is retried up to three times.
- After a session is destroyed, the adapter removes `tmp_mf_<session>_*`
  tables and `mf_udf_<session>_*` functions that MaxFrame may leave in the
  model schema. Cleanup is restricted to the exact session IDs created by the
  current dbt node.
- Automatic-partition staging tables use lifecycle `1`, are removed before
  reuse, and are removed after a successful run. If execution fails after the
  MaxFrame stage succeeds but before SQL cleanup completes, a staging table can
  remain until the next run or its lifecycle expires.
- MaxCompute does not provide traditional multi-statement transactions, and
  dbt does not report affected row counts for MaxCompute DML.

## Current limitations

The following capabilities are intentionally unsupported or not yet complete:

- Python `view`, `ephemeral`, snapshot, and materialized-view outputs; use
  `table` or `incremental`.
- `cluster_by`; MaxCompute does not provide BigQuery's clustering contract.
- Enforced dbt model contracts for Python models.
- Dynamic installation of model-level `packages`.
- dbt cancellation is not yet wired to the active MaxFrame execution object.
  A session is destroyed on normal completion or caught failure, but an
  interrupted worker is not guaranteed to cancel the remote DAG immediately.

## Migration notes for dbt-bigquery users

1. Keep BigQuery-style `partition_by={"field": ..., "data_type": ...}`;
   the adapter accepts it directly.
2. Rewrite row predicates as MaxFrame boolean indexing, for example
   `df[df["id"] > 5]`; do not assume another DataFrame API's `.filter(...)`
   semantics are identical.
3. Remove `cluster_by` from Python models and design MaxCompute partitioning or
   transactional-table layout instead.
4. Replace model-level `packages` with environment installation or
   `with_python_requirements` for remote UDF code.
5. `insert_overwrite` is available for MaxFrame Python models, but verify the
   incremental filter returns only partitions intended for replacement.
6. Replace enforced Python contracts with dbt tests and
   `on_schema_change` where appropriate.
7. Migrate dbt `functions:` resources using the separate
   [Python UDF workflow](python-udfs.md); they are not part of the MaxFrame
   model runtime and default to MaxCompute CPython 3.11 (`cp311`).

## Troubleshooting

### A custom Python UDF worker stalls or crashes

First reproduce the custom-function model with a Python 3.11 dbt environment:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install "dbt-maxcompute[maxframe]"
python -m dbt run --select my_python_model
```

Using `python -m dbt` makes it explicit which interpreter owns the process.
This is a custom-UDF compatibility diagnostic, not a general MaxFrame SDK
installation requirement. A production target that intentionally enforces the
validated environment can set `maxframe_python_version_check: error`.

### MaxFrame is required for Python models

Install the optional runtime in the exact environment that runs dbt:

```bash
python -m pip install "dbt-maxcompute[maxframe]"
```

### `model() must return a MaxFrame DataFrame`

Return the lazy MaxFrame DataFrame. Do not call `.fetch()`, convert it to
Pandas, or return a Series.

### `packages` is not supported

Remove `packages=[...]` from `dbt.config`. Install graph-building dependencies
in the dbt environment or decorate remote UDF functions with
`with_python_requirements`.

### Existing incremental target is not transactional

Recreate it once:

```bash
dbt run --select my_incremental_model --full-refresh
```

### A transport error is retried

The adapter retries transport-level `OSError` failures with a new MaxFrame
session. Semantic, permission, schema, and user-code errors are not retried.
Increase `maxframe_retries` only after confirming the failure is transient.

For additional detail, run the model with dbt debug logging:

```bash
dbt --debug run --select my_model
```
