from __future__ import annotations

from .server import run_stdio


def main() -> int:
    return run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
