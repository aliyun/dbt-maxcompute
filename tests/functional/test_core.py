"""
Core Test Suite for dbt-maxcompute

This module contains hand-written tests that cover all materialization strategies
and core functionality of the dbt-maxcompute adapter.

Each test class documents:
1. 测试场景 (Test Scenario): What functionality is being tested
2. 初始状态 (Initial State): What tables/data exist before the test
3. ETL 过程 (ETL Process): What dbt operations are performed
4. 结果表 (Result Tables): What the final state should look like

测试类索引 (Test Class Index):
1. TestTableMaterialization - 基础物理表物化
2. TestViewMaterialization - 视图物化
3. TestIncrementalMaterialization - 增量物化 (append 策略)
4. TestIncrementalPartition - 分区表增量物化 (insert_overwrite 策略)
5. TestIncrementalAutoPartition - 自动分区表增量物化
6. TestIncrementalPartitionFilter - 带分区过滤的增量物化
7. TestIncrementalMerge - 增量物化 (merge 策略)
8. TestMaterializedView - 物化视图
9. TestPartitionTable - 分区表创建
10. TestDeltaTable - 事务表 (Delta Table)
11. TestSnapshot - 快照物化
12. TestEphemeralMaterialization - 临时物化 (CTE)
"""

import pytest
from dbt.tests.util import run_dbt, check_relations_equal, get_relation_columns


# ==============================================================================
# Fixtures
# ==============================================================================

SEED_SAMPLE_CSV = """
id,name,value,updated_at
1,Alice,100,2024-01-01 00:00:00
2,Bob,200,2024-01-02 00:00:00
3,Charlie,300,2024-01-03 00:00:00
4,Diana,400,2024-01-04 00:00:00
5,Eve,500,2024-01-05 00:00:00
""".strip()

SCHEMA_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: sample
        identifier: "{{ var('seed_name', 'sample') }}"
"""


# ==============================================================================
# 1. Table Materialization
# ==============================================================================
#
# 测试场景: 基础物理表物化
#
# 初始状态:
#   - seed 表 sample (5行数据: id 1-5)
#
# ETL 过程:
#   1. dbt seed: 加载种子数据到 sample 表
#   2. dbt run: 创建 my_table 表，SQL 中过滤 id <= 3
#   3. 再次 dbt run: 验证幂等性（表应被重建，数据不变）
#
# 结果表:
#   - my_table: 物理表，包含 3 行数据 (id: 1, 2, 3)
#
# ==============================================================================

MODEL_TABLE_SQL = """
{{ config(materialized='table') }}
select * from {{ source('raw', 'sample') }}
where id <= 3
"""


@pytest.mark.core_test
class TestTableMaterialization:
    """
    测试物理表物化策略
    
    场景: 创建一个简单的物理表，验证创建和幂等性
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_table.sql": MODEL_TABLE_SQL,
            "schema.yml": SCHEMA_YML,
        }

    def test_table_create(self, project):
        # First run - create table
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 1

        # Verify table exists and has correct data
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="my_table",
        )
        assert relation is not None
        assert relation.type == "table"

        # Check row count
        result = project.run_sql("select count(*) from my_table", fetch="one")
        assert result[0] == 3

    def test_table_rerun_idempotent(self, project):
        # Run twice - should be idempotent
        run_dbt(["seed"])
        run_dbt(["run"])
        results = run_dbt(["run"])
        assert len(results) == 1

        result = project.run_sql("select count(*) from my_table", fetch="one")
        assert result[0] == 3


# ==============================================================================
# 2. View Materialization
# ==============================================================================
#
# 测试场景: 视图物化
#
# 初始状态:
#   - seed 表 sample (5行数据: id 1-5)
#
# ETL 过程:
#   1. dbt seed: 加载种子数据
#   2. dbt run: 创建 my_view 视图，SQL 中过滤 id > 2
#   3. 再次 dbt run: 验证幂等性（视图应被重建）
#
# 结果表:
#   - my_view: 视图，查询时返回 3 行数据 (id: 3, 4, 5)
#   - 视图不存储数据，每次查询动态计算
#
# ==============================================================================

MODEL_VIEW_SQL = """
{{ config(materialized='view') }}
select * from {{ source('raw', 'sample') }}
where id > 2
"""


