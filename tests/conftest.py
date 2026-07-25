"""Shared test fixtures.

``_CliArgumentParser._json_hint`` is class-level state that ``main()`` sets
from raw argv on every call and never clears. Inside one CLI process that is
correct — each ``main()`` sets it before parsing. Across a test session it is
sticky: a test that runs ``main([..., "--json"])`` leaves the flag ``True``
for every later test, so a parser built directly (not through ``main()``)
renders its parse errors as JSON instead of the ``error:``/``hint:`` text the
error contract promises. That coupling is invisible per-file — it only bites
when a ``--json``-ending module happens to run before a module that builds its
own parser — so reset it around every test rather than leaving the ordering to
luck.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from webcam_cli.cli import _CliArgumentParser


@pytest.fixture(autouse=True)
def _reset_json_hint() -> Iterator[None]:
    _CliArgumentParser._json_hint = False
    yield
    _CliArgumentParser._json_hint = False
