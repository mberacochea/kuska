"""PyInstaller entry point - a plain script, since the frozen binary cannot use
the package-relative import that `python -m achka` relies on."""

from achka.cli import main

if __name__ == "__main__":
    main()
