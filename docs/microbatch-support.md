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
| `test_microbatch_date_partition.py` | a `date` event column is refused, and the replacement that works |
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
against all three, so every batch selected nothing while the run still reported success. That was a
different defect from this one - a silent empty target rather than a plan the server refuses - and it
is now refused where the batch predicate is built: the adapter asks the warehouse for the physical
type of the relation being read (see the `date` section below), because no rendering fix can tell the
two types apart.

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
| the column being filtered is physically `date`, SQL model | rejected before any write, on UTC and shifted sessions alike: the batch names the `date` column and states why nothing was written | this adapter |
| `event_time` declared as `partition_by.data_type: date` while the physical type is an instant | **logged and still runs**, `--warn-error` included; a declaration is an early diagnostic, never the authority | this adapter |
| the type could not be read (`{}` from the describe) | refused when the session would move the window; left as dbt-core's rendering on a session that would not - that path is *unverified*, not DATE-safe | this adapter |
| the same declaration on a MaxFrame Python model | **accepted**; the window is bound in Python instead (see below) | nobody |
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

## A `date` event column cannot match a batch window

`partition_by` with `data_type: date` is refused for `microbatch` **SQL models**, and it is worth
knowing why the refusal is the only safe shape there. dbt-core compares each batch as

```sql
event_time >= '2025-05-01 00:00:00+00:00' and event_time < '2025-05-02 00:00:00+00:00'
```

and since #40 this adapter submits the suffix-free form whenever the session clock reads UTC, i.e.
`>= '2025-05-01 00:00:00'`. Neither of those two texts is converted against a `DATE` column, and
neither complains - the comparison evaluates to `NULL`. No row satisfies a `NULL` predicate, so every batch selects nothing;
an empty `insert overwrite` changes nothing; and the run reports the model as a success with the
target left empty. Measured on a real project with four rows spanning two days: `PASS=2`, zero rows
in the target, and the same `NULL` in `Etc/GMT`, `Asia/Shanghai` and `Etc/GMT+8` sessions alike. This
is therefore not the session-clock problem below: the comparison is null-valued whatever the clock.

Writing the boundary as a bare `'YYYY-MM-DD'` *does* work for a `DATE` column - measured, it selects
the day's rows - but it cannot be rendered blindly: the point inside dbt-core that writes the boundary
(`Relation.render_event_time_filtered`) is handed a field name, not a column type, which is exactly
why the decision belongs one level up, in the adapter path that can describe the relation,
and the date-only text is not safe for the types that work today:

Each cell is the comparison as the server evaluates it, in a `Etc/GMT` and an `Asia/Shanghai`
session alike - the third column is the shape a DATE needs, the fourth is what a rendering point that
cannot see the column type would have to emit to cover both:

