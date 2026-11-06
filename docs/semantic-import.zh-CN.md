# 将 Published 语义包导入 dbt 项目

`dbt run-operation dbt_maxcompute.import_semantic_package` 从指定的 MaxCompute Published revision 读取语义定义，检查实际表结构，并生成一个独立的 dbt Core 1.12 项目。这个项目使用 `source()` 引用已有数据，在用户指定的目标 schema 中构建视图。原加工 SQL 由原项目维护。

本功能是源码原型。进入适配器仓库后安装当前分支及配套工具。

```bash
python -m pip install -e '.[semantic]' 'dbt-core==1.12.6' 'maxc-cli==0.9.5b1'
```

## 读取并生成项目

使用已配置 MaxCompute profile 的任意 dbt 项目作为入口。全新环境可以先用 `dbt init semantic_bootstrap` 建立入口项目，并按[认证配置](authentication.md)准备 profile。入口项目可以没有模型。发布回执中的 `data.publishedRevisionId` 是下面的 `revision`。maxc 使用自身的连接配置读取 Published 版本和源表元数据。

```bash
dbt run-operation dbt_maxcompute.import_semantic_package --args '{
  "name": "sales",
  "revision": "<published_revision>",
  "output": "../sales_imported",
  "project_name": "sales_imported",
  "table_prefix": "sales_review",
  "time_spine_start": "2026-01-31",
  "time_spine_end": "2026-02-04"
}'
```

输出目录必须尚未存在，且其父目录已存在。相对路径以入口项目为准。指定独立的 `table_prefix` 作为生成视图的前缀。日期范围左闭右开，根据待验证数据选定。时间轴用来满足 dbt 和 MetricFlow 的日历配置，指标保留各自的业务时间列。

可以在 `args` 中加入 `config`、`namespace`、`maxc` 和 `user_agent`，沿用[正向导出](semantic-export.zh-CN.md)的 CLI 参数。运行结果包含实际 revision、快照摘要、指标与关系数量，`remoteMutations` 为 `false`。

## 审阅并构建

| 文件 | 内容 |
| --- | --- |
| `dbt_project.yml` | 项目配置和视图物化 |
| `profiles.yml.example` | 目标项目、schema、数据 endpoint 和凭证环境变量 |
| `models/sources.yml` | 语义包引用的物理表 |
| `models/*.sql` | 引用源表列的视图与日历模型 |
| `models/semantic.yml` | 指标、时间维度、实体关联和主键测试 |
| `conversion-report.md` | 来源 revision、实体与物理表映射、待审阅事项 |
| `published-package.json` | 原始快照，包含业务说明、术语和查询示例，默认由 Git 忽略 |

先审阅报告与 YAML，再根据模板设置 `MAXCOMPUTE_PROJECT`、`MAXCOMPUTE_SCHEMA`、`MAXCOMPUTE_ENDPOINT` 和 AccessKey 环境变量。目标连接需要读取源表及在开发 schema 创建视图的权限。profile 使用 MaxCompute 的数据 API endpoint。语义包的 Catalog endpoint 用于读取语义定义。

> **重要**：`dbt build` 会创建或替换指定别名的视图。请使用独立开发 schema 或未使用的前缀，并核对报告中的别名。

```bash
cd ../sales_imported
cp profiles.yml.example profiles.yml
export DBT_PROFILES_DIR="$PWD"
dbt parse
dbt build
```

