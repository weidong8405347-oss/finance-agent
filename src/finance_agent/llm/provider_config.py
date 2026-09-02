"""P5 provider 自配：配置文件操作与脱敏视图（api 层与 cli 共用）。

纪律：
- api_key 永不以明文出 API（脱敏视图）；保存时 "***"/缺省 = 沿用已存明文；
- 校验走 LLMRouter.from_config（fail-closed 规则单一真相源），候选配置先写临时文件试装，
  非法配置在写入前被拒（422 带原因），不允许把服务推进不可启动状态；
- 文件写入原子化（tmp + replace），热生效由调用方每次重读保证（cli._router）。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .router import LLMRouter, ProviderConfigError

#: 脱敏占位符（UI 回显/保存时「不动这把 key」的哨兵）
KEY_MASK = "***"

CONFIG_FILENAME = "llm-providers.json"


class ProviderConfigValidationError(ValueError):
    """候选配置校验失败（API 层转 422，消息必须可操作）。"""


def load_raw(path: Path) -> dict[str, Any] | None:
    """读自有配置文件原文；不存在 → None；JSON 损坏 → ProviderConfigValidationError。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProviderConfigValidationError(
            f"现有配置文件 JSON 损坏：{path}（{e}）——修复或用合法配置覆盖"
        ) from e


def masked_view(raw: dict[str, Any]) -> dict[str, Any]:
    """配置文件的脱敏视图：明文 key → KEY_MASK；env:VAR 间接引用原样展示（不含秘密）。"""
    out = {**raw, "providers": {}}
    for name, prov in (raw.get("providers") or {}).items():
        key = str(prov.get("api_key") or "")
        shown = key if key.startswith("env:") else (KEY_MASK if key else "")
        out["providers"][name] = {**prov, "api_key": shown}
    return out


def _resolve_key(api_key: str, env: Mapping[str, str]) -> str:
    """env:VAR 间接引用 → 环境变量值；其余原样。"""
    if api_key.startswith("env:"):
        return env.get(api_key[4:], "")
    return api_key


def prepare_candidate(
    payload: dict[str, Any], existing: dict[str, Any] | None
) -> dict[str, Any]:
    """合并 key 继承语义并做结构预检（语义校验在 validate_candidate）。

    - api_key == KEY_MASK 或缺省 → 沿用 existing 同名 provider 的 key；无沿用处 → 报错；
    - providers 为空 → 报错（清空配置请用 reset，不是保存空文件）。
    """
    providers = payload.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ProviderConfigValidationError(
            "providers 不能为空——要回到 pi/.env 兜底配置请用「恢复默认」（删除自有配置文件）"
        )
    merged: dict[str, Any] = {**payload, "providers": {}}
    for name, prov in providers.items():
        if not isinstance(prov, dict):
            raise ProviderConfigValidationError(f"provider {name!r} 必须是对象")
        prov = dict(prov)
        key = str(prov.get("api_key") or "")
        if key in ("", KEY_MASK):
            inherited = str(((existing or {}).get("providers") or {}).get(name, {}).get("api_key") or "")
            if not inherited:
                raise ProviderConfigValidationError(
                    f"provider {name!r} 缺 api_key：无已存配置可继承，请填明文或 env:VAR"
                )
            prov["api_key"] = inherited
        merged["providers"][name] = prov
    return merged


def validate_candidate(payload: dict[str, Any], env: Mapping[str, str] | None = None) -> None:
    """用 LLMRouter.from_config 试装候选配置（fail-closed 规则单一真相源）。

    env 缺省 = .env 打底 + 进程环境（与生产解析口径一致）。
    """
    if env is None:
        from .router import _read_dotenv

        env = {**_read_dotenv(), **os.environ}
    # env:VAR 引用必须可解析——校验的是「写出来的配置在运行时真能装起来」
    resolved = json.loads(json.dumps(payload))  # 深拷贝，不动调用方对象
    for prov in (resolved.get("providers") or {}).values():
        key = str(prov.get("api_key") or "")
        if key.startswith("env:"):
            var = key[4:]
            if not env.get(var):
                raise ProviderConfigValidationError(
                    f"api_key 引用的环境变量 {var} 不存在——写入后运行时也解析不到（fail-closed）"
                )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(resolved, tmp)
        tmp_path = Path(tmp.name)
    try:
        LLMRouter.from_config(tmp_path, env=env)
    except ProviderConfigError as e:
        raise ProviderConfigValidationError(str(e)) from e
    finally:
        tmp_path.unlink(missing_ok=True)


def save(path: Path, payload: dict[str, Any]) -> None:
    """原子写（tmp + replace）——热生效期间读方不会看到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
