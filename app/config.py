"""课堂配置：一个 JSON 文件描述网络参数，教师可在教师页修改。"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.units import SAT_PER_COIN

VERSION = "2.0.0"


class ConfigError(Exception):
    """配置内容不合法时抛出，启动阶段直接失败而不是带病运行。"""


@dataclass(frozen=True)
class ChainSettings:
    """共识参数：所有区块的有效性都由这些数字决定。"""

    initial_difficulty: int = 24
    difficulty_min: int = 4
    difficulty_max: int = 56
    difficulty_override: int | None = None
    retarget_interval_blocks: int = 8
    target_block_seconds: int = 15
    retarget_tolerance_percent: int = 20
    stall_seconds: int = 60
    initial_reward_coins: int = 50
    halving_interval_blocks: int = 64
    finder_bonus_percent: int = 5
    max_block_transactions: int = 80
    block_max_bytes: int = 65_536
    packet_expiry_seconds: int = 900
    max_packet_claims: int = 50
    max_nfts_per_player: int = 20
    min_fee_sat: int = 10_000
    max_fee_sat: int = SAT_PER_COIN

    def subsidy_sat(self, height: int) -> int:
        """第 height 个区块的基础奖励，每 halving_interval_blocks 块减半。"""
        halvings = max(height - 1, 0) // self.halving_interval_blocks
        sat = self.initial_reward_coins * SAT_PER_COIN
        return sat >> halvings if halvings < 64 else 0

    def next_halving_height(self, height: int) -> int:
        return (max(height, 0) // self.halving_interval_blocks + 1) * self.halving_interval_blocks + 1


@dataclass(frozen=True)
class PoolSettings:
    """矿池参数：决定学生多久提交一次份额、以及份额对应的难度。"""

    share_target_seconds: float = 2.0
    initial_share_bits: int = 12
    share_bits_min: int = 8
    share_bits_max: int = 40
    job_refresh_seconds: int = 8
    max_shares_per_second: int = 8


@dataclass(frozen=True)
class Settings:
    classroom_name: str = "区块链科普课堂"
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    storage_path: str = "./data/classroom.db"
    admin_key: str = ""
    chain: ChainSettings = field(default_factory=ChainSettings)
    pool: PoolSettings = field(default_factory=PoolSettings)


def _int_field(raw: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    if value < minimum or value > maximum:
        raise ConfigError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _chain_from_raw(raw: dict[str, Any]) -> ChainSettings:
    if not isinstance(raw, dict):
        raise ConfigError("chain 必须是对象")
    override = raw.get("difficulty_override")
    if override is not None and (isinstance(override, bool) or not isinstance(override, int)):
        raise ConfigError("difficulty_override 必须是整数或 null")
    chain = ChainSettings(
        initial_difficulty=_int_field(raw, "initial_difficulty", 24, minimum=0, maximum=256),
        difficulty_min=_int_field(raw, "difficulty_min", 4, minimum=0, maximum=256),
        difficulty_max=_int_field(raw, "difficulty_max", 56, minimum=1, maximum=256),
        difficulty_override=override,
        retarget_interval_blocks=_int_field(raw, "retarget_interval_blocks", 8, minimum=1, maximum=2016),
        target_block_seconds=_int_field(raw, "target_block_seconds", 15, minimum=1, maximum=3600),
        retarget_tolerance_percent=_int_field(raw, "retarget_tolerance_percent", 20, minimum=0, maximum=90),
        stall_seconds=_int_field(raw, "stall_seconds", 60, minimum=0, maximum=86_400),
        initial_reward_coins=_int_field(raw, "initial_reward_coins", 50, minimum=1, maximum=21_000_000),
        halving_interval_blocks=_int_field(raw, "halving_interval_blocks", 64, minimum=1, maximum=1_000_000),
        finder_bonus_percent=_int_field(raw, "finder_bonus_percent", 5, minimum=0, maximum=90),
        max_block_transactions=_int_field(raw, "max_block_transactions", 80, minimum=1, maximum=10_000),
        block_max_bytes=_int_field(raw, "block_max_bytes", 65_536, minimum=1024, maximum=4_194_304),
        packet_expiry_seconds=_int_field(raw, "packet_expiry_seconds", 900, minimum=60, maximum=86_400),
        max_packet_claims=_int_field(raw, "max_packet_claims", 50, minimum=2, maximum=500),
        max_nfts_per_player=_int_field(raw, "max_nfts_per_player", 20, minimum=1, maximum=1000),
        min_fee_sat=_int_field(raw, "min_fee_sat", 10_000, minimum=0, maximum=SAT_PER_COIN),
        max_fee_sat=_int_field(raw, "max_fee_sat", SAT_PER_COIN, minimum=1, maximum=100 * SAT_PER_COIN),
    )
    if chain.difficulty_min > chain.difficulty_max:
        raise ConfigError("difficulty_min 不能大于 difficulty_max")
    if chain.initial_difficulty < chain.difficulty_min or chain.initial_difficulty > chain.difficulty_max:
        raise ConfigError("initial_difficulty 必须在 difficulty_min 与 difficulty_max 之间")
    if chain.difficulty_override is not None and not (
        chain.difficulty_min <= chain.difficulty_override <= chain.difficulty_max
    ):
        raise ConfigError("difficulty_override 必须在 difficulty_min 与 difficulty_max 之间")
    if chain.min_fee_sat > chain.max_fee_sat:
        raise ConfigError("min_fee_sat 不能大于 max_fee_sat")
    return chain


def _pool_from_raw(raw: dict[str, Any]) -> PoolSettings:
    if not isinstance(raw, dict):
        raise ConfigError("pool 必须是对象")
    target_seconds = raw.get("share_target_seconds", 2.0)
    if isinstance(target_seconds, bool) or not isinstance(target_seconds, (int, float)):
        raise ConfigError("share_target_seconds 必须是数字")
    if not 0.2 <= float(target_seconds) <= 60:
        raise ConfigError("share_target_seconds 必须在 0.2 到 60 之间")
    pool = PoolSettings(
        share_target_seconds=float(target_seconds),
        initial_share_bits=_int_field(raw, "initial_share_bits", 12, minimum=1, maximum=64),
        share_bits_min=_int_field(raw, "share_bits_min", 8, minimum=1, maximum=64),
        share_bits_max=_int_field(raw, "share_bits_max", 40, minimum=1, maximum=128),
        job_refresh_seconds=_int_field(raw, "job_refresh_seconds", 8, minimum=1, maximum=600),
        max_shares_per_second=_int_field(raw, "max_shares_per_second", 8, minimum=1, maximum=1000),
    )
    if pool.share_bits_min > pool.share_bits_max:
        raise ConfigError("share_bits_min 不能大于 share_bits_max")
    if not pool.share_bits_min <= pool.initial_share_bits <= pool.share_bits_max:
        raise ConfigError("initial_share_bits 必须在 share_bits_min 与 share_bits_max 之间")
    return pool


def settings_from_raw(raw: dict[str, Any]) -> Settings:
    if not isinstance(raw, dict):
        raise ConfigError("配置文件最外层必须是对象")
    name = str(raw.get("classroom_name", "区块链科普课堂")).strip() or "区块链科普课堂"
    host = str(raw.get("web_host", "0.0.0.0")).strip() or "0.0.0.0"
    port = _int_field(raw, "web_port", 8000, minimum=1, maximum=65_535)
    storage_path = str(raw.get("storage_path", "./data/classroom.db")).strip() or "./data/classroom.db"
    admin_key = str(raw.get("admin_key", "")).strip()
    return Settings(
        classroom_name=name,
        web_host=host,
        web_port=port,
        storage_path=storage_path,
        admin_key=admin_key,
        chain=_chain_from_raw(raw.get("chain", {})),
        pool=_pool_from_raw(raw.get("pool", {})),
    )


def settings_to_raw(settings: Settings) -> dict[str, Any]:
    return {"version": VERSION, **asdict(settings)}


def generate_admin_key() -> str:
    return secrets.token_hex(3).upper()


def load_settings(path: str | Path) -> Settings:
    """读取配置；首次运行会生成配置文件，并写入一个随机教师口令。"""
    config_path = Path(path)
    if not config_path.exists():
        settings = Settings(
            admin_key=generate_admin_key(),
            **{
                key: value
                for key, value in asdict(Settings()).items()
                if key not in {"admin_key", "chain", "pool"}
            },
        )
        save_settings(settings, config_path)
        return settings

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{exc}") from exc
    settings = settings_from_raw(raw)
    if not settings.admin_key:
        settings = Settings(
            classroom_name=settings.classroom_name,
            web_host=settings.web_host,
            web_port=settings.web_port,
            storage_path=settings.storage_path,
            admin_key=generate_admin_key(),
            chain=settings.chain,
            pool=settings.pool,
        )
        save_settings(settings, config_path)
    return settings


def save_settings(settings: Settings, path: str | Path) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(settings_to_raw(settings), indent=2, ensure_ascii=False) + "\n"
    config_path.write_text(text, encoding="utf-8")


def resolve_storage_path(settings: Settings, config_path: str | Path) -> Path:
    storage = Path(settings.storage_path)
    if storage.is_absolute():
        return storage
    return (Path(config_path).resolve().parent / storage).resolve()