@pytest.mark.core_test
class TestViewMaterialization:
    """
    测试视图物化策略
    
    场景: 创建一个视图，验证视图查询正确性和幂等性
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_view.sql": MODEL_VIEW_SQL,
            "schema.yml": SCHEMA_YML,
        }

    def test_view_create(self, project):
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 1

        # Verify view exists
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="my_view",
        )
        assert relation is not None
        assert relation.type == "view"

        # Check row count (should have 3 rows: id 3, 4, 5)
        result = project.run_sql("select count(*) from my_view", fetch="one")
        assert result[0] == 3

    def test_view_rerun_idempotent(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        results = run_dbt(["run"])
        assert len(results) == 1


# ==============================================================================
# 3. Incremental Materialization (Append Strategy)
# ==============================================================================
#
# 测试场景: 增量物化 - 追加策略 (append)
#
# 初始状态:
#   - seed 表 sample (事务表, 5行数据: id 1-5)
#
# ETL 过程:
#   1. dbt seed: 加载种子数据到 sample 表 (事务表，支持 INSERT)
#   2. dbt run (首次): 创建 my_incremental 表，加载全部 5 行数据
#   3. INSERT INTO sample: 手动插入新数据 (id=6, Frank)
#   4. dbt run (增量): 只追加 id > max(id) 的新数据
#
# 结果表:
#   - my_incremental: 物理表，首次 5 行，增量后 6 行
#   - 使用 append 策略，新数据追加到表末尾
#
# 适用场景:
#   - 数据只增不改的历史数据表
#   - 不需要去重的场景
#
# ==============================================================================

# Seed must be transactional to support INSERT operations
SEED_INCREMENTAL_SCHEMA_YML = """
version: 2
seeds:
  - name: sample
    config:
      transactional: true
"""

# Use 'append' strategy for non-partitioned table
MODEL_INCREMENTAL_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='append'
) }}

select * from {{ ref('sample') }}

{% if is_incremental() %}
where id > (select max(id) from {{ this }})
{% endif %}
"""


@pytest.mark.core_test
class TestIncrementalMaterialization:
    """
    测试增量物化策略 - append 追加模式
    
    场景: 
    - 首次运行加载全量数据
    - 后续运行只追加新增数据（基于 id > max(id) 条件）
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_incremental.sql": MODEL_INCREMENTAL_SQL,
            "seeds.yml": SEED_INCREMENTAL_SCHEMA_YML,
        }

    def test_incremental_create_and_update(self, project):
        # Initial run
        run_dbt(["seed"])
        run_dbt(["run"])
        result = project.run_sql("select count(*) from my_incremental", fetch="one")
        assert result[0] == 5

        # Insert new data into source (seed table is transactional)
        # Use explicit CAST for timestamp type
        project.run_sql(
            "insert into sample (id, name, value, updated_at) values (6, 'Frank', 600, CAST('2024-01-06 00:00:00' AS TIMESTAMP))"
        )

        # Incremental run
        run_dbt(["run"])
        result = project.run_sql("select count(*) from my_incremental", fetch="one")
        assert result[0] == 6


# ==============================================================================
# 4. Incremental on Partition Table (insert_overwrite Strategy)
# ==============================================================================
#
# 测试场景: 分区表增量物化 - insert_overwrite 策略
#
# 初始状态:
#   - seed 表 events (8行数据，分布在 3 个日期分区)
#     - 2024-01-01: 3 行 (login, click, purchase)
#     - 2024-01-02: 3 行
#     - 2024-01-03: 2 行
#
# ETL 过程:
#   1. dbt seed: 加载种子数据到 events 表
#   2. dbt run (首次):
#      - 创建 source_events 分区表（按 event_date 分区）
#      - 创建 fact_events 增量分区表，加载全部数据
#   3. dbt run (增量): 幂等性验证
#      - 由于没有新分区，数据应保持不变
#
# 结果表:
#   - source_events: 分区表，3 个分区，8 行数据
#   - fact_events: 增量分区表，3 个分区，8 行数据
#
# 增量策略说明:
#   - insert_overwrite: 只覆盖有新数据的分区，不影响历史分区
#   - where 条件: 只处理源表中存在但目标表中不存在的分区
#
# 适用场景:
#   - 每日数据增量加载
#   - 分区级别的数据更新
#
# ==============================================================================

# Source data: raw events with date - initial load
SEED_EVENTS_CSV = """
event_id,event_name,event_value,event_date
1,login,100,2024-01-01
2,click,200,2024-01-01
3,purchase,300,2024-01-01
4,login,150,2024-01-02
5,click,250,2024-01-02
6,purchase,350,2024-01-02
7,login,180,2024-01-03
8,click,280,2024-01-03
""".strip()

# Source table must be partitioned for incremental partition overwrite
SOURCE_EVENT_SQL = """
{{ config(
    materialized='table',
    partition_by={"fields": "event_date", "data_types": "string"}
) }}
select
    event_id,
    event_name,
    event_value,
    event_date
