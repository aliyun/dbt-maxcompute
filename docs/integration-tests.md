# SQL integration regression

Unit tests and package builds in `main.yml` never contact a MaxCompute project,
so everything that is really decided by the server - materialization SQL,
incremental strategy output, persisted docs, error propagation - had no
regression gate. This page documents the entry point that covers it.

## What runs

| Suite | Selection | Contents |
| --- | --- | --- |
| minimal (default) | `pytest -m integration_smoke` | `tests/functional/test_minimal_sql_set.py`: table, view, incremental, dbt tests (passing and failing), persisted docs, invalid SQL, schema cleanup |
| release core | `pytest -m core_test` | `tests/functional/test_core.py`: the broader materialization suite run before a release |

Both suites create their own schema per test class (prefix `test`), and drop it
again in teardown. Every assertion that claims a model worked reads the object
back from the server - a row count, a relation type, a comment - rather than
trusting dbt's own exit status.

## Prerequisites

* A **three-tier (schema-enabled) MaxCompute project**. dbt discovers schemas at
  startup; on a two-tier project it fails with
  `ODPS-0110061 ... Invalid database operations on two-tier model`, which is an
  unusable environment, not an adapter bug.
* Python 3.10+ with the development requirements installed:

```bash
pip install -r dev-requirements.txt
pip install -e .
```

## Run it locally

```bash
./scripts/run-integration-tests.sh                        # minimal set
./scripts/run-integration-tests.sh --suite core            # release core suite
./scripts/run-integration-tests.sh -- -k TestMinimalView   # extra pytest arguments
```

The script resolves a profile in this order:

1. `$DBT_PROFILE_PATH`, or `./dbt_profile.yml` next to the repository root if it
   exists (that file is git-ignored). It holds one profile target mapping:

   ```yaml
   type: maxcompute
   project: your_three_tier_project
   endpoint: http://service.cn-hangzhou.maxcompute.aliyun.com/api
   auth_type: access_key
   access_key_id: <your access key id>
   access_key_secret: <your access key secret>
   ```

2. Otherwise, with `MC_PROJECT` and `MC_ENDPOINT` exported, the script writes a
   temporary profile that uses `auth_type: chain`, so the access keys stay in
   the environment and are never copied into a file in the repository:

   ```bash
   export ALIBABA_CLOUD_ACCESS_KEY_ID=...      # or ODPS_ACCESS_ID / ODPS_ACCESS_KEY
   export ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
   export MC_PROJECT=your_three_tier_project
   export MC_ENDPOINT=http://service.cn-hangzhou.maxcompute.aliyun.com/api
   ./scripts/run-integration-tests.sh
   ```

`dbt-core` 1.11 stopped rendering Jinja inside `profiles.yml`, so
`env_var(...)` in a profile file does not work; generating the profile from the
environment as shown above is the supported way to keep credentials out of
committed files.

### Reading the result

```text
target:  project=... endpoint_host=... auth_type=chain schema_prefix=test*
suite:     smoke (pytest -m integration_smoke)
INTEGRATION RESULTS
  cases:   executed=6 passed=6 failed=0 errors=0 skipped=0
  cleanup: ok (6 schemas created by this run, all dropped)
  status:  PASSED (server-side evidence produced)
```

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Cases ran against the project and all passed |
| 1 | Cases ran and at least one failed, or test schemas were left behind |
| 2 | **Blocked** - no credentials, a two-tier project, or the project was unreachable. Nothing reached a server, so this is never reported as a pass |

Cases skipped because credentials are missing also make the script exit `2`.

### Unrelated breakage is reported, not absorbed

`pytest` aborts the session when any collected file fails to import, so a broken
module anywhere in `tests/functional` could leave this entry with no evidence at
all - and the JUnit suite header still reports a `tests` count for the collection
error, which reads like "1 case ran". The entry therefore passes
`--continue-on-collection-errors` and counts cases per `<testcase>` element, so
the numbers describe what actually executed:

```text
  cases:   executed=1 passed=1 failed=0 errors=1 skipped=1 not_run=[tests.functional.test_zz_import_probe]
  status:  FAILED (ran with failures)
```

Measured on a live project with a deliberately broken import: our case still ran
and passed, and the run stayed red because the repository is broken. Before the
change the same session reported `executed=1 passed=0` when nothing had run. A
collection error can neither become a pass nor hide the cases that did run.

