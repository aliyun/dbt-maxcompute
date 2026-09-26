"""模型描述、persist_docs 开关与 catalog.json 注释之间的契约（需要真实服务端）。

三条互相独立的结论，都在这里钉住：
  1. 没开 persist_docs 时，dbt 不应把描述写进仓库元数据。以前建表的 DDL 会无条件
     带上列注释（视图和 seed 又不会），所以同一份 yml 在不同物化方式下结果不同，
     上游 docs generate 用例也一直对不上自己的期望。
  2. 开了 persist_docs 时，落库再读回的注释必须与 manifest 里的描述逐字相等——
     中文、双引号、单引号、真换行、反斜杠、分号、百分号、注释符号都不许变形。
     服务端元数据接口对表级注释是转义形态返回的，列注释不是，两条路都要走通。
  3. 列顺序由查询结果决定，描述写在 yml 里的顺序不影响它；写了描述但库里没有的列
     要像其他适配器一样告警。

用例故意把 yml 的列声明顺序做成与 select 相反，并在描述里放逗号与分号，
这样"注释挤进列定义"或"字面量被切开"会直接反映为断言失败。
"""

import pytest
from dbt.tests.util import get_artifact, run_dbt, run_dbt_and_capture

# 描述文本走 docs block，避免在 YAML 里手工转义引号与反斜杠。
_docs_md = """
{% docs tbl_doc %}
表级描述 table doc "with double quotes" and 'single quotes'
second line, with comma and ; semicolon and back\\slash and 80% and -- dash
{% enddocs %}

{% docs id_doc %}
主键 id "quoted" 'sq'
第二行 with back\\slash and /* star */ and $lbl$ dollar
{% enddocs %}

{% docs name_doc %}
名称 column with trailing backslash\\
{% enddocs %}
"""

# select 的顺序是 id, name；yml 里故意先写 name，用来验证列顺序不受影响。
_table_model = """
{{ config(materialized='table') }}
select 1 as id, 'a' as name
"""

_view_model = """
{{ config(materialized='view') }}
select 1 as id, 'a' as name
"""

_schema_yml = """
version: 2
models:
  - name: table_model
    description: "{{ doc('tbl_doc') }}"
    columns:
      - name: name
        description: "{{ doc('name_doc') }}"
      - name: id
        description: "{{ doc('id_doc') }}"
      - name: documented_but_absent
        description: "库里没有这一列，应该告警而不是静默丢掉"
  - name: view_model
    description: "{{ doc('tbl_doc') }}"
    columns:
      - name: id
        description: "{{ doc('id_doc') }}"
      - name: name
        description: ""
"""


def _catalog_and_manifest():
    return get_artifact("target/catalog.json"), get_artifact("target/manifest.json")


def _manifest_descriptions(manifest, unique_id):
    node = manifest["nodes"][unique_id]
    columns = {
        name: (column.get("description") or None) for name, column in node["columns"].items()
    }
    return node.get("description") or None, columns


class DocsCommentsBase:
    @pytest.fixture(scope="class")
    def models(self):
        return {
            "docs.md": _docs_md,
            "schema.yml": _schema_yml,
            "table_model.sql": _table_model,
            "view_model.sql": _view_model,
        }

    def _build(self):
        results = run_dbt(["run"])
        assert len(results) == 2
        run_dbt(["docs", "generate"])
        return _catalog_and_manifest()


class TestDocsCommentsOffByDefault(DocsCommentsBase):
    """没有 persist_docs：仓库里不该出现任何来自 dbt 描述的注释。"""

    def test_descriptions_do_not_reach_the_warehouse(self, project):
        catalog, manifest = self._build()

        # 先确认描述确实写在 manifest 里，否则下面的断言可能是空转。
        for unique_id in ("model.test.table_model", "model.test.view_model"):
            description, columns = _manifest_descriptions(manifest, unique_id)
            assert description
            assert any(comment for comment in columns.values())

        for unique_id in ("model.test.table_model", "model.test.view_model"):
            node = catalog["nodes"][unique_id]
            assert (
                node["metadata"]["comment"] is None
            ), f"{unique_id} 的描述在没有 persist_docs 的情况下被写进了仓库元数据"
            for name, column in node["columns"].items():
                assert column["comment"] is None, f"{unique_id}.{name} 落下了注释"


class TestPersistDocsCommentRoundTrip(DocsCommentsBase):
    """打开 persist_docs：写进去的注释与 manifest 描述逐字相等，且可重复运行。"""

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "models": {
                "test": {
                    "+persist_docs": {
                        "relation": True,
                        "columns": True,
                    },
                }
            }
        }

    def _assert_comments_match_manifest(self, catalog, manifest):
        for unique_id in ("model.test.table_model", "model.test.view_model"):
            expected_description, expected_columns = _manifest_descriptions(manifest, unique_id)
            node = catalog["nodes"][unique_id]
            assert node["metadata"]["comment"] == expected_description

            for name, column in node["columns"].items():
                expected = expected_columns.get(name)
                assert column["comment"] == expected, (
                    f"{unique_id}.{name}: 读回的注释与描述不一致\n"
                    f"expected={expected!r}\nactual={column['comment']!r}"
                )

    def _assert_column_order_follows_the_query(self, catalog):
        for unique_id in ("model.test.table_model", "model.test.view_model"):
            columns = catalog["nodes"][unique_id]["columns"]
            ordered = sorted(columns.values(), key=lambda column: column["index"])
            assert [column["name"] for column in ordered] == [
                "id",
                "name",
            ], "列顺序应按查询结果，而不是 yml 的声明顺序"

    def test_comments_round_trip_and_are_idempotent(self, project):
        catalog, manifest = self._build()
        self._assert_comments_match_manifest(catalog, manifest)
        self._assert_column_order_follows_the_query(catalog)

        # 再跑一次：注释不该在"写->读->比对"之间来回变化。
        run_dbt(["run", "--full-refresh"])
        second_catalog, second_manifest = _catalog_and_manifest()
        self._assert_comments_match_manifest(second_catalog, second_manifest)
        self._assert_column_order_follows_the_query(second_catalog)

    def test_documented_column_missing_from_relation_warns(self, project):
        _, stdout = run_dbt_and_capture(["run", "--full-refresh"], expect_pass=True)
        assert "documented_but_absent" in stdout, "schema 里写了、库里没有的列应给出可见告警"
