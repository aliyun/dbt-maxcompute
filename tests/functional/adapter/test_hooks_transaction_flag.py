"""`transaction: false` model hooks must run, in dbt's documented order.

dbt-core runs a model's hooks in two passes per phase -- pre-hooks with
``inside_transaction=False`` then ``=True``, post-hooks the other way round --
and ``run_hooks`` keeps only the hooks whose ``transaction`` flag matches the
pass. A materialization that calls ``run_hooks(pre_hooks)`` once therefore
silently drops every hook a user marked ``transaction: false``, which is also
what the documented ``before_begin()`` / ``after_commit()`` helpers produce.

Each hook inserts its own label plus the number of rows already in the audit
table, so the recorded pairs show which hooks ran and in what order. The audit
table is read back from the server, not from dbt's run result.
"""

import pytest
from dbt.tests.util import run_dbt

AUDIT_TABLE = "hook_transaction_audit"

# `{{ target.schema }}` is deliberately left unrendered here: run_hooks() renders
# the hook text again when the model is built.
_HOOK_SQL = (
    "insert into {{ target.schema }}."
    + AUDIT_TABLE
    + " select '%s', count(*) from {{ target.schema }}."
    + AUDIT_TABLE
)


def _hook(label, inside_transaction):
    return {"sql": _HOOK_SQL % label, "transaction": inside_transaction}


class BaseHookTransactionFlag:
    # Listed in the reverse of dbt's execution order on purpose: the
    # outside-transaction pre-hook is listed last but must run first, and the
    # outside-transaction post-hook is listed first but must run last.
    PRE_HOOKS = [_hook("pre_inside", True), _hook("pre_outside", False)]
    POST_HOOKS = [_hook("post_outside", False), _hook("post_inside", True)]

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "models": {
                "test": {
                    "pre-hook": self.PRE_HOOKS,
                    "post-hook": self.POST_HOOKS,
                }
            }
        }

    @pytest.fixture(scope="class", autouse=True)
    def audit_table(self, project):
        project.run_sql(f"drop table if exists {project.test_schema}.{AUDIT_TABLE}")
        project.run_sql(
            f"create table {project.test_schema}.{AUDIT_TABLE} (hook string, saw bigint)"
        )
        yield
        project.run_sql(f"drop table if exists {project.test_schema}.{AUDIT_TABLE}")

    def read_audit(self, project):
        rows = project.run_sql(
            f"select hook, saw from {project.test_schema}.{AUDIT_TABLE}", fetch="all"
        )
        return {row[0]: int(row[1]) for row in rows}

    def test_outside_transaction_hooks_run_and_are_ordered(self, project):
        run_dbt(["run"])
        # `saw` is how many hooks had already inserted a row when this one ran,
        # so the values spell out the order and a missing key spells out a drop.
        assert self.read_audit(project) == {
            "pre_outside": 0,
            "pre_inside": 1,
            "post_inside": 2,
            "post_outside": 3,
        }


class TestHooksOnTableModel(BaseHookTransactionFlag):
    @pytest.fixture(scope="class")
    def models(self):
        return {"hooked_table.sql": "select 1 as id"}


class TestHooksOnIncrementalModel(BaseHookTransactionFlag):

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "hooked_incremental.sql": "{{ config(materialized='incremental') }}\nselect 1 as id"
        }
