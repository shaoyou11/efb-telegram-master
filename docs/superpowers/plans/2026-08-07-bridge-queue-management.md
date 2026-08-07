# Bridge 队列管理实现计划

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

Goal: 在 Telegram 管理端提供 Bridge 队列总览、明细、立即投递、重新投递、放弃投递和持久化操作开关，并通过 Bridge 内部事务 API 完成所有队列变更。

Architecture: ComWechat Bridge 负责 SQLite 状态迁移、活动消息重试、死信重投和放弃投递；EFB Telegram Master 通过容器内部 HTTP API 读取和修改队列。Telegram UI 只允许管理员操作，写操作由 NAS 持久化开关和二次确认共同保护。

Tech Stack: Python 3.11, SQLite WAL, `http.server`, python-telegram-bot 13, pytest, Docker Compose, GitHub Actions GHCR。

## Global Constraints

- Bridge API 只监听现有容器内部共享网络，不新增局域网或公网端口。
- 管理开关文件为 `/data/operations/state/bridge-queue-settings.json`，缺失或损坏时默认关闭写操作。
- 活动队列的 `inflight` 消息不允许立即投递或放弃。
- 放弃操作保留去重标记并清空正文，不直接删除 SQLite 文件或绕过事务。
- Telegram 页面不显示附件完整路径、Bot Token、长链接或完整消息正文。
- GitHub 仓库使用 `shaoyou11`；提交信息使用中文的问题描述和实现思路格式。
- 不改变既有 Bridge FIFO、联系人优先、群聊排序和 EFB 正常消费流程。
- 生产更新前创建带时间戳备份，先更新 ComWechat Bridge，再更新 EFB。

---

### Task 1: Bridge 队列状态和事务操作

Files:
- Modify: `reliable_queue.py`
- Test: `tests/test_reliable_queue.py`

Interfaces:
- Produces `SQLiteMessageQueue.retry_active(message_id: str) -> str`，返回 `retried`、`inflight` 或 `not_found`。
- Produces `SQLiteMessageQueue.discard_message(message_id: str, reason: str) -> str`，返回 `discarded`、`inflight` 或 `not_found`。
- Produces `SQLiteMessageQueue.requeue_all_dead() -> int`。
- Produces `SQLiteMessageQueue.discard_all_dead(reason: str) -> int`。
- Extends `snapshot()` with `discarded_size` while preserving existing keys.

- [ ] Step 1: Add failing tests for retry, discard, dedup retention, inflight protection, and batch operations.

Add these behaviors to `tests/test_reliable_queue.py` using the existing `ReliableQueueTests.queue()` helper:

```python
def test_retry_active_releases_pending_message_immediately(self):
    queue = self.queue()
    message_id, _, _ = queue.stage(self.message("active-retry"))
    self.assertEqual(queue.retry_active(message_id), "retried")
    result = queue.pull(1, 0, True, "efb")
    self.assertEqual(result["messages"][0]["msgid"], "active-retry")
    queue.close()

def test_retry_active_rejects_inflight_message(self):
    queue = self.queue()
    message_id, _, _ = queue.stage(self.message("active-inflight"))
    queue.release([message_id])
    queue.pull(1, 0, True, "efb")
    self.assertEqual(queue.retry_active(message_id), "inflight")
    queue.close()

def test_discard_dead_removes_dead_count_but_keeps_deduplication(self):
    queue = self.queue(max_attempts=1, retry_delay_seconds=0)
    message = self.message("dead-discard")
    message_id, dedup_key, _ = queue.stage(message)
    queue.release([message_id])
    delivery = queue.pull(1, 0, True, "efb")["deliveries"][0]
    queue.nack([delivery["delivery_id"]], "efb", "test failure")

    self.assertEqual(queue.discard_message(message_id, "admin"), "discarded")
    self.assertEqual(queue.snapshot()["dead_letter_size"], 0)
    self.assertEqual(queue.snapshot()["discarded_size"], 1)
    repeated_id, repeated_key, inserted = queue.stage(message)
    self.assertEqual(repeated_id, message_id)
    self.assertEqual(repeated_key, dedup_key)
    self.assertFalse(inserted)
    queue.close()

def test_discard_batch_only_changes_dead_messages(self):
    queue = self.queue(max_attempts=1, retry_delay_seconds=0)
    ids = []
    for msgid in ("dead-a", "dead-b"):
        message_id, _, _ = queue.stage(self.message(msgid))
        ids.append(message_id)
        queue.release([message_id])
        delivery = queue.pull(1, 0, True, "efb")["deliveries"][0]
        queue.nack([delivery["delivery_id"]], "efb", "test failure")
    active_id, _, _ = queue.stage(self.message("still-active"))
    self.assertEqual(queue.discard_all_dead("admin"), 2)
    self.assertEqual(queue.snapshot()["dead_letter_size"], 0)
    self.assertEqual(queue.snapshot()["pending_size"], 1)
    self.assertEqual(queue.discard_message(active_id, "admin"), "discarded")
    queue.close()
```

