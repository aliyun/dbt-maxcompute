"""The clocks a microbatch model depends on, and the profile field that used to move one of them.

A microbatch batch is written by overwriting the partitions its rows fall into, and this adapter
creates those partitions with ``auto partitioned by (trunc_time(<field>, '<granularity>'))``. So
the strategy is only correct while two things read the same clock:

* the batch window. dbt-core renders it as a timestamp string carrying an offset,
  ``event_time >= '2025-05-01 00:00:00+00:00'``, and MaxCompute evaluates that comparison in the
  **session** timezone (measured: `+00:00`, `+08:00` and `-05:00` on the same wall clock all
  produce the same rows, so the offset in the text is not what picks the clock);
* `trunc_time()`, which assigns each row to a partition, and cuts on **UTC**.

What sets the session timezone is the profile: `MaxComputeCredentials._get_odps` writes
pyodps' `options.local_timezone`, and pyodps turns that into the `odps.sql.timezone` session
setting (`Etc/GMT` when it is falsey). With no `timezone` in the profile - the adapter's default -
both clocks are UTC and nothing has to be corrected; `TestMicrobatchDefaultUtcSession` pins that
the rendering on that path is untouched.

`TestMicrobatchProfileTimezone` sets the documented `timezone` field instead. Before the window was
stated on the session clock this combination lost rows in silence: the window was read as local
wall clock while `trunc_time()` kept cutting UTC days, so a "day" window straddled two partitions
and the next window's `insert overwrite` erased rows the previous window had written - with the run
still reporting success. That is the behaviour pinned here, in both directions:

* the assertions are the ones a correct adapter has to satisfy, and they are shared by the two
  cells, so a change that fixes one clock pairing and breaks the other cannot pass silently;
* the fixture rows are built with `from_unixtime()`, whose instant does **not** depend on the
  session. Writing them as `TIMESTAMP'...'` text instead would move the data by the same amount the
  window moves, and the two shifts would cancel each other out: the cell would pass whether or not
  the rendering was aligned.
"""

import os

import pytest
import yaml
from dbt.tests.util import run_dbt

# Two UTC days, two rows in each, both far from the day boundary so an eight-hour shift in the
# window cannot move a row between days. `from_unixtime()` yields the same instant whatever the
# session timezone is, which is what makes the cells below comparable: rows written as
# `TIMESTAMP'...'` text would move with the session exactly as the window does, and the two shifts
# would cancel - the cell would then pass whether or not the rendering was aligned.
_EPOCHS = (
    1746061200,
    1746140400,
    1746147600,
    1746226800,
)  # 05-01 01:00/23:00, 05-02 01:00/23:00 UTC


def _input_sql(event_type, mode="epoch"):
    """The four fixture rows, either as instants (`from_unixtime`) or as literal wall-clock text.

    `epoch` is what the instant-bearing cells use: `from_unixtime()` does not depend on the session,
    so a window moved by the session offset moves the rows by the same amount and the two shifts
    would cancel out - the cell would pass whether or not the rendering was aligned.
    `literal` is what a naive column needs: it has no clock to interpret an instant through, so the
    text *is* the value - and the times straddle the shift, so a wrongly moved window misplaces them
    instead of collecting them in the batch they came from.
    """
    if mode == "literal":
        rows = "\nunion all\n".join(
            f"select {i} as id, {event_type}'{text}' as event_time"
            for i, text in enumerate(_LITERALS, start=1)
        )
    else:
        rows = "\nunion all\n".join(
            f"select {i} as id, cast(from_unixtime({epoch}) as {event_type}) as event_time"
            for i, epoch in enumerate(_EPOCHS, start=1)
        )
    return "{{ config(materialized='table', event_time='event_time') }}\n" + rows + "\n"


