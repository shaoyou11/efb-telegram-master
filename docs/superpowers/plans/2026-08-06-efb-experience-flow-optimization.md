# EFB Experience and Message Flow Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Improve EFB message durability, contact-first delivery scheduling, end-to-end diagnosis, operator experience, content parsing, and WeChat login recovery without disrupting the active session.

**Architecture:** Keep the existing EFB Telegram Master, ComWechat Bridge, and Watchdog boundaries. The Bridge remains the durable ingress and ACK boundary; EFB owns Telegram delivery, trace records, policy views, and operator actions; Watchdog owns bounded UI recovery and login state transitions. All new state is written atomically under existing persistent mounts and remains report-only unless an explicit button action is used.

**Tech Stack:** Python 3, SQLite WAL, python-telegram-bot, Pillow, existing JSON state files, Docker Compose, unittest.

## Global Constraints

- Preserve the current WeChat session; never recreate ComWechat during an EFB-only change.
- Contacts have higher scheduling priority than group chats; messages within one source chat remain ordered.
- Telegram flood-control retries must honor `retry_after`, never retry forever, and must not create duplicate durable records.
- Attachment handling must not acknowledge a message before its source path is a regular, stable file or the message has been copied into durable failed-media storage.
- Watchdog must keep the current 02:50-03:50 window, two-minute checks, cooldown, and three-failure pause unless a test proves a narrower change is needed.
- Automatic recovery may report a successful click once per recovery event; manual successful login must also produce one login-success event without duplicating the automatic event.
- No automatic Telegram message deletion, cache deletion, or WeChat automatic read-marking is introduced.
- No credentials, tokens, private repositories, or public network addresses are written to source files or output.
- Every edited runtime/config file receives a timestamped adjacent backup before deployment.

---

### Task 1: Baseline and test scaffolding

**Files:**
- Create: `efb_telegram_master/tests/unit/test_flow_optimizations.py`
- Create: `docker-ComWechat-shaoyou11/tests/test_priority_queue.py`
- Modify: `efb-watchdog/test_watchdog.py`

**Interfaces:**
- Tests will exercise pure scheduling, file stability, trace-state formatting, and watchdog login-event decisions without requiring a live Telegram or VNC session.

- [ ] Record the current commit, working-tree state, Compose image references, and existing test commands in the deployment report.
- [ ] Add failing tests for contact priority, same-chat ordering, unstable-file rejection, trace stage transitions, issue severity formatting, and duplicate login-success suppression.
- [ ] Run the focused tests and confirm each new behavior fails for the expected missing implementation.

### Task 2: Bridge durability and contact-first scheduling

**Files:**
- Modify: `docker-ComWechat-shaoyou11/reliable_queue.py`
- Modify: `docker-ComWechat-shaoyou11/comwechat_bridge.py`
- Modify: `docker-ComWechat-shaoyou11/tests/test_reliable_queue.py`
- Modify: `docker-ComWechat-shaoyou11/tests/test_bridge.py`

**Interfaces:**
- Add `message_priority(message: dict) -> int`, returning contact priority `0`, group priority `10`, and unknown priority `20`.
- Add `source_chat_key(message: dict) -> str` for stable per-chat ordering.
- Extend queue rows with a backward-compatible integer priority and source-chat key; existing databases migrate with defaults.
- Add `is_stable_regular_file(path: str, settle_seconds: float = 0.25) -> bool` and use it before Bridge ACK/ready release for file-bearing payloads.

- [ ] Persist priority and source-chat key at staging time while retaining existing deduplication and TTL behavior.
- [ ] Select ready rows by priority, source-chat sequence, and receive order, so contacts are released first without reordering messages within a chat.
- [ ] Keep login reorder/probe behavior intact and expose priority counts in `/healthz` and Bridge metrics.
- [ ] For file-bearing payloads, wait only within a bounded settle window; if the file is missing or still changing, leave the message staged/pending for retry rather than dropping it.
- [ ] Run Bridge and queue tests, including reopening an older SQLite database without data loss.

### Task 3: EFB delivery trace, ingress preflight, and rate control

**Files:**
- Create: `efb_telegram_master/delivery_trace.py`
- Create: `efb_telegram_master/delivery_scheduler.py`
- Modify: `efb_telegram_master/slave_message.py`
- Modify: `efb_telegram_master/__init__.py`
- Modify: `efb_telegram_master/operations_ui.py`
- Modify: `tests/test_slave_message.py`
- Modify: `tests/test_operations_ui.py`

**Interfaces:**
- Add `DeliveryTraceStore(path)` with `record(uid, stage, **fields)`, `get(uid)`, and atomic retention of recent records.
- Add `DeliveryScheduler.submit(chat_key, is_contact, callback)` and `DeliveryScheduler.close()`; it must preserve per-chat order and prioritize contacts over groups.
- Add `/trace <消息ID>` and an Operations UI “消息追踪” button for the latest pending/failed item.

