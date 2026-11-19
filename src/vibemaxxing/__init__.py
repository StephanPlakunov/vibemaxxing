"""Manage several Claude Code accounts and see pooled plan usage across all of them."""

from __future__ import annotations


def __getattr__(name: str) -> str:
    # On first use, not at import: importlib.metadata costs about 20 ms, and `vibe
    # statusline` runs on every redraw of Claude Code's status line.
    if name == "__version__":
        from importlib.metadata import version

        return version("vibemaxxing")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
