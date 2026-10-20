# Microbatch models

`microbatch` is dbt-core's batched incremental strategy. This adapter implements it as
`incremental_strategy='microbatch'` on a `materialized='incremental'` model, and a batch is written
as a **partition overwrite**. Everything below was measured against a real MaxCompute project; what
is not listed is not verified.

The regressions that hold these statements in place live in
`tests/functional/adapter/incremental/`:

| File | Pins |
| --- | --- |
| `test_incremental.py::TestMicrobatchMaxCompute` | upstream's microbatch contract, on this warehouse |
| `test_microbatch_window_semantics.py` | windows, replay, empty window, late data, duplicate keys, rejected configurations |
| `test_microbatch_partition_timezone.py` | the session-timezone dependency described below |
| `test_microbatch_datetime_event_column.py` | the shape of a batch boundary, and which event-column types can be compared against it |
| `test_microbatch_failure_retry.py` | what survives a batch that fails, and what `dbt retry` replays |

## How a batch is written

dbt-core computes the batch list from `begin`, `batch_size` and the run's event-time window,
re-compiles the model once per batch, and attaches a half-open filter
(`event_time >= <start> and event_time < <end>`) to every upstream ref or source that declares
`event_time`. This adapter then builds a temporary relation from that filtered query and runs

```sql
insert overwrite table <target> (select * from <tmp>)
```

On an auto-partitioned target - which is what `partition_by` with a time-typed field creates, and
what microbatch requires - MaxCompute replaces **only the partitions that appear in the batch's
rows**. Neighbouring partitions keep their rows, and a batch whose query returns nothing is a no-op.

| Situation | Result |
| --- | --- |
| Row exactly at `<start>` | written by this batch |
| Row exactly at `<end>` | left to the next batch |
| Re-running the same window | same rows; nothing appended twice |
| Window with no matching rows | target unchanged: nothing added, nothing deleted |
| Late row inside an already-written window | invisible until that window is replayed (or `lookback` covers it) |

Windows can be driven without touching any clock:

```
dbt run --event-time-start 2025-05-02 --event-time-end 2025-05-03
```

The two flags are mutually required, and they are what makes these runs reproducible.

## The event column's type decides whether the window text parses at all

dbt-core computes every batch window in UTC and renders it from a `datetime`, so the text it
produces carries an offset: `event_time >= '2025-05-01 00:00:00+00:00'`. MaxCompute compares that
shape against `timestamp` and `timestamp_ntz` columns, but a `datetime` column accepts
`yyyy-mm-dd hh:mi:ss` and nothing else - the offset makes the batch fail while the physical plan is
being generated, before any row is read:

```
ODPS-0130071:[0,0] Semantic analysis exception - physical plan generation failed:
ODPS-0121095:Invalid argument - in function cast, string datetime's format must be
yyyy-mm-dd hh:mi:ss, input string is:2025-05-01 00:00:00+00:00
```

So on the adapter's default profile - no `timezone` in the profile, so the session runs on UTC - a
microbatch model whose event column is `datetime` could not run at all, while the same model on
`timestamp` ran. The adapter now states the window **without the offset suffix** whenever the
session clock is UTC, which leaves the instant the window means unchanged. Measured on a real
project, three column types x three boundary texts, each repeated on three session clocks
(`Etc/GMT`, `Asia/Shanghai`, `Etc/GMT+8`). The table states whether a text **parses** - which rows a
session picks with a text that parses depends on its clock, and that is the next section's subject:

| Event column | `'2025-05-01 09:00:00'` | `'2025-05-01 09:00:00+00:00'` | `'2025-05-01 09:00:00.500000'` |
| --- | --- | --- | --- |
| `datetime` | parses; selects its rows on a UTC session | refused (`ODPS-0121095`) | refused (`ODPS-0121095`) |
| `timestamp` | parses | parses | parses |
| `timestamp_ntz` | parses | parses | parses |

Removing the suffix moves no window: on a given session clock, the same rows were selected with
and without the `+00:00` suffix for both `timestamp` and `timestamp_ntz` (measured 2026-10-07 at
18:43 on a real project, three session clocks x both texts). Only the session clock decides which
rows a text selects - the subject of the next section - and `datetime` is the one type that parses
only the suffix-free form.