def _model_sql(event_type, upstream="ref"):
    source = (
        "{{ ref('input_model') }}" if upstream == "ref" else "{{ source('etl', 'src_events') }}"
    )
    return (
        "{{ config(\n"
        "    materialized='incremental',\n"
        "    incremental_strategy='microbatch',\n"
        "    unique_key='id',\n"
        "    event_time='event_time',\n"
        "    batch_size='day',\n"
        "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
        f"    partition_by={{'field': 'event_time', 'data_type': '{event_type}', "
        "'granularity': 'day'}\n"
        ") }}\n"
        f"select id, event_time from {source}\n"
    )


# 01:00 and 23:00 on each of the two batch days, as written. These are the naive cells' fixtures, and
# the times are picked to *straddle* the shift they have to detect, not to hide from it: a naive
# column is compared as written, so a window wrongly moved by +08:00 puts 01:00 before the batch and
# sweeps 05-02's 01:00 into the previous batch - the per-step assertions below stop holding.
# 09:00/15:00 were here first, and both of those sit inside `[00:00, 00:00)` and the wrong
# `[08:00, 08:00)` alike, so those cells stayed green with the original row loss (red/green pair
# measured in this file's sibling evidence: forcing the shift fails them, the shipped renderer passes).
_LITERALS = (
    "2025-05-01 01:00:00",
    "2025-05-01 23:00:00",
    "2025-05-02 01:00:00",
    "2025-05-02 23:00:00",
)

# Where every row has to end up, checked after each step and not only at the end: a window that
# selects the right *number* of rows but the wrong ones - an off-by-one batch, a partition cut on
# another clock - passes a count assertion and fails this one.
EXPECTED_ASSIGNMENT = {1: "2025-05-01", 2: "2025-05-01", 3: "2025-05-02", 4: "2025-05-02"}

DAY1 = ("2025-05-01", "2025-05-02")
DAY2 = ("2025-05-02", "2025-05-03")
DAY1_ROWS = 2
DAY2_ROWS = 2


def _relation(project, identifier="microbatch_model"):
    return project.adapter.Relation.create(
        database=project.database, schema=project.test_schema, identifier=identifier
    )


def _ids(project):
    rows = project.run_sql(f"select id from {_relation(project)} order by id", fetch="all") or []
    return sorted(int(row[0]) for row in rows)


def _partition_of(project):
    """`{id: partition day}` as the server itself groups it, and the partition list."""
    rows = project.run_sql(
        f"select id, trunc_time(event_time, 'day') as part_day from {_relation(project)} "
        "order by id",
        fetch="all",
    )
    per_row = {int(row[0]): str(row[1]) for row in rows or []}
    table = project.adapter.get_odps_client().get_table(
        "microbatch_model", schema=project.test_schema
    )
    return per_row, sorted(p.name for p in table.partitions)


def _state_of(project, grain="day"):
    """`{id: partition label}` plus the table's partition list, at the grain the model is cut on."""
    rows = project.run_sql(
        f"select id, trunc_time(event_time, '{grain}') as part from {_relation(project)} order by id",
        fetch="all",
    )
    per_row = {int(row[0]): str(row[1])[:19] for row in rows or []}
    table = project.adapter.get_odps_client().get_table(
        "microbatch_model", schema=project.test_schema
    )
    return per_row, sorted(p.name for p in table.partitions)


def _window(start, end, select=None):
    args = ["run", "--event-time-start", start, "--event-time-end", end]
    if select is not None:
        args += ["--select", select]
    return run_dbt(args)


