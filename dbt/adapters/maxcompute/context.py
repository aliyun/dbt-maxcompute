from dbt.adapters.maxcompute.utils import _dbt_maxcompute_version

GLOBAL_SQL_HINTS = {
    "dbt.maxcompute.version": _dbt_maxcompute_version(),
    "odps.sql.type.system.odps2": "true",
    "odps.sql.decimal.odps2": "true",
    "odps.sql.hive.compatible": "true",
    "odps.sql.allow.fullscan": "true",
    "odps.sql.select.output.format": "csv",
    "odps.sql.submit.mode": "script",
    "odps.sql.allow.cartesian": "true",
    "odps.sql.timezone": "GMT",
    "odps.sql.allow.schema.evolution": "true",
}
