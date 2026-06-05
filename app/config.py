from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from app.utils.crypto import hash_json


DEFAULT_CONFIG: dict[str, Any] = {
    "version": "0.1.0",
    "network_id": "btc-sim-classroom",
    "node_name": "server1",
    "listen_ip": "0.0.0.0",
    "enable_ipv6": True,
    "advertise_ip": None,
    "advertise_ipv6": None,
    "listen_port": 7464,
    "web_host": "127.0.0.1",
    "web_port": 8000,
    "difficulty_mode": "adaptive_target",
    "difficulty": 5,
    "initial_target_prefix": "000",
    "auto_difficulty": True,
    "target_block_seconds": 60,
    "difficulty_adjustment_interval": 10,
    "difficulty_adjustment_tolerance": 0.25,
    "difficulty_max_step": 1,
    "target_adjustment_base_bps": 1000,
    "target_adjustment_max_multiplier": 5,
    "min_difficulty": 0,
    "max_difficulty": 12,
    "mining_reward": 50.0,
    "max_block_transactions": 100,
    "mempool_max_bytes": 314572800,
    "sync_interval_seconds": 10,
    "servers": [
        ["127.0.0.1", 7464],
        ["127.0.0.1", 7465],
        ["127.0.0.1", 7466],
    ],
    "storage": {
        "type": "sqlite",
        "path": "./data/blockchain.db",
    },
}

CONSENSUS_PARAM_KEYS = (
    "difficulty_mode",
    "difficulty",
    "initial_target_prefix",
    "auto_difficulty",
    "target_block_seconds",
    "difficulty_adjustment_interval",
    "difficulty_adjustment_tolerance",
    "difficulty_max_step",
    "target_adjustment_base_bps",
    "target_adjustment_max_multiplier",
    "min_difficulty",
    "max_difficulty",
    "mining_reward",
    "max_block_transactions",
)


def consensus_params(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config.get(key, DEFAULT_CONFIG[key]) for key in CONSENSUS_PARAM_KEYS}


def chain_params_hash(config: dict[str, Any]) -> str:
    return hash_json(consensus_params(config))


def network_identity(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "network_id": str(config.get("network_id", DEFAULT_CONFIG["network_id"])),
        "chain_params_hash": chain_params_hash(config),
        "consensus_params": consensus_params(config),
    }


def _merge_defaults(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: str | Path = "config.json") -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    config = _merge_defaults(DEFAULT_CONFIG, raw)
    config["_config_path"] = str(path.resolve())
    config["_config_dir"] = str(path.resolve().parent)
    return config


def save_config(config: dict[str, Any]) -> None:
    """Persist public configuration values back to the active config file."""
    path = Path(config.get("_config_path", "config.json"))
    public_config = {
        key: value
        for key, value in config.items()
        if not key.startswith("_")
    }
    path.write_text(
        json.dumps(public_config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def resolve_project_path(config: dict[str, Any], value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (Path(config.get("_config_dir", ".")).resolve() / path).resolve()
