# dbt-maxcompute showcase

A runnable dbt project that demonstrates the MaxCompute-specific features of
the `dbt-maxcompute` adapter. The aim is to be useful as a reference if you
already know dbt and are new to MaxCompute (ODPS).

## What this is not

- Not a tutorial on dbt itself — see <https://docs.getdbt.com>.
- Not a tutorial on MaxCompute the product — see
  <https://www.alibabacloud.com/help/en/maxcompute/>.
- Not packaged with the wheel. This directory lives in the source repo only
  and is excluded from the published `dbt-maxcompute` distribution.

## MaxCompute mental model in one minute

If you arrive from BigQuery / Snowflake / Postgres, three things are
different enough to call out:

1. **Project = top-level namespace, schema is optional.** MaxCompute always
   has a `project`; `schema` only exists when the project has schema mode
   enabled. In dbt terms, `database` maps to project and `schema` maps to
   MaxCompute schema (or the default schema if disabled).
2. **Partitions are first-class, not a hint.** A partitioned table has a
   declared partition spec. `INSERT INTO ... PARTITION (...)` and
   `INSERT OVERWRITE ... PARTITION (...)` operate on whole partitions, and
   the partition column is *not* part of the data column list.
3. **Lifecycle, not vacuum.** A `LIFECYCLE n` clause tells MaxCompute to
   drop the partition (or the table) `n` days after last modification. This
   is the primary cost-control knob.

These three points are exactly what the showcase walks through.

## Layout

```
models/
  01_partitioning/         partition_by variants (static / auto / multi-col)
  02_incremental/          all five incremental strategies
  03_materialized_view/    MV with lifecycle + partitioning
  04_operations/           lifecycle, transactional / delta tables
snapshots/                 SCD-2 snapshot
seeds/                     a tiny synthetic orders dataset
```

Each subfolder has its own `README.md` explaining what the models in it
demonstrate and which adapter features they exercise.

## Run it

```bash
# 1. install the adapter (either way is fine)
pip install dbt-maxcompute              # released
pip install -e ../..                    # dev — points at this repo

# 2. supply credentials. profiles.yml.example shows the env-var form; copy
#    it next to dbt_project.yml and fill in the project/endpoint:
cp profiles.yml.example profiles.yml

export ODPS_ACCESS_ID=...
export ODPS_SECRET_ACCESS_KEY=...

# 3. run from this directory (so dbt picks up the local profiles.yml)
dbt deps
dbt seed
dbt run
dbt snapshot
dbt test
```

`profiles.yml` is git-ignored — credentials never leave your machine.

## A note on cost

The whole project runs against a tiny seeded dataset and uses
`lifecycle: 1` on the demonstration tables, so the data is deleted by
MaxCompute the day after you run it. Nothing here is expensive, but you are
still hitting a real MaxCompute project and the usual rules apply: a
mis-typed `partition_by` can turn a one-partition scan into a full scan.
