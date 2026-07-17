# EFB 会话接收设置实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个微信会话提供正常接收、静默接收和完全过滤三档 Telegram 转发策略。

**Architecture:** 新建独立的 JSON 规则存储模块，以 `channel_id + chat_uid` 为键并原子写入 EFB profile 持久化目录。新增管理器复用现有会话分页，在消息发送入口合并规则与现有静默逻辑，因此所有消息类型自动遵循同一策略。

**Tech Stack:** Python 3、python-telegram-bot、EFB、pytest、JSON。

## Global Constraints

- 不实现或调用微信自动已读。
- 每个管理页面均提供“关闭”按钮。
- 只有管理员可以修改规则。
- 未配置、无效或无法读取规则时回退为正常接收。
- 不删除消息、附件、缓存或聊天记录。
- Git 提交身份统一使用 `shaoyou11 <20028830+shaoyou11@users.noreply.github.com>`。

---

### Task 1: 会话策略存储

**Files:**
- Create: `efb_telegram_master/delivery_policy.py`
- Create: `tests/unit/test_delivery_policy.py`

**Interfaces:**
- Produces: `DeliveryPolicy(str, Enum)`，值为 `normal`、`silent`、`filtered`。
- Produces: `DeliveryPolicyStore(path: Path)`，包含 `get(chat_key) -> DeliveryPolicy`、`set(chat_key, policy, metadata=None)`、`reset(chat_key)`、`list_rules()`。

- [ ] **Step 1: 写失败测试**

覆盖默认 `normal`、三档写入后重载、恢复默认删除单条规则、无效 JSON 和无效策略回退，以及临时文件替换。

- [ ] **Step 2: 验证测试因模块不存在而失败**

Run: `pytest -q tests/unit/test_delivery_policy.py`
Expected: FAIL，提示 `efb_telegram_master.delivery_policy` 不存在。

- [ ] **Step 3: 实现最小存储模块**

规则文件结构固定为：

```json
{"version": 1, "rules": {"channel chat": {"policy": "silent", "name": "会话名", "type": "group"}}}
```

写入同目录临时文件后用 `os.replace` 原子替换；读取异常记录日志并使用空规则。

- [ ] **Step 4: 验证通过**

Run: `pytest -q tests/unit/test_delivery_policy.py`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add efb_telegram_master/delivery_policy.py tests/unit/test_delivery_policy.py
git commit -m "feat: 增加会话接收策略持久化" -m "需求描述：按微信会话保存正常、静默和过滤策略。" -m "实现思路：使用稳定会话标识和原子 JSON 写入，异常时回退正常接收。"
```

### Task 2: 在消息入口应用策略

**Files:**
- Modify: `efb_telegram_master/slave_message.py`
- Modify: `efb_telegram_master/__init__.py`
- Modify: `tests/unit/test_slave_message.py`

**Interfaces:**
- Consumes: `DeliveryPolicyStore.get(utils.chat_id_to_str(chat=msg.chat))`。
- Produces: `SlaveMessageProcessor.delivery_policy(msg) -> DeliveryPolicy`。

- [ ] **Step 1: 写失败测试**

增加三项测试：`filtered` 不调用 `get_slave_msg_dest` 和 Telegram 发送；`silent` 将现有 `is_silent` 结果提升为 `True`；`normal` 保持现有行为。

- [ ] **Step 2: 验证测试因处理器尚未读取策略而失败**

Run: `pytest -q tests/unit/test_slave_message.py -k delivery_policy`
Expected: FAIL，过滤消息仍进入发送流程。

- [ ] **Step 3: 实现入口判断**

在 `TelegramChannel` 初始化 `DeliveryPolicyStore`，路径为 `efb_utils.get_config_path(channel_id).parent / "delivery-policies.json"`；在 `send_message` 最前端处理 `filtered`，并将 `silent` 与已有 `is_silent` 结果合并。

- [ ] **Step 4: 验证消息测试**

Run: `pytest -q tests/unit/test_slave_message.py`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add efb_telegram_master/__init__.py efb_telegram_master/slave_message.py tests/unit/test_slave_message.py
git commit -m "feat: 应用会话级接收策略" -m "需求描述：让所有微信消息遵循会话独立的转发设置。" -m "实现思路：在统一发送入口过滤消息或启用 Telegram 静默参数。"
```