- [ ] Step 2: Run the new tests and verify the failure is caused by missing queue methods or missing `discarded_size`.

Run:

```bash
cd /tmp/docker-comwechat-inspect
python3 -m pytest tests/test_reliable_queue.py -q
```

Expected: the new tests fail with missing method or key errors while the pre-existing tests continue to run.

- [ ] Step 3: Implement schema migration and minimal queue methods.

In `SQLiteMessageQueue._create_schema()` add nullable columns `discarded_at REAL` and `discard_reason TEXT`, plus a migration that adds either column when an existing database does not contain it. Keep `ACTIVE_STATES` unchanged.

In `_maintenance_locked()` retain existing acked/dead cleanup and add cleanup for `state='discarded'` where `discarded_at` is older than `dead_retention_seconds`.

Implement `retry_active()` in one transaction: call maintenance, select the row by ID, return `not_found` if absent, return `inflight` if leased, update `staged` or `pending` to `pending` with `available_at=now`, `sort_at=_next_release_sequence_locked()`, clear `last_error`, and notify the condition.

Implement `discard_message()` in one transaction: call maintenance, return `not_found` if absent, return `inflight` for an active lease, accept only `staged`, `pending`, or `dead`, update the row to `discarded`, clear lease and dead fields, set `discarded_at`, set `discard_reason`, and replace `payload` with `{}` while keeping `dedup_key` and audit fields.

Implement batch methods by selecting dead IDs inside one transaction and applying the same state transition; return the changed row count and notify once.

- [ ] Step 4: Run the queue tests and the complete Bridge test suite.

Run:

```bash
cd /tmp/docker-comwechat-inspect
python3 -m pytest tests/test_reliable_queue.py -q
python3 -m pytest -q
```

Expected: all tests pass with zero failures.

- [ ] Step 5: Commit the Bridge data-layer change.

```bash
cd /tmp/docker-comwechat-inspect
git add reliable_queue.py tests/test_reliable_queue.py
git commit -m "问题或需求描述：Bridge 队列缺少安全放弃操作" -m "修复或实现思路：增加活动重试、死信批量处理和保留去重标记的放弃状态"
```

### Task 2: Bridge HTTP 管理接口

Files:
- Modify: `comwechat_bridge.py`
- Create: `tests/test_bridge_queue_api.py`

Interfaces:
- Adds `POST /v1/messages/retry-active` with JSON `{ "message_id": "..." }` and response `{ "ok": true, "result": "retried|inflight|not_found" }`.
- Adds `POST /v1/messages/discard` with JSON `{ "message_id": "...", "reason": "admin" }` and response `{ "ok": true, "result": "discarded|inflight|not_found" }`.
- Adds `POST /v1/messages/requeue-all-dead` with response `{ "ok": true, "requeued": N }`.
- Adds `POST /v1/messages/discard-all-dead` with JSON `{ "reason": "admin" }` and response `{ "ok": true, "discarded": N }`.
- Extends `/healthz` with `discarded_size`.

