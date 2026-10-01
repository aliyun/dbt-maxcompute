import json

import pytest
from dbt.tests.adapter.persist_docs.test_persist_docs import (
    BasePersistDocs,
    BasePersistDocsColumnMissing,
    BasePersistDocsCommentOnQuotedColumn,
)
from dbt.tests.adapter.persist_docs import fixtures
from dbt.tests.util import run_dbt


# MaxCompute can persist docs onto a view: the relation comment goes through
# `CREATE OR REPLACE VIEW ... COMMENT ... AS ...`, column comments through
# `ALTER VIEW ... CHANGE COLUMN ... COMMENT`. The class below is the full
# upstream battery (table + view + undocumented model) and passes against a
# real project; the old "not supported" note and the unused `_MODELS__VIEW`
# override that hid the view did not.
class TestPersistDocsToView(BasePersistDocs):
    pass


# A narrower variant with the view model removed, so the table path and a model
# without any docs are checked on their own.
class TestPersistDocsRedshift(BasePersistDocs):
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "no_docs_model.sql": fixtures._MODELS__NO_DOCS_MODEL,
            "table_model.sql": fixtures._MODELS__TABLE,
        }

    def test_has_comments_pglike(self, project):
        run_dbt(["docs", "generate"])
        with open("target/catalog.json") as fp:
            catalog_data = json.load(fp)
        assert "nodes" in catalog_data
        assert len(catalog_data["nodes"]) == 3
        table_node = catalog_data["nodes"]["model.test.table_model"]
        view_node = self._assert_has_table_comments(table_node)

        no_docs_node = catalog_data["nodes"]["model.test.no_docs_model"]
        self._assert_has_view_comments(no_docs_node, False, False)

    pass


class TestPersistDocsRedshiftColumn(BasePersistDocsColumnMissing):
    pass


class TestPersistDocsCommentOnQuotedColumn(BasePersistDocsCommentOnQuotedColumn):
    pass
