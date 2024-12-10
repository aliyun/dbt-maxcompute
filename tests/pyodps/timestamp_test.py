from io import StringIO

import odps
import yaml
from odps import ODPS, options
import pandas as pd

seeds__data_datediff_csv = """string_text,length_expression,output,timestamp_ntz
abcdef,3,def,1981-05-20T06:46:51
fishtown,4,town,1981-05-20T06:46:51
december,5,ember,1981-05-20T06:46:51
december,0,,1981-05-20T06:46:51
"""


class TestTimestamp(object):
    def test_load_table_from_file(self):
        options.local_timezone = False
        data_io = StringIO(seeds__data_datediff_csv)
        pd_dataframe = pd.read_csv(data_io)
        o = self.get_test_odps_client()
        print(pd_dataframe)

        o.write_table(
            "timestamp_ntz_test",
            pd_dataframe,
            create_table=False,
            create_partition=False,
            lifecycle=1,
        )
        # AttributeError: 'pyarrow.lib.DataType' object has no attribute 'tz'

    def test_timezone(self):
        odps = self.get_test_odps_client()
        from odps import options

        options.local_timezone = False
        instance = odps.execute_sql("select current_timestamp();")

        with instance.open_reader() as reader:
            for record in reader:  # iterate to handle result with schema
                print(record)

    def get_test_odps_client(self) -> odps.ODPS:
        with open(
            "/Users/dingxin/pythonProject/dbt-maxcompute/dbt-maxcompute/dbt_profile.yml", "r"
        ) as file:
            data = yaml.safe_load(file)
            project = data.get("project")
            schema = data.get("schema")
            endpoint = data.get("endpoint")
            access_id = data.get("accessId")
            access_key = data.get("accessKey")

            o = ODPS(
                access_id,
                access_key,
                project=project,
                endpoint=endpoint,
            )
            o.schema = schema

            return o