from {{ source('raw', 'events') }}
"""

# Incremental model: partitioned table with incremental overwrite
# Strategy: Only overwrite partitions that have new data
MODEL_INCREMENTAL_PARTITION_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={"fields": "event_date", "data_types": "string"},
    require_partition_filter=True
) }}

select
    event_id,
    event_name,
    event_value,
    event_date

from {{ ref('source_events') }}

{% if is_incremental() %}
-- Only process partitions that exist in source but not yet in target
where event_date in (
    select distinct event_date from {{ ref('source_events') }}
    where event_date not in (
        select distinct event_date from {{ this }}
    )
)
{% endif %}
"""

SCHEMA_EVENTS_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: events
        identifier: "{{ var('seed_name', 'events') }}"
"""


@pytest.mark.core_test
class TestIncrementalPartition:
    """
    测试分区表增量物化 - insert_overwrite 策略
    
    场景: 
    - 源表按日期分区
    - 目标表增量加载，只处理新分区
    - 历史分区数据不变
    
    这是最常见的 ETL 模式：每日增量分区加载
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"events.csv": SEED_EVENTS_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "source_events.sql": SOURCE_EVENT_SQL,
            "fact_events.sql": MODEL_INCREMENTAL_PARTITION_SQL,
            "schema.yml": SCHEMA_EVENTS_YML,
        }

    def test_incremental_partition_initial_load(self, project):
        """
        Initial load: Create partitioned table with all existing data.
        Expected: 3 partitions (2024-01-01, 2024-01-02, 2024-01-03)
        """
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 2

        # Verify table exists and is partitioned
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="fact_events",
        )
        assert relation is not None

        # Check total row count (8 rows across 3 partitions)
        result = project.run_sql("select count(*) from fact_events", fetch="one")
        assert result[0] == 8

        # Check partition count - show partitions returns a tuple with all partitions
        partitions = project.run_sql("show partitions fact_events", fetch="all")
        # partitions format: [('event_date=2024-01-01\nevent_date=2024-01-02\nevent_date=2024-01-03',)]
        partition_text = partitions[0][0] if partitions else ""
        partition_count = len([p for p in partition_text.split("\n") if p.strip()])
        assert partition_count == 3

    def test_incremental_partition_rerun_idempotent(self, project):
        """
        Re-run: Should be idempotent when no new partitions.
        Expected: Same partitions and row count.
        """
        run_dbt(["seed"])
        run_dbt(["run"])

        # Get initial state
        initial_count = project.run_sql("select count(*) from fact_events", fetch="one")
        partitions = project.run_sql("show partitions fact_events", fetch="all")
        partition_text = partitions[0][0] if partitions else ""
        initial_partition_count = len([p for p in partition_text.split("\n") if p.strip()])

        # Run again - should be idempotent
        run_dbt(["run"])

        # Verify unchanged
        result = project.run_sql("select count(*) from fact_events", fetch="one")
        assert result[0] == initial_count[0]

        partitions = project.run_sql("show partitions fact_events", fetch="all")
        partition_text = partitions[0][0] if partitions else ""
        partition_count = len([p for p in partition_text.split("\n") if p.strip()])
        assert partition_count == initial_partition_count


# ==============================================================================
# 5. Incremental on Auto Partition Table (insert_overwrite Strategy)
# ==============================================================================
#
# 测试场景: 自动分区表增量物化 - insert_overwrite 策略
#
# 初始状态:
#   - seed 表 auto_events (8行数据，带时间戳 event_ts)
#     - 2024-01-01: 3 行
#     - 2024-01-02: 3 行
#     - 2024-01-03: 2 行
#
# ETL 过程:
#   1. dbt seed: 加载种子数据
#   2. dbt run (首次):
#      - 创建 source_auto_partition 普通表
#      - 创建 fact_auto_partition_day (按天自动分区)
#      - 创建 fact_auto_partition_month (按月自动分区)
#   3. dbt run (增量): 幂等性验证
#
# 结果表:
#   - fact_auto_partition_day: 
#     - 按天粒度自动分区
#     - 3 个分区: 20240101, 20240102, 20240103
#     - 8 行数据
#   - fact_auto_partition_month:
#     - 按月粒度自动分区
#     - 1 个分区: 202401
#     - 8 行数据
#
# 自动分区说明:
#   - granularity="day": 自动按天创建分区，分区名如 20240101
#   - granularity="month": 自动按月创建分区，分区名如 202401
#   - partition_by 中指定 timestamp 类型列，系统自动提取分区值
#
# 适用场景:
#   - 时间序列数据，按时间粒度分区
#   - 日志数据、事件数据等
#
# ==============================================================================

