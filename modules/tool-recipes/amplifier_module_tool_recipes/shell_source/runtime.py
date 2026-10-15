"""Bounded, in-process qualification of the engine's selected Bash image.

The marker establishes version evidence under the existing trusted executable
and package-integrity boundary; it is not an authenticity check. No installer,
version-probe subprocess, command prelude, or environment mutation is involved.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass

from .binding import has_occurrences
from .model import ShellSourceError

_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_PREFIX = b"@(#)Bash version "
_MARKER = re.compile(rb"@\(#\)Bash version (\d+\.\d+\.\d+)\(\d+\) release GNU\x00")


@dataclass(frozen=True)
class RuntimeProfile:
    version: str
    sha256: str
    executable: str


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _image(data: bytes) -> bool:
    """Recognize executable image headers, never marker-bearing shell scripts."""
    if (
        data[:4] == b"\x7fELF"
        and len(data) >= 64
        and data[4] in (1, 2)
        and data[5] in (1, 2)
        and data[6] == 1
    ):
        endian = "little" if data[5] == 1 else "big"
        return (
            int.from_bytes(data[16:18], endian) in (2, 3)
            and int.from_bytes(data[20:24], endian) == 1
        )
    if data[:2] == b"MZ" and len(data) >= 64:
        offset = int.from_bytes(data[60:64], "little")
        return (
            offset >= 64
            and offset + 26 <= len(data)
            and data[offset : offset + 4] == b"PE\x00\x00"
            and int.from_bytes(data[offset + 24 : offset + 26], "little")
            in (0x10B, 0x20B)
        )
    if (
        data[:4]
        in (
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
        )
        and len(data) >= 32
    ):
        endian = (
            "little"
            if data[:4] in (b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe")
            else "big"
        )
        return int.from_bytes(data[12:16], endian) == 2
    return False


def require_runtime_profile(executable: str) -> RuntimeProfile:
    """Fail before launch when the selected executable lacks a qualified profile."""
    try:
        with open(executable, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 < before.st_size <= _MAX_IMAGE_BYTES
            ):
                raise ValueError(
                    "selected Bash is not a bounded regular executable image"
                )
            image = stream.read(_MAX_IMAGE_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(executable)
    except (OSError, ValueError) as error:
        raise ShellSourceError(
            "BASH_RUNTIME_PROFILE", f"cannot qualify selected Bash executable: {error}"
        ) from error
    if (
        _identity(before) != _identity(after)
        or _identity(after) != _identity(current)
        or len(image) != before.st_size
    ):
        raise ShellSourceError(
            "BASH_RUNTIME_PROFILE", "selected Bash image changed during qualification"
        )
    if not _image(image) or image.count(_PREFIX) != 1:
        raise ShellSourceError(
            "BASH_RUNTIME_PROFILE",
            "selected executable is not one identifiable GNU Bash image",
        )
    markers = list(_MARKER.finditer(image))
    if len(markers) != 1 or markers[0].group(1) != b"5.2.21":
        raise ShellSourceError(
            "BASH_RUNTIME_PROFILE",
            "selected Bash version has no qualified recipe caller profile (qualified: GNU Bash 5.2.21)",
        )
    return RuntimeProfile(
        "5.2.21", hashlib.sha256(image).hexdigest(), os.path.realpath(executable)
    )


def qualify_template_runtime(template: str, executable: str) -> RuntimeProfile | None:
    """Zero-occurrence scripts retain their existing engine execution contract."""
    return require_runtime_profile(executable) if has_occurrences(template) else None
