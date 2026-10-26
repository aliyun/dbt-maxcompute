# 将 dbt 语义定义导出到 MaxAgent 语义包

本功能复用 dbt 官方的 `run-operation` 入口。适配器自带 `dbt_maxcompute.export_semantic_package` 宏，由宏调用适配器 Python 转换方法，不增加独立 CLI，也不修改 dbt Core。

```text
semantic.yml → dbt run-operation（解析当前项目）
                         ↓ export_semantic_package 宏 / adapter Python
                   imported-draft.json
                         ↓ maxc semantic diff / apply / publish
                   Published 语义包 → MaxAgent
```

业务语义仍由项目中的 `semantic.yml` 定义，Agent 可辅助生成并运行 dbt 验证。导出器转换已有定义，保留经过声明的指标口径、时间维度与实体关联，不自动猜测新的业务关系。

## 安装试用版

建议使用独立的 Python 3.12 虚拟环境。执行一条命令安装已验证的版本组合：

```shell
python -m pip install 'dbt-core==1.12.6' 'dbt-maxcompute[semantic]==1.12.1b1' 'maxc-cli==0.9.5b1'
```

`semantic` 扩展包含 SQLGlot/Jinja 转换依赖。maxc 使用独立的命令行 JSON 接口，负责认证、表结构读取和语义包操作。版本固定后无需再添加 `--pre`；普通 `pip install -U dbt-maxcompute` 仍优先选择正式版。

确认安装结果：

```shell
dbt --version
maxc --version
maxc meta describe --help
```

maxc 应为 0.9.5b1，帮助中应包含 `--metadata-only`。该功能当前面向独立 Python 入口 `maxc`；`aliyun maxc` 的内置运行时需等对应分发版本更新。

从适配器源码开发时使用 `python -m pip install -e '.[semantic]'`。宏随适配器安装，无需用户复制宏或新增 `packages.yml` 依赖。

在 dbt 项目目录先完成建表与已配置的数据测试：

```shell
dbt run
dbt test
```

导出操作会沿用 dbt 的项目解析流程，从当前项目 target 目录读取 `semantic_manifest.json`，通常无需单独执行 `dbt parse`。数据库及语义包的远端访问由 maxc 连接承担，须核对该连接与当前 dbt target 指向的实际 project/schema/table 一致。导出默认使用当前项目 target 下的 `maxagent-semantic-proposal.json`；可以用 output 参数指定项目相对路径。

当前支持 Core 1.12 latest spec、simple/ratio、UTC、直接列或 COALESCE 表达式，以及单跳外实体到主实体的 LEFT 关联。窗口、累计、SCD、time-spine 绑定和分区策略尚未映射。分区源表会被拒绝，避免丢失裁剪及迟到数据规则。

## 确认身份和 namespace

先检查当前连接及在线身份：

```shell
maxc agent context --json
maxc agent manifest --json
maxc agent doctor --online --user-agent "$UA" --json
maxc auth whoami --user-agent "$UA" --json
```

`UA` 按 CLI Skill 的会话 User-Agent 格式设置。在线检查要求 `data.ready=true`。`auth whoami` 的 `data.identity` 新增 `principal_id`、`principal_type`、`account_id`、`semantic_namespace` 和来源字段。

省略 `--namespace` 时，语义包命令从在线 whoami 的可信账号信息解析主账号 ID：主账号使用其数值 ID；RAM 主体只在服务端返回明确 AccountId 或可识别的复合 ID 时解析账号前缀。RAM 的单独主体 ID 不会当作主账号 namespace，项目 tenant ID 也不会参与推断。无法解析时命令停止，你可以使用独立核实过的主账号 ID 显式传入 `--namespace`。