| `event_time` column | `'2025-05-01 00:00:00'` (what this adapter submits on a UTC session since #40) | `'2025-05-01 00:00:00+00:00'` (what dbt-core renders) | `'2025-05-01'` | naive text OR date-only |
| --- | --- | --- | --- | --- |
| `date` | `NULL` - batch selects nothing | `NULL` - batch selects nothing | selects its day | selects its day |
| `datetime` | selects its day | **ODPS-0130071** | **ODPS-0130071** | **ODPS-0130071** |
| `timestamp` | selects its day | selects its day | `NULL` - batch selects nothing | selects its day |
| `timestamp_ntz` | selects its day | selects its day | `NULL` - batch selects nothing | selects its day |

So no type-blind rendering exists: the date-only text either silently empties a model (`timestamp`,
`timestamp_ntz`) or stops it compiling (`datetime`), and OR-ing the two texts inherits the DATETIME
failure. The remaining option is to ask the warehouse what the column being filtered actually is and
refuse when it answers `DATE`. That question is answerable in the adapter's own filter path, which
holds the relation being read (#38), so the physical type - never a YAML declaration - is the
authority here, on a UTC session as well as a shifted one and before any unavailable-session-clock
fallback. The config-level check in `mc_validate_microbatch_config` stays as an early diagnostic that
names the declaration the model made; a declaration does not decide the outcome, and the Python
(MaxFrame) path, which filters the returned DataFrame instead of comparing this text, is not refused
by it.

The `datetime` half of that reading is closed: #40 (master `9fbaa8e`) renders the boundary without
the offset suffix on a UTC session, so a `datetime` event column runs on the default profile while
`timestamp` and `timestamp_ntz` keep selecting the rows they selected before. What #40 did not do is
make a `DATE` column comparable - it refuses both texts, which is the difference between the second
and third columns of the table above. A `datetime` column on a profile that pins a non-UTC `timezone`
is still open and belongs to the session-timezone section below (pull request #38): there the window
has to be moved onto that clock as well, and stripping only the suffix would silently change which
rows are selected.

**What to write instead.** The window is attached to the *upstream* relation - dbt-core wraps every
ref or source that declares `event_time` - so it is that column which has to stop being a DATE. Cast
it where the upstream model is built, declare `event_time` on the resulting instant column, and
partition the microbatch model on it with `data_type: timestamp`. Measured end to end
(`test_microbatch_date_partition.py`): two adjacent day windows write their own rows into their own
partitions, and replaying the first window does not erase the second.

Two shapes that look like fixes and are measured not to be:

* a `cast()` inside the microbatch model's own SELECT - the batch predicate was already applied to
  the upstream DATE column, so it cannot rescue the window. Before this change that shape wrote an
  empty target behind a successful run; now it is refused with the physical-type error (measured,
  `TestMicrobatchCastInsideModelIsRefused`);
* `cast(event_date as datetime)`, and making the DATE column itself the partition key. This
  warehouse refuses the DATE->DATETIME cast, and partition keys must be `BIGINT` or `STRING`.

The refusal is not limited to the model that declares its event field as a `date` partition column -
that declaration is only the informational note. The authority asks the warehouse for the type of the
relation the window is attached to, so an *upstream* relation whose `event_time` column is physically
a DATE is caught as well: behind a `ref()` (the staged-instant and cast-inside-model cells) and behind
a `source()` on a shifted session with different event and partition names, where no declaration-level
check could have fired at all. What is left unmeasured is stated in *Not verified here*.

The workaround is measured on the adapter's default session (`timezone` unset, so
`odps.sql.timezone=Etc/GMT`). Under a profile that sets `timezone`, the DATE->TIMESTAMP reading is
taken on the session clock (`2025-05-01` reads as `2025-04-30 16:00:00` UTC in an `Asia/Shanghai`
session) and lands on the previous partition day: that is the session-timezone problem below, not a
new one.

### Measured ordering: which refusal the console actually shows

Both checks exist, so it is worth stating which one fires first, measured on a real project:

* declaration **and** physical type both `date` - the adapter asks while it builds the batch
  predicate, before the materialization macro reaches `mc_validate_microbatch_config`, so the run
  shows the refusal line and an error, and no warning line at all (`PASS=1 WARN=0 ERROR=1`);
* declaration `date` over an upstream that is physically an instant - nothing is refused: the model
  compiles, the diagnostic is printed, and both day windows write their rows (`PASS=3 ERROR=0`);
* a target that already holds rows, then the same model recompiled over the DATE upstream - the
  refused batch leaves the rows and the partition set exactly as they were, which is asserted with a
  `--vars` switch in `test_microbatch_date_partition.py` rather than a second fixture.

### Why the refusal stops at SQL models

The comparison above is a property of the boundary *text* submitted to SQL. A MaxFrame Python
model does not take that path: `maxframe_write_table` binds the window in Python, converting the
event series with `astype("datetime64[ns]")` and filtering it against `pandas.Timestamp` bounds
(`dbt/include/maxcompute/macros/relations/table/create.sql:102-116`). A text-versus-`DATE`
comparison never happens there, so the refusal would stop a model for a reason that does not
describe it, and it is not applied to `language == 'python'`. Whether that path selects the right
rows for a `DATE` event column is **not measured** here - what is measured is only that it does
not use the comparison this error describes. Any language other than an explicit `python` keeps
the check, so an unexpected one fails closed instead of running empty.

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
  declaration would have been wrong in both directions.
* **When the warehouse cannot be asked, nothing is inferred from YAML.** A relation that does not
  exist yet (`dbt compile` against a target schema where the upstream has not been built), a
  connection that cannot describe it, or an ephemeral model in between - `ref()`ing one puts a CTE
  name where a relation would be - all leave the type unverified, and an unverified declaration
  picks the clock just as unverified as guessing did. On a session that would move the text the batch
  is **refused**: declared `timestamp` would shift a naive column into its neighbour's partition,
  declared `timestamp_ntz` would leave an instant-bearing column unshifted, and the describe that
  failed is exactly the moment those two claims stop being evidence. The refusal names the
  declaration it declined, so the user sees what was not trusted; materialise the relation being read
  (or run on the default UTC session) and the window is decided from the column again. A
  declaration-only mode would have to be *asked for* - an explicit trust contract, off by default -
  which is a different feature from a fallback that switches on after a failure nobody noticed.
* **Nothing is remembered between window decisions.** The type of a relation is asked of the
  warehouse each time a batch's clock is decided, and a failed lookup is not remembered either. A
  memo keyed by project/schema/table cannot see the two things that do change the answer: another
  invocation in the same process (a wrapper script, a retried batch after the upstream was recreated
  with a different column type), and a describe that failed once and would otherwise keep refusing
  after the relation appeared.
* **Type names are normalised before the decision** - case-insensitive, separators dropped, so
  `TIMESTAMP_NTZ`, `timestamp-ntz` and `timestamp_ntz` land in the same branch. A spelling this
  version does not recognise is `unknown` (refused on a moved session), not silently naive.

Two costs, stated as costs. Models on a non-UTC session whose event column is `date`, an unrecognised
type, or unverifiable now **fail** where they previously ran: for `date` that run selected no rows in
any window and reported success (a separate limitation, still out of scope here - what changed is
that it is no longer silent), and for a naive column with no readable type the run used to pick the
shifted reading by default. And deciding the clock costs a metadata call per batch per relation being
filtered - a model read by ten daily batches is described ten times - because an answer that could
be stale would decide the wrong clock, which is the failure this file is about. The ten-cell live run
in `tests/functional/adapter/incremental/test_microbatch_partition_timezone.py` is the only cost
measurement here; it is not a benchmark of projects with many upstream relations.

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
window logs nothing at that level. A declaration the resolver declined to trust is named in the
refusal, and the config-level note about it is logged at info level rather than through
`exceptions.warn` - dbt escalates the latter to an error under `--warn-error`, which would give a YAML
property the veto this adapter deliberately takes away from it.


## Not verified here

- Python (MaxFrame) microbatch models, which apply the window to the returned DataFrame instead of
  to the compiled SQL;
- `batch_size` of `month` or `year`, and how their units interact with `trunc_time()`;
- `partitions`, `lookback`, `on_schema_change`, `grants` and model contracts on microbatch models;
- `dbt retry` for a model whose *first* batch fails;
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
- there is no declaration-only mode any more, so nothing is unmeasured about one: the unverifiable
  branch refuses with or without a declaration, unit-pinned for both declared spellings and for an
  ephemeral relation (a CTE name, which cannot be described at all). What is *not* measured on a live
  project is the ephemeral shape specifically - the live probe used a missing upstream relation
  instead: describe retried three times, the batch errored at ~30s, and the reason was the
  unverifiable-clock message rather than a guess;
- `source()`-based event-time upstreams are committed cells now: a Shanghai session, a physically
  `timestamp` table declared `timestamp_ntz` in its source properties, the same four rows and the same
  per-step partition assertions as the `ref()` cells; and a physically `date` table behind a
  `source()` on that shifted session with the event field named apart from the partition field
  (`TestMicrobatchPhysicalDateSourceOnShiftedSession`) - on that shape no declaration-level check can
  fire, so the refusal is the warehouse answer alone. Still not run as a cell: `timestamp_ntz` and
  `datetime` behind a `source()`, the other sessions behind a `source()`, and a source whose relation
  is missing - which the rule refuses;
- the metadata call per batch is not benchmarked on projects with many upstream relations; what is
  pinned is that no answer is reused across decisions (unit cases: repeated renders ask again, a
  recreated relation with a new type follows the new answer, a failed lookup does not poison the next
  batch).
