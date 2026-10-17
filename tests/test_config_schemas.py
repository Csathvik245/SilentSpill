import pytest
from pydantic import ValidationError

from backend.app import schemas
from backend.app.config import ConfigError, load_settings

REQUIRED = {
    "NHDPLUS_GPKG_PATH": "gpkg",
    "ECHO_NPDES_DIR": "npdes",
    "ECHO_EFFLUENT_DIR": "eff",
    "TRI_CSV_PATH": "tri.csv",
    "REACH_COMID": "10000500083697",
    "NWIS_SITE_ID": "01634000",
    "NOAA_STATION_ID": "GHCND:USW00013731",
    "RAIN_THRESHOLD_INCHES": "0.5",
    "RAIN_WINDOW_HOURS": "24",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in list(REQUIRED) + [
        "GROQ_API_KEY", "GROQ_MODEL", "NOAA_CDO_TOKEN", "ROBOFLOW_API_KEY",
        "NAIP_PATH", "DEM_TIF_PATH", "YOLO_DATASET_DIR", "SIMULATION_MODE",
    ]:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "gpkg").write_text("x")
    (tmp_path / "tri.csv").write_text("x")
    (tmp_path / "npdes").mkdir()
    (tmp_path / "eff").mkdir()
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, str(tmp_path / v) if k.endswith(("_PATH", "_DIR")) else v)
    return tmp_path


def test_loads_with_required_values(env):
    s = load_settings(env_file=None)
    assert s.reach_nhdplusid == 10000500083697
    assert s.rain_threshold_inches == 0.5 and s.rain_window_hours == 24
    assert s.simulation_mode is True


def test_optional_values_may_be_absent(env):
    s = load_settings(env_file=None)
    assert s.groq_api_key is None and not s.groq_configured
    assert s.naip_path is None and not s.naip_available
    assert not s.dem_available and not s.yolo_available


def test_empty_required_value_fails_fast_with_name(env, monkeypatch):
    monkeypatch.setenv("REACH_COMID", "")
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=None)
    assert "REACH_COMID" in str(exc.value)


def test_missing_required_path_fails_fast(env, monkeypatch):
    monkeypatch.setenv("TRI_CSV_PATH", str(env / "nope.csv"))
    with pytest.raises(ConfigError, match="TRI_CSV_PATH"):
        load_settings(env_file=None)


def test_rain_threshold_must_be_positive(env, monkeypatch):
    monkeypatch.setenv("RAIN_WINDOW_HOURS", "0")
    with pytest.raises(ConfigError, match="RAIN_WINDOW_HOURS"):
        load_settings(env_file=None)


def test_violation_window_years_setting(env, monkeypatch):
    assert load_settings(env_file=None).violation_window_years == 5
    monkeypatch.setenv("VIOLATION_WINDOW_YEARS", "10")
    assert load_settings(env_file=None).violation_window_years == 10
    monkeypatch.setenv("VIOLATION_WINDOW_YEARS", "0")
    with pytest.raises(ConfigError, match="VIOLATION_WINDOW_YEARS"):
        load_settings(env_file=None)


def test_secrets_never_appear_in_output(env, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_supersecret")
    monkeypatch.setenv("NOAA_CDO_TOKEN", "tok_supersecret")
    s = load_settings(env_file=None)
    assert "supersecret" not in repr(s) and "supersecret" not in str(s.public_dict())
    monkeypatch.setenv("REACH_COMID", "not-a-number")
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=None)
    assert "not-a-number" not in str(exc.value) and "supersecret" not in str(exc.value)


def test_relative_paths_resolve_against_project_root(env, monkeypatch):
    monkeypatch.setenv("NAIP_PATH", "data/naip/x.tif")
    s = load_settings(env_file=None)
    assert s.naip_path.is_absolute() and s.naip_path.parts[-3:] == ("data", "naip", "x.tif")


def test_responses_carry_flags():
    r = schemas.TriageResult(classification="incident", confidence=0.8, reasons=["x"])
    assert r.simulated is True and r.requires_human_review is True


def test_human_review_flag_cannot_be_disabled():
    with pytest.raises(ValidationError):
        schemas.TriageResult(
            classification="noise", confidence=0.1, reasons=[], requires_human_review=False
        )


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        schemas.TriageResult(classification="noise", confidence=1.5, reasons=[])


def test_banner_mentions_simulation_and_review():
    assert "SIMULATED" in schemas.BANNER and "human review" in schemas.BANNER
