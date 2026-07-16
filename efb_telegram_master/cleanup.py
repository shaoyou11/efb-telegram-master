import json
import re
import subprocess


TOKEN_PATTERN = re.compile(r"^[0-9a-f]{20}$")


def run_cleanup(action, token=None, script="/operations/storage_cleanup.py"):
    if action not in {"list", "delete"}:
        raise ValueError("invalid cleanup action")
    command = ["python3", script, action]
    if action == "delete":
        if not token or not TOKEN_PATTERN.fullmatch(token):
            raise ValueError("invalid cleanup token")
        command.append(token)
    output = subprocess.check_output(command, timeout=30)
    return json.loads(output.decode("utf-8"))


def build_cleanup_text(items):
    lines = [
        "EFB 安全清理",
        "这里只列出超过保留期的缓存文件，每次只能逐个删除一个文件。",
        "聊天附件、微信登录数据和备份目录不会在 Telegram 中删除。",
    ]
    if not items:
        lines.append("当前没有符合条件的缓存文件。")
        return "\n".join(lines)
    lines.append("")
    for item in items:
        size_mb = item["bytes"] / 1024 / 1024
        lines.append(f"- {item['name']}（{size_mb:.2f} MB）")
    return "\n".join(lines)
