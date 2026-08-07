# Bridge 队列管理设计

日期：2026-08-07

## 目标

在 Telegram 管理端增加 Bridge 队列菜单，允许管理员查看活动队列和死信队列，并在确认后重新投递、立即推动待投递消息或放弃投递。菜单操作由独立的持久化开关控制，不影响 Bridge 正常接收微信消息。

## 当前背景

Bridge 已将消息持久化到 SQLite，现有接口支持：

- `/healthz`：队列统计。
- `/v1/messages/active`：活动队列明细。
- `/v1/messages/dead`：死信明细。
- `/v1/messages/requeue`：单条死信重新投递。

当前缺少放弃投递接口、活动消息立即投递接口和 Telegram 管理菜单。直接由 EFB 改 Bridge SQLite 会绕过 Bridge 的锁、租约和维护逻辑，因此不采用这种方式。

## 方案

### Telegram 入口

- 新增管理员命令 `/bridge`。
- `/status` 和 `/health` 页面增加“Bridge 队列”按钮。
- 所有回调仅接受 `admins` 配置中的管理员账号。
- 新增独立模块 `bridge_queue_ui.py`，负责菜单、分页、确认和状态渲染；Bridge API 客户端与界面逻辑分开。

### 菜单结构

首页显示：

```text
Bridge 队列管理
活动队列：暂存 0｜待投递 0｜处理中 0
死信队列：7 条
管理操作：关闭
```

按钮包括：

- `刷新`
- `活动队列`
- `死信队列`
- `开启/关闭管理操作`
- `关闭`

活动或死信明细按页展示，每页最多 8 条，只显示消息类型、状态、尝试次数、来源标识和时间，不显示完整本地路径、Bot Token 或其他敏感配置。

单条详情提供：

- 活动消息：`立即投递`、`放弃投递`、`返回`。
- 死信消息：`重新投递`、`放弃投递`、`返回`。

批量页面提供：

- `全部重新投递`。
- `清理全部死信`。

批量操作必须先进入确认页，再执行；操作完成后刷新统计。活动队列中的 `处理中` 消息不提供删除按钮，避免消费者仍持有租约时产生重复或丢失。

### 管理操作开关

开关文件：`/data/operations/state/bridge-queue-settings.json`。

格式：

```json
{"management_enabled": false}
```

文件采用临时文件、`fsync` 和原子替换写入。文件不存在或内容损坏时默认关闭管理操作，但仍允许查看队列。关闭时，重新投递、立即投递和放弃投递按钮均拒绝执行；现有 Bridge 消费不受影响。

### Bridge API 和状态模型

在 `python-comwechat` Bridge 中增加以下能力：

- `POST /v1/messages/retry-active`：将 `staged` 或 `pending` 消息的可用时间设为当前时间，立即唤醒消费者；`inflight` 返回不可操作。
- `POST /v1/messages/discard`：将 `staged`、`pending` 或 `dead` 消息标记为 `discarded`；`inflight` 返回不可操作。
- `POST /v1/messages/requeue-all-dead`：在 Bridge 内部事务中将全部死信重置为 `pending`。
- `POST /v1/messages/discard-all-dead`：在 Bridge 内部事务中将全部死信标记为 `discarded`。

`discarded` 是终态记录，不再出现在活动队列或死信统计中。为避免旧消息重新进入时被重复投递，保留 `dedup_key` 和最小审计字段，并清空消息正文；达到死信保留周期后由 Bridge 维护逻辑清理。不会直接删除 SQLite 文件或绕过队列事务。

SQLite 增加 `discarded_at` 和 `discard_reason` 字段，并保持旧数据库自动迁移兼容。`snapshot` 增加 `discarded_size`，现有字段含义不变。

### 失败告警兼容

现有死信提醒中的“重新投递”按钮接入同一个管理开关和队列客户端。开关关闭时不执行操作，并提示“Bridge 管理操作已关闭”。成功重投或放弃后，从 `bridge-dead-alerts.json` 移除对应已处理 ID，避免后续重复提醒。

## 安全和错误处理

- Bridge API 继续只监听容器内部共享网络，不新增局域网或公网暴露端口。
- Telegram 端所有操作校验管理员身份。
- 删除和批量操作使用二次确认，确认文本明确说明消息将不再推送。
- API 超时、参数错误、消息已不存在或状态不允许时只返回中文错误提示，不删除本地状态。
- 页面只显示截断后的可读摘要，不输出附件完整路径、消息原文中的长链接或敏感配置。
- 重启和更新前备份 Compose、Bridge 数据库及 EFB 状态文件；失败时回退镜像和配置，不清理用户数据。

## 测试

Bridge 侧新增测试：

- 活动消息立即投递只处理 `staged`、`pending`。
- `inflight` 消息拒绝立即投递和放弃。
- 死信单条和批量放弃后不再出现在 `list_dead` 和 `snapshot` 统计。
- 放弃后保留去重记录，重复 `stage` 不会重新创建消息。
- 旧数据库缺少新字段时自动迁移。

Telegram 主端新增测试：

- 管理员可以打开菜单，普通账号无权操作。
- 开关默认关闭，关闭时所有写操作被拦截。
- 单条和批量操作都需要确认。
- 菜单分页、刷新、错误响应和死信提醒回调正常。
- 页面不会显示本地路径、Bot Token 或长消息正文。

## 非目标

- 不改变 EFB 正常投递速度和 Bridge FIFO、联系人优先、群聊排序规则。
- 不自动清理活动消息，不自动批量重新投递。
- 不通过菜单停止或重启 ComWechat、EFB 或 Telegram Bot API。
- 不修改 Telegram 云端聊天记录。

## 部署和回滚

实现分为两个公开源码仓库：EFB Telegram 主端和 ComWechat Bridge。先分别运行单元测试，再构建 GHCR 镜像并在 NAS 建立带时间戳备份。部署顺序为：先更新 ComWechat Bridge，再更新 EFB；两者健康后回读 API、菜单开关文件、队列统计和容器日志。任一阶段失败，恢复对应旧镜像，不删除 Bridge 数据库。