class _DayWindowScenario:
    """Subclasses choose the session timezone and the event column; expectations are shared.

    `declared_column_type` is what the upstream model's YAML property *says* its event column is:
    left None for the honest cases, and set to something that disagrees with `event_column_type` for
    the mismatch cases. The warehouse's own answer governs the window, so a property that contradicts
    the column cannot move a batch back onto the wrong clock - and the two mismatch directions land
    in different clocks, which is why both are here.
    """

    profile_timezone = None
    event_column_type = "timestamp"
    declared_column_type = None
    input_mode = "epoch"
    # `ref` (the model reads a model dbt built in this run) or `source` (it reads a relation dbt did
    # not build). dbt-core hands those two to the adapter at different call sites, so both need the
    # same answer; `TestMicrobatchSourceUpstreamOnShiftedSession` is the `source` variant.
    upstream = "ref"
    # `None` lets the first step run the whole project, which is how the upstream model gets built.
    # A `source` upstream is not a dependency edge, so that cell builds the table itself and pins the
    # batch steps to the model - otherwise the run order between the two is dbt's choice.
    batch_select = None

    @pytest.fixture(scope="class")
    def models(self):
        files = {
            "input_model.sql": _input_sql(self.event_column_type, mode=self.input_mode),
            "microbatch_model.sql": _model_sql(self.event_column_type, self.upstream),
        }
        if self.declared_column_type and self.upstream == "ref":
            files["schema.yml"] = yaml.safe_dump(
                {
                    "version": 2,
                    "models": [
                        {
                            "name": "input_model",
                            "columns": [
                                {"name": "event_time", "data_type": self.declared_column_type}
                            ],
                        }
                    ],
                }
            )
        if self.upstream == "source":
            table = {"name": "src_events", "identifier": "input_model"}
            # without an explicit schema the source resolves under the source's own name and the
            # table is not found (ODPS-0130131); target.schema is the schema `input_model` was built in
            table["schema"] = "{{ target.schema }}"
            table["config"] = {"event_time": "event_time"}
            if self.declared_column_type:
                table["columns"] = [{"name": "event_time", "data_type": self.declared_column_type}]
            files["sources.yml"] = yaml.safe_dump(
                {"version": 2, "sources": [{"name": "etl", "tables": [table]}]}
            )
        return files

    @pytest.fixture(scope="class")
    def dbt_profile_target(self):
        """The shared profile, optionally with the documented `timezone` field set.

        Read the same way `tests/conftest.py` reads it, and restore pyodps' global
        `local_timezone` afterwards: the adapter assigns it per connection, so leaving
        `Asia/Shanghai` behind would silently change the session clock of later test classes.
        """
        from odps import options

        filepath = os.environ.get("DBT_PROFILE_PATH")
        if not filepath:
            pytest.skip("needs a real MaxCompute profile via DBT_PROFILE_PATH")
        saved = options.local_timezone
        with open(filepath) as handle:
            target = yaml.safe_load(handle)
        if self.profile_timezone is None:
            # "no timezone in the profile" has to mean that literally: the shared profile, and CI's
            # DBT_PROFILE_YAML, may carry one - leaving it in place would run this class on a session
            # its own docstring says it does not use.
            target.pop("timezone", None)
        else:
            target["timezone"] = self.profile_timezone
        try:
            yield target
        finally:
            options.local_timezone = saved

    @staticmethod
    def _assignment(per_row):
        return {row_id: day[:10] for row_id, day in per_row.items()}

    def test_adjacent_day_windows_never_lose_or_duplicate_rows(self, project):
        _window(*DAY1, select=self.batch_select)
        assert _ids(project) == [1, 2], "the first window writes its own rows"
        per_row, partitions = _partition_of(project)
        assert len(partitions) == 1, f"one day window must touch one partition: {partitions}"
        assert self._assignment(per_row) == {1: "2025-05-01", 2: "2025-05-01"}, per_row

        _window(*DAY2, select="microbatch_model")
        assert _ids(project) == [
            1,
            2,
            3,
            4,
        ], "an adjacent window must add its rows without erasing the previous window's"
        per_row, partitions = _partition_of(project)
        assert len(partitions) == 2, f"two day windows, two partitions: {partitions}"
        assert self._assignment(per_row) == EXPECTED_ASSIGNMENT, per_row

        _window(*DAY1, select="microbatch_model")
        assert _ids(project) == [
            1,
            2,
            3,
            4,
        ], "replaying window 1 across the window-2 partition must not lose window 2's rows"
        per_row, partitions = _partition_of(project)
        assert len(partitions) == 2, f"the replay must not open a third partition: {partitions}"
        counts = {}
        for row_id, part_day in per_row.items():
            counts[part_day[:10]] = counts.get(part_day[:10], 0) + 1
        assert (
            sum(counts.values()) == DAY1_ROWS + DAY2_ROWS
        ), f"each batch day keeps its own rows: {per_row}"
        assert len(counts) == 2, f"one partition per batch day, no window spans two: {per_row}"

        assert self._assignment(per_row) == EXPECTED_ASSIGNMENT, per_row


