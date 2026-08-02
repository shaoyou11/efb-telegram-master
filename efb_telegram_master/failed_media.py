import os
import shutil
import tempfile
from pathlib import Path


def persist_failed_media(source: Path, token: str, root: Path) -> Path:
    source = Path(source)
    root = Path(root)
    if not source.is_file():
        raise FileNotFoundError(source)

    safe_token = Path(str(token)).name
    if not safe_token or safe_token != str(token):
        raise ValueError("invalid failed media token")

    target_dir = root / safe_token
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(source.name).name or "payload"
    target = target_dir / filename
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=str(target_dir),
        prefix=f".{filename}.",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        with source.open("rb") as source_file:
            shutil.copyfileobj(source_file, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
    os.replace(temporary_path, target)
    shutil.copystat(source, target, follow_symlinks=True)
    return target


def cleanup_failed_media(target: Path, root: Path) -> bool:
    root = Path(root).resolve()
    target = Path(target).resolve()
    if root not in target.parents or not target.is_file():
        return False
    target.unlink()
    try:
        target.parent.rmdir()
    except OSError:
        pass
    return True
