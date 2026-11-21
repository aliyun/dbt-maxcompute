# MaxFrame Python user guide

`dbt-maxcompute` 1.11.3 provides MaxFrame Python models and MaxCompute catalog
Python UDF/UDAF resources as **Generally Available** capabilities within their
documented supported scope.

Use MaxFrame Python models when a transformation is easier to express with a
DataFrame API than SQL—for example feature engineering, Python-based data
cleaning, or reusable Python scoring logic—while keeping normal dbt
dependencies, tests, partitions, and incremental materializations.

## 1. Install the adapter

Create a dedicated environment. Python 3.11 is the recommended baseline when
your model serializes custom Python functions to MaxCompute workers.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "dbt-maxcompute[maxframe]==1.11.3"
python -m dbt --version
```

Keep an exact version pin for reproducible production environments.

The adapter does not restrict MaxFrame installation to Python 3.11. Other
adapter-supported Python versions can run built-in MaxFrame DataFrame
operations. Python 3.11 is recommended for `DataFrame.apply`, `Series.apply`,
and `with_python_requirements`, because those paths serialize local Python
functions to the default CPython 3.11 worker image.

## 2. Configure a profile

Add a MaxCompute output to `~/.dbt/profiles.yml`:

```yaml
my_maxcompute_project:
  target: prod
  outputs:
    prod:
      type: maxcompute
      project: my_project
      schema: analytics
      endpoint: https://service.cn-hangzhou.maxcompute.aliyun.com/api

      auth_type: access_key
      access_key_id: "{{ env_var('ODPS_ACCESS_ID') }}"
      access_key_secret: "{{ env_var('ODPS_SECRET_ACCESS_KEY') }}"

      submission_method: maxframe
      # maxframe_quota_name: my_maxframe_quota
      maxframe_retries: 2
      maxframe_python_version_check: warn
      maxframe_pythonpack_production: true
      timezone: Asia/Shanghai
```

Use STS, RAM roles, OIDC, or another supported credential provider instead of
long-lived access keys when possible. Never commit credentials to a dbt
project.

To use the Alibaba Cloud default credential provider chain, replace the three
access-key lines above with:

```yaml
      auth_type: chain
```

The chain works for both dbt SQL and MaxFrame Python models. No
`sitecustomize.py` authentication workaround is required.

The MaxFrame-specific settings are:

| Setting | Default | Meaning |
|---|---:|---|
| `submission_method` | `maxframe` | Python model execution backend. |
| `maxframe_quota_name` | project default | Optional MaxFrame quota. |
| `maxframe_retries` | `2` | New-session retries for transient transport or service failures. |
| `maxframe_python_version_check` | `warn` | `warn`, `error`, or `off` for CP311 custom-function compatibility. |
| `maxframe_pythonpack_production` | `true` | Reuse successful remote dependency builds. |

Keep `warn` unless the target is a controlled Python 3.11 environment where
rejecting every non-3.11 MaxFrame submission is intentional.

## 3. Create your first Python model

Create `models/orders_positive.py`:

```python
def model(dbt, session):
    dbt.config(
        materialized="table",
        submission_method="maxframe",
        timeout=1800,
    )

    orders = dbt.ref("stg_orders")
    return orders[orders["amount"] > 0][
        ["order_id", "customer_id", "amount", "order_time"]
    ]
```

Run it like any other dbt model:

```bash
dbt run --select orders_positive
dbt test --select orders_positive
```

Expected dbt output includes the successful MaxFrame session ID:

```text
OK created python table model analytics.orders_positive
OK (MaxFrame session: ...)
```

The model must return one MaxFrame DataFrame. Pandas DataFrames, MaxFrame
Series objects, and arbitrary Python values are not valid model results.

## 4. Use partitions

The partition column must be present in the returned DataFrame:

```python
def model(dbt, session):
    dbt.config(
        materialized="table",
        submission_method="maxframe",
        partition_by={"field": "ds", "data_type": "string"},
        lifecycle=30,
    )

    events = dbt.ref("stg_events")
    return events[["event_id", "customer_id", "event_type", "ds"]]
