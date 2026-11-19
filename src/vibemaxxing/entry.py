"""The console scripts `vibe` and `vibemaxxing`.

`vibe statusline` runs on every redraw of Claude Code's status line, so it is answered
here, before `cli` imports the dashboard, the web server and urllib: about 200 ms that a
status line does not have. Everything else is `cli.main`, unchanged.
"""

from __future__ import annotations

import sys


def main() -> int:
    rest = sys.argv[2:]
    plain = (
        not rest
        or (len(rest) == 2 and rest[0] == "--session")
        or (len(rest) == 1 and rest[0].startswith("--session="))
    )
    if sys.argv[1:2] == ["statusline"] and plain:
        from vibemaxxing import statusline

        return statusline.main(rest)
    from vibemaxxing import cli

    return cli.main()
