# 在 DLF Paimon 外部项目中试用 dbt

dbt-maxcompute `1.12.2b1` 支持从 MaxCompute 的 DLF Paimon 外部项目（EP v2）读取 `source()`，并试用 SQL `table` 和 `incremental` 模型。写入目标取决于模型所在的项目和 Schema。请先创建并配置外部项目，并为运行账号授予所需的数据权限。[Paimon_DLF 外部项目](https://help.aliyun.com/zh/maxcompute/paimon-dlf-external-project)

## 安装试用版

```bash
python -m pip install 'dbt-maxcompute==1.12.2b1'
```

## 物化方式

| 方式 | 试用范围 |
| --- | --- |
| `table` | 目标名称可用时建表并写入。存在同名表或视图时，普通运行和 `--full-refresh` 都会报错。 |
| `incremental` | 只支持 `insert_overwrite`。每次指定一个尚不存在的完整静态分区。已存在的分区和 `--full-refresh` 都会触发错误。 |
| `view` | 当前 MaxCompute 服务拒绝在 EP v2 中执行 `CREATE VIEW`。目标名称已被占用时，adapter 会在提交 DDL 前报错。 |

## 调度约束

> **重要**
> 同一张目标表的同一个完整分区键必须由一个任务写入。调度需要覆盖 dbt 的独立进程、手动重跑，以及 Flink、Spark 等其他写入方。互斥从检查分区前开始，持续到 MaxCompute 写入作业终止。

adapter 先检查分区，再提交 `INSERT OVERWRITE`。两个任务可能同时查到分区不存在，后提交的任务可能覆写先提交的数据。以 `dt, region` 为分区键时，`dt=2026-11-23, region=cn` 和 `dt=2026-11-23, region=us` 可以并行。两个任务写同一个 `dt, region` 组合时，必须由调度保证互斥。

新表由单个任务完成建表和首次写入，再交给其他任务使用。首次写入失败时，已创建的表可能保留为空表。遇到超时或连接中断，先确认作业结果和目标分区状态，再决定是否重跑。

## 新增静态分区

在 SQL 模型中使用 `partition_by` 声明分区列，并通过 `partition_values` 提供每个分区列的值。模型查询需要返回分区列，adapter 会筛选本次目标分区的数据。已有目标表的分区列及其顺序必须与 `partition_by` 一致。

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={'fields': ['dt', 'region']},
    partition_values={'dt': var('run_date'), 'region': var('run_region')}
) }}

select order_id, amount, dt, region
from {{ ref('stg_orders') }}
```

假设模型文件为 `models/lake_orders.sql`，可运行以下命令新增一个分区。

```bash
dbt run --select lake_orders --vars '{run_date: "2026-11-23", run_region: "cn"}'
```

增量模型使用默认的 `on_schema_change='ignore'`，采用完整静态分区值。分区列表格式无法识别或达到查询上限时，adapter 会在写入前报错。