```

A downstream Python model can read `ds` from `dbt.ref(...)` and use it for
filtering, grouping, selection, or joins. MaxCompute stores it as a partition
column, but MaxFrame exposes it as part of the DataFrame row shape.

Automatic time partitions are also supported:

```python
dbt.config(
    partition_by={
        "field": "event_time",
        "data_type": "timestamp",
        "granularity": "day",
        "generate_column_name": "ds",
    }
)
```

## 5. Create an incremental model

```python
def model(dbt, session):
    dbt.config(
        materialized="incremental",
        submission_method="maxframe",
        incremental_strategy="merge",
        unique_key=["customer_id", "ds"],
        partition_by={"field": "ds", "data_type": "string"},
        on_schema_change="sync_all_columns",
        timeout=3600,
    )

    events = dbt.ref("event_features")
    if dbt.is_incremental:
        events = events[events["ds"] >= "2026-08-01"]

    return (
        events.groupby(["customer_id", "ds"])[["event_id"]]
        .count()
        .reset_index()
        .rename(columns={"event_id": "event_count"})
    )
```

Supported incremental strategies are `merge`, `append`, `delete+insert`,
`insert_overwrite`, and `microbatch`.

Validate both the initial and recurring paths before production rollout:

```bash
dbt run --select customer_daily --full-refresh
dbt run --select customer_daily
dbt test --select customer_daily
```

## 6. Add third-party packages to a MaxFrame UDF

Install packages needed to build the DataFrame graph in the local dbt
environment. For code executed remotely by a MaxFrame UDF, declare packages
with `with_python_requirements`:

```python
import maxframe.dataframe as md
from maxframe.udf import with_python_requirements


@with_python_requirements("rapidfuzz==3.14.5", prefer_binary=True)
def normalized_similarity(value):
    from rapidfuzz import fuzz

    return float(fuzz.WRatio(str(value or ""), "reference name"))


def model(dbt, session):
    dbt.config(materialized="table", timeout=3600)
    customers = dbt.ref("customers")
    customers["name_similarity"] = customers["name"].apply(
        normalized_similarity,
        dtype=md.dtype("float64"),
    )
    return customers
```

The first remote package build is expected to be slower. Production cache is
enabled by default, so later runs can reuse a successful build.

Large native scientific stacks such as NumPy, SciPy, and scikit-learn should
use an approved managed or custom MaxCompute image when available:

```python
dbt.config(sql_hints={"odps.session.image": "sklearn"})
```

Do not assume that every package version has a compatible CP311 manylinux
wheel. Pin and test exact versions before production rollout.

## 7. Create a persistent catalog Python UDF

Catalog UDFs are separate from MaxFrame Python models. They are registered in
MaxCompute and can be called by SQL models, BI tools, and notebooks.

Set the default runtime once in `dbt_project.yml`:

```yaml
functions:
  +runtime_version: "3.11"
```

Create `functions/double_value.py`:

```python
def main(value):
    if value is None:
        return None
    return value * 2
```

Create `functions/double_value.yml`:

```yaml
functions:
  - name: double_value
    config:
      entry_point: main
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
```

Build and call the function:

```bash
dbt build --select double_value
```

```sql
select {{ function('double_value') }}(amount) as doubled_amount
from {{ ref('orders') }}
```

Python aggregate UDFs are also supported. See
[Python UDFs](python-udfs.md) for UDAF lifecycle methods, archive resources,
safe updates, and current limitations.

## 8. Production rollout checklist

Before enabling a project workload:

1. Pin the adapter, MaxFrame, and all remote package versions.
2. Use Python 3.11 for models that serialize custom functions.
3. Run table, partition, full-refresh, recurring incremental, and empty-output
   cases with production-like permissions and quota.
4. Verify row counts, partition values, uniqueness, and business invariants
   with dbt tests.
5. Record the dbt invocation and MaxFrame session ID for failures. Do not copy
   token-bearing LogView URLs into tickets.
6. Validate cold and cached dependency-build latency against your operational
   limits.
7. Confirm cleanup and retry behavior in a dedicated schema before promotion.

The complete release gates are documented in
[MaxFrame production readiness](maxframe-production-readiness.md).

## 9. Current boundaries

- Supported model materializations: `table` and `incremental`.
- `cluster_by`, enforced Python model contracts, dynamic model-level
  `packages`, Python views, snapshots, and ephemeral Python models are not
  supported.
- dbt interruption is not yet guaranteed to cancel an active remote MaxFrame
  DAG immediately. Account for this in operational procedures.
- Catalog Python UDF/UDAF resources have their own documented limitations.

For detailed configuration and migration notes, see
[MaxFrame Python Models](maxframe-python-models.md). For a demanding reference
project with partitions, incremental models, PythonPack dependencies, a
managed scikit-learn image, catalog UDFs, and a UDAF, see
[`examples/maxcompute-showcase/models/06_python_workloads`](../examples/maxcompute-showcase/models/06_python_workloads/README.md).
