# -*- coding: utf-8 -*-
"""数据主权：导出 / 导入 / 每日滚动备份。只用标准库。

所有文件路径来自白名单，不接受调用方给的路径，避免导入包里夹带
路径穿越。导入会先自动备份当前状态，再原子替换每个 JSON 文件。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import zipfile
from pathlib import Path

BUNDLE_FORMAT = "proficiency-backup"
BUNDLE_VERSION = 1
FILES: tuple[str, ...] = (
    "skills.json", "world.json", "captures.json",
    "benchmarks.json", "decisions.json", "settings.json",
    "custom_skills.json",
)
BACKUP_DIR = "backups"
BACKUP_KEEP_DAYS = 14
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def _now(now: int | None = None) -> int:
    import time
    return int(now if now is not None else time.time())


def _stamp(unix_seconds: int) -> str:
    return datetime.datetime.fromtimestamp(
        unix_seconds, datetime.timezone.utc).strftime("%Y%m%d")


def export_bundle(web_save: str | os.PathLike[str], *,
                  now: int | None = None) -> dict:
    """把存档目录打成一份可下载、可核对的 JSON 包。"""
    root = Path(web_save)
    files: dict[str, object] = {}
    for name in FILES:
        path = root / name
        if path.exists():
            files[name] = json.loads(path.read_text(encoding="utf-8"))
        else:
            files[name] = None
    payload = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "created_at": _now(now),
        "files": files,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload["sha256"] = hashlib.sha256(raw).hexdigest()
    return payload


def validate_bundle(bundle: object) -> dict:
    """导入前校验：格式、版本、字段、体积、类型、以及 skills.json 的结构。"""
    if not isinstance(bundle, dict):
        raise ValueError("备份包必须是 JSON 对象")
    if bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError("备份包格式标识不匹配")
    if int(bundle.get("version", 0)) != BUNDLE_VERSION:
        raise ValueError("备份包版本不兼容")
    files = bundle.get("files")
    if not isinstance(files, dict):
        raise ValueError("备份包缺少 files 对象")
    if set(files) - set(FILES):
        raise ValueError("备份包含有白名单之外的文件")
    raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("备份包超过 32MB 上限")
    for name, content in files.items():
        if content is not None and not isinstance(content, (dict, list)):
            raise ValueError(f"{name} 必须是 JSON 对象或数组")
    skills = files.get("skills.json")
    if skills is not None:
        if not isinstance(skills, dict) \
                or not isinstance(skills.get("states"), dict) \
                or not isinstance(skills.get("events"), list):
            raise ValueError("skills.json 结构不合法（缺 states / events）")
        ver = int(skills.get("version", 1))
        if ver < 1 or ver > 6:
            raise ValueError(f"skills.json 版本 {ver} 不受支持")
    return bundle  # type: ignore[return-value]


def write_bundle(web_save: str | os.PathLike[str], bundle: dict) -> list[str]:
    """原子写入；任一文件失败时已写的文件保留，但调用方应先备份。"""
    root = Path(web_save)
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for name in FILES:
        if name not in bundle["files"]:
            continue
        content = bundle["files"][name]
        path = root / name
        if content is None:
            if path.exists():
                path.unlink()
            continue
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(content, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
        written.append(name)
    return written


def make_backup(web_save: str | os.PathLike[str], *,
                now: int | None = None, tag: str = "daily") -> str:
    """把当前存档打包成 zip。返回文件名。"""
    root = Path(web_save)
    folder = root / BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    t = _now(now)
    stamp = datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{tag}-{stamp}.zip"
    path = folder / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in FILES:
            src = root / f
            if src.exists():
                zf.write(src, arcname=f)
    return name


def rotate_backups(web_save: str | os.PathLike[str], *,
                   now: int | None = None,
                   keep_days: int = BACKUP_KEEP_DAYS) -> list[str]:
    """删除超过保留天数的每日备份。只删本目录里符合命名规则的 zip。"""
    folder = Path(web_save) / BACKUP_DIR
    if not folder.exists():
        return []
    cutoff = _stamp(_now(now) - keep_days * 86_400)
    removed = []
    for p in sorted(folder.glob("daily-*.zip")):
        day = p.name[len("daily-"):][:8]
        if len(day) == 8 and day.isdigit() and day < cutoff:
            p.unlink()
            removed.append(p.name)
    return removed


def daily_backup(web_save: str | os.PathLike[str], *,
                 now: int | None = None) -> tuple[str | None, list[str]]:
    """每天最多生成一份备份，并清理超过 14 天的旧备份。"""
    folder = Path(web_save) / BACKUP_DIR
    day = _stamp(_now(now))
    existing = sorted(folder.glob(f"daily-{day}-*.zip")) if folder.exists() else []
    created = None
    if not existing:
        created = make_backup(web_save, now=now, tag="daily")
    return created, rotate_backups(web_save, now=now)
