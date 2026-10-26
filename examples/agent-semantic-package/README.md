# Agent 语义包示例

按[完整中文教程](../../docs/agent-semantic-package-tutorial.zh-CN.md)配置连接并运行。

`models/*.sql` 创建合成订单、客户和日历表，`models/semantic.yml` 描述指标、维度和关联。表名前缀由 `SEMANTIC_DEMO_PREFIX` 指定，表生命周期为 7 天；请使用独立前缀。`profiles.yml.example` 只引用环境变量，`maxc.json` 不包含凭证。
