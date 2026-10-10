"""终端着色与排版的最小子集。

为什么不用 ``rich``
------------------
它只作为 litellm 的**传递依赖**存在，不在 ``pyproject.toml`` 里。脚本依赖一个
没被声明的包，某天 litellm 换掉它就会在别人机器上炸。这里只用 ANSI 转序列，
约 40 行，且在没有 TTY 时自动退化成纯文本（CI 日志、重定向到文件都不会带上
一堆乱码）。

``NO_COLOR`` 环境变量（https://no-color.org/）一并尊重。
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap

_RESET = "\033[0m"
_CODES = {
    "dim": "2",
    "bold": "1",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "gray": "90",
}


def supports_color() -> bool:
    """只有真终端且用户没关色彩时才着色。"""
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


SUPPORTS_COLOR = supports_color()


def paint(text: str, *styles: str) -> str:
    if not SUPPORTS_COLOR or not styles:
        return text
    codes = ";".join(_CODES.get(style, "0") for style in styles)
    return f"\033[{codes}m{text}{_RESET}"


def width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:  # noqa: BLE001 - 拿不到终端尺寸就用默认值，不该因此崩
        return default


def rule(title: str = "", char: str = "─") -> str:
    line = char * max(8, width())
    if not title:
        return paint(line, "gray")
    label = f" {title} "
    pad = max(0, (width() - len(label)) // 2)
    return (
        paint(char * pad, "gray")
        + paint(label, "bold", "cyan")
        + paint(char * max(0, width() - pad - len(label)), "gray")
    )


def wrap(text: str, indent: str = "  ", initial: str | None = None) -> str:
    """按终端宽度折行，续行用 ``indent`` 对齐。"""
    return textwrap.fill(
        text,
        width=max(40, width() - len(indent)),
        initial_indent=initial if initial is not None else indent,
        subsequent_indent=indent,
    )


def kv(key: str, value: str, key_width: int = 14) -> str:
    return paint(f"{key:<{key_width}}", "dim") + value


def erase_line() -> str:
    """清掉当前行并回到行首——用来把"思考中"替换成结果。"""
    # 非 TTY 时返回空串：往文件或管道里写回车和空格只会污染日志。
    return "\r\033[2K" if SUPPORTS_COLOR else ""


__all__ = [
    "SUPPORTS_COLOR",
    "erase_line",
    "kv",
    "paint",
    "rule",
    "supports_color",
    "width",
    "wrap",
]
