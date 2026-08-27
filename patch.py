#!/usr/bin/env python3
import sys


if sys.version_info < (3, 9):
    print("Ostriv for macOS")
    print("Python 3.9 or newer is required.")
    raise SystemExit(2)

from ostriv_macos.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
