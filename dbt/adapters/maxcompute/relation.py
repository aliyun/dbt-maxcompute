from dataclasses import dataclass, field
from datetime import datetime, tzinfo, timezone
from typing import FrozenSet, Optional, TypeVar

from dbt.adapters.base.relation import BaseRelation, EventTimeFilter, InformationSchema
from dbt.adapters.contracts.relation import RelationType, Path, Policy, RelationConfig
from dbt.adapters.events.logging import AdapterLogger
from odps import options
from odps.models import Table

from dbt.adapters.maxcompute.relation_configs._materialized_view import (
    MaxComputeMaterializedViewConfig,
)

Self = TypeVar("Self", bound="MaxComputeRelation")

logger = AdapterLogger("MaxCompute")


@dataclass
class OdpsIncludePolicy(Policy):
    database: bool = True
    schema: bool = True
    identifier: bool = True


#: Zone names that all denote a session clock of zero offset. Matching the names already seen keeps
#: the common profiles off the resolution path; anything else is decided by its own offset.
_UTC_SESSION_NAMES = frozenset(
    {"utc", "gmt", "z", "etc/gmt", "etc/utc", "etc/zulu", "gmt+0", "gmt-0", "+00:00", "-00:00"}
)


def _utc_moment(moment: datetime) -> datetime:
    """The same instant on the UTC clock, treating a naive boundary as UTC (which dbt-core renders)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def session_wall_clock(moment: datetime, zone: Optional[tzinfo] = None) -> str:
    """The timestamp text ``YYYY-MM-DD HH:MM:SS[.ffffff]`` - an instant stated with no offset.

    ``zone`` is the clock to state it on; without one, the instant's own reading is used.
    Microseconds are kept rather than rounded away, because rounding would quietly move rows for
    the column types that can hold them.
    """
    reading = moment if zone is None else moment.astimezone(zone)
    text = reading.strftime("%Y-%m-%d %H:%M:%S")
    if reading.microsecond:
        text += f".{reading.microsecond:06d}"
    return text


def _session_zone(name: str) -> Optional[tzinfo]:
    """Resolve a session timezone name, preferring the host's own tz database.

    ``zoneinfo`` is the fresher source where the host has tz data; ``pytz`` carries its own copy of
    the database and is already a dependency of the stack this adapter builds on (dbt-core's own
    microbatch builder uses it), which is what a Windows host with no system tz data can fall back
    to. ``None`` means this host cannot name the clock the server is about to use.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - no tzdata on the host is expected on some platforms
        pass
    try:
        import pytz

        return pytz.timezone(name)
    except Exception:  # noqa: BLE001 - unknown name: the caller degrades, it does not guess
        logger.warning(
            "Cannot resolve the session timezone '%s' here, so the microbatch window stays as "
            "dbt-core rendered it: it will be compared on the session clock while trunc_time() "
            "cuts UTC, which can let adjacent batches overwrite each other's partitions." % name
        )
        return None


def session_boundary_text(moment: datetime) -> Optional[str]:
    """The batch boundary as the session clock will parse it back into the instant dbt meant.

    Two properties of the text matter, and one thing decides both - the session timezone:

    * **no offset suffix.** dbt-core renders a UTC ``datetime``, so its text reads
      ``2025-05-01 00:00:00+00:00``. MaxCompute compares that against ``timestamp`` and
      ``timestamp_ntz`` columns, but a ``datetime`` column accepts ``yyyy-mm-dd hh:mi:ss`` and
      nothing else, and the batch dies at physical-plan time before reading a row
      (``ODPS-0121095``);
    * **the session's own reading of that instant.** A timestamp string is parsed on the session
      timezone, while the partitions the batch overwrites come from ``trunc_time()``, which cuts
      UTC. On a UTC session the two readings are the same text, so stating the instant with no
      offset moves nothing. On a session that pinned ``Asia/Shanghai`` the same instant has to be
      written ``2025-05-01 08:00:00`` for the server to read it back as ``2025-05-01 00:00:00`` UTC;
      written as dbt-core does, the window starts eight hours early, reaches into its neighbour's
      partition, and the later ``insert overwrite`` erases rows the earlier batch had written.

    A zone is judged at the instant of this boundary rather than by its name or a fixed date, so a
    zone that is UTC in January and UTC+1 in July gets the summer shift, not the winter one.

    Returns ``None`` when the session clock cannot be stated from here: nothing pinned (the
    project's own default applies, unknowable locally), a value that is not a zone name, or a name
    this host cannot resolve. The caller then keeps dbt-core's rendering - a guessed shift is
    worse than the known behaviour.
    """
    pinned = options.local_timezone
    if pinned is None:
        return None  # nothing pinned here: the project's own default applies, unknowable locally
    if not pinned:
        # The adapter's default: ``MaxComputeCredentials._get_odps`` leaves pyodps' value falsey,
        # and pyodps then submits ``odps.sql.timezone=Etc/GMT`` with every statement.
        return session_wall_clock(_utc_moment(moment))
    if not isinstance(pinned, str):
        return None
    utc_aware = _utc_moment(moment)
    if pinned.strip().lower() in _UTC_SESSION_NAMES:
        return session_wall_clock(utc_aware)
    zone = _session_zone(pinned)
    if zone is None:
        return None
    return session_wall_clock(utc_aware.astimezone(zone), zone)