SEED_AUTO_PARTITION_CSV = """
event_id,event_name,event_value,event_ts
1,login,100,2024-01-01 10:00:00
2,click,200,2024-01-01 11:00:00
3,purchase,300,2024-01-01 12:00:00
4,login,150,2024-01-02 09:00:00
5,click,250,2024-01-02 10:00:00
6,purchase,350,2024-01-02 11:00:00
7,login,180,2024-01-03 08:00:00
8,click,280,2024-01-03 09:00:00
""".strip()

# Source table with timestamp column
SOURCE_AUTO_PARTITION_SQL = """
{{ config(
    materialized='table'
) }}
select
    event_id,
    event_name,
    event_value,
    event_ts
from {{ source('raw', 'auto_events') }}
"""

# Incremental model with auto partition by day
MODEL_INCREMENTAL_AUTO_PARTITION_DAY_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={
        "fields": "event_ts",
        "data_types": "timestamp",
        "granularity": "day"
    }
) }}

select
    event_id,
    event_name,
    event_value,
    event_ts

from {{ ref('source_auto_partition') }}
"""

# Incremental model with auto partition by month
MODEL_INCREMENTAL_AUTO_PARTITION_MONTH_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={
        "fields": "event_ts",
        "data_types": "timestamp",
        "granularity": "month"
    }
) }}

select
    event_id,
    event_name,
    event_value,
    event_ts

from {{ ref('source_auto_partition') }}
"""

SCHEMA_AUTO_PARTITION_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: auto_events
        identifier: "{{ var('seed_name', 'auto_events') }}"
"""


@pytest.mark.core_test
class TestIncrementalAutoPartition:
    """
    测试自动分区表增量物化 - insert_overwrite 策略
    
    场景: 
    - 时间戳列自动转换为分区
    - 支持不同粒度: day/month/hour
    - 适用于时间序列数据的自动分区管理
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"auto_events.csv": SEED_AUTO_PARTITION_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "source_auto_partition.sql": SOURCE_AUTO_PARTITION_SQL,
            "fact_auto_partition_day.sql": MODEL_INCREMENTAL_AUTO_PARTITION_DAY_SQL,
            "fact_auto_partition_month.sql": MODEL_INCREMENTAL_AUTO_PARTITION_MONTH_SQL,
            "schema.yml": SCHEMA_AUTO_PARTITION_YML,
        }

    def test_incremental_auto_partition_initial_load(self, project):
        """
        Initial load: Create tables with auto partitions.
        - Day granularity: 3 partitions (2024-01-01, 2024-01-02, 2024-01-03)
        - Month granularity: 1 partition (2024-01)
        """
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 3

        # Verify day partitioned table
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="fact_auto_partition_day",
        )
        assert relation is not None

        # Check row count for day partitioned table
        result = project.run_sql(
            "select count(*) from fact_auto_partition_day", fetch="one"
        )
        assert result[0] == 8

        # Check partitions for day granularity (should have 3 daily partitions)
        partitions = project.run_sql(
            "show partitions fact_auto_partition_day", fetch="all"
        )
        partition_text = partitions[0][0] if partitions else ""
        day_partition_count = len([p for p in partition_text.split("\n") if p.strip()])
        assert day_partition_count == 3

        # Verify month partitioned table
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="fact_auto_partition_month",
        )
        assert relation is not None

        # Check row count for month partitioned table
        result = project.run_sql(
            "select count(*) from fact_auto_partition_month", fetch="one"
        )
        assert result[0] == 8

        # Check partitions for month granularity (should have 1 monthly partition)
        partitions = project.run_sql(
            "show partitions fact_auto_partition_month", fetch="all"
        )
        partition_text = partitions[0][0] if partitions else ""
        month_partition_count = len([p for p in partition_text.split("\n") if p.strip()])
        assert month_partition_count == 1

    def test_incremental_auto_partition_rerun_idempotent(self, project):
        """
        Re-run: Should be idempotent when no new data.
        """
        run_dbt(["seed"])
        run_dbt(["run"])

        # Get initial state
        initial_count_day = project.run_sql(
            "select count(*) from fact_auto_partition_day", fetch="one"
        )
        initial_count_month = project.run_sql(
            "select count(*) from fact_auto_partition_month", fetch="one"
        )

        # Run again - should be idempotent
        run_dbt(["run"])

        # Verify unchanged
        result_day = project.run_sql(
            "select count(*) from fact_auto_partition_day", fetch="one"
        )
        result_month = project.run_sql(
            "select count(*) from fact_auto_partition_month", fetch="one"
        )
        assert result_day[0] == initial_count_day[0]
        assert result_month[0] == initial_count_month[0]


