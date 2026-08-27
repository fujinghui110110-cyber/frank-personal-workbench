# WorkBuddy 接入与部署

## MCP 配置

把下面配置中的路径改成本项目绝对路径，并使用独立的 MCP token：

```json
{
  "mcpServers": {
    "财务AI工作台": {
      "command": "python3",
      "args": [
        "/absolute/path/to/mcp_server.py"
      ],
      "env": {
        "WORKBENCH_BASE_URL": "https://你的工作台域名",
        "WORKBENCH_MCP_TOKEN": "独立的-mcp-token"
      }
    }
  }
}
```

可用工具：

- `intake_text`：投递文字并进入远端队列。
- `list_pending_materials`：查看待处理材料。
- `list_matters`、`get_matter`：读取事项及证据、行动和提醒。
- `claim_job`、`start_job`、`complete_job`：领取并回写结构化结果。
- `morning_brief`：读取今日总控简报。
- `resolve_reminder`：关闭、暂缓或忽略提醒。

## WorkBuddy 回写约束

`complete_job` 的 `result` 使用以下结构：

```json
{
  "matter_title": "民生银行一般户开户",
  "summary": "已收集开户资料，等待确认账户切换时间。",
  "facts": [
    {
      "field_type": "日期",
      "value": "2026-08-12",
      "source_locator": "第 18 行",
      "quote": "财务部于2026-08-12前完成开户材料准备",
      "confidence": 1.0
    }
  ],
  "inferences": [
    {
      "field_type": "事项类型",
      "value": "银行账户管理",
      "source_locator": "模型推断",
      "quote": "",
      "confidence": 0.82
    }
  ],
  "actions": [
    {
      "kind": "decision",
      "title": "确认原账户停止收款时间",
      "detail": "当前材料未形成明确结论",
      "owner": "财务负责人",
      "due_date": null
    }
  ]
}
```

金额、日期、人员、制度依据和审批状态一旦写入 `facts`，必须包含 `source_locator`。模型判断写入 `inferences`，系统会自动放进“待我确认”。

## 权限

- Owner token：全权 API，只由财务负责人保管。
- Worker token：只能领取任务、读取材料、回写任务结果和更新节点状态。
- MCP token：可读取事项、操作提醒、领取和回写 WorkBuddy 任务。

三个 token 必须不同。不要把 token 写入聊天、截图、代码仓库或日志。

## 公网部署最低要求

- 一台 24 小时在线的容器主机。
- `/data` 持久卷和定期备份。
- HTTPS 域名。
- 强口令和随机 token。
- 限制运维端口，只暴露 HTTPS。
- 生产环境数据库和对象库的磁盘加密。

第一版使用 SQLite 和文件对象库，适合单一财务负责人。出现多租户、并发写入或大规模音视频后，再迁移 PostgreSQL 和 S3 兼容对象存储。
