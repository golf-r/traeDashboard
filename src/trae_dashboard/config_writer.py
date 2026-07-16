"""Write-back helpers for config.yaml and .env.

Mutates only the specific sections exposed via the UI. PyYAML rewrites the
loaded document, so comments and original formatting are not preserved.

All file writes use a temporary file plus ``os.replace`` to avoid leaving
half-written configuration files.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

import yaml

from .validation import is_valid_email, normalize_email

HEADER_COMMENT = "# 本文件的 email.* 由 Trae Dashboard 管理,手动编辑可能被覆盖"
_EMAIL_KEY = "email"
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class _DoubleQuotedString(str):
    """String marker for scalars that must retain double quotes in YAML."""


def _represent_double_quoted_string(
    dumper: yaml.SafeDumper, value: _DoubleQuotedString
) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')


yaml.SafeDumper.add_representer(_DoubleQuotedString, _represent_double_quoted_string)


def _load_yaml(path: Path) -> dict:
    """Load a YAML mapping, raising ``RuntimeError`` on parse failure."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip() != HEADER_COMMENT]
    try:
        data = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError as exc:
        raise RuntimeError(f"failed to parse YAML config: {path}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError("config YAML must contain a mapping")
    return data


def _ensure_header(text: str) -> str:
    """Prepend the managed-file header unless it is already first."""
    if text.lstrip().startswith(HEADER_COMMENT):
        return text
    return HEADER_COMMENT + "\n" + text


def _atomic_write(path: Path, text: str) -> None:
    """Write text atomically using a sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _dump_yaml(data: dict) -> str:
    """Dump YAML in insertion order and retain quoted ``send_time`` values."""
    dump_data = data
    email = data.get(_EMAIL_KEY)
    if isinstance(email, dict) and isinstance(email.get("send_time"), str):
        dump_data = dict(data)
        dump_email = dict(email)
        dump_email["send_time"] = _DoubleQuotedString(email["send_time"])
        dump_data[_EMAIL_KEY] = dump_email
    return yaml.safe_dump(
        dump_data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def save_recipients(config_path: Path, recipients: Iterable[str]) -> None:
    """Update ``email.recipients`` while preserving other email fields.

    Empty list is allowed only when ``email.enabled`` is False (or absent).
    If the file currently has ``enabled: true`` and a non-empty recipients
    list, removing all of them would write a config that ``load_config``
    refuses to load — the service would fail to restart. Refuse the write
    in that case so the caller gets an actionable error.
    """
    cleaned: list[str] = []
    for recipient in recipients:
        addr = normalize_email(recipient)
        if not addr:
            continue
        if not is_valid_email(addr):
            raise ValueError(f"invalid recipient email: {recipient!r}")
        cleaned.append(addr)

    seen: set[str] = set()
    deduped = [addr for addr in cleaned if not (addr in seen or seen.add(addr))]

    data = _load_yaml(config_path)
    email = data.get(_EMAIL_KEY)
    if email is None:
        email = {"enabled": False}
        data[_EMAIL_KEY] = email
    elif not isinstance(email, dict):
        raise RuntimeError("email config must be a YAML mapping")

    if not deduped and bool(email.get("enabled")):
        raise ValueError(
            "refusing to clear recipients while email.enabled is true; "
            "the resulting config cannot be loaded. Disable email first "
            "or keep at least one recipient."
        )

    email["recipients"] = deduped
    _atomic_write(config_path, _ensure_header(_dump_yaml(data)))


def save_email_config(
    config_path: Path,
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    from_addr: str,
    send_time: str,
) -> None:
    """Update SMTP fields while preserving all other ``email`` values."""
    if not (smtp_host or "").strip():
        raise ValueError("smtp_host must not be empty")
    if (
        isinstance(smtp_port, bool)
        or not isinstance(smtp_port, int)
        or not (1 <= smtp_port <= 65535)
    ):
        raise ValueError(f"smtp_port must be 1..65535, got {smtp_port!r}")
    if not is_valid_email(smtp_user):
        raise ValueError(f"invalid smtp_user: {smtp_user!r}")
    if not is_valid_email(from_addr):
        raise ValueError(f"invalid from_addr: {from_addr!r}")
    if not _TIME_RE.match(send_time or ""):
        raise ValueError(f"send_time must match HH:MM, got {send_time!r}")

    data = _load_yaml(config_path)
    email = data.get(_EMAIL_KEY)
    if email is None:
        email = {"enabled": False}
        data[_EMAIL_KEY] = email
    elif not isinstance(email, dict):
        raise RuntimeError("email config must be a YAML mapping")
    email["smtp_host"] = smtp_host.strip()
    email["smtp_port"] = int(smtp_port)
    email["smtp_user"] = smtp_user.strip()
    email["from_addr"] = from_addr.strip()
    email["send_time"] = send_time.strip()
    _atomic_write(config_path, _ensure_header(_dump_yaml(data)))


def _needs_quoting(value: str) -> bool:
    if value == "":
        return True
    return any(ch.isspace() or ch in '= "\\' for ch in value)


def _format_env_line(key: str, value: str) -> str:
    if _needs_quoting(value):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r", "\\r")
            .replace("\n", "\\n")
        )
        return f'{key}="{escaped}"\n'
    return f"{key}={value}\n"


def save_env_var(env_path: Path, key: str, value: str) -> None:
    """Set or replace ``key`` in a dotenv file, preserving other lines."""
    if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise ValueError(f"invalid env var name: {key!r}")

    original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    new_line = _format_env_line(key, value)
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(original):
        updated = pattern.sub(lambda _: new_line.rstrip("\n"), original)
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        separator = "" if not original or original.endswith("\n") else "\n"
        updated = original + separator + new_line

    _atomic_write(env_path, updated)
