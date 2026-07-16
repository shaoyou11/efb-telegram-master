import json
from pathlib import Path


def load_storage_report(path="/data/storage-audit-latest.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def format_size(size):
    if size >= 1024**3:
        return f"{size / 1024**3:.2f} GB"
    return f"{size / 1024**2:.2f} MB"


def build_storage_text(report, host_root="/vol4/1000/docker/efb"):
    storage = f"{host_root}/comwechat/Files/shaoyou11/FileStorage"
    return "\n".join([
        "EFB 存储占用",
        "",
        f"普通缓存：{format_size(report['cache']['bytes'])}",
        "可以删除，建议只清理 3 天前内容。",
        f"路径：{storage}/Cache",
        "",
        f"朋友圈缓存：{format_size(report['sns_cache']['bytes'])}",
        "可以删除，建议只清理 7 天前内容。",
        f"路径：{storage}/Sns/Cache",
        "",
        f"聊天附件：{format_size(report['attachments']['bytes'])}",
        "谨慎删除；删除后微信中的旧图片、视频或文件可能无法再次打开。",
        f"路径：{storage}/MsgAttach",
        "",
        f"配置备份：{format_size(report['backups']['bytes'])}（{report['backup_count']} 份）",
        "可以删除旧备份，建议保留最近 3 份。",
        f"路径：{host_root}/backups",
        "",
        "请在飞牛文件管理器中按上述路径手动处理。",
    ])
