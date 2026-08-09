# EFB 运维可观测性与维护流程设计

## 目标

补齐 `/status` 的三个后续能力，同时保持 ComWechat 登录会话、Telegram Bot API 和现有持久化队列不被更新流程重启或覆盖。

## 设计

### 1. 24 小时投递统计

`DeliveryTelemetry` 在现有 `delivery.json` 旁边维护 `delivery-stats.json`，按 UTC 小时保存聚合桶。每个桶只包含接收、成功、过滤、失败数量，以及已完成投递的延迟总和和样本数，不保存消息正文、文件名或聊天名称。读取时只汇总最近 24 小时，旧桶在写入时裁剪。

`/status` 增加一行：微信接收、Telegram 成功、过滤、失败和平均延迟。旧状态文件缺少统计文件时显示 0，不影响现有投递。

### 2. 只读备份校验

NAS 配置仓库增加 `backup_audit.py`。它只读取最新配置备份、`SHA256SUMS`、备份内 SQLite 和最新加密归档：

- 校验文件清单中的文件存在且摘要匹配；
- 以只读方式执行 SQLite `quick_check`；
- 使用密钥通过管道解密并让 `tar` 读取目录，不落地解密内容；
- 结果写入 `backup-audit-latest.json`，不覆盖生产配置或备份内容。

更新前强制执行一次校验；健康任务按低频计划执行。EFB `/status` 只读取报告，显示清单、SQLite、解密三项状态和最近时间。

### 3. 维护模式

`update_efb.sh` 使用 `operations/state/maintenance.json` 记录准备、排空、停止、更新、验证和回滚阶段。流程为：先创建配置备份并校验，进入维护状态，等待 `delivery.json` 当前投递结束，停止 EFB，拉取并只重建 EFB，执行既有健康检查，成功后清除维护标志并记录结果。

ComWechat、Telegram Bot API、watchdog 不停止。排空超时则不进入重建；更新失败使用旧镜像回滚，回滚健康后清除维护标志并保留回滚结果。回滚也失败时保留故障状态，避免假装恢复。

### 4. 管理员手动全栈重启

`/status` 增加“全部重启”按钮。EFB 不访问 Docker socket，只在持久化状态目录写入一次性请求；NAS 健康守护领取请求后调用现有 `start-ordered.sh`，先停止全栈，再按 ComWechat、Bot API 与 watchdog、EFB 的依赖顺序启动，并执行四容器健康检查。请求状态回写到 `manual-restart.json`，避免重复执行和无限重启。

## 验证

- 新增单元测试覆盖统计聚合、24 小时窗口、备份报告展示和维护状态展示。
- 运行 EFB 相关测试及配置仓库运维测试。
- 通过 canary 后构建生产镜像；NAS 更新前创建新备份，更新后只验证 EFB 重建、四容器健康、队列状态和 ComWechat 未被重启。
