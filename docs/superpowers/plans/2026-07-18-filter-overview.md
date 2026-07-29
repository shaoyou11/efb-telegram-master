# Filter Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让主聊天的 `/filter` 只显示策略统计和分类入口，在选择分类或提供关键词后才显示具体会话。

**Architecture:** 保留现有 `DeliveryPolicyUI` 会话状态与回调协议，新增 `overview` 和 `contact` 视图。主命令根据上下文在当前会话详情、关键词结果和概览三种入口之间选择，不改变策略存储格式。

**Tech Stack:** Python 3、python-telegram-bot、EFB、pytest

## Global Constraints

- 优先使用 EFB 会话缓存，仅在缓存为空时刷新从端。
- 保留分页、返回、关闭、公众号批量设置和策略持久化。
- 不增加微信端自动已读或无限重启行为。

---

### Task 1: 分类和概览渲染

**Files:**
- Modify: `efb_telegram_master/delivery_policy_ui.py:79-177`
- Test: `tests/unit/test_delivery_policy_ui.py`

**Interfaces:**
- Consumes: `DeliveryPolicyUI._view_chats(chats, view)` 和现有 session 字典。
- Produces: `contact` 分类；`overview` 视图不生成会话按钮。

- [x] **Step 1: 写失败测试**

增加联系人分类测试，并验证 `overview` 不包含 `filter:chat:` 回调、包含五个分类入口。

- [x] **Step 2: 运行测试确认失败**

Run: `pytest -q tests/unit/test_delivery_policy_ui.py`
Expected: FAIL，联系人视图仍返回全部会话或概览仍出现会话按钮。

- [x] **Step 3: 最小实现**

在 `_view_chats` 中将联系人定义为非公众号且非群聊；在 `_render_list` 中为 `overview` 跳过会话行，移除“全部”，加入“联系人”，保留统计与夜间静默按钮。

- [x] **Step 4: 运行测试确认通过**

Run: `pytest -q tests/unit/test_delivery_policy_ui.py`
Expected: PASS。

### Task 2: 命令入口和部署验证

**Files:**
- Modify: `efb_telegram_master/delivery_policy_ui.py:117-136`
- Modify: `Dockerfile`（镜像仓库）

**Interfaces:**
- Consumes: `/filter` 的 `context.args` 和 `_context_chat(update)`。
- Produces: 无参数主聊天进入 `overview`；关键词进入匹配列表；独立话题进入详情。

- [x] **Step 1: 写失败测试**

验证无关键词主聊天 session 的 `view` 为 `overview` 且 `chats` 为空，关键词入口保留匹配结果。

- [x] **Step 2: 最小实现并验证语法和回归测试**

Run: `python3 -m py_compile efb_telegram_master/delivery_policy_ui.py && pytest -q tests/unit/test_delivery_policy_ui.py`
Expected: PASS。

- [x] **Step 3: 提交源码并更新镜像固定提交**

提交到 `shaoyou11/efb-telegram-master`，再将镜像仓库 Dockerfile 固定到新提交并推送。

- [x] **Step 4: 构建、备份、部署和验证**

等待 GitHub Actions 成功；在 NAS 容器内创建 `/data/backups/config-时间戳`；仅重建 EFB 容器；验证健康状态、重启次数、镜像版本及分类回归测试。
