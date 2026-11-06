from __future__ import annotations

import json
from pathlib import Path
import pytest

from app.config import (
    Settings,
    ConfigError,
    load_settings,
    resolve_config_path,
    resolve_storage_path,
    settings_from_raw,
    settings_to_raw,
)
from app.runtime import ClassroomService
from main import main


def test_config_path_precedence_and_legacy_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BTC_SIM_CONFIG", raising=False)
    assert resolve_config_path() == tmp_path / "config.json"
    legacy = tmp_path / "config.json"
    legacy.write_text("{}", encoding="utf-8")
    assert resolve_config_path() == legacy
    local = tmp_path / "config.local.json"
    local.write_text("{}", encoding="utf-8")
    assert resolve_config_path() == legacy
    legacy.unlink()
    assert resolve_config_path() == local
    monkeypatch.setenv("BTC_SIM_CONFIG", str(tmp_path / "external.json"))
    assert resolve_config_path() == tmp_path / "external.json"
    assert resolve_config_path("explicit.json") == tmp_path / "explicit.json"


def test_external_config_is_initialized_without_starting_server(tmp_path, monkeypatch, capsys):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    external = tmp_path / "classroom" / "config.local.json"
    monkeypatch.setattr("sys.argv", ["main.py", "--config", str(external), "--init-config"])
    main()
    settings = load_settings(external)
    assert not settings.admin_key
    assert "admin_key" not in json.loads(external.read_text(encoding="utf-8"))
    assert str(external) in capsys.readouterr().out
    assert resolve_storage_path(settings, external) == external.parent / "data" / "classroom.db"
    assert not list(checkout.iterdir())
    assert not (external.parent / "data").exists()
    assert load_settings(external).admin_key == settings.admin_key


def test_utf8_bom_config_preserves_chinese_and_settings(tmp_path):
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"classroom_name": "测试课堂", "web_port": 8123}), encoding="utf-8-sig")
    settings = load_settings(path)
    assert settings.classroom_name == "测试课堂"
    assert settings.web_port == 8123
    assert load_settings(path) == settings


def test_teacher_saves_to_selected_external_config(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    path = tmp_path / "classroom" / "config.local.json"
    monkeypatch.setenv("BTC_SIM_CONFIG", str(path))
    service = ClassroomService()
    try:
        original_key = service.settings.admin_key
        service.update_settings({"classroom_name": "更新课堂"})
        persisted = load_settings(path)
        assert persisted.classroom_name == "更新课堂"
        assert persisted.admin_key == original_key
        assert not list(checkout.iterdir())
    finally:
        service.store.close()


def test_public_template_has_no_admin_key_and_matches_defaults():
    template = Path(__file__).resolve().parents[1] / "config.example.json"
    raw = json.loads(template.read_text(encoding="utf-8"))
    assert not {"admin_key", "chain", "pool"} & raw.keys()
    assert settings_from_raw(raw) == Settings()
    saved = settings_to_raw(settings_from_raw(raw))
    assert {key: saved[key] for key in raw} == raw


@pytest.mark.parametrize("raw", [{}, {"node_name": ""}, {"node_name": "   "}, {"node_name": None}])
def test_node_name_generated_once_and_persisted(tmp_path, raw):
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"admin_key":"KEEPKEY", **raw}), encoding="utf-8")
    first = load_settings(path)
    assert first.node_name.startswith("node-")
    assert first.peers == []
    assert first.admin_key == "KEEPKEY"
    assert load_settings(path).node_name == first.node_name
    assert json.loads(path.read_text(encoding="utf-8"))["node_name"] == first.node_name


def test_manual_node_name_survives_teacher_save(tmp_path):
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"node_name":"教师节点A", "admin_key":"KEEPKEY", "peers":[]}), encoding="utf-8")
    service = ClassroomService(path)
    try:
        assert service.public_state()["node_name"] == "教师节点A"
        service.update_settings({"classroom_name":"第二课堂"})
        assert load_settings(path).node_name == "教师节点A"
    finally:
        service.store.close()


def test_peers_schema_and_unsupported_network_fail_clearly(tmp_path):
    raw = settings_from_raw({"peers":["http://127.0.0.1:8001/", "http://127.0.0.1:8001"]})
    assert raw.peers == ["http://127.0.0.1:8001"]
    assert settings_from_raw({"peers": ["1.1.1.1", "http://1.1.1.1:8000", "::1"]}).peers == [
        "http://1.1.1.1:8000", "http://[::1]:8000",
    ]
    for peers in (None, "http://host", [42], ["file:///tmp"], ["http://user:pass@host"], ["http://host:0"]):
        with pytest.raises(ConfigError):
            settings_from_raw({"peers": peers})
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"peers":["http://127.0.0.1:8001"]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="P2P"):
        ClassroomService(path)
    assert not (tmp_path / "data").exists()
