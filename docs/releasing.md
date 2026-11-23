# Releasing dbt-maxcompute

Two workflows are involved, and they do different jobs:

| Workflow | Trigger | What it does | What it does **not** do |
| --- | --- | --- | --- |
| `.github/workflows/release.yml` (*Release checks on tag*) | push of a tag matching `v*` | unit tests; the Core / Python-model / UDF functional set when `DBT_PROFILE_YAML` is configured; a package build uploaded as a workflow artifact | publish to PyPI, create a GitHub Release |
| `.github/workflows/publish.yml` (*Publish release (manual)*) | *Run workflow* with a tag typed in | preflight peels the tag to one commit, the same tests with the functional set **required**, a build with `twine check`, publish to PyPI through the `pypi` environment, create the GitHub Release with the built files - and every job works on that one commit, not on a tag name that could move | run on a tag push |

## Prerequisites for the publish path

All three have to exist before `publish.yml` can get through:

1. The repository secret `DBT_PROFILE_YAML` - a profile that points at a live
   three-tier MaxCompute test project. Without it the run stops at
   *Require a MaxCompute test profile*; the workflow will not publish blind.
   See [integration-tests.md](integration-tests.md) for what that suite covers and
   what the profile needs.
2. A repository environment named `pypi`, **precreated and protected** before the first
   publish. The workflow declares `environment: pypi` with
   `url: https://pypi.org/p/dbt-maxcompute`, and the intended policy is that this is a
   *protected* deployment environment. That has to be set up out of band, because
   [GitHub creates an absent named environment without protection rules](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments#running-a-workflow-that-references-an-environment):
   if the name is missing, the first run quietly materialises an unguarded `pypi`.
   Preconfigure it with whichever reviewers the project wants (plus a branch/version rule
   if the project uses them), so an upload always needs an approval that a workflow edit
   cannot skip. Verify instead of assuming -
   `gh api repos/aliyun/dbt-maxcompute/environments/pypi --jq '.protection_rules'` must
   answer with a non-empty list. The repository currently reports zero environments, so
   nothing guards an upload yet.
3. Trusted publishing on the PyPI side for that project: publisher
   `github` / `aliyun` / `dbt-maxcompute` / `release.yml` until this file is used,
   then `publish.yml` (and the `pypi` environment as the branch filter). Update the
   PyPI publisher **before** the first manual run, otherwise the upload is rejected.
4. The tag itself must carry the gate (see *Publishing a version*): `scripts/publish_gate.py`
   and `tests/maxcompute_gating.py` have to exist in that checkout, or the run refuses to
   publish rather than testing nothing.

## Publishing a version

```bash
# 1. the release commit and tag are prepared as usual, then publish **that** tag:
gh workflow run publish.yml --repo aliyun/dbt-maxcompute -f tag=v1.12.2
gh run watch                                      # or: gh run list --workflow publish.yml

# 2. verify what actually landed, from the outside:
curl -s https://pypi.org/pypi/dbt-maxcompute/json | python3 -c '
import sys, json
d = json.load(sys.stdin)
rel = d["releases"]
for v in sorted(rel)[-3:]:
    print(v, [u["upload_time_iso_8601"] for u in rel[v]])
'
gh release view v1.12.2 --repo aliyun/dbt-maxcompute
```

Use the version number you actually tagged; `v1.12.2` above is a placeholder for "the next
release". **Tags cut before this workflow existed are refused on purpose**: `v1.12.1` and
earlier do not contain `scripts/publish_gate.py` or `tests/maxcompute_gating.py`, so the
first step of the run stops with `that predates the publish gate`. That is not a bug to
work around - re-publishing a pre-gate tag would mean shipping a version no run can prove
was tested against a live MaxCompute project. To release one of those tags anyway, cut a
new patch tag from a commit that carries the gate.

### What counts as verified

* **Same bytes on both surfaces.** Each file's `digests.sha256` in
  `https://pypi.org/pypi/dbt-maxcompute/<version>/json` must equal the `digest`
  reported for the identically named asset on the GitHub Release
  (`gh api repos/aliyun/dbt-maxcompute/releases/tags/v1.12.2 --jq '.assets[]|{name,digest}'`,
  with the version you actually published). The release body already carries a `sha256sum`
  table for its own assets, so the comparison starts from inside the release: the two
  tables must agree *and* match PyPI.
* **One commit for the whole run.** `preflight` peels the tag to a commit and publishes
  that value to the rest of the run. Each job checks out the commit (never the tag name),
  asserts its own `HEAD` is it, and the two jobs that write to the outside world re-ask
  the remote whether the tag still identifies it *immediately before* acting:
  `Refuse to publish if the tag moved` (before the PyPI upload) and
  `Refuse to label the release if the tag moved` (before the GitHub Release). The tag
  stays the release label; the commit is recorded in the release body, the run summary and
  the notes' `sha256` table. A re-tag while the run is in flight stops the run instead of
  publishing different bytes under the same label.
* **The publish job really ran.** `Publish to PyPI` must be `success`, and the PyPI
  `upload_time_iso_8601` must be **later** than that job, not merely close to the run.
* **The functional set ran.** The run summary of `Core, MaxFrame, and UDF Tests
  (required)` must show the pytest output, not a `NOT RUN` notice.
* **Nothing was skipped silently.** `Require live evidence before anything downstream can
  run` reads the functional JUnit report, not the exit code: it fails when a suite is
  missing, when nothing passed, or when any case was skipped for a reason not listed with
  `--allow-skip` - a suite that also has a passing case does not excuse that skip. The
  functional report has **no** allowlist. The unit report accepts only the handful of
  environment-shaped reasons named in the workflow (optional dependency missing, library
  layout differs); a unit skip with any other reason fails the run too.

A green-looking tag history is not evidence of a release: before this document
existed, `release.yml` had 11 recorded runs (2026-03-03 through 2026-10-22), every
one of them `completed/failure` at `Prepare MaxCompute test profile`, with
`Build distribution`, `Publish to PyPI` and `Create GitHub Release` `skipped` -
while `1.11.3`, `1.11.4`, `1.11.5`, `1.12.0` and `1.12.1b1` all appeared on PyPI and
as GitHub Releases, uploaded from outside that workflow. Two of them were uploaded
*before* the corresponding tag-triggered run had even reached the failing step.

## Going back to automatic publishing on a tag

If the project decides that pushing `v*` should publish by itself, that is a
deliberate change, not a leftover:

1. configure `DBT_PROFILE_YAML` and the `pypi` environment, and point PyPI's
   trusted publisher at the workflow that will publish;
2. add the publish and release jobs back to `release.yml` (kept intact in git
   history) or chain `publish.yml` off the tag push;
3. keep the functional gate required, so a tag push without credentials cannot
   publish;
4. check the first such release against the three bullets in *What counts as
   verified* before making another tag.
