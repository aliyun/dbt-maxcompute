import re
from dataclasses import dataclass, field, replace
from datetime import datetime, tzinfo, timezone
from typing import Dict, FrozenSet, Optional, Tuple, TypeVar

from dbt.adapters.base.relation import BaseRelation, EventTimeFilter, InformationSchema
from dbt.adapters.contracts.relation import RelationType, Path, Policy, RelationConfig
from dbt.adapters.events.logging import AdapterLogger
from dbt_common.exceptions import DbtRuntimeError
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


#: The physical types MaxCompute parses with the *session* clock. `trunc_time()`, which cuts this
#: adapter's partitions, works in UTC - so on a session that pinned a non-UTC timezone the batch
#: window has to be restated on that same session clock or adjacent batches overwrite each other.
_INSTANT_BEARING_TYPES = frozenset(
    {"timestamp", "datetime"}
)  # separator-free: see normalize_column_type
#: The physical types compared exactly as written, with no clock involved at all. Stating their
#: window on the session clock moves every batch into its neighbour's partition (measured end to
#: end: the next `insert overwrite` erases the rows the previous batch wrote).
_NAIVE_TYPES = frozenset({"timestampntz"})

#: The physical types of a relation are read once per invocation, keyed by its path. A column type
#: cannot change underneath a run that is already in flight, and the window is decided per batch.
_PHYSICAL_TYPE_CACHE: Dict[Tuple[Optional[str], Optional[str], Optional[str]], Dict[str, str]] = {}


def normalize_column_type(raw_type: Optional[str]) -> str:
    """`TIMESTAMP_NTZ`, `timestamp ntz`, `timestamp-ntz` and `timestamp_ntz` are the same type.

    Compared with its separators dropped, so MaxCompute's own spelling and a hand-typed alias reach
    the same branch. Anything that is not spelled like one of the known types stays `unknown`.
    """
    return re.sub(r"[\s_\-]+", "", str(raw_type or "")).lower()


def clock_kind_of(raw_type: Optional[str]) -> str:
    """`instant` / `naive` / `unknown` - which clock a column of this type is compared on.

    `unknown` is deliberately not folded into either branch: it covers `date`, an unparseable
    declaration, or a type a future MaxCompute adds. On a session whose clock differs from UTC the
    caller refuses instead of guessing, because both readings are wrong half the time.
    """
    name = normalize_column_type(raw_type)
    if name in _INSTANT_BEARING_TYPES:
        return "instant"
    if name in _NAIVE_TYPES:
        return "naive"
    return "unknown"


def _unquoted_field_name(field_name: str) -> str:
    """dbt-core can hand back the event field already quoted (```event_time```)."""
    return str(field_name).strip().strip("`").strip('"').lower()


def declared_event_field_type(relation_config: RelationConfig, field_name: str) -> Optional[str]:
    """The type this model's YAML property declares for the event field, when it declares one.

    A *fallback*, not the authority: measured on the real `ref()` path, an upstream column that is
    physically `timestamp` is declared `timestamp_ntz` in YAML, and the declaration picks the wrong
    clock. It is used only when the warehouse cannot be asked (a relation that does not exist yet,
    a connection that cannot describe it), and the run says so in the debug log.
    """
    wanted = _unquoted_field_name(field_name)
    columns = getattr(relation_config, "columns", None)
    if not isinstance(columns, dict):
        return None
    for info in columns.values():
        if _unquoted_field_name(getattr(info, "name", "")) == wanted:
            return getattr(info, "data_type", None) or None
    return None


def physical_column_types(relation: "MaxComputeRelation") -> Dict[str, str]:
    """This relation's column types as the warehouse states them, or {} when it cannot be read.

    The declared type in a YAML property is *not* used here: measured on the real `ref()` path, a
    column physically `timestamp` was declared `timestamp_ntz`, and the declaration is the one that
    would have picked the wrong clock. `{}` means unverifiable - it does not mean naive.
    """
    key = (relation.path.database, relation.path.schema, relation.path.identifier)
    cached = _PHYSICAL_TYPE_CACHE.get(key)
    if cached is not None:
        return cached
    types: Dict[str, str] = {}
    try:
        from dbt.adapters.factory import get_adapter_by_type

        columns = get_adapter_by_type("maxcompute").get_columns_in_relation(relation)
        types = {
            str(getattr(column, "column", "") or getattr(column, "field", "")).lower(): str(
                column.dtype
            )
            for column in columns
        }
    except Exception as exc:  # noqa: BLE001 - unverifiable is reported by the caller, not guessed
        logger.debug(
            "MaxCompute adapter: could not read the physical column types of %s (%s), so the "
            "microbatch window clock cannot be decided from the type." % (relation.render(), exc)
        )
    _PHYSICAL_TYPE_CACHE[key] = types
    return types


