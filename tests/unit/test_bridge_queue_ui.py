from pathlib import Path
from types import SimpleNamespace

from efb_telegram_master.bridge_queue import BridgeQueueSettings
from efb_telegram_master.bridge_queue_ui import BridgeQueueUI


class FakeMessage:
    def __init__(self):
        self.sent = []
        self.deleted = False

    def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return self

    def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.answers = []
        self.edits = []

    def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class FakeClient:
    def __init__(self):
        self.calls = []
        self.snapshot = {
            "staged_size": 1,
            "pending_size": 2,
            "inflight_size": 3,
            "dead_letter_size": 4,
            "discarded_size": 5,
            "queue_size": 6,
        }

    def health(self):
        return self.snapshot

    def active(self, _limit=100):
        return []

    def dead(self, _limit=100):
        return []

    def retry_active(self, message_id):
        self.calls.append(("retry_active", message_id))
        return "retried"

    def requeue_dead(self, message_id):
        self.calls.append(("requeue_dead", message_id))
        return True

    def discard(self, message_id, reason):
        self.calls.append(("discard", message_id, reason))
        return "discarded"

    def requeue_all_dead(self):
        self.calls.append(("requeue_all_dead",))
        return 4

    def requeue_all_dead_ids(self):
        self.calls.append(("requeue_all_dead_ids",))
        return ["dead-1", "dead-2", "dead-3", "dead-4"]

    def discard_all_dead(self, reason):
        self.calls.append(("discard_all_dead", reason))
        return 5

    def discard_all_dead_ids(self, reason):
        self.calls.append(("discard_all_dead_ids", reason))
        return ["dead-1", "dead-2", "dead-3", "dead-4", "dead-5"]

    def retry_all_active(self):
        self.calls.append(("retry_all_active",))
        return 2

    def discard_all_active(self, reason):
        self.calls.append(("discard_all_active", reason))
        return 3


def make_ui(tmp_path: Path, enabled=False):
    channel = SimpleNamespace(config={"admins": [123]})
    settings = BridgeQueueSettings(tmp_path / "bridge-queue-settings.json")
    settings.enabled = enabled
    return BridgeQueueUI(channel, client=FakeClient(), settings=settings)


def update(data, user_id=123):
    message = FakeMessage()
    query = FakeQuery(data) if data else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type="private"),
        effective_message=message,
        callback_query=query,
    )


def callback_values(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_home_text_shows_counts_and_switch_state():
    text = BridgeQueueUI.home_text(
        {
            "staged_size": 1,
            "pending_size": 2,
            "inflight_size": 3,
            "dead_letter_size": 4,
            "discarded_size": 5,
            "queue_size": 6,
        },
        enabled=False,
    )

    assert "Bridge 队列管理" in text
    assert "待处理：6" in text
    assert "死信：4" in text
    assert "放弃记录：5" in text
    assert "管理开关：关闭" in text


def test_pagination_and_rendering_hide_paths_and_links():
    items = [
        {
            "id": f"item-{index}",
            "state": "pending",
            "message": {
                "sender": "联系人",
                "content": "https://private.example/video?id=secret",
                "filepath": "/data/operations/secret/original.mp4",
            },
        }
        for index in range(6)
    ]

    text = BridgeQueueUI.active_text(items, page=1)

    assert "item-5" in text
    assert "item-0" not in text
    assert "private.example" not in text
    assert "/data/operations" not in text
    assert "[链接]" in text
    assert "[附件]" in text


def test_default_off_blocks_write_action(tmp_path):
    ui = make_ui(tmp_path, enabled=False)
    event = update("bridgeq:retry:item-1")

    ui.callback(event, None)

    assert "请先开启管理开关" in event.callback_query.answers[0][0][0]
    assert ui.client.calls == []


def test_single_action_requires_confirmation_and_then_executes(tmp_path):
    ui = make_ui(tmp_path, enabled=True)
    first = update("bridgeq:retry:item-1")
    ui.callback(first, None)
    assert "确认" in first.callback_query.edits[0][0]
    assert "bridgeq:retry-confirm:item-1" in callback_values(first.callback_query.edits[0][1])

    second = update("bridgeq:retry-confirm:item-1")
    ui.callback(second, None)

    assert ("retry_active", "item-1") in ui.client.calls
    assert "已重新加入投递队列" in second.callback_query.edits[-1][0]


def test_batch_action_requires_confirmation(tmp_path):
    ui = make_ui(tmp_path, enabled=True)
    event = update("bridgeq:discard-all")

    ui.callback(event, None)

    assert "确认" in event.callback_query.edits[0][0]
    assert "bridgeq:discard-all-confirm" in callback_values(event.callback_query.edits[0][1])


def test_active_batch_action_requires_confirmation(tmp_path):
    ui = make_ui(tmp_path, enabled=True)
    event = update("bridgeq:retry-all-active")

    ui.callback(event, None)

    assert "确认" in event.callback_query.edits[0][0]
    assert "bridgeq:retry-all-active-confirm" in callback_values(
        event.callback_query.edits[0][1]
    )


def test_group_callback_is_rejected(tmp_path):
    ui = make_ui(tmp_path, enabled=True)
    event = update("bridgeq:home")
    event.effective_chat.type = "group"

    ui.callback(event, None)

    assert event.callback_query.answers[0][0][0] == "无权执行"


def test_non_admin_cannot_open_menu(tmp_path):
    ui = make_ui(tmp_path, enabled=True)
    event = update("bridgeq:home", user_id=999)

    ui.callback(event, None)

    assert event.callback_query.answers[0][0][0] == "无权执行"