Cleanup is attributed, not guessed. An observer fixture
(`tests/functional/conftest.py`) records the schema each test class is about to
use into `DBT_INTEGRATION_SCHEMA_MANIFEST` (a file in the script's private temp
dir, appended as each class starts), and both the in-test assertion and the
script's check ask only about *those* names - which is why `--suite core` gets
the same attribution as the minimal set. The observer depends on nothing but the
schema name, so it cannot drag credentials or server state into a test that never
asked for them. Checking "did any `test*` schema
appear while I was running" was the first version, and it is wrong on a shared
project: measured on 2026-09-26, an unrelated `test*` schema created mid-run
made a fully passing suite report `6 passed, 1 error` and exit `1`. If no names
were recorded the script says `cleanup: UNKNOWN` instead of inventing a verdict.
Running `pytest` directly is fine too: every case that needs a server is
skipped with the reason computed by `tests/maxcompute_gating.py`.

## Verified baselines

The entry has been run against a live project on two dependency resolutions,
because they are not the same thing:

| baseline | what it is | result |
| --- | --- | --- |
| Python 3.12.3, dbt-core 1.11.2, pyodps 0.13.2, pytest 9.1.1 | the compatibility line README declares, adapter installed from this source tree | minimal set 6/6 (389 s); `--suite core` 20/20 (1180 s) |
| Python 3.10.21, dbt-core 1.12.5, dbt-common 1.39.0, dbt-adapters 1.24.5, dbt-tests-adapter 1.20.0, pytest 9.1.1 | what `pip install -r dev-requirements.txt && pip install -e .` resolves to today, which is what this workflow job installs | minimal set 6/6 (379 s), 136 unit tests pass, all 246 functional tests still collect |

Note the asymmetry rather than reading past it: `setup.py` bounds `dbt-core` as
`>=1.11.2` with no upper bound, so a CI install lands on 1.12.5 while README
declares "the dbt Core 1.11 compatibility line". Both suites passed on both
resolutions here, but the declared window is a compatibility statement about the
adapter, not about this test entry, and this change does not alter it.

## Run it in CI

`.github/workflows/integration.yml` runs on push to `master`/`develop`, on pull
requests, and on a manual dispatch (with an optional `pytest -k` expression).

* Credentials come from the repository secret `DBT_PROFILE_YAML`, the same
  secret the release workflow uses: the profile mapping above, written to the
  runner's temporary directory with mode `600` and never echoed.
* A workflow triggered by a **pull request from a fork cannot read secrets**.
  The `gate` job reports that case as `integration: NOT RUN`, and the
  integration job stays *skipped* (grey) - it does not run and does not turn
  green.
* If a run that is supposed to produce evidence (push or manual dispatch) has
  no configured profile, the gate job fails with `integration blocked:
  DBT_PROFILE_YAML is not configured`. A missing credential is an outage of the
  gate, not a pass.
* The script writes the case counts and the verdict into the job summary, so
  "how many cases actually ran" is visible without opening the log.

### Two repository settings this workflow needs

Both were read from the GitHub API on 2026-09-25; re-check before acting on them.

1. **The workflow file is currently registered as disabled.** `GET
   /repos/aliyun/dbt-maxcompute/actions/workflows` lists
   `.github/workflows/integration.yml` as `state: disabled_manually` (entry
   created 2024-10-28, last changed 2024-11-13) with **zero recorded runs**;
   `master` still carries the file as 0 bytes (`git cat-file -s
   master:.github/workflows/integration.yml` -> `0`). Someone with repository
   admin rights has to enable the workflow after the merge, or it will not run
   at all no matter what the file contains.
2. **`DBT_PROFILE_YAML` looks unset.** The most recent release run
   (`v1.11.3b3`, 2026-08-26) failed in the `Prepare MaxCompute test profile`
   step, whose first command is `test -n "$DBT_PROFILE_YAML"`, and every later
   job - including the release functional tests - was `skipped`. A secret added
   since then would not show in that old run, so the live answer is whatever the
   next run says: with no secret, `gate` reports `integration: BLOCKED` and
   fails on push/manual runs, which is the intended behaviour rather than a bug.

Until both are sorted out, the credential-free half of the gate is what CI
actually exercises: `tests/unit/test_maxcompute_gating.py` (10 cases) runs in
the ordinary `main.yml` job, on fork pull requests included, and it is what
keeps "we never reached a server" from being reported as a passing integration.