class TestMicrobatchDefaultUtcSession(_DayWindowScenario):
    """No `timezone` in the profile: pyodps submits `odps.sql.timezone=Etc/GMT`.

    The adapter's default, and the path whose rendering must not change: the window and the
    partition key already read the same clock.
    """

    profile_timezone = None


class TestMicrobatchProfileTimezone(_DayWindowScenario):
    """`timezone: Asia/Shanghai` in the profile - the documented way to run on local time.

    This is the combination that used to lose rows between adjacent microbatch windows. The window
    is now stated in the session's own reading of the same UTC instant, so it lands on the partition
    `trunc_time()` cuts for that batch, and the two cells above have the same expectations.
    """

    profile_timezone = "Asia/Shanghai"


class TestMicrobatchNegativeOffsetSession(_DayWindowScenario):
    """`timezone: America/New_York` - a session *behind* UTC, and one that observes DST.

    Added because a shift with the wrong sign still keeps adjacent windows the same width: the
    +08:00 cell alone cannot tell "aligned" from "aligned and eight hours off". Here the boundary
    moves to the previous local day (2025-05-01 00:00 UTC is 2025-04-30 20:00 in EDT), so a wrong
    sign moves a row into the wrong window and the assertions below stop holding.
    """

    profile_timezone = "America/New_York"


class TestMicrobatchNaiveColumnOnShiftedSession(_DayWindowScenario):
    """`timestamp_ntz` on a session that pinned `Asia/Shanghai` - the case the shift would break.

    A naive column is compared as written; it has no session clock to state a window on. Moving its
    boundaries by +08:00 is exactly what turned "does not lose rows today" into "loses rows", so the
    window stays on the UTC reading and every shared expectation holds.
    """

    profile_timezone = "Asia/Shanghai"
    event_column_type = "timestamp_ntz"
    input_mode = "literal"


class TestMicrobatchNtzPhysicalDeclaredAsTimestamp(_DayWindowScenario):
    """Physical `timestamp_ntz` declared `timestamp` in YAML: the warehouse answer governs.

    Trusting the declaration here would shift the window and erase a neighbour's partition.
    """

    profile_timezone = "Asia/Shanghai"
    event_column_type = "timestamp_ntz"
    declared_column_type = "timestamp"
    input_mode = "literal"


class TestMicrobatchTimestampPhysicalDeclaredAsNtz(_DayWindowScenario):
    """Physical `timestamp` declared `timestamp_ntz`: still shifted, because the column has a clock.

    This is the shape a declaration-based guard would have left broken: the property says naive, the
    column is instant-bearing, and skipping the shift reproduces the silent overwrite this branch
    exists to fix.
    """

    profile_timezone = "Asia/Shanghai"
    event_column_type = "timestamp"
    declared_column_type = "timestamp_ntz"


