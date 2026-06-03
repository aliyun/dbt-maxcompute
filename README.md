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

### Important Note

The `README` you are currently viewing will be updated with specific instructions and details on how to utilize the
adapter as development progresses.

### Adapter Versioning

This adapter plugin follows [semantic versioning](https://semver.org/). The initial version is **v1.8.0-a0**, designed
for compatibility with dbt Core v1.8.0. Since the plugin is in its early stages, the version number **a0** indicates
that it is an Alpha release. A stable version will be released in the future, focusing on MaxCompute-specific
functionality and aiming for backwards compatibility.

## Getting Started

### Install the plugin

```bash
# we use conda and python 3.10 for this example
conda create --name dbt-maxcompute-example python=3.10
conda activate dbt-maxcompute-example

pip install dbt-core
pip install dbt-maxcompute
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


## Compatible dbt Packages for MaxCompute
The following community-maintained dbt packages have been verified to work with dbt-maxcompute:

1. [dbt-date (MaxCompute Edition)](https://github.com/dingxin-tech/dbt-date)
2. [dbt-utils (MaxCompute Edition)](https://github.com/dingxin-tech/dbt-utils)
3. [dbt-expectations (MaxCompute Edition)](https://github.com/dingxin-tech/dbt-expectations)
4. [elementary (MaxCompute Edition)](https://github.com/dingxin-tech/elementary)
5. [dbt-project-evaluator (MaxCompute Edition)](https://github.com/dingxin-tech/dbt-project-evaluator)


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