@dataclass(frozen=True, eq=False, repr=False)
class MaxComputeRelation(BaseRelation):
    quote_character: str = "`"
    # subquery alias name is not required in MaxCompute
    require_alias: bool = False

    def without_quote(self):
        return self.quote(False, False, False)

    include_policy: Policy = field(default_factory=lambda: OdpsIncludePolicy())

    renameable_relations: FrozenSet[RelationType] = field(
        default_factory=lambda: frozenset(
            {
                RelationType.View,
                RelationType.Table,
            }
        )
    )

    replaceable_relations: FrozenSet[RelationType] = field(
        default_factory=lambda: frozenset(
            {
                RelationType.View,
                RelationType.Table,
                RelationType.MaterializedView,
            }
        )
    )

    def _render_event_time_filtered(self, event_time_filter: EventTimeFilter) -> str:
        """State the batch window on the clock its partitions are cut on, in text every type parses.

        dbt-core compares ``event_time`` against a UTC boundary rendered as an offset-bearing
        timestamp string. `session_boundary_text` restates that instant as a suffix-free string on
        the session clock, which is what makes the comparison land on the instant (see
        `docs/microbatch-support.md` for the measurements behind both halves).

        One residual is deliberately left visible: a ``datetime`` column rejects fractional seconds
        as well, so a ``--sample`` window (whose end is ``datetime.now(UTC)``, microseconds
        included) still fails to compile against one. Rounding those away would have traded a loud
        error for a silently different window on the types that do accept fractions.

        Where the session clock cannot be stated from here the window is left exactly as dbt-core
        rendered it, offset suffix and all.
        """
        base = super()._render_event_time_filtered(event_time_filter)
        if not base:
            return base

        parts = []
        readings = []
        for boundary, operator in (
            (event_time_filter.start, ">="),
            (event_time_filter.end, "<"),
        ):
            if boundary is None:
                continue
            text = session_boundary_text(boundary)
            if text is None:
                # A session clock this cannot state the window on: leave dbt-core's rendering alone.
                return base
            readings.append(text)
            parts.append(f"{event_time_filter.field_name} {operator} '{text}'")

        if len(readings) == 2 and readings[0] == readings[1]:
            # Two different instants, one wall clock: the session is inside a DST fold, and a
            # half-open window cannot be stated there at all. The batch will select nothing and
            # write nothing - and whether the hour it was meant to cover is written by its
            # neighbour depends on which side of the fold the server resolves the repeated text
            # to, which differs by zone (measured: America/New_York picks the earlier instant,
            # Australia/Sydney the later one, so there the hour is written by no batch). Saying
            # nothing while a batch writes nothing is the silence this file exists to remove.
            logger.warning(
                "The session timezone %r reads both ends of this microbatch window as the same "
                "wall clock (%s), so this batch selects no rows and writes nothing. That happens once "
                "per "
                "daylight-saving fold with `batch_size: hour`, and the hour it was meant to cover "
                "may end up written by the next batch or by no batch at all, depending on how the "
                "server resolves the repeated text. For hour-granularity models, leave `timezone` "
                "unset (the session then runs on UTC) or pin a fixed-offset zone; a coarser "
                "`batch_size` also avoids the repeated hour."
                % (options.local_timezone, readings[0])
            )

        return " and ".join(parts) if parts else base

    @property
    def project(self):
        return self.database

    @property
    def is_transactional(self):
        return self.get("transactional", False)

    def information_schema(
        self, identifier: Optional[str] = None
    ) -> "MaxComputeInformationSchema":
        return MaxComputeInformationSchema.from_relation(self, identifier)

    @classmethod
    def from_odps_table(cls, table: Table):
        schema = table.get_schema()
        schema = schema.name if schema else "default"

        table_type = RelationType.Table
        if table.is_virtual_view:
            table_type = RelationType.View
        if table.is_materialized_view:
            table_type = RelationType.MaterializedView

        return cls.create(
            database=table.project.name,
            schema=schema,
            identifier=table.name,
            type=table_type,
        )

    @classmethod
    def materialized_view_from_relation_config(
        cls, relation_config: RelationConfig
    ) -> MaxComputeMaterializedViewConfig:
        return MaxComputeMaterializedViewConfig.from_relation_config(relation_config)


@dataclass(frozen=True, eq=False, repr=False)
class MaxComputeInformationSchema(InformationSchema):
    quote_character: str = "`"

    @classmethod
    def get_path(cls, relation: BaseRelation, information_schema_view: Optional[str]) -> Path:
        return Path(
            database="SYSTEM_CATALOG",
            schema="INFORMATION_SCHEMA",
            identifier=information_schema_view,
        )

    @classmethod
    def get_include_policy(cls, relation, information_schema_view):
        return relation.include_policy.replace(database=True, schema=True, identifier=True)

    @classmethod
    def get_quote_policy(
        cls,
        relation,
        information_schema_view: Optional[str],
    ) -> Policy:
        return relation.quote_policy.replace(database=False, schema=False, identifier=False)
