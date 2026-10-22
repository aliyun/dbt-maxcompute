# dbt Core compatibility

Starting with dbt-maxcompute 1.12.0, dbt Core 1.12 compatibility is verified
alongside the tested 1.11 combinations. The adapter's version and dbt Core's
version do not need to match. The runtime
requirement remains `dbt-core>=1.11.2`; this upgrade adds no dependency on a
1.12-only Python API. A version accepted by the dependency resolver is not, by
itself, a verified feature combination.

## Test combinations

The CI workflows explicitly select these Core versions:

| Core version | Purpose |
| --- | --- |
| 1.11.2 | Minimum supported Core version |
| 1.11.16 | 1.11 patch regression baseline |
| 1.12.6 | 1.12 compatibility baseline |

`dev-requirements.txt` pins dbt-adapters 1.24.5, dbt-common 1.39.0 and
dbt-tests-adapter 1.20.0. Install the selected Core version and all test
requirements in one resolution:

```sh
python -m pip install -r dev-requirements.txt -e . 'dbt-core==1.12.6'
python -m pip check
python -m pytest tests/unit tests/unit_test -q
```

Use `-e '.[maxframe]'` to include the optional MaxFrame runtime.

The unit job crosses the three Core versions with Python 3.10–3.13. Wheel
MaxFrame import/Arrow-writer checks use Python 3.11 and all three Core versions.
The live SQL job also selects all three Core versions, but requires the existing
credential gate. A skipped job or skipped test is not evidence of a successful
MaxCompute run. See [SQL integration regression](integration-tests.md).

Local upgrade regression uses macOS arm64 / Python 3.12 and the pinned shared
libraries above. Real MaxCompute regression on Core 1.12.6 covers table/view,
incremental and partition strategies, merge, snapshots, materialized views,
Delta Table updates/deletes, UDFs, microbatch, persisted documentation and error
propagation. Tests use isolated schemas and verify their cleanup. This does not
replace execution of the full CI platform matrix.

## Core 1.12 feature boundaries

| Feature | Adapter behavior |
| --- | --- |
| Existing SQL models, Python UDFs and materializations | Existing paths are retained and regression-tested |
| `seed --empty` | Verified to create a table without loading seed rows |
| `run-operation --sql` | Verified with a warehouse SQL statement |
| Latest-version pointer | Verified to create a view and return the latest model's rows |
| Function `overloads` | Rejected before any root function/resource write; use distinct function names |
| Function argument defaults | Remain unsupported and explicitly rejected |
| SQL/JavaScript function resources | Not implemented by this adapter; existing Python UDF support is unchanged |
| Optional v2 parser | Not certified; the local probe exited with code -9 and the cause was not established |
| `catalogs` v2 / Iceberg integrations | Not certified by this upgrade |
| Semantic Layer YAML / OSI | Core parsing support does not certify MetricFlow SQL execution on MaxCompute |
| Python 3.14 | Not added to the adapter's tested Python matrix |

The MaxFrame extra can be installed and imported with Core 1.12.6 (MaxFrame
2.9.0, NumPy 1.26.4 and PyArrow 25.0.1 in the local check). This check is not a
real MaxFrame DAG execution. The existing [MaxFrame supported scope and
limitations](maxframe-python-models.md) still apply.

## Project migration checks

Core 1.12 enables several existing behavior flags by default, including
`require_batched_execution_for_custom_microbatch_strategy`, `validate_macro_args`,
`skip_nodes_if_on_run_start_fails` and `state_modified_compare_more_unrendered_values`.
Review custom macro declarations, project hooks, microbatch inputs and state-based
selection when upgrading a project. The adapter does not disable these defaults.
The live microbatch regression exercises repeated windows and advancing days.

New Core features such as `vars.yml`, private packages and `on_error` do not
imply new warehouse capabilities. Consult the [official Core 1.12 upgrade
guide](https://docs.getdbt.com/docs/dbt-versions/dbt-upgrade/upgrading-to-v1.12)
and the adapter-specific guides before enabling optional features.