- [ ] Step 1: Write API tests against a temporary `MessageBuffer` and a local HTTP server, covering valid input, invalid input, state results, and batch counts.
- [ ] Step 2: Run `python3 -m pytest tests/test_bridge_queue_api.py -q` and observe the expected 404 or missing route failures.
- [ ] Step 3: Add `MessageBuffer` delegating methods and the four POST routes. Validate IDs as non-empty strings, cap no user-provided limit beyond existing queue caps, and return 400 for invalid JSON or arguments.
- [ ] Step 4: Run the API test and complete Bridge suite again; verify zero failures.
- [ ] Step 5: Commit with `问题或需求描述：Bridge 缺少队列管理 API` and an implementation body describing internal transactional operations.

### Task 3: EFB 管理开关和 Bridge API 客户端

Files:
- Create: `efb_telegram_master/bridge_queue.py`
- Test: `tests/unit/test_bridge_queue.py`

Interfaces:
- `BridgeQueueSettings(path: Path)` loads `{ "management_enabled": false }` on missing or invalid files, exposes `enabled`, and atomically saves changes.
- `BridgeQueueClient(base_url: str)` exposes `health()`, `active(limit)`, `dead(limit)`, `retry_active(message_id)`, `requeue_dead(message_id)`, `discard(message_id, reason)`, `requeue_all_dead()`, and `discard_all_dead(reason)`.
- `BridgeQueueClient` raises a bounded `BridgeQueueError` for HTTP, timeout, JSON, or API errors; it never includes a Bot Token or full endpoint in user-facing text.

- [ ] Step 1: Add unit tests for default-off settings, atomic round-trip, corrupt-file fallback, API payloads, and redacted errors.
- [ ] Step 2: Run `python3 -m pytest tests/unit/test_bridge_queue.py -q` and verify the new tests fail because the module is absent.
- [ ] Step 3: Implement the settings class with a temporary file in the target directory, `flush`, `fsync`, and `os.replace`; implement the API client using the existing `urllib.request` style and a five-second timeout.
- [ ] Step 4: Run the new unit tests and existing unit tests; verify all pass.
- [ ] Step 5: Commit with `问题或需求描述：Telegram 端缺少 Bridge 管理客户端` and an implementation body describing the persistent switch and bounded API client.

### Task 4: Telegram Bridge 队列菜单

Files:
- Create: `efb_telegram_master/bridge_queue_ui.py`
- Modify: `efb_telegram_master/operations_ui.py`
- Modify: `efb_telegram_master/bridge_dead_letter.py`
- Modify: `efb_telegram_master/__init__.py`
- Modify: `efb_telegram_master/wizard.py`
- Create: `tests/unit/test_bridge_queue_ui.py`
- Modify: `tests/test_bridge_dead_letter.py`
- Modify: `tests/unit/test_operations_ui.py`
- Modify: `tests/test_watchdog_control.py`

Interfaces:
- `BridgeQueueUI.command(update, context)` opens the admin-only home page.
- `BridgeQueueUI.callback(update, context)` handles callback data prefix `bridgeq:`.
- `BridgeQueueUI.home_text(snapshot, enabled)`, `active_text(items, page)`, and `dead_text(items, page)` are pure render helpers suitable for unit tests.
- Callback data uses short action names and a single queue ID; all destructive actions first render a confirmation page.

