"""Source-checkout entry point. Installed users should invoke ``devagent``."""

from devagent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
