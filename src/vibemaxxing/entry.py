"""The console scripts `vibe` and `vibemaxxing`.

`vibe statusline` runs on every redraw of Claude Code's status line, so it is answered
here, before `cli` imports the dashboard, the web server and urllib: about 200 ms that a
status line does not have. Everything else is `cli.main`, unchanged.
"""

from __future__ import annotations

import sys


def main() -> int:
    if sys.argv[1:] == ["statusline"]:
        from vibemaxxing import statusline

        return statusline.main()
    from vibemaxxing import cli

    return cli.main()
