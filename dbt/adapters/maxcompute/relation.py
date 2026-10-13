from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import FrozenSet, Optional, TypeVar

from dbt.adapters.base.relation import BaseRelation, EventTimeFilter, InformationSchema
from dbt.adapters.contracts.relation import RelationType, Path, Policy, RelationConfig
from odps import options
from odps.models import Table

from dbt.adapters.maxcompute.relation_configs._materialized_view import (
    MaxComputeMaterializedViewConfig,
)

Self = TypeVar("Self", bound="MaxComputeRelation")


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


def session_wall_clock(moment: datetime, zone: Optional[object] = None) -> str:
    """The timestamp text ``YYYY-MM-DD HH:MM:SS[.ffffff]`` - an instant stated with no offset.

    ``zone`` is the clock to state it on; without one, the instant's own reading is used.
    Microseconds are kept rather than rounded away, because rounding would quietly move rows for
    the column types that can hold them.
    """
    reading = moment if zone is None else moment.astimezone(zone)  # type: ignore[arg-type]
    text = reading.strftime("%Y-%m-%d %H:%M:%S")
    if reading.microsecond:
        text += f".{reading.microsecond:06d}"
    return text


def session_boundary_text(moment: datetime) -> Optional[str]:
    """The boundary as this session reads it, but only when stating it cannot move the window.

    Returns ``None`` when the session clock is not knowable from here, or is not the UTC one: the
    window then has to be shifted onto that clock as well, which is a separate change, and doing
    only half of it would silently select a different set of rows. A zone is judged at the instant
    of the boundary rather than by its name, so a zone that is UTC in January and UTC+1 in July
    does not get the suffix dropped in the summer.
    """
    pinned = options.local_timezone
    utc_text = session_wall_clock(_utc_moment(moment))
    if pinned is None:
        return None  # nothing pinned here: the project's own default applies, unknowable locally
    if not pinned:
        # The adapter's default: ``MaxComputeCredentials._get_odps`` leaves pyodps' value falsey,
        # and pyodps then submits ``odps.sql.timezone=Etc/GMT`` with every statement.
        return utc_text
    if not isinstance(pinned, str):
        return None
    if pinned.strip().lower() in _UTC_SESSION_NAMES:
        return utc_text
    try:
        import pytz

        zone = pytz.timezone(pinned)
    except Exception:  # noqa: BLE001
        # A name this cannot resolve is a session clock this cannot reason about.
        return None
    utc_aware = _utc_moment(moment)
    return utc_text if session_wall_clock(utc_aware.astimezone(zone), zone) == utc_text else None


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
        """State the batch window in the shape a UTC session can parse for every time type.

        dbt-core renders the window from a UTC ``datetime``, so the text carries an offset:
        ``event_time >= '2025-05-01 00:00:00+00:00'``. MaxCompute compares that against
        ``timestamp`` and ``timestamp_ntz`` columns, but refuses it for ``datetime`` ones, whose
        string form is ``yyyy-mm-dd hh:mi:ss`` exactly - the batch then fails at physical-plan
        time, before a row is read::

            ODPS-0130071:[0,0] Semantic analysis exception - physical plan generation failed:
            ODPS-0121095:Invalid argument - in function cast, string datetime's format must be
            yyyy-mm-dd hh:mi:ss, input string is:2025-05-01 00:00:00+00:00

        ``datetime`` is a documented event-time type, so on the adapter's default profile - no
        ``timezone`` in the profile, session clock UTC - a microbatch model using one could not run
        at all while the same model on ``timestamp`` ran. Dropping the suffix fixes that and moves
        nothing else: measured on a real project, ``timestamp`` and ``timestamp_ntz`` select the
        same rows with or without it when the session is UTC
        (``docs/microbatch-support.md``).

        The suffix is dropped only where ``session_boundary_text`` says the session reads UTC, so
        the instant the window denotes is unchanged. Where the profile pins another zone the window
        must be shifted onto that clock too, which is a separate change: this method returns
        dbt-core's rendering there rather than half-correcting it, so a ``datetime`` column on such
        a profile keeps failing loudly, as it does today.

        One residual is deliberately left visible: a ``datetime`` column rejects fractional seconds
        as well, so a ``--sample`` window (whose end is ``datetime.now(UTC)``, microseconds
        included) still fails to compile against one. Rounding those away would have traded a loud
        error for a silently different window on the types that do accept fractions.
        """
        base = super()._render_event_time_filtered(event_time_filter)
        if not base:
            return base

        parts = []
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
            parts.append(f"{event_time_filter.field_name} {operator} '{text}'")

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
