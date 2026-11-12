"""Config loading: env_file for launches without a shell (desktop icon, scheduled runs)."""


def test_env_file_fills_missing_variables_only(tmp_path, monkeypatch):
    from studio_pulse.config import load_config

    monkeypatch.setenv("PULSE_TEST_PROJECT", "x")  # recorded, so teardown removes it again
    monkeypatch.delenv("PULSE_TEST_PROJECT")
    monkeypatch.setenv("PULSE_TEST_KEPT", "from-shell")
    (tmp_path / "private.env").write_text(
        "# private\nexport PULSE_TEST_PROJECT=my-proj\nPULSE_TEST_KEPT=from-file\n\nnot a line\n",
        encoding="utf-8",
    )
    (tmp_path / "pulse.yaml").write_text(
        "env_file: [private.env, missing.env]\n"
        "title: ${PULSE_TEST_PROJECT}/${PULSE_TEST_KEPT}\napps: [{id: a}]\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path / "pulse.yaml")
    assert cfg.title == "my-proj/from-shell"  # the shell wins over the file