# ==============================================================================
# 6. Incremental with Partition Filter (insert_overwrite Strategy)
# ==============================================================================
#
# 测试场景: 分区过滤增量物化 - insert_overwrite 策略
#
# 初始状态:
#   - seed 表 orders (6行数据，分布在 3 个日期分区)
#     - 2024-01-01: 2 行
#     - 2024-01-02: 2 行
#     - 2024-01-03: 2 行
#
# ETL 过程:
#   1. dbt seed: 加载种子数据
#   2. dbt run (首次):
#      - 创建 source_partition_filter 分区表
#      - 创建 fact_orders 增量表，加载全部 6 行
#   3. dbt run (增量):
#      - 由于 where order_date >= '2024-01-02' 条件
#      - 只处理 2024-01-02 和 2024-01-03 分区
#      - 但因为没有新数据，结果保持幂等
#
# 结果表:
#   - fact_orders: 分区表，3 个分区，6 行数据
#   - 增量运行时只处理满足过滤条件的分区
#
# 分区过滤说明:
#   - 通过 WHERE 子句控制增量运行处理的分区范围
#   - 首次运行不受过滤条件影响，加载全量数据
#   - 后续增量运行只处理满足条件的分区
#
# 适用场景:
#   - 只回填最近 N 天的数据
#   - 跳过历史分区，只处理活跃分区
#   - 分区级别的数据修复
#
# ==============================================================================

SEED_PARTITION_FILTER_CSV = """
order_id,customer_id,order_amount,order_date
1,C001,100,2024-01-01
2,C002,200,2024-01-01
3,C003,150,2024-01-02
4,C001,300,2024-01-02
5,C002,250,2024-01-03
6,C003,180,2024-01-03
""".strip()

SOURCE_PARTITION_FILTER_SQL = """
{{ config(
    materialized='table',
    partition_by={"fields": "order_date", "data_types": "string"}
) }}
select
    order_id,
    customer_id,
    order_amount,
    order_date
from {{ source('raw', 'orders') }}
"""

# Incremental model with explicit partition filter
# This demonstrates how to use incremental_predicates for fine-grained control
MODEL_INCREMENTAL_PARTITION_FILTER_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by={"fields": "order_date", "data_types": "string"}
) }}

select
    order_id,
    customer_id,
    order_amount,
    order_date

from {{ ref('source_partition_filter') }}

{% if is_incremental() %}
-- Filter to only process certain partitions
where order_date >= '2024-01-02'
{% endif %}
"""

SCHEMA_PARTITION_FILTER_YML = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    tables:
      - name: orders
        identifier: "{{ var('seed_name', 'orders') }}"
"""


