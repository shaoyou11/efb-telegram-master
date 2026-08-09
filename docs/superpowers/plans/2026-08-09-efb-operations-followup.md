# EFB 运维可观测性与维护流程实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 EFB 增加 24 小时投递统计、只读备份校验展示和更新维护模式，并安全部署到 NAS。

**Architecture:** EFB 镜像只负责投递统计和读取状态报告；NAS 配置仓库负责备份清单、只读校验和维护状态机。更新脚本只停止和重建 EFB，不操作 ComWechat、Bot API 或 watchdog。

**Tech Stack:** Python 3、SQLite、POSIX shell、Docker Compose、pytest。

## Global Constraints

- 不记录消息正文、文件名、聊天名称或凭证。
- 更新前创建时间戳备份；失败时使用旧镜像回滚。
- 不批量删除文件，不覆盖生产配置，不重启 ComWechat。
- GitHub 代码和镜像归属统一使用 `shaoyou11`。

### Task 1: 24 小时投递统计

**Files:**
- Modify: `efb_telegram_master/delivery_telemetry.py`
- Modify: `efb_telegram_master/operations_ui.py`
- Test: `tests/unit/test_delivery_telemetry.py`
- Test: `tests/unit/test_operations_ui.py`

**Interfaces:**
- `DeliveryTelemetry` persists aggregate buckets in `delivery-stats.json`.
- `delivery_stats_summary(data_root, now=None)` returns counts and average latency for the last 24 hours.

- [ ] Write failing tests for inbound, delivered, filtered, failed counters and window trimming.
- [ ] Run the focused tests and confirm the new assertions fail because the aggregate state and status line are absent.
- [ ] Implement atomic bucket persistence and status formatting without message content.
- [ ] Run focused tests, then the existing EFB unit set.
- [ ] Commit with the Chinese log format: problem, implementation, optional reproduction path.

### Task 2: Read-only backup audit and status display

**Files:**
- Create: `operations/backup_audit.py` in `efb-config-private`
- Modify: `operations/update_efb.sh` in `efb-config-private`
- Modify: `operations/efb2026-health-guard.cron` in `efb-config-private`
- Modify: `efb_telegram_master/operations_ui.py`
- Test: `operations/test_operations.py`
- Test: `tests/unit/test_operations_ui.py`

**Interfaces:**
- `backup_audit.py` writes `backup-audit-latest.json` with manifest, SQLite and decrypt checks.
- `backup_verification_text(data_root)` reads that report and never writes to a backup directory.

- [ ] Write failing tests for a valid manifest, a failed SQLite check, and status formatting.
- [ ] Run the tests and confirm failure before implementation.
- [ ] Implement report generation using read-only SQLite and a decrypt-to-tar pipeline.
- [ ] Invoke the audit before production update and on the low-frequency health schedule.
- [ ] Run config tests and EFB status tests.
- [ ] Commit the config and source changes separately.

### Task 3: Update maintenance state machine

**Files:**
- Modify: `operations/update_efb.sh` in `efb-config-private`
- Modify: `efb_telegram_master/operations_ui.py`
- Test: `tests/unit/test_operations_ui.py`
- Test: `operations/test_operations.py`

**Interfaces:**
- `maintenance.json` contains `enabled`, `phase`, `reason`, `updated_at`, and the last result without secrets.
- `update_efb.sh` waits for a clear delivery pending state, stops only EFB, verifies, and rolls back on failure.

- [ ] Write failing tests for maintenance state rendering and drain timeout decisions.
- [ ] Run tests and confirm expected failure.
- [ ] Add atomic maintenance state writes, drain timeout, controlled stop/start, and rollback state handling.
- [ ] Run shell syntax checks and the full focused test set.
- [ ] Commit and push source/config branches.

### Task 4: Build and deploy

**Files:**
- Modify: image Dockerfile source pin after source merge.

- [ ] Run source CI and PR canary.
- [ ] Merge only after checks pass; wait for production image build.
- [ ] Create a fresh NAS backup, run the audit, and execute `update_efb.sh`.
- [ ] Verify `/status` data, four healthy containers, zero unexpected restarts, queue state, and recent EFB error count.
- [ ] Record rollback path and exact backup directory.
