# Frank 工作台 v2 验收证据

最后核对：2026-08-28 01:05（Asia/Shanghai）

## 验收结论

本轮 v2 已形成“事项清单持续可见、当前事项连续办理、系统判断可人工纠正、AI 只准备建议、事项收尾须人工核对”的桌面端闭环。正式服务只进行了页面浏览和只读 API 核对，没有确认事项、完成行动、发送消息或处理真实业务材料。

## 需求与证据

| 需求 | 实现证据 | 验收证据 |
|---|---|---|
| 事项推进不再反复跳转 | `app/static/app.js` 的 `renderMatter()` 使用左侧完整清单和右侧办理区；事项切换只替换右侧内容并保留清单滚动位置 | 从旧 `#/today` 打开后直接规范到 `#/matters`；正式页面显示 47 项进行中事项，切换到另一事项后清单仍在 |
| 主导航符合办理逻辑 | `app/static/index.html` 桌面主入口为“事项推进、规定、搜索”；旧 `#/today` 在 `renderRoute()` 中直接按事项页渲染 | Chrome 页面主导航未显示“今天”，也没有“返回今天”或“返回事项清单” |
| 当前事项只显示需要处理的内容 | `renderMatter()` 第一屏为事项摘要、当前办理、待确认和待办；业务依据、办理记录和更多工具默认折叠 | 正式页面第一屏可直接看到当前事项的 3 项待办，辅助内容未占用主办理区 |
| AI 只准备建议 | `complete_job()` 不自动改写正式行动、提醒、事实或事项；建议步骤写入 `work_packages.draft` | `tests/test_workbench_v2_suggestion_only.py`、`tests/test_workbench_v2_acceptance.py` 覆盖不自动创建行动和人工采纳边界 |
| 工作包人工选择性采纳 | `/api/matters/{id}/work-package` 生成、编辑、应用接口；只有 `apply` 创建所选行动 | 合成测试验证生成不创建行动、只创建选中步骤、重复采纳幂等 |
| 所有系统结论可纠正 | 事项、行动、提醒、事实、邮件、聊天、规定和材料归属均有人工修改入口 | `tests/test_workbench_v2_editing_api.py` 与 `tests/test_workbench_v2_frontend_completion.py` 覆盖字段、冲突和前端入口 |
| 原始材料不覆盖 | 事实修正采用新记录替代旧记录；材料重新归属只移动直接派生记录 | 合成测试验证旧事实保留为 `superseded`，材料、事实及直接派生记录保持可回查 |
| 修改可撤销且不静默覆盖 | 编辑请求携带 `expected_updated_at`；撤销使用保存后的版本；409 时提示并刷新 | 合成测试验证旧版本写入返回 409；前端测试验证撤销冲突走错误反馈 |
| 同事项重归属不制造假记录 | `reassign_material()` 返回 `changed: false`，不写审计、不显示撤销；真实移动更新时间单调递增 | `test_material_reassignment_is_monotonic_and_same_target_is_a_noop` 通过 |
| 事项关闭不静默处理子项 | `close-preview` 列出未完成行动、提醒和待确认；`close` 只在人工处理后关闭事项 | 合成测试验证预览只读、存在阻塞时拒绝、关闭与重新打开均不篡改子项 |
| 日期边界明确 | 人工日期使用 `user_entered`；无原文依据的建议日期不自动生成提醒 | `tests/test_workbench_evolution.py` 与 v2 编辑测试覆盖日期依据和提醒边界 |
| 静态资源不再命中旧页面 | HTML、Service Worker 和注册地址统一使用 v72 | 正式服务返回 `/static/app.js?v=72`、`/static/app.css?v=72`，缓存名为 `frank-personal-workbench-shell-v72` |

## 自动验证

- `.venv/bin/pytest -p no:cacheprovider -q`：203 项测试全部通过。
- `.venv/bin/python -m py_compile app/services.py`：通过。
- `node --check app/static/app.js`：通过。
- `node --check app/static/sw.js`：通过。
- `git diff --check`：通过。

## 桌面浏览器验收

- 浏览器：Chrome / Playwright headed，桌面视口。
- 起始地址：`http://127.0.0.1:8000/#/today`。
- 实际结果：地址与界面均直接进入 `#/matters`；左侧显示全部 47 项进行中事项，右侧显示当前事项办理内容。
- 切换事项：点击另一事项后，URL 更新为 `#/matters/{id}`，左侧完整清单、分组与滚动位置保留，右侧更新为新事项。
- 控制台：无脚本错误；仅有登录表单缺少 username 字段的浏览器建议，不影响运行。
- 截图：`/private/tmp/frank-workbench-v72-final.png`。
- 按 Frank 最新要求，本轮不做手机端验收。

## 数据安全与备份

- 正式数据只读验收，没有执行确认、修改、完成、关闭或发送操作。
- 写操作测试全部使用 TestClient 临时数据库和合成数据。
- 正式 Docker 数据卷已使用 SQLite 在线备份机制复制，备份目录：`backups/20260828-0105-v2-final/`。
- 备份包含 `workbench.sqlite3` 和完整 `objects/`，共 7 个对象文件。
