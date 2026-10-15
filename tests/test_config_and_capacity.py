import pytest

from dcsim import config, paths, system


def test_defaults_copied_on_first_run(home):
    assert paths.config_file().exists() and paths.interfaces_file().exists()
    assert len(list(paths.profiles_dir().glob("*.yaml"))) >= 10


def test_deleted_profile_is_not_restored_on_next_launch(home):
    config.delete_profile("normal_day")
    paths.ensure_home()
    assert not (paths.profiles_dir() / "normal_day.yaml").exists()
    assert "normal_day" in paths.restore_default_profiles()


def test_every_shipped_profile_validates(home):
    ifs = config.load_interfaces()
    for p in config.list_profiles():
        config.validate_profile(config.load_profile(p["id"]), ifs)


def test_percent_users_scale_with_capacity(home):
    prof = config.validate_profile(config.load_profile("normal_day"))
    small = config.resolve_profile(prof, 100)
    big = config.resolve_profile(prof, 1000)
    assert max(s["users"] for s in big) == 10 * max(s["users"] for s in small)
    assert small[0]["start"] == 0 and small[1]["start"] == small[0]["end"]


def test_profile_validation_messages(home):
    base = {"name": "x", "mix": {"web": 1}, "stages": [{"phase": "peak", "duration": 10, "users": "50%"}]}
    config.validate_profile(base)
    with pytest.raises(config.ConfigError, match="unknown interface"):
        config.validate_profile({**base, "mix": {"nope": 1}})
    with pytest.raises(config.ConfigError, match="phase"):
        config.validate_profile({**base, "stages": [{"phase": "boom", "duration": 10, "users": 1}]})
    with pytest.raises(config.ConfigError, match="percentage"):
        config.validate_profile({**base, "stages": [{"phase": "peak", "duration": 10, "users": "lots"}]})
    with pytest.raises(config.ConfigError, match="only"):
        config.validate_profile({**base, "stages": [{"phase": "peak", "duration": 10, "users": 5, "only": ["api"]}]})


def test_capacity_follows_machine_and_settings(home):
    cfg = config.load_config()
    info = {"logical_cores": 4, "memory_available_mb": 100000, "fd_limit": None}
    assert system.estimate_capacity(cfg, info)["users"] == 4 * cfg["capacity"]["users_per_core"]
    info8 = {**info, "logical_cores": 8}
    assert system.estimate_capacity(cfg, info8)["users"] == 2 * system.estimate_capacity(cfg, info)["users"]
    low_mem = {**info, "memory_available_mb": 300}
    cap = system.estimate_capacity(cfg, low_mem)
    assert cap["limiting_factor"] == "memory"
    cfg["capacity"].update(mode="fixed", fixed_users=77)
    assert system.estimate_capacity(cfg, info)["users"] == 77
    cfg["capacity"].update(mode="measured", measured_users=55)
    assert system.estimate_capacity(cfg, info)["users"] == 55


def test_worker_count(home):
    cfg = config.load_config()
    info = {"logical_cores": 8}
    assert system.worker_count(cfg, 100, info) == 1
    assert system.worker_count(cfg, 2000, info) == 5
    assert system.worker_count(cfg, 100000, info) == 7  # one core left for the server
    cfg["load"]["workers"] = 3
    assert system.worker_count(cfg, 10, info) == 3


def test_settings_save_and_reject_bad_values(home):
    config.set_value("sla.p95_ms", 250)
    assert config.load_config()["sla"]["p95_ms"] == 250
    with pytest.raises(config.ConfigError):
        config.set_value("target.mode", "cloud")
    with pytest.raises(config.ConfigError):
        config.set_value("sla.p95_ms", "fast")
    with pytest.raises(config.ConfigError):
        config.set_value("load.workers", 0)


def test_interface_validation(home):
    ifs = config.load_interfaces()
    config.save_interfaces(ifs)
    bad = dict(ifs)
    bad["web"] = {**ifs["web"], "tasks": [{"method": "GET", "path": "no-slash"}]}
    with pytest.raises(config.ConfigError, match="start with /"):
        config.save_interfaces(bad)


def test_free_port_skips_busy_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    busy = s.getsockname()[1]
    assert system.free_port("127.0.0.1", busy) != busy
    s.close()