### Task 3: `/filter` 管理界面和关闭按钮

**Files:**
- Create: `efb_telegram_master/delivery_policy_ui.py`
- Create: `tests/unit/test_delivery_policy_ui.py`
- Modify: `efb_telegram_master/__init__.py`
- Modify: `efb_telegram_master/chat_binding.py`

**Interfaces:**
- Consumes: `ChatBindingManager.slave_chats_pagination(...)` 和 `DeliveryPolicyStore`。
- Produces: `/filter` 命令及 `filter:*` 回调；回调格式限制为 `filter:list:*`、`filter:chat:*`、`filter:set:*`、`filter:reset:*`、`filter:close`。

- [ ] **Step 1: 写失败测试**

测试管理员校验、会话列表选择、详情三档按钮、恢复默认、规则写入、返回，以及列表页、详情页、操作结果页全部包含 `filter:close`。

- [ ] **Step 2: 验证测试因 UI 模块不存在而失败**

Run: `pytest -q tests/unit/test_delivery_policy_ui.py`
Expected: FAIL，提示模块或处理器不存在。

- [ ] **Step 3: 实现管理器**

新增 `DeliveryPolicyUI`，负责渲染和回调；会话键不直接塞入 Telegram callback data，而是使用当前管理消息对应的短期会话列表索引。`close` 先应答 callback，再删除当前机器人消息。

- [ ] **Step 4: 注册命令并更新帮助**

注册 `CommandHandler("filter", ...)` 和 `CallbackQueryHandler(..., pattern=r"^filter:")`，在帮助文本中加入 `/filter`。

- [ ] **Step 5: 验证 UI 测试**

Run: `pytest -q tests/unit/test_delivery_policy_ui.py tests/test_cleanup.py`
Expected: PASS，且不破坏已有 `/cleanup` 关闭按钮。

- [ ] **Step 6: 提交**

```bash
git add efb_telegram_master/delivery_policy_ui.py efb_telegram_master/__init__.py efb_telegram_master/chat_binding.py tests/unit/test_delivery_policy_ui.py
git commit -m "feat: 增加会话接收设置界面" -m "需求描述：通过 Telegram 按钮为每个微信会话选择接收策略。" -m "实现思路：复用会话分页，所有页面提供关闭按钮并限制管理员操作。"
```

### Task 4: 集成、镜像和 NAS 部署

**Files:**
- Modify: `ehforwarderbot-shaoyou11/Dockerfile`
- Modify: `efb-config-private/README.md`（仅在实际持久化路径说明需要更新时）

**Interfaces:**
- Consumes: `efb-telegram-master-shaoyou11` 已推送提交哈希。
- Produces: `shaoyou11` 最新 EFB 镜像和 NAS 持久化规则文件。

- [ ] **Step 1: 完整验证**

Run: `pytest -q tests/unit/test_delivery_policy.py tests/unit/test_delivery_policy_ui.py tests/unit/test_slave_message.py tests/test_cleanup.py`
Expected: 全部 PASS；再运行 `python -m compileall -q efb_telegram_master` 和 `git diff --check`。

- [ ] **Step 2: 推送 Telegram Master 分支**

推送 `local-bot-api`，确认 GitHub Actions 成功，并取得最终提交哈希。

- [ ] **Step 3: 更新自有镜像固定提交**

将 `ehforwarderbot-shaoyou11/Dockerfile` 的 Telegram Master 提交固定为新哈希，提交、推送并等待多架构镜像构建成功。

- [ ] **Step 4: 部署前备份**

在 `/vol4/1000/docker/efb/backups/config-YYYYMMDD-HHMMSS` 创建单次配置备份；不批量删除旧备份。

- [ ] **Step 5: 更新并验证 NAS**

只重建 EFB 容器，等待健康状态后运行 `operations/verify_stack.py`，确认微信登录状态；实际测试一个会话的静默、过滤、恢复正常和关闭按钮，再确认容器重启后规则仍保留。

- [ ] **Step 6: 核对最终状态**

确认三个仓库工作区干净，Git 作者均为 `shaoyou11`，记录 NAS 备份路径和已部署镜像摘要。