class TestMicrobatchFoldHourIsRefusedBeforeWriting:
    """A folded hour on an instant-bearing column is refused before anything is written.

    2025-11-02 in `America/New_York`: the UTC boundaries 05:00 and 06:00 are both 01:00 on that wall
    clock. Rendering the window anyway selects no rows and the batch reports success, and whether the
    folded hour is covered by a neighbour depends on the zone (measured: New York resolves the
    repeated text to the earlier instant, Sydney to the later one, so in Sydney no batch covers it).
    `tests/unit/test_microbatch_session_clock.py` pins the message and both zones; this pins the part
    only a run can show - that a real batch is refused, that it is refused *for that reason*, and that
    the partition the model had already written is untouched. `expect_pass=False` alone would also be
    satisfied by an unrelated metadata or SQL failure, so the logged reason and the unchanged target
    are asserted too, and the batch that runs before it gives the assertion something to preserve.
    """

    profile_timezone = "America/New_York"

    @pytest.fixture(scope="class")
    def models(self):
        input_sql = (
            "{{ config(materialized='table', event_time='event_time') }}\n"
            "select 1 as id, TIMESTAMP'2025-11-02 09:30:00' as event_time\n"
            "union all\n"
            "select 2 as id, TIMESTAMP'2025-11-02 10:30:00' as event_time\n"
        )
        model_sql = (
            "{{ config(\n"
            "    materialized='incremental',\n"
            "    incremental_strategy='microbatch',\n"
            "    unique_key='id',\n"
            "    event_time='event_time',\n"
            "    batch_size='hour',\n"
            "    begin=modules.datetime.datetime(2025, 11, 2, 0, 0, 0),\n"
            "    partition_by={'field': 'event_time', 'data_type': 'timestamp', "
            "'granularity': 'hour'}\n"
            ") }}\n"
            "select id, event_time from {{ ref('input_model') }}\n"
        )
        return {"input_model.sql": input_sql, "microbatch_model.sql": model_sql}

    @pytest.fixture(scope="class")
    def dbt_profile_target(self):
        from odps import options

        filepath = os.environ.get("DBT_PROFILE_PATH")
        if not filepath:
            pytest.skip("needs a real MaxCompute profile via DBT_PROFILE_PATH")
        saved = options.local_timezone
        with open(filepath) as handle:
            target = yaml.safe_load(handle)
        target["timezone"] = self.profile_timezone
        try:
            yield target
        finally:
            options.local_timezone = saved

    def test_the_fold_batch_is_refused_says_why_and_writes_nothing(self, project):
        run_dbt(["run", "--select", "input_model"])
        # 2025-11-02 13:00Z-14:00Z is 09:00-10:00 on that wall clock, an ordinary hour: it writes a
        # partition that the refused batch must not disturb.
        _window("2025-11-02 13:00:00", "2025-11-02 14:00:00", select="microbatch_model")
        rows_before, partitions_before = _state_of(project, grain="hour")
        assert rows_before == {1: "2025-11-02 09:00:00"}, rows_before

        logged = []
        # `expect_pass=False` is still the core assertion: if this degrades back to "warn and write
        # nothing", the run passes and this cell fails.
        run_dbt(
            [
                "run",
                "--select",
                "microbatch_model",
                "--event-time-start",
                "2025-11-02 05:00:00",
                "--event-time-end",
                "2025-11-02 06:00:00",
            ],
            expect_pass=False,
            callbacks=[lambda event: logged.append(str(getattr(event.info, "msg", "")))],
        )
        text = "\n".join(logged)
        assert "daylight-saving fold" in text, text[-2000:]
        assert "2025-11-02 01:00:00" in text, text[-2000:]
        # two distinct instants that read the same: a fold, and not the other collapsed-window case
        assert "window is empty" not in text
        rows_after, partitions_after = _state_of(project, grain="hour")
        assert rows_after == rows_before, f"a refused batch writes nothing: {rows_after}"
        assert (
            partitions_after == partitions_before
        ), f"a refused batch opens no partition: {partitions_after}"


class TestMicrobatchSourceUpstreamOnShiftedSession(_DayWindowScenario):
    """The event rows come from a `source()`, not a `ref()`, on a session that has to be shifted.

    dbt-core builds the window at two call sites: `ref()` hands over the target model built in this
    run, a `source()` hands over a relation dbt never created. The type that decides the clock is the
    physical column of the relation being read in both cases - which the shipped rule can only answer
    if it consults the warehouse for the source too. The source here *declares* `timestamp_ntz` over
    a physically `timestamp` column: the declaration is what a YAML-driven guard would have trusted,
    and the answer that keeps the rows is the warehouse's.

    Same four rows, same session, same shared expectations as the `ref()` cells.
    """

    profile_timezone = "Asia/Shanghai"
    event_column_type = "timestamp"
    declared_column_type = "timestamp_ntz"
    upstream = "source"
    batch_select = "microbatch_model"

    @pytest.fixture(scope="class", autouse=True)
    def _build_the_source_table(self, project):
        """A source is not a dependency edge: create the relation before any batch runs.

        Otherwise dbt may schedule the microbatch model first, the source's table would not exist to
        be described, and the refusal would be correct but for the wrong reason.
        """
        return run_dbt(["run", "--select", "input_model"])