`date` does not appear above because no text in it compares: a `date` event column yields NULL
against all three, so every batch selects nothing while the run still reports success. That is a
different defect from this one, and pull request #39 proposes refusing it at compile time.

Two edges are kept visible rather than smoothed over:

* **Fractional seconds are not rounded away.** `datetime` refuses them too (see the table), so a
  window carrying one still fails to compile against such a column - in practice only `--sample`,
  whose end is `datetime.now(UTC)`. Rounding would have traded that loud failure for a silently
  different window on the two types that do accept fractions. Batch boundaries themselves never
  carry a sub-second value: dbt-core aligns every boundary to `batch_size`, and the finest grain is
  an hour.
* **A profile that pins a non-UTC `timezone` is not changed here.** There the window has to be
  shifted onto that clock as well, which is the subject of the next section; on such a session a
  `datetime` column keeps failing outright, exactly as it does today.

## Requirements, and who enforces them

| Combination | Outcome | Enforced by |
| --- | --- | --- |
| no `partition_by` | rejected: "The 'microbatch' strategy requires a `partition_by` config." | this adapter |
| `partition_by.granularity` != `batch_size` | rejected: "requires a `partition_by` config with the same granularity as its configured `batch_size`" | this adapter |
| no `event_time` | rejected during parsing: "Microbatch model '<name>' must provide an 'event_time' (string) config" | dbt-core |
| no `batch_size` | rejected during parsing | dbt-core |
| no `begin` | rejected during parsing: "Microbatch model '<name>' must provide a 'begin' (datetime) config" | dbt-core |
| no `unique_key` | **accepted**; the model runs | nobody |
| the same `unique_key` twice inside one window | **both rows are kept** | - |

Read the last two lines before relying on `unique_key`: a batch is a partition overwrite, so
`unique_key` neither makes the write an upsert nor deduplicates, and nothing enforces that it is
set. For key-based updates use `merge` or `delete+insert`. (The example models in this repository
used to state that a missing `unique_key` raises a compiler error; it does not, and the comment is
corrected in the same change as this page.)

## The session timezone decides whether a window maps onto one partition

dbt renders the window as a timestamp string carrying an offset - `event_time >=
'2025-05-01 00:00:00+00:00'` - and MaxCompute evaluates that comparison on the **session** timezone
(measured: `+00:00`, `+08:00` and `-05:00` on the same wall clock select the same rows). The
partition a row lands in comes from `trunc_time(<field>, '<granularity>')`, which cuts on **UTC**.
Those two clocks have to agree, and what makes them agree is the session timezone:

* `MaxComputeCredentials._get_odps` sets pyodps' `local_timezone` to false when the profile omits
  `timezone`, and pyodps then submits `odps.sql.timezone=Etc/GMT` with every statement;
* with that default, one day window maps onto exactly one partition and two adjacent windows never
  touch the same partition;
* with `timezone: Asia/Shanghai` in the profile, the same fixtures behave differently: the window is
  read on the session clock while `trunc_time()` keeps cutting UTC days, rows before 08:00 local
  belong to the previous partition day, one "day" window covers two partitions, and the next
  window's overwrite erases rows the earlier window had written. The run reports success.

So `timezone` in the profile is not a display option as far as `microbatch` is concerned: it changes
which rows survive. Until the two clocks are reconciled in code - or the combination is refused at
compile time - **leave `timezone` unset for projects whose models use `microbatch`**.
`TestMicrobatchProfileTimezone` pins the failing combination as `xfail(strict=True)`, so it starts
reporting an error the moment the behaviour changes and can be deleted deliberately.

## Not verified here

- Python (MaxFrame) microbatch models, which apply the window to the returned DataFrame instead of
  to the compiled SQL;
- `batch_size` of `month` or `year`, and how their units interact with `trunc_time()`;
- `partitions`, `lookback`, `on_schema_change`, `grants` and model contracts on microbatch models;
- `datetime` event columns on a profile that pins a non-UTC `timezone`: the window has to be
  shifted onto that clock first, and the refusal above is what was measured today;
- `dbt retry` for a model whose *first* batch fails.
