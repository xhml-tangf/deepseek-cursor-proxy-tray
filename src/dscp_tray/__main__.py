"""Allow `python -m dscp_tray` to launch the tray supervisor."""

from .tray import main


if __name__ == "__main__":
    raise SystemExit(main())
