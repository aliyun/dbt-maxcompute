# MaxFrame production readiness

MaxFrame Python models are **Generally Available** starting with `1.11.3`
within their documented supported scope. Validate each production project's
quota, dependencies, and permission model with the gates below. dbt
interruption is not fully propagated to an active remote MaxFrame DAG; this
remains a known operational limitation.

## Release gates

1. **Client compatibility** — the adapter must remain installable on every
   Python version supported by its package metadata. Models that serialize
   custom Python functions should run on Python 3.11, or explicitly accept the
   compatibility warning after workload validation. A controlled target can
   set `maxframe_python_version_check: error`.
2. **Partition fidelity** — a partitioned `ref` or `source` must expose its
   partition columns to downstream MaxFrame filtering, grouping, and joins.
3. **Dependency repeatability** — PythonPack production caching is enabled by
   default. Large native scientific stacks should use an approved managed or
   custom runtime image rather than rely on source builds.
4. **Failure isolation** — table and full-refresh writes must use an
   intermediate relation, and incremental SQL must not run until the MaxFrame
   stage succeeds.
5. **Bounded recovery** — transient timeouts, throttling, connection failures,
   and service HTTP 5xx responses must retry in a new MaxFrame session, up to
   `maxframe_retries`.
6. **Resource hygiene** — every created session, including a failed retry
   session, must leave no matching `tmp_mf_<session>_*` tables or
   `mf_udf_<session>_*` functions after the node finishes.

## Regression baseline

The 2026-08-11 release-candidate regression used Python 3.11, dbt Core 1.11.7,
MaxFrame 2.8.0.post0, and the adapter source from the production-hardening
commit. It validated:

- catalog scalar UDF and UDAF creation followed by in-place update;
- ten UDF rows, four UDAF groups, malformed-input handling, and CP311 archive
  dependencies;
- a four-dependency row UDF, two dimension joins, and partition counts of
  `6 / 2 / 2` across three dates;
- incremental full refresh followed by a second merge, with four unique keys
  and ten source events;
- a managed `sklearn` image combined with RapidFuzz/text-unidecode PythonPack
  dependencies, producing the expected identity candidate;
- an injected `ConnectionResetError` that created a second session and then
  completed a real remote table write;
- zero session-scoped tables/functions after successful, failed, and retried
  sessions;
- thirteen remote dbt data tests and 119 local unit tests.

The heavy dependency model took about 356 seconds before cache reuse and 148
seconds on the immediate rerun. These values demonstrate cache effectiveness,
not a latency SLA; quota pressure and service scheduling can change them.

All cloud regression objects used unique schemas that were confirmed absent
before the run. Both schemas and all contained tables, functions, and resources
were deleted after semantic verification.

## Promotion decision

A project can proceed when all six release gates pass with its own quota,
runtime image, dependencies, data volume, and permission model. Operational
procedures must account for an active remote DAG continuing after dbt
interruption or forced worker termination.
