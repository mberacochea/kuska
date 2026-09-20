"""PyInstaller entry point - a plain script, since the frozen binary cannot use
the package-relative import that `python -m kuska` relies on."""

from kuska.cli import main

if __name__ == "__main__":
    main()