如果已安装支持 MaxCompute 的 MetricFlow，可用实际查询复核口径。下面是[订单与客户示例](agent-semantic-package-tutorial.zh-CN.md)导回后的查询。验收使用 MaxCompute 试用版 MetricFlow 和 dbt-metricflow。[MetricFlow MaxCompute 试用版](https://github.com/dingxin-tech/metricflow-maxcompute/releases/tag/maxcompute-trial-20261022.2)

```bash
mf validate-configs
mf query --metrics paid_revenue,paid_orders,paid_aov
mf query --metrics paid_revenue,paid_orders,paid_aov --group-by metric_time__day
mf query --metrics paid_revenue,paid_orders,paid_aov --group-by customers__region
```

逻辑表名作为默认主实体名。可以在导入参数中指定 `"entity_names": {"orders": "order", "customers": "customer"}`，以保留已有的 `customer__region` 查询名。关系的基数由生成的主键唯一性、非空测试和真实关联查询共同验证。

## 审阅手工语义包

当前覆盖非分区表、单列主键、直接列维度，以及 simple 和同模型同时间维度的 ratio 指标。simple 支持 SUM、COUNT、COUNT_DISTINCT、AVG、MIN、MAX，行表达式支持直接列和 COALESCE。命名过滤器可引用同一逻辑表的分类维度。ratio 的公式采用 `分子 / NULLIF(分母, 0)`。

已批准的单列等值关联支持 LEFT 多对一，以及交换两侧后等价的 RIGHT 一对多。MetricFlow 可沿无歧义的路径访问两跳以内的维度。同一对表有多个连接键、存在环或有多条路径通向同一维表时，导入会停止并在错误信息中标明相关表。

同义词、枚举值、字段标签和用法说明写入 `config.meta.semantic_package`。当物理列名包含空格或特殊符号时，生成视图会引用原始带引号的列名，并投影为 dbt 可用的别名。维度名中的连续下划线会规范化，`year` 等 MetricFlow 保留词会改为 `dim_year` 等查询名。元数据保留原名称，命名过滤器和指标表达式随映射更新。

### 核实主键和时间候选

Agent 可先读取固定 Published revision 和表结构，再从字段定义中提出主键候选。用实际数据对照 `COUNT(*)`、`COUNT(候选列)` 和 `COUNT(DISTINCT 候选列)`，字符串键还需检查空白值。记录字段含义、覆盖率和歧义，核实后以 `primary_keys` 提供包中缺失的主键。导入器生成 `unique` 和 `not_null` 数据测试。跨表关系沿用包中已批准的关联。

对于只有复合粒度或尚无单列主键的模型，可显式提供 `"unkeyed_models": ["lap_times"]`。生成的模型保留维度命名空间和指向其他模型的外键。作为多对一关联目标的模型仍需要经核实的主键。原包只有维度和关系时，生成项目仍可用 dbt 构建和检查视图。需要实际执行 `mf query` 时，由业务方补充指标定义。

对时间候选，先检查非空数和取值范围，再判断时间代表业务事件、状态变化还是记录维护。原生 DATE 列可保留为日粒度维度。只有包中明确的默认时间或经审阅的 `time_mapping` 会成为业务聚合时间。下面的示例为订单指定缺失的主键和已确认的聚合时间。

```json
{
  "primary_keys": {"orders": "order_id"},
  "time_mapping": {
    "orders.ordered_at": {"timezone": "UTC", "granularity": "day", "default": true}
  }
}
```

业务时间仍待确认时，可把对应逻辑表加入 `"totals_only": ["orders"]`，以验证总量。该选项生成值为 NULL 的技术日期来满足 MetricFlow 的计划要求，保留原有日期维度供审阅。转换报告会记录这一选择，正向导出要求先补齐业务时间映射。原包未声明的指标可见性标为待审阅。

原文说明、未绑定指标的命名过滤器和已验证查询留在本地快照中。分区策略、累计和窗口指标、SCD、复合关联键、跨模型比率和额外 JOIN 条件会报出阻塞字段。物理类型与声明不一致时，先核实数据并修订包定义或提供类型匹配的源视图。例如声明为 DATE 的 STRING 列需要先确认日期格式。

**空结果计数**。验收的 MetricFlow 版本将 COUNT 编译为对 0/1 表达式求和，输入为空时返回 NULL。原始 dbt 项目和导回后的项目行为一致，MaxCompute 的 SQL COUNT 基准返回 0。客单价在两边均为 NULL。请把空输入结果列为迁移验收用例。

将生成项目再次导出时，提案会引用 dbt 建出的视图，原始表绑定仍可在 `sources.yml` 中查看。审阅往返结果时，分别核对物理引用、指标公式、过滤条件、时间约定、关联关系及查询结果。
