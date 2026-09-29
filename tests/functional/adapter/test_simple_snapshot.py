import pytest
from dbt.tests.adapter.simple_snapshot import common, seeds, snapshots
from dbt.tests.adapter.simple_snapshot.test_snapshot import BaseSimpleSnapshot, BaseSnapshotCheck
from dbt.tests.util import run_dbt, relation_from_name

schema_seed__yml = """
seeds:
  - name: seed
    config:
      transactional: true
      column_types: {
        updated_at: 'timestamp'
      }
model:
  - name: fact
    config:
      transactional: true
"""


class TestSnapshot(BaseSimpleSnapshot):
    @pytest.fixture(scope="class")
    def seeds(self):
        """
        This seed file contains all records needed for tests, including records which will be inserted after the
        initial snapshot. This table will only need to be loaded once at the class level. It will never be altered, hence requires no further
        setup or teardown.
        """
        return {"seed.csv": seeds.SEED_CSV, "schema.yaml": schema_seed__yml}

    def test_updates_are_captured_by_snapshot(self, project):
        """
        Update the last 5 records. Show that all ids are current, but the last 5 reflect updates.
        """
        self.update_fact_records(
            {"updated_at": "updated_at + interval 1 day"}, "id between 16 and 20"
        )
        run_dbt(["snapshot"])
        self._assert_results(
            ids_with_current_snapshot_records=range(1, 21),
            ids_with_closed_out_snapshot_records=range(16, 21),
        )

    def create_fact_from_seed(self, where: str = None):  # type: ignore
        to_table_name = relation_from_name(self.project.adapter, "fact")
        from_table_name = relation_from_name(self.project.adapter, "seed")
        where_clause = where
        sql = f"drop table if exists {to_table_name}"
        self.project.run_sql(sql)
        sql = f"""
            clone table {from_table_name} to {to_table_name}
        """
        self.project.run_sql(sql)
        sql = f"""
        delete from {to_table_name} where not({where_clause})
        """
        self.project.run_sql(sql)

    def test_new_column_captured_by_snapshot(self, project):
        """
        Add a column to `fact` and populate the last 10 records with a non-null value.
        Show that all ids are current, but the last 10 reflect updates and the first 10 don't
        i.e. if the column is added, but not updated, the record doesn't reflect that it's updated

        Same case as upstream with one difference: upstream spells the definition
        `varchar(200) default null`, and MaxCompute refuses to parse `DEFAULT NULL`
        in an `add column`/`add columns` statement (ParseError, on plain and on
        transactional tables alike).  A literal default (`default 'x'`) does parse -
        measured 2026-09-26, which is why the ephemeral snapshot cases keep
        upstream's own alter statement - so only the `null` form is replaced, and
        the column is added plain.  This used to be the second of two copies of the
        case in this class - the `varchar(200) default null` copy above shadowed
        nothing and ran nothing, and read like extra coverage that did not exist.
        """
        self.add_fact_column("full_name", "varchar(200)")
        self.update_fact_records(
            {
                "full_name": "first_name || ' ' || last_name",
                "updated_at": "updated_at + interval 1 day",
            },
            "id between 11 and 20",
        )
        run_dbt(["snapshot"])
        self._assert_results(
            ids_with_current_snapshot_records=range(1, 21),
            ids_with_closed_out_snapshot_records=range(11, 21),
        )


class TestSnapshotCheck(BaseSnapshotCheck):
    @pytest.fixture(scope="class")
    def seeds(self):
        """
        This seed file contains all records needed for tests, including records which will be inserted after the
        initial snapshot. This table will only need to be loaded once at the class level. It will never be altered, hence requires no further
        setup or teardown.
        """
        return {"seed.csv": seeds.SEED_CSV, "schema.yaml": schema_seed__yml}

    def create_fact_from_seed(self, where: str = None):  # type: ignore
        to_table_name = relation_from_name(self.project.adapter, "fact")
        from_table_name = relation_from_name(self.project.adapter, "seed")
        where_clause = where
        sql = f"drop table if exists {to_table_name}"
        self.project.run_sql(sql)
        sql = f"""
            clone table {from_table_name} to {to_table_name}
        """
        self.project.run_sql(sql)
        sql = f"""
        delete from {to_table_name} where not({where_clause})
        """
        self.project.run_sql(sql)

    def test_column_selection_is_reflected_in_snapshot(self, project):
        """
        Update the first 10 records on a non-tracked column.
        Update the middle 10 records on a tracked column. (hence records 6-10 are updated on both)
        Show that all ids are current, and only the tracked column updates are reflected in `snapshot`.
        """
        self.update_fact_records(
            {"last_name": "substr(last_name, 1, 3)"}, "id between 1 and 10"
        )  # not tracked
        self.update_fact_records(
            {"email": "substr(email, 1, 3)"}, "id between 6 and 15"
        )  # tracked
        run_dbt(["snapshot"])
        self._assert_results(
            ids_with_current_snapshot_records=range(1, 21),
            ids_with_closed_out_snapshot_records=range(6, 16),
        )

    pass
