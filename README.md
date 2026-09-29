<p align="left">
  <img src="https://raw.githubusercontent.com/aliyun/dbt-maxcompute/master/icon_MaxCompute.svg" alt="MaxCompute logo" width="300" height="150" style="margin-right: 100px;"/>
  <img src="https://raw.githubusercontent.com/dbt-labs/dbt/ec7dee39f793aa4f7dd3dae37282cc87664813e4/etc/dbt-logo-full.svg" alt="dbt logo" width="300" height="150"/>
</p>

# dbt-maxcompute
[![PyPI version](https://img.shields.io/pypi/v/dbt-maxcompute.svg?style=flat-square)](https://pypi.python.org/pypi/dbt-maxcompute)
[![License](https://img.shields.io/pypi/l/pyodps.svg?style=flat-square)](https://github.com/aliyun/dbt-maxcompute/blob/master/License)
<a href="https://github.com/aliyun/dbt-maxcompute/actions/workflows/main.yml">
<img src="https://github.com/aliyun/dbt-maxcompute/actions/workflows/main.yml/badge.svg?event=push" alt="Unit Tests Badge"/>
</a>

Welcome to the **dbt-maxCompute** repository! This project aims to extend the capabilities of **dbt** (data build tool)
for users of Alibaba MaxCompute, a cutting-edge data processing platform.

## What is dbt?

**[dbt](https://www.getdbt.com/)** empowers data analysts and engineers to transform their data using software
engineering best practices. It serves as the **T** in the ELT (Extract, Load, Transform) process, allowing users to
organize, cleanse, denormalize, filter, rename, and pre-aggregate raw data, making it analysis-ready.

## About MaxCompute

MaxCompute is Alibaba Group's cloud data warehouse and big data processing platform, supporting massive data storage and
computation, widely used for data analysis and business intelligence. With MaxCompute, users can efficiently manage and
analyze large volumes of data and gain real-time business insights.

This repository contains the foundational code for the **dbt-maxcompute** adapter plugin. For guidance on developing the
adapter, please refer to the [official documentation](https://docs.getdbt.com/docs/contributing/building-a-new-adapter).

### Adapter Versioning

This adapter follows [semantic versioning](https://semver.org/) and the dbt Core
1.11 compatibility line. Individual capabilities can have a narrower maturity
level than the adapter package:

| Capability | Status | Intended use |
|---|---|---|
| SQL models and established materializations | Generally Available | Production |
| MaxFrame Python models | **Production Preview** | Selected production workloads after reviewing documented limitations |
| Python scalar and aggregate UDFs | **Beta** | Evaluation and controlled workloads; dependency conventions may change before GA |

Production Preview and Beta limitations are documented in their respective
guides. Pre-release package versions such as `1.11.3b3` do not replace the
latest stable release.

## Getting Started

### Install the plugin

```bash
# Python 3.11 is a reproducible baseline for MaxFrame custom-UDF workloads.
conda create --name dbt-maxcompute-example python=3.11
conda activate dbt-maxcompute-example

pip install dbt-core
pip install dbt-maxcompute
```

To run Python models on MaxFrame, install the optional MaxFrame runtime:

```bash
pip install "dbt-maxcompute[maxframe]"
```

### Configure dbt profile:

1. Create a file in the ~/.dbt/ directory named profiles.yml.
2. Copy the following and paste into the new profiles.yml file. Make sure you update the values where noted.

```yaml
jaffle_shop: # this needs to match the profile in your dbt_project.yml file
  target: dev
  outputs:
    dev:
      type: maxcompute
      project: dbt-example # Replace this with your project name
      schema: default # Replace this with schema name, e.g. dbt_bilbo
      endpoint: http://service.cn-shanghai.maxcompute.aliyun.com/api # Replace this with your maxcompute endpoint
      auth_type: access_key
      access_key_id: XXX # Replace this with your accessId(ak)
      access_key_secret: XXX # Replace this with your accessKey(sk)
```

Currently we support the following parameters：

| **Field**           | **Description**                                                                                             | **Default Value**                     |
|---------------------|-------------------------------------------------------------------------------------------------------------|---------------------------------------|
| `type`              | The type of database connection. Must be set to `"maxcompute"` for MaxCompute connections.                  | `"maxcompute"`                        |
| `project`           | The name of your MaxCompute project.                                                                        | **Required (no default)**             |
| `endpoint`          | The endpoint URL used to connect to MaxCompute.                                                             | **Required (no default)**             |
| `schema`            | The namespace schema that the models will use in MaxCompute.                                                | **Required (no default)**             |
| `auth_type`         | Authentication method for accessing MaxCompute.                                                             | `"access_key"`                        |
| `access_key_id`     | Access ID used for authentication.                                                                          | **Required if using access key auth** |
| `access_key_secret` | Access Key Secret used for authentication.                                                                  | **Required if using access key auth** |
| `timezone`          | The Timezone used for MaxCompute.                                                                           | `"GMT"`                               |
| `tunnel_endpoint`   | The tunnel endpoint URL used to fetch result from MaxCompute.                                               | **Auto detected by endpoint**         |
| `execution_mode`    | SQL execution engine. `"offline"` uses the standard batch engine; `"maxqa"` routes queries through MaxQA (MCQA V2) for interactive acceleration. | `"offline"` |
| `quota_name`        | Interactive quota group name for MaxQA. When omitted, the server returns a default connection (if available). | -                                     |
| `maxqa_fallback`    | Enable server-side fallback to offline when MaxQA cannot handle a query (e.g. DDL).                         | `true`                                |
| `maxqa_fallback_quota` | Offline quota group name used for fallback. When omitted, the server uses the project default.           | -                                     |
| `submission_method` | Default Python model submission method. The supported value is `maxframe`. | `maxframe` |
| `maxframe_quota_name` | Optional quota used by MaxFrame sessions. | Project default |
| `maxframe_retries` | Number of new-session retries for transient MaxFrame DAG transport failures. | `2` |
| `maxframe_python_version_check` | Custom-UDF CP311 compatibility policy: `warn`, `error`, or `off`. It does not affect installation. | `warn` |
| `maxframe_pythonpack_production` | Reuse successful PythonPack builds from MaxFrame's production cache. | `true` |
| Other auth options  | Alternative authentication methods such as STS. See [Authentication Configuration](docs/authentication.md). | **Varies by auth type**               |

> **Note**: Fields marked with "Required" must be explicitly specified in your configuration.

### Run your dbt models

If you are new to DBT, we have prepared a [Tutorial document](docs/Tutorial.md) for your reference. Of course, you can also access the
official documentation provided by DBT (but some additional adaptations may be required for MaxCompute)

### Configure Your dbt Models

You can customize dbt materialization behavior through model configurations. For general dbt configuration reference,
see the official documentation: [dbt Model Configs](https://docs.getdbt.com/reference/model-configs).

While dbt core provides native configurations like `materialized` and `sql_header`, this section focuses on
**dbt-maxcompute specific configurations** that control table creation behavior during materialization.

For Append and PK Delta Table creation with SQL `table` and `incremental` models, see
[Delta Table support and configuration (中文)](docs/delta-tables.md).


#### dbt-maxcompute Specific Configurations

| Parameter                  | Type               | Default                | Description                                                                                                                                                                                                                                                                                                                          |
|----------------------------|--------------------|------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **tblproperties**          | Map[String,String] | -                      | Additional table properties. Example: `{'table.format.version'='2'}` creates an Append2 table.                                                                                                                                                                                                                                       |
| **transactional**          | Boolean            | `false`                | Equivalent to `tblproperties ('transactional' = 'true')`. Indicates whether to create a transactional table.                                                                                                                                                                                                                         |
| **delta**                  | Boolean            | `false`                | Same to **transactional**, additional primary key validation.                                                                                                                                                                                                                                                                        |
| **primary_keys**           | List[String]       | -                      | List of primary key column names (e.g., `['c1']`). Required when `delta=true`.                                                                                                                                                                                                                                                       |
| **delta_table_bucket_num** | Integer            | `16`                   | Equivalent to `tblproperties ('write.bucket.num' = 'xx')`. Controls bucket count for Delta tables.                                                                                                                                                                                                                                   |
| **partition_by**           | Map                | -                      | Defines partitioning strategy with two fields:<br>• `fields`: Comma-separated partition columns<br>• `data_types`: Optional data types (default: `string`). When specifying time types (`date`, `datetime`, `timestamp`), creates auto-partitioned tables.<br>Example: `{"fields": "name,some_date", "data_types": "string,string"}` |
| **lifecycle**              | Integer            | -                      | Table retention period in days (e.g., `30` for 30-day lifecycle).                                                                                                                                                                                                                                                                    |
| **sql_hints**              | Map[String,String] | See below for defaults | SQL hints applied to all queries for optimization or compatibility.                                                                                                                                                                                                                                                                  |

**Default SQL Hints**

MaxCompute supports global SQL hints to control query behavior and optimize performance. The following are the default global hints used by our system:
```yaml
odps.sql.type.system.odps2: "true"
odps.sql.decimal.odps2: "true"
odps.sql.allow.fullscan: "true"
odps.sql.select.output.format: "csv"
odps.sql.submit.mode: "script"
odps.sql.allow.cartesian: "true"
odps.sql.allow.schema.evolution: "true"
odps.table.append2.enable": "true"
```
You can override these defaults by specifying your own `sql_hints` use model config. Your custom hints will be merged with the defaults — you do not need to repeat the entire list unless you want to change specific values.

### MaxQA (Interactive Query Acceleration)

MaxQA (MCQA V2) is MaxCompute's interactive query acceleration engine. It provides significantly faster execution for suitable workloads — queries that take 30+ seconds in offline mode can often complete in under 5 seconds with MaxQA.

#### Enable MaxQA in your profile

```yaml
my_profile:
  target: dev
  outputs:
    dev:
      type: maxcompute
      project: my_project
      schema: default
      endpoint: http://service.cn-hangzhou.maxcompute.aliyun.com/api
      access_key_id: "{{ env_var('ODPS_ACCESS_ID') }}"
      access_key_secret: "{{ env_var('ODPS_SECRET_ACCESS_KEY') }}"
      execution_mode: maxqa
      quota_name: my_interactive_quota   # optional
```

When `execution_mode` is set to `maxqa`, all SQL is submitted through the MaxQA endpoint. By default, server-side fallback is enabled (`maxqa_fallback: true`), so DDL and complex queries that MaxQA cannot handle are automatically routed to the offline engine.

#### Fallback configuration

| Setting | Behavior |
|---------|----------|
| `maxqa_fallback: true` (default) | Server automatically falls back to offline for unsupported queries |
| `maxqa_fallback: false` | No fallback — unsupported queries will fail |
| `maxqa_fallback_quota: my_offline_quota` | Falls back to a specific offline quota group |

#### Per-model override

You can override the execution mode on individual models using `sql_hints`:

```sql
-- Force a heavy model to use offline, even when the profile default is maxqa
{{ config(
    materialized='table',
    sql_hints={'dbt.execution_mode': 'offline'}
) }}
SELECT ...
```

```sql
-- Use MaxQA for a specific model when the profile default is offline
{{ config(
    materialized='table',
    sql_hints={'dbt.execution_mode': 'maxqa', 'dbt.quota_name': 'my_quota'}
) }}
SELECT ...
```

The `dbt.execution_mode` and `dbt.quota_name` hints are consumed by the adapter and never sent to MaxCompute.


### MaxFrame Python Models

Python models can use MaxFrame DataFrames with the standard dbt `ref`, `source`,
`config`, and `this` objects. The adapter creates an isolated MaxFrame session,
writes to a dbt intermediate table, and then uses the normal table swap flow so
an existing target is not replaced until the MaxFrame job succeeds.

Start with the [MaxFrame Python user guide](docs/maxframe-python-user-guide.md),
then use the [MaxFrame Python Models reference](docs/maxframe-python-models.md)
for complete installation, partitioning, incremental, operations, and
migration details. Run the
[production readiness checklist](docs/maxframe-production-readiness.md) before
promoting a project workload.

```python
def model(dbt, session):
    dbt.config(
        materialized="table",
        submission_method="maxframe",
        timeout=3600,
        lifecycle=7,
    )

    orders = dbt.ref("stg_orders")
    return orders[orders.amount > 0][["order_id", "amount"]]
```

Run it like any other dbt model:

```bash
dbt run --select path:models/my_maxframe_model.py
```

The dbt output includes the MaxFrame session ID and reports whether LogView is
available. Signed LogView URLs are not written to logs or artifacts because
they contain temporary access tokens. Model-level `sql_hints` are forwarded to
MaxFrame, and `maxframe_quota_name` can be set in the model or profile.

Table and incremental materializations support regular MaxCompute partitions.
Both the MaxCompute plural form and the BigQuery-style singular form are
accepted, which makes migrated Python models easier to reuse:

```python
def model(dbt, session):
    dbt.config(
        materialized="incremental",
        incremental_strategy="merge",
        unique_key="order_id",
        partition_by={"field": "ds", "data_type": "string"},
        on_schema_change="sync_all_columns",
    )

    orders = dbt.ref("stg_orders")
    if dbt.is_incremental:
        # Replace this predicate with the project's watermark policy.
        orders = orders[orders["order_id"] > 1_000_000]
    return orders
```

Time columns create MaxCompute automatic partitions. `granularity` and
`generate_column_name` control the generated partition column:

```python
partition_by={
    "field": "event_time",
    "data_type": "timestamp",
    "granularity": "day",
    "generate_column_name": "ds",
}
```

Supported incremental strategies are `merge`, `append`, `delete+insert`,
`insert_overwrite`, and `microbatch`. Incremental targets are created as
transactional MaxCompute tables because MaxCompute `MERGE` requires that
property. Python microbatch models apply dbt's half-open event-time window to
the returned DataFrame before writing each batch.

Writes are failure-safe: table models and Python full refreshes build an
intermediate relation and swap it only after the MaxFrame DAG succeeds. Failed
DAG output relations are removed with bounded retries. Automatic-partition
staging tables use lifecycle `1` and are cleaned before reuse and after a
successful run. Transient DAG transport errors are retried in a new MaxFrame
session (`maxframe_retries`, default `2`). The compiled Python model is
re-executed against the new session after its failed staging relation is
cleaned. Successful remote dependency builds use MaxFrame's production
PythonPack cache by default; set `maxframe_pythonpack_production: false` only
when short-lived dependency builds should not be retained.

Current intentional differences from dbt-bigquery Python models:

- `cluster_by` is rejected because MaxCompute partitioning and transactional
  tables do not provide BigQuery's clustering contract.
- Enforced dbt model contracts are rejected for Python models. Schema changes
  on incremental models should use `on_schema_change`.
- `packages` is not installed dynamically into the dbt process. Install
  graph-building dependencies in the dbt runtime. For dependencies used by a
  remote MaxFrame UDF, decorate the function with
  `maxframe.udf.with_python_requirements(...)`.


### Python UDFs

dbt `functions:` resources can create persistent MaxCompute Python scalar and
aggregate functions. Scalar functions keep the standard dbt plain-function
authoring experience; aggregate functions use MaxCompute's bounded
`BaseUDAF` buffer lifecycle.

See the [Python UDF guide](docs/python-udfs.md) for deployment safety,
dependencies, data types, UDAF examples, and BigQuery migration differences.

```python
# functions/double_value.py
def main(value):
    return None if value is None else value * 2
```

```yaml
# functions/double_value.yml
functions:
  - name: double_value
    config:
      entry_point: main
      runtime_version: "3.11"
    arguments:
      - name: value
        data_type: bigint
    returns:
      data_type: bigint
```

Build and reference it through dbt's normal function DAG:

```bash
dbt build --select double_value
```

```sql
select {{ function('double_value') }}(quantity) from {{ ref('orders') }}
```

dbt-maxcompute defaults SQL UDF execution to MaxCompute CPython 3.11
(`cp311`). Third-party libraries must be uploaded as compatible MaxCompute
resources; dynamic dbt `packages` installation is not available on this
runtime. To avoid repeating `runtime_version` on every Python function, set
`functions: {+runtime_version: "3.11"}` once in `dbt_project.yml`.


## Compatible dbt Packages for MaxCompute

"Compatible" is not a single state, so this table records the level we have evidence for, from a
dbt-core 1.11.2 + dbt-maxcompute 1.11.3b3 run on 2026-09-25 against a three-tier MaxCompute project:

* **build** — `dbt deps`, `dbt parse`, `dbt compile` and a real `dbt build` of representative models all ran green.
* **partial** — installs and compiles, but at least one macro family fails once the SQL reaches MaxCompute.
* **blocked** — `dbt build` stops early, so the package cannot be relied on end to end yet.

| Package (MaxCompute Edition) | Level | What the 2026-09-25 run showed |
|---|---|---|
| [dbt-utils](https://github.com/dingxin-tech/dbt-utils) | build | `width_bucket`, `deduplicate`, `get_relations_by_pattern` and the generic tests all executed. |
| [dbt-expectations](https://github.com/dingxin-tech/dbt-expectations) | build | `type_timestamp` plus the unique / in-set / of-type / stdev tests executed; the date-part coverage test works and reports real gaps. |
| [dbt-date](https://github.com/dingxin-tech/dbt-date) | partial | Calendar macros (`day_of_week`, `week_start`, `iso_week_of_year`, `date_part`, `convert_timezone`) executed. The date-spine family (`get_base_dates`, `get_date_dimension`, fiscal macros) fails with `ODPS-0130161 invalid TIMESTAMP format` when a plain `YYYY-MM-DD` date is passed, because `maxcompute__get_base_dates` emits a `timestamp'YYYY-MM-DD'` literal without a time part. |
| [elementary](https://github.com/dingxin-tech/elementary) | partial | Its own models and tests build; the `monitors_runs` view fails with `ODPS-0130071` because a DOUBLE value is written into the FLOAT-typed `execution_time` column. |
| [dbt-project-evaluator](https://github.com/dingxin-tech/dbt-project-evaluator) | blocked | `stg_nodes` fails on the same DOUBLE/FLOAT conflict, which cascades to skip most of the package's models and tests. |
| [dbt-data-reliability](https://github.com/dingxin-tech/dbt-data-reliability) | partial | Same `monitors_runs` failure as elementary. It also still depends on the upstream `dbt-labs/dbt_utils`, which has no MaxCompute dispatch and does not build here, so that dependency has to be repointed to the MaxCompute edition. |

Two things to watch when installing these packages:

* Several `packages.yml` files reference a branch (`revision: main`) instead of a tag, so a build is
  only reproducible if you commit `package-lock.yml`; `dbt-data-reliability` has no release tag yet.
* dbt needs a three-tier MaxCompute project (`project.schema.table`). On a two-tier project, `dbt build`
  fails up front with `ODPS-0110061 Invalid database operations on two-tier model`.


## Known Limitations

Due to MaxCompute engine characteristics, the following limitations apply:

| Limitation | Description |
|------------|-------------|
| **No rowcount support** | MaxCompute does not return the number of affected rows after DML operations. The `rows_affected` field in adapter responses will not be available. |
| **No transaction support** | MaxCompute does not support traditional database transactions. `BEGIN`, `COMMIT`, and `ROLLBACK` operations are no-ops. |


## Developers Guide

If you want to contribute or develop the adapter, use the following command to set up your environment:

```bash
pip install -r dev-requirements.txt
```

## Reporting Bugs and Contributing

Your feedback helps improve the project:

- To report bugs or request features, please open a
  new [issue](https://github.com/aliyun/dbt-maxcompute/issues/new) on GitHub.

## Code of Conduct

We are committed to fostering a welcoming and inclusive environment. All community members are expected to adhere to
the [dbt Code of Conduct](https://community.getdbt.com/code-of-conduct).
