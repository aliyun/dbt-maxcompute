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
| `test_microbatch_partition_timezone.py` | the window/partition clock pairing: a UTC session, `timezone: Asia/Shanghai`, a negative/DST zone, and a `datetime` column on a shifted session |
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
* **Both halves are one rule.** Stating the boundary as the session's own reading of the instant -
  which is what the next section is about - is also what makes the text parseable here: on a
  session that pinned `Asia/Shanghai` a `datetime` column reads `'2025-05-01 08:00:00'` for the UTC boundary
  `2025-05-01 00:00:00+00:00`, so it both compiles and lands on its own partition
  (`TestMicrobatchDatetimeColumnOnShiftedSession`). What still refuses to compile is a boundary with
  fractional seconds, for the reason above.

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

## The batch window is compared on the session clock, the partition key on UTC

dbt renders the window as a timestamp string carrying an offset - `event_time >=
'2025-05-01 00:00:00+00:00'` - and MaxCompute evaluates that comparison on the **session** timezone
(measured: `+00:00`, `+08:00` and `-05:00` on the same wall clock select the same rows). The
partition a row lands in comes from `trunc_time(<field>, '<granularity>')`, which cuts on **UTC**.

What sets the session timezone is the profile: `MaxComputeCredentials._get_odps` writes pyodps'
`local_timezone`, and pyodps submits it as `odps.sql.timezone` with every statement - `Etc/GMT`
when the profile omits `timezone`. On that default both clocks read UTC, so nothing needs
correcting. A profile that *does* set `timezone` used to move one clock and not the other: a "day"
window reached into its neighbour's partition, and the later `insert overwrite` erased rows the
earlier window had written, while the run reported success.

`MaxComputeRelation._render_event_time_filtered` states each boundary in the session's own reading
of the same UTC instant - `2025-05-01 00:00:00+00:00` becomes `'2025-05-01 08:00:00'` under
`timezone: Asia/Shanghai` - which the server parses back into that instant, so the window and the
partition key are cut by the same clock. **That restatement is applied to the column types that have
a clock to restate against**: a `timestamp_ntz` field's boundaries stay on the UTC reading (see
"Which column type decides" below), because moving a naive value's window is what puts rows into
their neighbour's batch. The same call drops the offset suffix for every type, so the suffix fix is
one rule, not a branch. Two properties of that rendering are deliberate:

* on a UTC session the two readings are the *same text*, so this shift moves nothing there - that
  path had already changed once, when the offset suffix was dropped (#40), and it changes again only
  in the sense that the same suffix-free string is now produced by one rule instead of a branch;
* the boundary stays a plain string literal. The alternative spelling of the same instant,
  `from_utc_timestamp(...)`, evaluates to a `TIMESTAMP`, and MaxCompute refuses to compare a
  `TIMESTAMP` with a `DATETIME` or `DATE` operand instead of converting it (measured) - so that form
  would trade a working query for a compile error on some models. It would also replace a constant
  with a function call in the predicate, and whether the optimizer still prunes the source with a
  non-constant boundary has not been measured here.

### Which column type decides, and what happens when it cannot be read

The event-time filter dbt-core hands the adapter carries a field name and two UTC instants - no
type. The relation that filter is rendered on, though, is the *upstream* one, and dbt-core builds it
from the upstream node: every `ref()` / `source()` path passes that node to
`Relation.create_from` (`dbt/context/providers.py`), and the adapter can ask the warehouse what the
relation's own columns are. So the type question is answerable here, and this pull request answers
it. Measured on the real `ref()` path with `timezone: Asia/Shanghai`:

| physical type of the event field on the relation being read | what the window does | why |
| --- | --- | --- |
| `timestamp` | shifted to the session's reading of the instant | the server parses the text on the session clock, and `trunc_time()` cuts UTC |
| `datetime` | shifted | same; it also refuses the offset suffix, which the same rendering drops |
| `timestamp_ntz` | **left on the UTC reading** (suffix still dropped) | a naive value has no clock to move; shifting it is what turned "loses nothing today" into "loses rows" |
| `date`, an unknown type, or a column whose type cannot be read | **the batch is refused** on a session that would move the text; on a UTC session nothing is moved, so nothing is refused | both readings cannot be right, and choosing one silently picks which rows a neighbour's `insert overwrite` erases |

Three details of that rule are the substance, not footnotes:

* **The column's physical type is the authority, not the YAML declaration.** Both mismatch
  directions were measured on the real path, on a Shanghai session. An upstream column physically
  `timestamp` declared `timestamp_ntz` in its property file: the physical answer shifts the window
  and all four rows survive in two partitions - trusting the declaration would have skipped the
  shift and reproduced the silent overwrite. The reverse, physically `timestamp_ntz` declared
  `timestamp`: the physical answer leaves the window on UTC and nothing is lost - trusting the
  declaration would have shifted it and erased a neighbour's partition. A guard that read the
  declaration would have been wrong in both directions, which is why it is only a fallback (below).
* **An ephemeral model in between is that fallback's real shape.** `ref()`ing an ephemeral model
  puts a CTE name where a relation would be, so there is nothing to describe: the declared type is
  used, and if nothing declares the column either the batch is refused on a moved session (the
  message says so) rather than guessed. Materialise the intermediate model, or declare its event
  column, and the window is decided from the column again.
* **When the warehouse cannot be asked, the declaration is used and said to be unverified.** A
  relation that does not exist yet (`dbt compile` against a target schema where the upstream has
  not been built) or a connection that cannot describe it yields no physical answer; the node's
  declared type is then used, with a debug line saying it was not verified against the column. If
  neither answers on a session that would move the text, the run **raises** rather than guessing,
  and the message names the relation, the boundary, the session timezone and the two ways to make
  the type verifiable.
* **Type names are normalised before the decision** - case-insensitive, separators dropped, so
  `TIMESTAMP_NTZ`, `timestamp-ntz` and `timestamp_ntz` land in the same branch. A spelling this
  version does not recognise is `unknown` (refused on a moved session), not silently naive.

Two costs, stated as costs. Models on a non-UTC session whose event column is `date`, an unrecognised
type, or unverifiable now **fail** where they previously ran: for `date` that run selected no rows in
any window and reported success (a separate limitation, still out of scope here - what changed is
that it is no longer silent), and for a naive column with no readable type the run used to pick the
shifted reading by default. And deciding the clock costs one metadata call per distinct relation per
invocation, cached rather than repeated per batch boundary.

On the adapter's default profile - no `timezone` in the profile, so the session runs on UTC - the two
readings are the same text: nothing is moved, no clock has to be consulted, and no type lookup
happens at all. That path is unchanged, and it is what the `TestMicrobatchDefaultUtcSession` cell
pins.

If the profile's `timezone` cannot be resolved on the host running dbt, the window is left as
dbt-core rendered it and a warning is logged: a guessed shift is worse than the known behaviour.

A `set odps.sql.timezone=...;` written inside a model does **not** move the clock the window is
stated on, so it cannot be used to work around a wrong `timezone`. The adapter turns leading `set`
statements into per-statement hints, and measured on a real project in both directions - profile on
UTC with a statement asking for `Asia/Shanghai`, and the reverse - the value pyodps submits from the
profile wins: the session parses timestamp strings with it, and the window is shifted with the same
value. The two therefore cannot drift apart. What a statement-level override loses is its own effect
on timestamp parsing, which is a separate question from this strategy. (A statement-level value does
apply when nothing is pinned globally; this adapter always pins the profile's value, including
`Etc/GMT` for an unset `timezone`.)

### The one thing a wall clock cannot state: the fold hour

Stating a boundary as the session's wall clock is only unambiguous while that clock maps one-to-one
onto instants. Where a zone *repeats* an hour, two UTC boundaries read the same, and a half-open
window cannot be written there. Measured on a real project, session `America/New_York`, hour
batches on the fold day:

| UTC boundary batch | rendered window | what the server does with it |
| --- | --- | --- |
| `[05:00Z, 06:00Z)` | `>= '2025-11-02 01:00:00' and < '2025-11-02 01:00:00'` | a degenerate window: selects nothing, and an `insert overwrite` of no rows writes no partition, so nothing is erased |
| `[06:00Z, 07:00Z)` | `>= '2025-11-02 01:00:00' and < '2025-11-02 02:00:00'` | `'01:00:00'` resolves to the *first* 01:00 (measured: equal to 05:00Z, not to 06:00Z), so this batch covers both hours |
| `[07:00Z, 08:00Z)` | `>= '2025-11-02 02:00:00' and < '2025-11-02 03:00:00'` | normal |

Which batch ends up covering the repeated hour depends on which instant the server picks for that
text, and that is not the same everywhere. Measured on the same project with a session of
`Australia/Sydney` (local `02:00` repeated on 2025-04-06, the two instants being 15:00Z and 16:00Z),
the text resolves to the **later** instant: the earlier batch's window is empty *and* the hour
`[15:00Z, 16:00Z)` is written by neither batch. In `America/New_York` the text resolves to the
**earlier** instant, so the following batch covers both hours and nothing is lost.

So at a fold, `batch_size: hour` in a DST-observing session either double-covers or drops that one
hour, depending on the zone - and "depending on the zone" is not something a batch should leave to
chance. This is why the batch is **refused** rather than warned about: a warning still submits an
`insert overwrite` that selects no rows and reports success, which is the same shape as the bug this
page documents. The run raises, naming the wall clock both ends collapsed to and the ways out -
leave `timezone` unset (UTC), pin a fixed-offset zone, or use a coarser `batch_size`. Day-granularity
batches never hit it in the zones measured here, where a UTC midnight boundary is 20:00/19:00 in New
York and midnight in Sydney, never the folded hour. A naive (`timestamp_ntz`) field is not refused on
a fold day at all: its two boundaries stay on the UTC reading, so they remain two distinct texts -
which is the case a blanket "a non-UTC session is unsafe" rule would have failed.

A collapsed text has one other cause, and it is not a fold: a window whose two ends are the *same*
instant. `--sample` given as a dict is not ordering-checked by dbt-core, so a zero-width sample window
does reach the renderer, and Core's own batch bounds collapse onto one instant
(`truncate_timestamp(start)` == `ceiling_timestamp(end)`, pinned against Core in the unit suite). That
is refused too, with its own message - "this is not a daylight-saving fold" - because the advice
differs: widen the sample window, or run the batch with `--event-time-start`/`--event-time-end`, which
*are* ordering-checked at the CLI. Both refusal branches assert the other branch's wording is absent,
so swapping them fails loudly rather than quietly mis-advising someone.

Refusing is only useful if the reason is seen. Measured on a real project: when a microbatch batch
fails this way, dbt reports the model with the message `ERROR` and the exception text does not reach
the console at all - it was visible only by intercepting the raise. So each refusal is logged at error
level too (the channel this adapter's other messages use, which the console does show), and a unit
case asserts the logged text is the raised text for all three refusals - and that an ordinary aligned
window logs nothing at that level. A declared type used as the fallback is said the same way, at
warning level: it is a decision that has not been checked against the column.


## Not verified here

- Python (MaxFrame) microbatch models, which apply the window to the returned DataFrame instead of
  to the compiled SQL;
- `batch_size` of `month` or `year`, and how their units interact with `trunc_time()`;
- `partitions`, `lookback`, `on_schema_change`, `grants` and model contracts on microbatch models;
- `datetime` event columns on a session whose zone this adapter cannot resolve (nothing pinned, or a
  name that is not in any tz database): the window stays as dbt-core rendered it, offset suffix
  included, so such a column still refuses to compile there;
- `dbt retry` for a model whose *first* batch fails.
- Hour-granularity batches in DST-observing sessions, beyond the two zones above: 144 hourly
  boundaries over six (zone, day) pairs were read back through the server and round-tripped except
  the two fold instants described. Half-hour and 45-minute offsets, zones not measured here, and a
  fold landing exactly on a day-granularity boundary have not been checked.
- `microbatch` models whose event time is a `date` column under a non-UTC `timezone`: the row-sifting
  comparison itself is unmeasured there, since no row falls inside any window on any session. Such a
  model now fails on a session that would move its window instead of reporting success, which is the
  honest half; fixing `date` as an event column is a separate change (this pull request does not
  attempt it). Also unmeasured: `timestamp_ntz` with `batch_size: hour`, and Python (MaxFrame) models
  on a non-UTC session - the MaxFrame path filters the returned DataFrame in Python rather than in
  SQL, so it does not go through this rendering at all;
- the fallback branch that uses a YAML-declared type when the warehouse answer cannot be read is
  unit-tested, and its *refusal* half is measured on a live project (a microbatch model run without
  its upstream table present, on a Shanghai session: the describe retries three times, the batch
  errors at ~30s, and the reason is the unverifiable-clock message rather than a guess). What is
  still not measured live is the fallback that *uses* the declaration, which would need a fixture
  whose upstream relation is missing while its YAML declares the column;
- `source()`-based event-time upstreams: measured as far as the plumbing goes on a real project -
  the filter arrives through the same `create_from(..., target_source, ...)` call as `ref()`, the
  declared type is readable there, and the relation in another schema can be described, with the
  window rendering on the session clock - but the row-and-partition behaviour of a *source*-based
  upstream across the type branches has not been run as a cell;
- the cost of one metadata call per distinct relation per invocation on projects with many upstream
  relations - cached, but not benchmarked. The cache lives as long as the dbt process: a model that
  re-creates the *same* relation with a *different* event-column type inside one invocation would be
  judged by the first answer. Nothing measured here covers that; dropping the cache (one describe per
  boundary instead of per relation) is the alternative, at the cost of more metadata calls.