class TestMicrobatchEphemeralUpstreamIsRefusedOnShiftedSession:
    """An ephemeral model in between: the review asked for that shape compiled, not mocked.

    `ref()`ing an ephemeral model inlines it as a CTE, so the relation the window is rendered onto is
    `__dbt__cte__...` - there is nothing in the warehouse to describe, and the unit cases that pin the
    lookup cannot show that the shape reaches the renderer at all. On a session that would move the
    text the batch is refused and the reason is the unverifiable-clock message; the same model on the
    default UTC session needs no answer from the warehouse, which is the unit case beside this one.
    """

    profile_timezone = "Asia/Shanghai"

    @pytest.fixture(scope="class")
    def models(self):
        # `event_time` on the upstream is what makes Core build a window at all
        # (`resolve_event_time_filter`); ephemeral is not a special case there.
        upstream = (
            "{{ config(materialized='ephemeral', event_time='event_time') }}\n"
            "select 1 as id, cast(from_unixtime(1746061200) as timestamp) as event_time\n"
        )
        model_sql = (
            "{{ config(\n"
            "    materialized='incremental',\n"
            "    incremental_strategy='microbatch',\n"
            "    unique_key='id',\n"
            "    event_time='event_time',\n"
            "    batch_size='day',\n"
            "    begin=modules.datetime.datetime(2025, 5, 1, 0, 0, 0),\n"
            "    partition_by={'field': 'event_time', 'data_type': 'timestamp', "
            "'granularity': 'day'}\n"
            ") }}\n"
            "select id, event_time from {{ ref('stg_events') }}\n"
        )
        return {"stg_events.sql": upstream, "microbatch_model.sql": model_sql}

    @pytest.fixture(scope="class")
    def dbt_profile_target(self):
        from odps import options

        filepath = os.environ.get("DBT_PROFILE_PATH")
        if not filepath:
            pytest.skip("needs a real MaxCompute profile via DBT_PROFILE_PATH")
        saved = options.local_timezone
        with open(filepath) as handle:
            target = yaml.safe_load(handle)
        target["timezone"] = self.profile_timezone
        try:
            yield target
        finally:
            options.local_timezone = saved

    def test_an_ephemeral_upstream_is_refused_and_says_the_type_is_unverified(self, project):
        logged = []
        run_dbt(
            [
                "run",
                "--select",
                "microbatch_model",
                "--event-time-start",
                "2025-05-01",
                "--event-time-end",
                "2025-05-02",
            ],
            expect_pass=False,
            callbacks=[lambda event: logged.append(str(getattr(event.info, "msg", "")))],
        )
        text = "\n".join(logged)
        assert "could not be read" in text, text[-2000:]
        assert "daylight-saving fold" not in text
        built = {
            relation.identifier
            for relation in project.adapter.list_relations_without_caching(
                project.adapter.Relation.create(
                    database=project.database, schema=project.test_schema
                )
            )
        }
        assert "microbatch_model" not in built, f"a refused batch writes nothing: {built}"


class TestMicrobatchDatetimeColumnOnShiftedSession(_DayWindowScenario):
    """A `datetime` event column on a session that has to be shifted: `timezone: Asia/Shanghai`.

    Two fixes meet in this cell. A `datetime` column refuses dbt-core's offset-bearing text outright
    (`ODPS-0121095`, refused at physical-plan time - that half was fixed by the suffix removal), and
    a session that pinned `Asia/Shanghai` needs the window shifted onto its own clock or adjacent
    batches overwrite each other's partitions. Stating the instant as the session's reading satisfies
    both at once, and the assertions are the same ones the `timestamp` cells use: no offset suffix in
    the text, and one partition per batch.
    """

    profile_timezone = "Asia/Shanghai"
    event_column_type = "datetime"