@pytest.mark.core_test
class TestIncrementalPartitionFilter:
    """
    测试带分区过滤的增量物化 - insert_overwrite 策略
    
    场景: 
    - 通过 WHERE 条件控制增量运行处理的分区范围
    - 首次运行加载全量，后续运行只处理指定分区
    - 适用于分区级别的数据修复和回填
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"orders.csv": SEED_PARTITION_FILTER_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "source_partition_filter.sql": SOURCE_PARTITION_FILTER_SQL,
            "fact_orders.sql": MODEL_INCREMENTAL_PARTITION_FILTER_SQL,
            "schema.yml": SCHEMA_PARTITION_FILTER_YML,
        }

    def test_incremental_partition_filter_initial(self, project):
        """
        Initial load: Process all data (filter not applied on first run).
        """
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 2

        # Check total row count
        result = project.run_sql("select count(*) from fact_orders", fetch="one")
        assert result[0] == 6

        # Check partition count
        partitions = project.run_sql("show partitions fact_orders", fetch="all")
        partition_text = partitions[0][0] if partitions else ""
        partition_count = len([p for p in partition_text.split("\n") if p.strip()])
        assert partition_count == 3

    def test_incremental_partition_filter_rerun(self, project):
        """
        Re-run: With filter, should still be idempotent for existing data.
        """
        run_dbt(["seed"])
        run_dbt(["run"])

        # Get initial state
        initial_count = project.run_sql("select count(*) from fact_orders", fetch="one")

        # Run again
        run_dbt(["run"])

        # Should be idempotent
        result = project.run_sql("select count(*) from fact_orders", fetch="one")
        assert result[0] == initial_count[0]


# ==============================================================================
# 7. Incremental with Unique Key (Merge Strategy)
# ==============================================================================
#
# 测试场景: 增量物化 - merge 合并策略
#
# 初始状态:
#   - seed 表 sample (事务表, 5行数据)
#     - id=1, Alice, value=100
#     - id=2, Bob, value=200
#     - ... (共5行)
#
# ETL 过程:
#   1. dbt seed: 加载种子数据到 sample 表
#   2. dbt run (首次): 创建 my_merge 表，加载全部 5 行
#   3. INSERT INTO sample: 插入新数据 (id=6, Frank)
#   4. UPDATE sample: 更新已有数据 (id=1, value=999)
#   5. dbt run (增量): 执行 merge 操作
#      - 新数据 (id=6) 被插入
#      - 已有数据 (id=1) 被更新
#
# 结果表:
#   - my_merge: 事务表，6 行数据
#     - id=1 的 value 已更新为 999
#     - id=6 为新插入的数据
#
# Merge 策略说明:
#   - unique_key='id': 指定唯一键用于匹配记录
#   - 新记录: 插入
#   - 已存在记录: 更新
#   - 需要目标表是事务表 (transactional=true)
#
# 适用场景:
#   - 维度表更新（客户信息、产品信息等）
#   - 需要 upsert 语义的场景
#   - 数据去重合并
#
# ==============================================================================

# Seed must be transactional to support INSERT/UPDATE operations
SEED_MERGE_SCHEMA_YML = """
version: 2
seeds:
  - name: sample
    config:
      transactional: true
"""

MODEL_INCREMENTAL_MERGE_SQL = """
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key='id'
) }}

select * from {{ ref('sample') }}
"""


@pytest.mark.core_test
class TestIncrementalMerge:
    """
    测试增量物化 - merge 合并策略
    
    场景: 
    - 基于唯一键合并数据
    - 新记录插入，已存在记录更新
    - 实现 upsert 语义
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_merge.sql": MODEL_INCREMENTAL_MERGE_SQL,
            "seeds.yml": SEED_MERGE_SCHEMA_YML,
        }

    def test_incremental_merge(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])

        # Initial count
        result = project.run_sql("select count(*) from my_merge", fetch="one")
        assert result[0] == 5

        # Insert new row (with explicit CAST for timestamp)
        project.run_sql(
            "insert into sample (id, name, value, updated_at) values (6, 'Frank', 600, CAST('2024-01-06 00:00:00' AS TIMESTAMP))"
        )
        # Update existing row
        project.run_sql("update sample set value = 999 where id = 1")

        run_dbt(["run"])

        # Check new row added
        result = project.run_sql("select count(*) from my_merge", fetch="one")
        assert result[0] == 6

        # Check updated value
        result = project.run_sql("select value from my_merge where id = 1", fetch="one")
        assert result[0] == 999


# ==============================================================================
# 8. Materialized View (物化视图)
# ==============================================================================
#
# 测试场景: 物化视图物化
#
# 初始状态:
#   - seed 表 sample (5行数据)
#
# ETL 过程:
#   1. dbt seed: 加载种子数据
#   2. dbt run: 创建 my_mv 物化视图
#   3. dbt run (再次): 验证幂等性
#
# 结果表:
#   - my_mv: 物化视图类型
#   - 查询结果为 id, name, value 三列，5 行数据
#
# 物化视图说明:
#   - 物化视图是预先计算并存储结果的数据库对象
#   - 与普通视图不同，物化视图存储实际数据
#   - 查询性能更好，但需要刷新策略
#   - MaxCompute 物化视图支持自动/手动刷新
#
# 适用场景:
#   - 频繁查询的聚合结果
#   - 复杂查询的性能优化
#   - 数据仓库中的汇总表
#
# ==============================================================================

MODEL_MATERIALIZED_VIEW_SQL = """
{{ config(materialized='materialized_view') }}
select id, name, value from {{ source('raw', 'sample') }}
"""


