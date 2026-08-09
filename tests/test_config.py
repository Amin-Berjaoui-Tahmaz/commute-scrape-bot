import pytest

import ns_tool.config as config_module
from ns_tool.config import (
    DEFAULT_MAX_TRANSFER_GAP_MINUTES,
    DEFAULT_WORK_STATIONS,
    load_config,
    run_setup_wizard,
)


def test_load_config_missing_file_returns_builtin_defaults(tmp_path):
    config = load_config(tmp_path / "does-not-exist.yaml")
    assert config.work_stations == DEFAULT_WORK_STATIONS
    assert config.max_gap_minutes == DEFAULT_MAX_TRANSFER_GAP_MINUTES


def test_load_config_reads_both_fields(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "work_stations:\n  - Papendrecht\n  - Dordrecht\nmax_gap_minutes: 30\n"
    )
    config = load_config(path)
    assert config.work_stations == ["Papendrecht", "Dordrecht"]
    assert config.max_gap_minutes == 30


def test_load_config_missing_field_falls_back_to_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("work_stations:\n  - Utrecht\n")  # no max_gap_minutes
    config = load_config(path)
    assert config.work_stations == ["Utrecht"]
    assert config.max_gap_minutes == DEFAULT_MAX_TRANSFER_GAP_MINUTES


def test_load_config_empty_file_returns_builtin_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("")
    config = load_config(path)
    assert config.work_stations == DEFAULT_WORK_STATIONS
    assert config.max_gap_minutes == DEFAULT_MAX_TRANSFER_GAP_MINUTES


def test_load_config_not_a_mapping_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_config(path)


def test_load_config_invalid_yaml_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("work_stations: [unclosed\n")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_config(path)


def test_load_config_work_stations_wrong_type_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("work_stations: Sliedrecht\n")  # should be a list
    with pytest.raises(ValueError, match="work_stations"):
        load_config(path)


def test_load_config_max_gap_wrong_type_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('work_stations:\n  - Sliedrecht\nmax_gap_minutes: "20"\n')
    with pytest.raises(ValueError, match="max_gap_minutes"):
        load_config(path)


def test_load_config_default_path_uses_package_config_when_available(
    monkeypatch, tmp_path
):
    package_dir = tmp_path / "ns_tool"
    package_dir.mkdir()
    (package_dir / "config.yaml").write_text("max_gap_minutes: 250\n")
    (package_dir / "config.py").write_text("")

    monkeypatch.setattr(config_module, "__file__", str(package_dir / "config.py"))
    monkeypatch.chdir(tmp_path)
    config = load_config()

    assert config.max_gap_minutes == 250
    assert config.work_stations == config_module.DEFAULT_WORK_STATIONS


def test_run_setup_wizard_writes_answers_to_file(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    answers = iter(["Papendrecht, Dordrecht", "15"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    config = run_setup_wizard(path)

    assert config.work_stations == ["Papendrecht", "Dordrecht"]
    assert config.max_gap_minutes == 15

    # And loading the file back gives the same thing -- the wizard's
    # output is valid input for load_config.
    assert load_config(path) == config


def test_run_setup_wizard_blank_answers_use_defaults(monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr("builtins.input", lambda _: "")

    config = run_setup_wizard(path)

    assert config.work_stations == DEFAULT_WORK_STATIONS
    assert config.max_gap_minutes == DEFAULT_MAX_TRANSFER_GAP_MINUTES
