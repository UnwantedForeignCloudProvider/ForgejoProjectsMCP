"""A local .env is auto-loaded, and does not override real env vars."""

import os

from forgejo_projects_mcp import _env


def test_load_env_reads_dotenv_from_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FPM_TEST_VAR", raising=False)
    (tmp_path / ".env").write_text("FPM_TEST_VAR=from_dotenv\n")
    _env.load_env()
    assert os.environ["FPM_TEST_VAR"] == "from_dotenv"


def test_existing_env_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FPM_TEST_VAR", "from_real_env")
    (tmp_path / ".env").write_text("FPM_TEST_VAR=from_dotenv\n")
    _env.load_env()
    assert os.environ["FPM_TEST_VAR"] == "from_real_env"   # dotenv does not override
