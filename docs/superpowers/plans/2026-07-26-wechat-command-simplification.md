# 微信命令简化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 `/login` 和 `/wechat` 提供稳定、中文化的微信登录与管理入口，并兼容原有附加功能命令。

**Architecture:** 在 Telegram 主端新增独立的微信管理控制器，按固定频道 ID 调用 ComWechat 的现有附加功能。`/extra` 转交新面板，原有动态命令处理器继续保留。

**Tech Stack:** Python 3.11、python-telegram-bot 13、EFB、pytest、Docker、GitHub Actions

## Global Constraints

- GitHub 用户统一使用 `shaoyou11`。
- 新入口只允许 Telegram 管理员使用。
- `/0_reauth`、`/h_0_reauth` 保持兼容。
- 强制退出微信必须二次确认。
- 所有用户可见提示使用中文。
- NAS 部署前必须备份，部署后必须端到端验证。

---

### Task 1: Telegram 微信管理控制器

**Files:**
- Create: `efb_telegram_master/wechat_control.py`
- Modify: `efb_telegram_master/__init__.py`
- Modify: `efb_telegram_master/commands.py`
- Modify: `efb_telegram_master/watchdog_control.py`
- Create: `tests/test_wechat_control.py`
- Modify: `tests/test_watchdog_control.py`

**Interfaces:**
- Consumes: `coordinator.slaves`、ComWechat 的 `reauth` 和 `force_logout` 附加功能。
- Produces: `WeChatControl.show()`、`WeChatControl.login()`、`WeChatControl.callback()`。

- [ ] **Step 1: 写失败测试**

覆盖固定频道查找、中文按钮、退出二次确认、`/extra` 转发及中文命令菜单。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest -q tests/test_wechat_control.py tests/test_watchdog_control.py`

Expected: FAIL，原因是 `wechat_control` 尚不存在且菜单仍含旧 `/extra`。

- [ ] **Step 3: 实现最小控制器**

注册 `/login`、`/wechat` 和 `wechat:` 回调；调用现有附加功能；异常时仅向用户显示中文摘要。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest -q tests/test_wechat_control.py tests/test_watchdog_control.py`

Expected: PASS。

### Task 2: ComWechat 离线提示

**Files:**
- Modify: `efb_wechat_comwechat_slave/efb_wechat_comwechat_slave/ComWechat.py`
- Create: `efb_wechat_comwechat_slave/tests/test_offline_notification.py`

**Interfaces:**
- Produces: 统一提示“发送 /login 获取登录二维码，或发送 /wechat 打开微信管理”。

- [ ] **Step 1: 写失败测试并确认旧提示不符合要求**

Run: `pytest -q tests/test_offline_notification.py`

- [ ] **Step 2: 替换定时离线与发送消息时的提示**

两个入口使用完全相同的中文引导，不再展示 `/extra`。

- [ ] **Step 3: 运行相关测试**

Run: `pytest -q tests/test_offline_notification.py`

Expected: PASS。

### Task 3: 镜像与 NAS 部署

**Files:**
- Modify: `ehforwarderbot-image/Dockerfile`

**Interfaces:**
- Consumes: 两个源码仓库的新提交。
- Produces: `ghcr.io/shaoyou11/efb:latest` 新镜像。

- [ ] **Step 1: 提交并推送两个源码仓库**

提交信息使用中文问题描述和修复思路。

- [ ] **Step 2: 更新 Dockerfile 固定提交并推送**

只更新 Telegram 主端和 ComWechat 从端的提交哈希及镜像修订标识。

- [ ] **Step 3: 等待 GitHub Actions 构建成功**

确认镜像清单对应新提交。

- [ ] **Step 4: 备份 NAS 并有序更新**

备份当前 Compose、配置和镜像标识，随后按现有 `start-ordered.sh` 部署。

- [ ] **Step 5: 端到端验证**

验证容器健康、共享网络命名空间、内部接口、`/login`、`/wechat`、`/extra` 和旧 `/0_reauth`。
