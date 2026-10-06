"""Final review A4 and A6: the ``python app.py`` start path and the startup
hint for an unknown ``CONNECTIVITY_PP_MODE``.

* ``python app.py`` (local) listens on 127.0.0.1 unless ``HOST`` says
  otherwise: without XSUAA the app is open, so it must not listen on every
  interface by default. Cloud Foundry starts uvicorn from ``mta.yaml`` with
  its own ``--host``.
* An unknown ``CONNECTIVITY_PP_MODE`` is said once at startup; the refusal
  at request time stays what enforces it.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.testdb import use_test_database  # noqa: E402

use_test_database()
os.environ.pop("VCAP_SERVICES", None)
os.environ.pop("VCAP_APPLICATION", None)

import app as app_module  # noqa: E402


def test_python_app_py_listens_on_loopback_unless_host_is_set():
    assert app_module.serve_address({}) == ("127.0.0.1", 7932)
    assert app_module.serve_address({"PORT": "8080"}) == ("127.0.0.1", 8080)
    assert app_module.serve_address({"HOST": "0.0.0.0", "PORT": "8080"}) == ("0.0.0.0", 8080)
    assert app_module.serve_address({"HOST": "  "}) == ("127.0.0.1", 7932)


def test_the_start_block_uses_that_address_and_prints_it():
    source = Path(app_module.__file__).read_text()
    block = source[source.index('if __name__ == "__main__":'):]
    assert "serve_address()" in block and "uvicorn.run(app, host=host, port=port)" in block
    assert '"0.0.0.0"' not in block and "127.0.0.1" not in block


def test_cloud_foundry_starts_uvicorn_with_its_own_host():
    assert "python -m uvicorn app:app --host 0.0.0.0 --port $PORT" in (
        ROOT / "mta.yaml"
    ).read_text()


def test_an_unknown_pp_mode_is_said_once_at_startup_without_the_value(caplog):
    with caplog.at_level(logging.WARNING, logger=app_module.logger.name):
        assert app_module.warn_on_unknown_pp_mode({"CONNECTIVITY_PP_MODE": "s3cret-typo"}) is True
    (record,) = [r for r in caplog.records if "CONNECTIVITY_PP_MODE" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "exchange" in record.getMessage() and "header" in record.getMessage()
    assert "s3cret-typo" not in caplog.text


def test_a_known_or_absent_pp_mode_says_nothing(caplog):
    with caplog.at_level(logging.DEBUG):
        for environ in ({}, {"CONNECTIVITY_PP_MODE": " Header "}, {"CONNECTIVITY_PP_MODE": ""}):
            assert app_module.warn_on_unknown_pp_mode(environ) is False
    assert "CONNECTIVITY_PP_MODE" not in caplog.text


def test_the_lifespan_runs_the_check():
    assert "warn_on_unknown_pp_mode()" in inspect.getsource(app_module.lifespan)