@dataclass(frozen=True, eq=False, repr=False)
class MaxComputeRelation(BaseRelation):
    quote_character: str = "`"
    # subquery alias name is not required in MaxCompute
    require_alias: bool = False

    def without_quote(self):
        return self.quote(False, False, False)

    include_policy: Policy = field(default_factory=lambda: OdpsIncludePolicy())
    # What the YAML property declares for the filtered event field, carried here because the
    # warehouse's own answer (`physical_column_types`) is the authority and this is only the
    # fallback when the warehouse cannot be asked. See `create_from`.
    event_field_declared_type: Optional[str] = None

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

    @classmethod
    def create_from(
        cls, quoting, relation_config: RelationConfig, **kwargs
    ) -> "MaxComputeRelation":
        """Keep the upstream's declared event-field type when dbt-core hands us the node.

        Every `ref()` / `source()` path that attaches an event-time filter goes through here with
        the *filtered* node as `relation_config` (`dbt/context/providers.py`), which is the only
        place a type for that field is reachable from a relation render.
        """
        relation = super().create_from(quoting, relation_config, **kwargs)
        event_time_filter = kwargs.get("event_time_filter")
        if event_time_filter is not None:
            declared = declared_event_field_type(relation_config, event_time_filter.field_name)
            if declared:
                relation = replace(relation, event_field_declared_type=declared)
        return relation

    @classmethod
    def create_ephemeral_from(
        cls, relation_config: RelationConfig, **kwargs
    ) -> "MaxComputeRelation":
        relation = super().create_ephemeral_from(relation_config, **kwargs)
        event_time_filter = kwargs.get("event_time_filter")
        if event_time_filter is not None:
            declared = declared_event_field_type(relation_config, event_time_filter.field_name)
            if declared:
                relation = replace(relation, event_field_declared_type=declared)
        return relation

    def _render_event_time_filtered(self, event_time_filter: EventTimeFilter) -> str:
        """State the batch window on the clock the field being compared is actually read on.

        dbt-core hands the adapter an `EventTimeFilter` with a field name and two UTC instants, and
        no type (`dbt/adapters/base/relation.py` builds the predicate from those three alone). What
        makes the text right is the **physical type of that field on the relation being read**:

        * `timestamp` / `datetime` are parsed on the session clock, so the instant has to be
          restated as the session's own reading of it (`session_boundary_text`) - otherwise a
          non-UTC session's window reaches into its neighbour's `trunc_time()` partition and the
          next `insert overwrite` erases rows the previous batch wrote;
        * `timestamp_ntz` carries no clock and is compared as written, so the window stays on the
          UTC reading with dbt-core's offset suffix dropped (a `datetime` column refuses the
          suffix, and MaxCompute refuses to compare an offset-bearing literal against a naive one
          in some shapes - see `docs/microbatch-support.md`);
        * anything else - `date`, a type this version does not know, or a relation whose columns
          could not be read - is **not** silently assigned to one of the two above. On a session
          that would move the text, that choice decides whether a batch loses rows, so it raises
          and says how to make the type verifiable.

        Where the session clock itself cannot be stated from here the window is left exactly as
        dbt-core rendered it, offset suffix and all: a guessed shift is worse than the known
        behaviour, and that path is documented rather than promised.
        """
        base = super()._render_event_time_filtered(event_time_filter)
        if not base:
            return base

        boundaries = [
            (boundary, operator)
            for boundary, operator in (
                (event_time_filter.start, ">="),
                (event_time_filter.end, "<"),
            )
            if boundary is not None
        ]
        if not boundaries:
            return base

        # Decide the clock once per window, not once per boundary: both ends are the same field.
        readings = []
        for boundary, _operator in boundaries:
            shifted = session_boundary_text(boundary)
            if shifted is None:
                # A session clock this cannot state the window on: leave dbt-core's rendering alone.
                return base
            readings.append((shifted, session_wall_clock(_utc_moment(boundary))))
        session_moved = any(shifted != utc_text for shifted, utc_text in readings)
        kind = (
            self._event_field_clock_kind(event_time_filter.field_name) if session_moved else "none"
        )

        parts = []
        chosen = []
        for (shifted, utc_text), (_boundary, operator) in zip(readings, boundaries):
            if kind == "naive" and shifted != utc_text:
                text = utc_text  # a naive field has no session clock to move it onto
            else:
                text = shifted
            if kind == "unknown" and shifted != utc_text:
                raise DbtRuntimeError(
                    self._unverifiable_clock_message(event_time_filter.field_name, _boundary, text)
                )
            chosen.append(text)
            parts.append(f"{event_time_filter.field_name} {operator} '{text}'")

        if len(chosen) == 2 and chosen[0] == chosen[1]:
            # Two different instants, one wall clock: the session is inside a DST fold, and a
            # half-open window cannot be stated there at all. Writing anyway would select nothing,
            # and whether the hour it was meant to cover is written by its neighbour depends on
            # which side of the fold the server resolves the repeated text to (measured:
            # America/New_York picks the earlier instant, Australia/Sydney the later one, so there
            # the hour is written by no batch). A warning would leave a batch that writes nothing
            # and reports success, so the batch is refused before anything is written.
            raise DbtRuntimeError(self._folded_window_message(chosen[0]))

        return " and ".join(parts) if parts else base

    def _event_field_clock_kind(self, field_name: str) -> str:
        """`instant` / `naive` / `unknown` - the field's own type, warehouse answer first.

        The physical type of the relation being read is the authority: it is what the server will
        compare on. A YAML declaration is used only when that cannot be read, and `unknown` when
        neither answers - which on a session whose clock differs from UTC is refused rather than
        guessed either way.
        """
        wanted = _unquoted_field_name(field_name)
        types = physical_column_types(self)
        for name, raw_type in types.items():
            if name == wanted:
                return clock_kind_of(raw_type)
        if self.event_field_declared_type:
            logger.debug(
                "MaxCompute adapter: the physical type of %r on %s could not be read, so the "
                "microbatch window is decided from the declared type %r, which is not verified "
                "against the column." % (field_name, self.render(), self.event_field_declared_type)
            )
            return clock_kind_of(self.event_field_declared_type)
        return "unknown"

    def _unverifiable_clock_message(self, field_name: str, boundary: datetime, text: str) -> str:
        return (
            "MaxCompute cannot tell which clock the microbatch window for %r belongs on. The batch "
            "boundary %s would read as '%s' on this session (`timezone: %s`), which is a different "
            "instant from the UTC reading, and the physical type of that column on %s could not be "
            "read - so whether it is a clock-bearing `timestamp`/`datetime` or a naive "
            "`timestamp_ntz` is unknown. Picking either one silently can let an adjacent batch "
            "overwrite this one's partition, so this is refused instead of guessed. To make it "
            "verifiable: materialise the relation being read before compiling against it (a "
            "`dbt compile` on a target schema where the upstream does not exist yet cannot answer "
            "this), or leave `timezone` unset so both readings are the same text."
            % (
                field_name,
                boundary,
                text,
                options.local_timezone,
                self.render(),
            )
        )

    def _folded_window_message(self, text: str) -> str:
        return (
            "MaxCompute refuses to write this microbatch batch: both of its boundaries read as the "
            "same wall clock '%s' on this session (`timezone: %s`), because the session is inside a "
            "daylight-saving fold, and a half-open window cannot be stated there. Writing anyway "
            "would select no rows and report success, and the hour this batch was meant to cover "
            "ends up written by its neighbour or by no batch at all - which side depends on the "
            "zone (measured: America/New_York resolves the repeated text to the earlier instant, "
            "Australia/Sydney to the later one). For models on a DST-observing session timezone, "
            "leave `timezone` unset (the session then runs on UTC) or pin a fixed-offset zone; a "
            "coarser `batch_size` also avoids the repeated hour." % (text, options.local_timezone)
        )

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
