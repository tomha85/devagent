from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Route domain subcommands without changing the existing software CLI."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) >= 2 and arguments[0] == "plc" and arguments[1] == "inspect":
        from devagent.plc.inspect_cli import main as plc_inspect_main

        return plc_inspect_main(arguments[2:])
    if arguments and arguments[0] == "plc":
        from devagent.plc.cli import main as plc_main

        return plc_main(arguments[1:])

    from devagent.cli import main as software_main

    return software_main(arguments)