@pytest.mark.core_test
class TestMaterializedView:
    """
    测试物化视图物化策略
    
    场景: 
    - 创建物化视图，存储预计算结果
    - 验证物化视图类型正确
    - 验证重新运行的幂等性
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_mv.sql": MODEL_MATERIALIZED_VIEW_SQL,
            "schema.yml": SCHEMA_YML,
        }

    def test_materialized_view_create(self, project):
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 1

        # Verify materialized view exists
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="my_mv",
        )
        assert relation is not None
        assert relation.type == "materialized_view"

    def test_materialized_view_rerun(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])
        results = run_dbt(["run"])
        assert len(results) == 1


# ==============================================================================
# 7. Partition Table
# ==============================================================================

MODEL_PARTITION_SQL = """
{{ config(
    materialized='table',
    partition_by={"fields": "name", "data_types": "string"}
) }}
select id, name, value, updated_at from {{ source('raw', 'sample') }}
"""


@pytest.mark.core_test
class TestPartitionTable:
    """Test MaxCompute partition table"""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_partition.sql": MODEL_PARTITION_SQL,
            "schema.yml": SCHEMA_YML,
        }

    def test_partition_table_create(self, project):
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 1

        # Verify table is partitioned
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="my_partition",
        )
        assert relation is not None

        # Check partition info
        partitions = project.run_sql("show partitions my_partition", fetch="all")
        assert len(partitions) > 0


# ==============================================================================
# 8. Delta Table (Transactional Table with Primary Keys)
# ==============================================================================

MODEL_DELTA_SQL = """
{{ config(
    materialized='table',
    transactional=true,
    primary_keys=['id']
) }}
select id, name, value, updated_at from {{ source('raw', 'sample') }}
"""


@pytest.mark.core_test
class TestDeltaTable:
    """Test MaxCompute Delta/transactional table"""

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_delta.sql": MODEL_DELTA_SQL,
            "schema.yml": SCHEMA_YML,
        }

    def test_delta_table_create(self, project):
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 1

        # Verify table exists
        relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="my_delta",
        )
        assert relation is not None
        assert relation.type == "table"

    def test_delta_table_support_update_delete(self, project):
        run_dbt(["seed"])
        run_dbt(["run"])

        # Test update on transactional table
        project.run_sql("update my_delta set value = 1000 where id = 1")
        result = project.run_sql("select value from my_delta where id = 1", fetch="one")
        assert result[0] == 1000

        # Test delete on transactional table
        project.run_sql("delete from my_delta where id = 5")
        result = project.run_sql("select count(*) from my_delta", fetch="one")
        assert result[0] == 4


# ==============================================================================
# 9. Snapshot Materialization
# ==============================================================================
#
# 测试场景: 快照物化 - 历史数据追踪
#
# 初始状态:
#   - seed 表 sample (事务表, 3行数据)
#     - id=1, Alice, value=100, updated_at=2024-01-01
#     - id=2, Bob, value=200, updated_at=2024-01-02
#     - id=3, Charlie, value=300, updated_at=2024-01-03
#
# ETL 过程:
#   1. dbt seed: 加载种子数据
#   2. dbt snapshot (首次): 创建 my_snapshot 快照表
#      - 记录当前状态，3 条记录
#   3. UPDATE sample: 修改 id=1 的记录
#      - value=999, updated_at=2024-02-01
#   4. dbt snapshot (增量): 检测变化
#      - 为 id=1 创建新版本记录
#      - 保留历史版本
#
# 结果表:
#   - my_snapshot: 快照表
#     - 首次运行: 3 条记录
#     - 增量运行: 4 条记录 (id=1 有 2 个版本)
#     - 包含 dbt 生成的元数据列:
#       - dbt_scd_id: 记录唯一标识
#       - dbt_updated_at: 更新时间
#       - dbt_valid_from: 有效起始时间
#       - dbt_valid_to: 有效结束时间
#
# 快照策略说明:
#   - strategy='timestamp': 基于时间戳检测变化
#   - updated_at='updated_at': 指定用于检测变化的列
#   - unique_key='id': 记录唯一标识
#
# 适用场景:
#   - 数据变更历史追踪
#   - 审计日志
#   - 数据回溯分析
#
# ==============================================================================

SNAPSHOT_SQL = """
{% snapshot my_snapshot %}

{{
    config(
      target_schema=schema,
      unique_key='id',
      strategy='timestamp',
      updated_at='updated_at',
    )
}}

select * from {{ ref('sample') }}

{% endsnapshot %}
"""

SEED_SNAPSHOT_CSV = """
id,name,value,updated_at
1,Alice,100,2024-01-01 00:00:00
2,Bob,200,2024-01-02 00:00:00
3,Charlie,300,2024-01-03 00:00:00
""".strip()

# Seed must be transactional to support UPDATE operations
SEED_SNAPSHOT_SCHEMA_YML = """
version: 2
seeds:
  - name: sample
    config:
      transactional: true
