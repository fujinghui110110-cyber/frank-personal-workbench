# 财务负责人 AI 工作台

这是一个以“事项”为中心的主动式财务工作台。手机或桌面只负责把文字、图片、文件、录音、视频、个人微信 Markdown 和企业微信审批材料投进去；服务端先保存和排队，Mac 开机后执行节点自动领取，WorkBuddy 也可以通过 MCP 读取和回写。

当前实际部署是只绑定 `127.0.0.1:8000` 的 Mac 本机版，不需要云主机、域名或公网 HTTPS。Mac 关机时，安卓材料留在手机 Syncthing-Fork 投递箱；Mac 开机后点对点同步，LaunchAgent 自动收件并继续处理。

## 已完成的闭环

```text
安卓保存到本地同步投递箱 / Mac 网页直接投递
→ 原始材料按 SHA-256 保存
→ 幂等收件与 SQLite 队列
→ Mac 或 WorkBuddy 租约领取
→ 事实 / 推断 / 任务 / 风险 / 决策 / 待反馈
→ 人工确认推断
→ 逾期、失败、待确认主动提醒
→ 审计事件与证据回链
```

## 本机启动

当前 Mac 已具备所需 Python 依赖时：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。开发环境初始口令是 `demo`，只用于本机验收。

另开一个终端，处理一条积压材料：

```bash
python3 -m scripts.mac_worker --once
```

持续运行执行节点：

```bash
python3 -m scripts.mac_worker --poll-seconds 15
```

## Mac 关机时安卓投递

在安卓的微信、录音、相册或文件管理器中点“分享”，选择 Syncthing-Fork，保存到：

`财务工作台投递箱 / 待处理`

材料会在安卓手机本地等待。Mac 开机后 Syncthing 自动同步，LaunchAgent 每 15 秒检查一次；成功收件后把原文件移动到“已接收”，再进入本机转写和 WorkBuddy 处理。未同步完成或读取失败的文件保留在“待处理”，下一轮自动重试。

## 数据目录

```text
data/
├── workbench.sqlite3
└── objects/
    └── <sha256 前两位>/<sha256>
```

备份必须同时包含 SQLite 数据库和 `objects/`。原始材料只读保存；审计记录只写动作元数据，不复制正文。

## WorkBuddy MCP

MCP 入口是项目根目录的 `mcp_server.py`，通过标准输入输出运行。配置示例见 `docs/WorkBuddy接入与部署.md`。

## 开机自动处理

运行安装脚本会创建 iCloud 投递箱、写入 LaunchAgent 配置并立即启动：

```bash
zsh deploy/install-mac-worker.sh
```

## 测试

```bash
ruff check app scripts mcp_server.py tests
python3 -m pytest
```

## 当前边界

- 第一版不直接抓取个人微信或企业微信；接收导出的 Markdown、审批表及附件。
- 音视频和复杂文档先安全入库；本地规则只做可证实的文本抽取，深度转写、OCR 和判断交给 WorkBuddy 或后续本地模型。
- 不自动付款、记账、提交审批或对外发送消息。
- 当前不提供公网网页；Mac 关机时的手机入口是安卓 Syncthing-Fork 本地投递箱。
