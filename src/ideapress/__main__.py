"""Entry point for ``python -m ideapress``, identical to the ``ideapress`` console script."""

from __future__ import annotations

from ideapress.cli.main import app

if __name__ == "__main__":
    app()
