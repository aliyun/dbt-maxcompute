"""Exercise the installed wheel through real dbt CLI; remote maxc is a strict fixture.

This verifies packaging, macro discovery, current-project parsing and transport
contracts without credentials. Live cloud verification is a separate release gate.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    dbt = str(Path(sys.executable).parent / "dbt")
    if not Path(dbt).is_file():
        raise RuntimeError("Install the built adapter wheel with its semantic extra first")
    with tempfile.TemporaryDirectory(prefix="dbt-semantic-wheel-") as temporary:
        root = Path(temporary).resolve()
        (root / "models").mkdir()
        (root / "dbt_project.yml").write_text(
            "name: semantic_smoke\nversion: '1.0'\nconfig-version: 2\n"
            "profile: semantic_smoke\nmodel-paths: [models]\n"
        )
        (root / "profiles.yml").write_text(
            "semantic_smoke:\n  target: dev\n  outputs:\n    dev:\n"
            "      type: maxcompute\n      project: p\n      schema: default\n"
            "      threads: 1\n      endpoint: https://example.invalid/api\n"
            "      auth_type: access_key\n      access_key_id: fixture\n"
            "      access_key_secret: fixture\n      timezone: UTC\n"
        )
        fixture = Path(__file__).parent / "fixtures" / "semantic-smoke.yml"
        shutil.copyfile(fixture, root / "models" / "semantic.yml")
        for name in ("orders", "customers", "time_spine"):
            (root / "models" / (name + ".sql")).write_text("select 1 as fixture_only\n")
        fake_maxc = root / "maxc-fixture"
        fake_maxc.write_text(
            "#!"
            + sys.executable
            + "\n"
            + """import json,sys
args=sys.argv[1:]
if args[:3] == ['semantic','get','sales']:
    data={'format':'maxc.semantic/v1','source':'USER_DRAFT','revisionId':'draft_1',
          'identity':{'endpoint':'https://example.invalid','namespace':'123','specName':'sales','specId':'spec_1'},
          'object':{'dataScope':{'dataReferences':[]}},'content':{'glossary':[{'term':'keep'}]}}
elif args[:2] == ['meta','describe'] and '--metadata-only' in args:
    assert args[args.index('--project')+1]=='p'
    assert args[args.index('--schema')+1]=='default'
    columns={'orders':[('order_id','INT'),('customer_id','INT'),('ordered_at','TIMESTAMP'),('amount','DECIMAL(18,2)'),('status','STRING')],
             'customers':[('customer_id','INT'),('region','STRING')]}
    data={'table':{'table_name':args[2],'project':'p','schema_name':'default',
                  'columns':[{'name':n,'type':t} for n,t in columns[args[2]]],
                  'partition_columns':[],'has_more_columns':False}}
else:
    raise AssertionError('Unexpected operation; the exporter must only read snapshot/schema')
print(json.dumps({'status':'success','data':data}))
"""
        )
        fake_maxc.chmod(0o700)
        args = {"name": "sales", "timezone": "UTC", "join_type": "LEFT", "maxc": str(fake_maxc)}
        env = dict(os.environ, DBT_SEND_ANONYMOUS_USAGE_STATS="false")
        for key in ("DBT_PROJECT_DIR", "DBT_PROFILES_DIR", "PYTHONPATH"):
            env.pop(key, None)
        result = subprocess.run(
            [
                dbt,
                "--no-partial-parse",
                "run-operation",
                "dbt_maxcompute.export_semantic_package",
                "--project-dir",
                str(root),
                "--profiles-dir",
                str(root),
                "--args",
                json.dumps(args),
            ],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        print(result.stdout)
        if result.returncode:
            raise RuntimeError(result.stderr or "dbt run-operation failed")
        proposal = json.loads((root / "target" / "maxagent-semantic-proposal.json").read_text())
        assert proposal["revisionId"] == "draft_1"
        assert proposal["content"]["glossary"] == [{"term": "keep"}]
        assert len(proposal["content"]["metricDefinitions"]) == 3
        assert (
            proposal["content"]["semanticModel"]["approvedRelationships"][0]["joinType"] == "LEFT"
        )
        assert "timeConventions" in proposal["content"]["instructions"]
        print(
            "PASS installed wheel: dbt CLI, bundled macro, metrics, time, join; fixture transport only"
        )


if __name__ == "__main__":
    main()