身份格式依据 [MaxCompute 用户身份格式](https://help.aliyun.com/en/maxcompute/add-user)。本次云端验收使用已有主账号身份；RAM 复合 ID 路径有单元测试覆盖，未声称完成 RAM 云端验收。

## 准备目标包

导入目标必须是已创建的语义包。新包先准备对象定义 `definition.json`，其中的表名应使用你自己的 dbt 实际输出表：

```json
{
  "description": "dbt 订单与客户语义定义",
  "tags": ["dbt"],
  "dataScope": {
    "dataReferences": [
      {"project": "my_project", "schema": "default", "table": "orders"},
      {"project": "my_project", "schema": "default", "table": "customers"}
    ]
  }
}
```

创建对象后回读确认身份。创建对象不会自动发布：

```shell
maxc semantic create sales --file definition.json --yes --user-agent "$UA" --json
maxc semantic get sales --user-agent "$UA" --json
```

## 生成和审阅提案

在能访问编译产物的目录执行导出（maxc 使用已配置的同一连接）：

```shell
dbt run-operation dbt_maxcompute.export_semantic_package \
  --args "{name: sales, timezone: UTC, join_type: LEFT, output: imported-draft.json, user_agent: '$UA'}"
```

该导出命令读取当前 USER_DRAFT、按编译后的 project/schema/table 检查真实表结构，并生成 `maxc.semantic/v1` 文件。输出包含规范化定义 hash、目标身份、Draft revision、转换数量，`remoteMutations=false`。命令不会执行用户 SQL，也不会写入 Draft 或发布。

文件中保留服务端身份和 revision，未参与导入的顶层章节（例如 glossary、verifiedQueries）保持原值。导入负责的章节包括 dataReferences、semanticModel、metricDefinitions 和 instructions；dataScope 会随导入表范围生成提案。该范围变化会出现在 diff 中，且 dataScope 没有服务端 CAS。

如果这些章节已存在且内容不同，命令默认停止。先检查现有章节，再用 `replace_sections: true` 允许生成章节替换提案；该选项不会执行远端替换。重新生成一个已存在的输出文件还需要 `overwrite: true`。章节内的人工说明和未知扩展字段可能随显式替换而消失，必须审阅完整 before/after。

重新运行 `dbt parse` 产生的 invocation_id、generated_at 等元信息不会进入业务定义 hash；相同语义定义的重复导入可得到空差异。

查看最终提案的完整差异：

```shell
maxc semantic diff sales --file imported-draft.json --user-agent "$UA" --json
```

审阅 dataReferences、逐行 expr/defaultAggregation、过滤器引用、比率公式、时间维度，以及关联列、基数和 LEFT 策略。主键唯一性与关联基数需要 dbt/data tests 验证；读取列元数据不能证明这些业务性质。

## 写入和发布

核对差异后，使用本次 diff 返回的 `planDigest` 写入 Draft：

```shell
maxc semantic apply sales \
  --file imported-draft.json \
  --plan-digest '<reviewed_plan_digest>' --yes \
  --user-agent "$UA" --json
```

回读新 Draft，并完成代表性业务查询验证。单纯结构校验通过不能证明指标口径正确。然后发布已验证的精确对象与 revision：

```shell
maxc semantic get sales --user-agent "$UA" --json
maxc semantic publish sales \
  --expected-spec-id '<verified_spec_id>' \
  --expected-revision '<verified_draft_revision>' --yes \
  --user-agent "$UA" --json
maxc semantic get sales --source PUBLISHED \
  --revision '<returned_published_revision>' \
  --user-agent "$UA" --json
```

遇到版本冲突时重新读取并审阅；遇到 `SEMANTIC_WRITE_UNCERTAIN` 时先核对远端结果，不自动重放写操作。分析使用固定的 Published 定义，继续通过实际 MaxAgent 工具链检查其读取的 revision 和云端查询结果。

## 已知边界

时区和最小时间粒度保存在 timeConventions/usageGuidance 文本中，目标格式暂时缺少强类型约束。Decimal 保留基本类型，未绑定完整精度/scale。来源 hash 在导入回执与说明中保留，尚无服务端原生 dbt repository/commit 绑定。

旧 PyODPS 发现裸 Catalog 域名时，CLI 会为合法 DNS 地址补上 HTTPS；显式 HTTP(S) 路由保持原值。若数据端点仍配置为 HTTP，建议在你的连接配置中使用已验证的 HTTPS 端点；本修复不自动改写全局认证配置。

## 连接与排障

宏参数 `config`、`user_agent`、`namespace` 传递给 maxc。`maxc` 参数可指定另一个 Python 环境里的命令绝对路径。`manifest` 可显式指定编译产物，默认使用当前 dbt target；`output` 默认也位于当前 target。相对文件路径以当前 dbt 项目根目录为基准。

需要替换既有章节或输出文件时，审阅后在 args 中加入 `replace_sections: true` 或 `overwrite: true`。这些参数只允许生成本地提案；apply/publish 始终单独执行。导出文件以 0600 权限原子写入，拒绝符号链接路径。

若 maxc 未返回成功 JSON，检查配套版本、`maxc auth whoami --json`、目标包访问权限及实时表结构接口。导出使用 `meta describe --metadata-only` 的 `data.table` 结果，不读取样本行、不回退到缓存。认证继续沿用 maxc 已配置的凭证，不在宏参数中传递 AccessKey。

实现依据：[官方 run-operation](https://docs.getdbt.com/reference/commands/run-operation)、[官方 adapter 对象](https://docs.getdbt.com/reference/dbt-jinja-functions/adapter)。