- [ ] Step 1: Write tests for admin authorization, default-off action blocking, home rendering, pagination, single retry/discard confirmation, batch confirmation, and sanitized message rendering.
- [ ] Step 2: Run the focused tests and verify expected failures.
- [ ] Step 3: Implement `BridgeQueueUI` with a `BridgeQueueClient` and `BridgeQueueSettings` rooted at `/data/operations/state/bridge-queue-settings.json`. Use existing Telegram `InlineKeyboardButton`, `InlineKeyboardMarkup`, and callback conventions.
- [ ] Step 4: Add `BridgeQueueUI` to `OperationsUI` status/health markup and register `/bridge` plus the `bridgeq:` callback handler in `__init__.py`. Add the Chinese `/bridge` description to `wizard.py` command definitions.
- [ ] Step 5: Update `BridgeDeadLetterGuard` to use the same settings/client, preserve its current alert text, and remove handled IDs after successful retry or discard.
- [ ] Step 6: Run focused UI tests and the complete Telegram Master test suite.
- [ ] Step 7: Update `README.md` with the `/bridge` command, menu behavior, switch persistence, and the meaning of “放弃投递”; commit using the required Chinese log format.

### Task 5: Cross-repository build and publication

Files:
- Modify: `Dockerfile` in `/tmp/efb-image-fix`
- Modify: `README.md` in `/tmp/efb-image-fix`
- Modify: `Dockerfile` or Bridge revision source in `/tmp/docker-comwechat-inspect` only if the build workflow requires a revision marker

- [ ] Step 1: Record the exact EFB Master and ComWechat Bridge commit IDs after their focused tests pass.
- [ ] Step 2: Update `/tmp/efb-image-fix/Dockerfile` to pin the new `efb-telegram-master` commit and the new `python-comwechatrobot-http` or Bridge package commit without changing local Bot API, file-size, media, or watchdog settings.
- [ ] Step 3: Run Dockerfile syntax/read-back checks and the image repository tests.
- [ ] Step 4: Push both source repositories as `shaoyou11` with Chinese commit logs, then wait for GitHub Actions to build and test the new GHCR tags.
- [ ] Step 5: Verify the published image revision from the workflow logs and retain the previous image revision for rollback.

### Task 6: NAS backup, deployment, and end-to-end verification

Files and paths:
- NAS Compose: `/vol4/1000/docker/efb/docker-compose.yaml`
- NAS state: `/vol4/1000/docker/efb/operations/state`
- NAS backups: `/vol4/1000/docker/efb/backups/`

- [ ] Step 1: Create a fresh timestamped backup of Compose, Bridge `queue.db` plus WAL/SHM, EFB state, profiles, pending files, and current container/image status; write a SHA256 manifest.
- [ ] Step 2: Read back the backup path, size, file count, and manifest hash without printing secrets.
- [ ] Step 3: Pull the new ComWechat image and recreate only `comwechat`; verify `/healthz`, the Bridge schema migration, and all four containers before moving on.
- [ ] Step 4: Pull the new EFB image and recreate only `efb`; verify the new `efb-telegram-master` version, the existing local Bot API, watchdog, image revision, and container health.
- [ ] Step 5: Read the Bridge API snapshot and confirm the existing seven dead letters remain available until the user chooses a menu operation; do not auto-delete or auto-requeue them during deployment.
- [ ] Step 6: Verify the persisted management switch defaults to off, then send the admin menu command, enable the switch, view the active/dead pages, and test one dead-letter retry or discard only after the menu is confirmed working.
- [ ] Step 7: Check EFB logs for no `RuntimeError`, no `No handler subscribed`, no unhandled callback exceptions, and no new dead letters caused by the deployment.
- [ ] Step 8: If any health check or API verification fails, stop, keep the backup, and restore the previous image without deleting queue data.

## Completion Checklist

- [ ] Bridge and Telegram Master focused tests pass.
- [ ] Full test suites pass.
- [ ] Both source commits are pushed to `shaoyou11` repositories.
- [ ] GHCR builds succeed and the EFB image pins exact new component revisions.
- [ ] NAS backup and SHA256 manifest exist.
- [ ] ComWechat then EFB deployment is healthy.
- [ ] Telegram Bridge menu, switch, view, retry, and discard paths are verified.
- [ ] No secret or real message payload is committed.
