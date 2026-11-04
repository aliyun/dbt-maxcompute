# Releasing dbt-maxcompute

Two workflows are involved, and they do different jobs:

| Workflow | Trigger | What it does | What it does **not** do |
| --- | --- | --- | --- |
| `.github/workflows/release.yml` (*Release checks on tag*) | push of a tag matching `v*` | unit tests; the Core / Python-model / UDF functional set when `DBT_PROFILE_YAML` is configured; a package build uploaded as a workflow artifact | publish to PyPI, create a GitHub Release |
| `.github/workflows/publish.yml` (*Publish release (manual)*) | *Run workflow* with a tag typed in | preflight on the tag, the same tests with the functional set **required**, a build with `twine check`, publish to PyPI through the `pypi` environment, create the GitHub Release with the built files | run on a tag push |

## Prerequisites for the publish path

All three have to exist before `publish.yml` can get through:

1. The repository secret `DBT_PROFILE_YAML` - a profile that points at a live
   three-tier MaxCompute test project. Without it the run stops at
   *Require a MaxCompute test profile*; the workflow will not publish blind.
   See [integration-tests.md](integration-tests.md) for what that suite covers and
   what the profile needs.
2. A repository environment named `pypi`. The workflow declares
   `environment: pypi` with `url: https://pypi.org/p/dbt-maxcompute`; a repository
   with no environments defined cannot resolve it.
3. Trusted publishing on the PyPI side for that project: publisher
   `github` / `aliyun` / `dbt-maxcompute` / `release.yml` until this file is used,
   then `publish.yml` (and the `pypi` environment as the branch filter). Update the
   PyPI publisher **before** the first manual run, otherwise the upload is rejected.

## Publishing a version

```bash
# 1. the release commit and tag are prepared as usual, then:
gh workflow run publish.yml --repo aliyun/dbt-maxcompute -f tag=v1.12.1
gh run watch                                      # or: gh run list --workflow publish.yml

# 2. verify what actually landed, from the outside:
curl -s https://pypi.org/pypi/dbt-maxcompute/json | python3 -c '
import sys, json
d = json.load(sys.stdin)
rel = d["releases"]
for v in sorted(rel)[-3:]:
    print(v, [u["upload_time_iso_8601"] for u in rel[v]])
'
gh release view v1.12.1 --repo aliyun/dbt-maxcompute
```

### What counts as verified

* **Same bytes on both surfaces.** Each file's `digests.sha256` in
  `https://pypi.org/pypi/dbt-maxcompute/<version>/json` must equal the `digest`
  reported for the identically named asset on the GitHub Release
  (`gh api repos/aliyun/dbt-maxcompute/releases/tags/v1.12.1 --jq '.assets[]|{name,digest}'`).
* **The publish job really ran.** `Publish to PyPI` must be `success`, and the PyPI
  `upload_time_iso_8601` must be **later** than that job, not merely close to the run.
* **The functional set ran.** The run summary of `Core, MaxFrame, and UDF Tests
  (required)` must show the pytest output, not a `NOT RUN` notice.

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