- [ ] Record stages `received`, `preflight`, `queued`, `telegram_send`, `telegram_ack`, `filtered`, `failed`, and `retry` with timestamps, source chat, sender, Telegram target, topic, file name, and sanitized reason.
- [ ] Add bounded file preflight before sending and persist a copy when the source disappears during delivery.
- [ ] Route inbound slave delivery through independent per-chat workers with a bounded worker count; contact queues are selected before group queues, while each queue remains FIFO.
- [ ] Add a process-local token bucket for Telegram sends and honor `RetryAfter` without sleeping the whole system; retain the current three-attempt ceiling.
- [ ] Make `/status` show scheduler active chats, oldest pending age, last trace stage, and flood-control delay.
- [ ] Keep all trace data under `/data/operations/state`, with a retention limit and no message-body logging beyond the existing sanitized preview.

### Task 4: Unified issues, mapping audit, digest, and content parsing

**Files:**
- Create: `efb_telegram_master/issues.py`
- Create: `efb_telegram_master/digest.py`
- Modify: `efb_telegram_master/operations_ui.py`
- Modify: `efb_telegram_master/__init__.py`
- Modify: `efb_telegram_master/chat_title_sync.py`
- Modify: `tests/test_operations_ui.py`
- Modify: `tests/test_chat_title_sync.py`

**Interfaces:**
- Add `/issues` with callbacks `ops:issues`, `ops:issue_retry:<id>`, and `ops:issue_close`.
- Add `/digest on|off|status` for per-chat silent summaries; default is off and it must not mark WeChat messages read.
- Add `audit_chat_mappings(db) -> list[dict]` returning only actionable orphan, missing-topic, duplicate, or stale-name findings.

- [ ] Merge queue failures, missing attachments, mapping problems, storage warnings, and Watchdog failures into one severity-sorted view with target recipient and exact action.
- [ ] Add explicit retry/view/close buttons; close only hides the issue record and does not delete the source message or durable attachment.
- [ ] Add an opt-in hourly digest for chats already configured as silent; keep original delivery records and preserve per-chat filtering.
- [ ] Normalize common HTML/WeChat service-account card markup into readable text and retain original links when available; do not invent Video Channels URLs when the backend does not provide one.
- [ ] Add mapping audit to `/status` and `/issues` without automatically changing bindings.

### Task 5: Login scan and recovery state machine

**Files:**
- Modify: `efb-watchdog/watchdog.py`
- Modify: `efb-watchdog/test_watchdog.py`
- Modify: `efb-config-private/watchdog/watchdog.py`
- Modify: `efb-config-private/watchdog/test_watchdog.py`

**Interfaces:**
- Add `LoginEventTracker` with `observe(state, now)`, `manual_success(now)`, and `automatic_success(now)`.
- Add explicit states `offline`, `qr_present`, `confirmation_present`, `enter_present`, `clicking`, `logged_in`, and `unknown`.

- [ ] Detect the confirmation dialog first, click it once, recapture, then detect “进入微信”; do not click a stale QR page after a state transition.
- [ ] Treat the QR as consumed after either a confirmed login or a terminal scan failure, and keep the latest diagnostic only for actionable failures.
- [ ] Emit one success notification for a manual login transition and one for a successful automatic recovery event; suppress duplicates from repeated `/status` probes.
- [ ] Keep event-triggered recovery bounded by cooldown and failure cap; never restart ComWechat merely because a QR is visible or a login check is temporarily unavailable.
- [ ] Add status fields for current login UI state, last QR event, last manual/automatic success, and paused reason.

### Task 6: Verification, backup, deployment, and publication

**Files:**
- Modify: `efb-config-private/operations/update_efb.sh`
- Modify: `efb-config-private/operations/update_service.sh`
- Modify: `efb-config-private/README.md`
- Modify: `docker-ComWechat-shaoyou11/README.md`
- Modify: `efb-watchdog/README.md`

- [ ] Run focused and full local tests, compile checks, and Compose config validation.
- [ ] Create timestamped runtime backups on NAS, including Compose, EFB state, Bridge SQLite database, Watchdog state, and current container inspect output.
- [ ] Build and publish only under `shaoyou11`, retaining rollback image tags and the current fixed tags.
- [ ] Recreate only the required EFB/Bridge/Watchdog services, verify ComWechat session, mounts, networks, health, restart counts, queue counts, and login status.
- [ ] Verify `/status`, `/issues`, `/delivery`, `/trace`, login recovery controls, and a text plus attachment canary.
- [ ] Push the validated changes to the corresponding `shaoyou11` repositories with Chinese log messages and report exact persistence and rollback artifacts.
