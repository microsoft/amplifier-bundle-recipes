"""Dual entry point: ``python -m amplifier_recipe_runner``.

The console script declared in ``pyproject.toml``
(``recipe-runner = "amplifier_recipe_runner.cli:main"``) and this module call
the *same* :func:`~amplifier_recipe_runner.cli.main`, so there is exactly one
command-line behaviour no matter how it is invoked.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