"""


@pytest.mark.core_test
class TestSnapshot:
    """
    测试快照物化
    
    场景: 
    - 首次快照记录当前状态
    - 数据变更后快照记录新版本
    - 保留完整的历史版本
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SNAPSHOT_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {"seeds.yml": SEED_SNAPSHOT_SCHEMA_YML}

    @pytest.fixture(scope="class")
    def snapshots(self):
        return {"my_snapshot.sql": SNAPSHOT_SQL}

    def test_snapshot_create(self, project):
        run_dbt(["seed"])
        results = run_dbt(["snapshot"])
        assert len(results) == 1

        # Verify snapshot table exists
        result = project.run_sql("select count(*) from my_snapshot", fetch="one")
        assert result[0] == 3

    def test_snapshot_capture_changes(self, project):
        run_dbt(["seed"])
        run_dbt(["snapshot"])

        # Update a record (seed table is transactional, use explicit CAST)
        project.run_sql("update sample set value = 999, updated_at = CAST('2024-02-01 00:00:00' AS TIMESTAMP) where id = 1")

        # Run snapshot again
        run_dbt(["snapshot"])

        # Should have 4 records now (3 original + 1 updated)
        result = project.run_sql("select count(*) from my_snapshot", fetch="one")
        assert result[0] == 4

        # Check that we have both versions of id=1
        result = project.run_sql(
            "select count(*) from my_snapshot where id = 1", fetch="one"
        )
        assert result[0] == 2


# ==============================================================================
# 10. Ephemeral Materialization
# ==============================================================================
#
# 测试场景: 临时物化 (CTE - Common Table Expression)
#
# 初始状态:
#   - seed 表 sample (5行数据)
#
# ETL 过程:
#   1. dbt seed: 加载种子数据
#   2. dbt run: 
#      - my_ephemeral (临时模型): 不创建实际表，只作为 CTE
#      - using_ephemeral: 引用 my_ephemeral，将其 SQL 内联为 CTE
#
# 结果表:
#   - my_ephemeral: 不存在（临时模型不创建物理表）
#   - using_ephemeral: 物理表，3 行数据
#     - 其 SQL 实际执行时包含 my_ephemeral 的 CTE
#
# Ephemeral 物化说明:
#   - 不创建物理表或视图
#   - SQL 被内联到引用它的模型中作为 CTE
#   - 适用于中间逻辑抽象，避免创建不必要的对象
#
# 执行流程:
#   1. my_ephemeral 定义了 id <= 3 的过滤逻辑
#   2. using_ephemeral 引用 my_ephemeral
#   3. 最终执行的 SQL 等价于:
#      WITH my_ephemeral AS (
#        SELECT * FROM sample WHERE id <= 3
#      )
#      SELECT * FROM my_ephemeral
#
# 适用场景:
#   - 复杂逻辑的中间步骤抽象
#   - 可复用的数据过滤逻辑
#   - 减少物理对象数量
#
# ==============================================================================

MODEL_EPHEMERAL_SQL = """
{{ config(materialized='ephemeral') }}
select * from {{ source('raw', 'sample') }}
where id <= 3
"""

MODEL_USING_EPHEMERAL_SQL = """
select * from {{ ref('my_ephemeral') }}
"""


@pytest.mark.core_test
class TestEphemeralMaterialization:
    """
    测试临时物化 (Ephemeral / CTE)
    
    场景: 
    - 临时模型不创建物理表
    - 其 SQL 被内联到引用模型中
    - 验证 CTE 逻辑正确工作
    """

    @pytest.fixture(scope="class")
    def seeds(self):
        return {"sample.csv": SEED_SAMPLE_CSV}

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "my_ephemeral.sql": MODEL_EPHEMERAL_SQL,
            "using_ephemeral.sql": MODEL_USING_EPHEMERAL_SQL,
            "schema.yml": SCHEMA_YML,
        }

    def test_ephemeral_as_cte(self, project):
        run_dbt(["seed"])
        results = run_dbt(["run"])
        assert len(results) == 1  # Only using_ephemeral is materialized

        # Verify ephemeral model is NOT materialized
        ephemeral_relation = project.adapter.get_relation(
            database=project.database,
            schema=project.test_schema,
            identifier="my_ephemeral",
        )
        assert ephemeral_relation is None

        # Verify using_ephemeral has correct data (filtered by ephemeral)
        result = project.run_sql("select count(*) from using_ephemeral", fetch="one")
        assert result[0] == 3