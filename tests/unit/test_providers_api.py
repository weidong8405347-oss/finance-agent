"""P5 provider 自配 API 的契约测试（research-capability-upgrade §4.4）。

纪律：api_key 永不出 API 明文；非法配置 422 不落盘；key 继承哨兵 "***"；
测活探针不依赖真实网络（httpx 打桩）；reset 回落 pi/.env。
"""

import json

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.llm.provider_config import CONFIG_FILENAME, KEY_MASK
from finance_agent.llm.router import LLMRouter

VALID_CFG = {
    "providers": {
        "dashscope": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-real-secret",
            "models": ["kimi-k3", "GLM-5.3"],
        }
    },
    "default_provider": "dashscope",
    "role_map": {"research": "dashscope:kimi-k3"},
    "role_options": {"research": {"effort": "max", "timeout": 300}},
}


def make_client(tmp_path, *, router_factory=None):
    app = create_app(
        kb=BitemporalStore(tmp_path / "kb.db"),
        events=EventStore(tmp_path / "events.db"),
        decisions=DecisionStore(tmp_path / "decisions.db"),
        evals_dir=tmp_path / "evals",
        data_dir=tmp_path,
        router_factory=router_factory,
    )
    return TestClient(app)


def test_get_without_own_config_reports_fallback(tmp_path):
    r = make_client(tmp_path).get("/api/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "fallback" and body["file"] is None
    assert body["config_path"].endswith(CONFIG_FILENAME)


def test_save_then_get_masked(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/api/providers", json=VALID_CFG)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "own"
    shown_key = body["file"]["providers"]["dashscope"]["api_key"]
    assert shown_key == KEY_MASK and "sk-real-secret" not in json.dumps(body)
    # 落盘的是真 key（运行时可用），API 只回脱敏视图
    on_disk = json.loads((tmp_path / CONFIG_FILENAME).read_text())
    assert on_disk["providers"]["dashscope"]["api_key"] == "sk-real-secret"
    # GET 也不泄 key
    assert "sk-real-secret" not in json.dumps(client.get("/api/providers").json())


def test_save_with_mask_inherits_existing_key(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/api/providers", json=VALID_CFG).status_code == 200
    updated = json.loads(json.dumps(VALID_CFG))
    updated["providers"]["dashscope"]["api_key"] = KEY_MASK  # UI 回显后原样保存
    updated["role_map"] = {"research": "dashscope:GLM-5.3"}
    r = client.post("/api/providers", json=updated)
    assert r.status_code == 200, r.text
    on_disk = json.loads((tmp_path / CONFIG_FILENAME).read_text())
    assert on_disk["providers"]["dashscope"]["api_key"] == "sk-real-secret"  # 继承未丢
    assert on_disk["role_map"]["research"] == "dashscope:GLM-5.3"


def test_save_invalid_rejected_422_and_nothing_written(tmp_path):
    client = make_client(tmp_path)
    bad = {"providers": {"broken": {"base_url": "", "api_key": "", "models": []}}}
    r = client.post("/api/providers", json=bad)
    assert r.status_code == 422 and "dashscope" not in r.text
    assert not (tmp_path / CONFIG_FILENAME).exists()
    # 空 providers 同样拒（清空配置请走 reset）
    assert client.post("/api/providers", json={"providers": {}}).status_code == 422
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_save_env_ref_unresolvable_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_SUCH_VAR_XYZ", raising=False)
    cfg = json.loads(json.dumps(VALID_CFG))
    cfg["providers"]["dashscope"]["api_key"] = "env:NO_SUCH_VAR_XYZ"
    r = make_client(tmp_path).post("/api/providers", json=cfg)
    assert r.status_code == 422 and "NO_SUCH_VAR_XYZ" in r.json()["detail"]
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_save_env_ref_preserved_in_view_and_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "sk-env-secret")
    cfg = json.loads(json.dumps(VALID_CFG))
    cfg["providers"]["dashscope"]["api_key"] = "env:MY_TEST_KEY"
    client = make_client(tmp_path)
    assert client.post("/api/providers", json=cfg).status_code == 200
    body = client.get("/api/providers").json()
    # env:VAR 是间接引用（非秘密），原样展示；值本身不出现
    assert body["file"]["providers"]["dashscope"]["api_key"] == "env:MY_TEST_KEY"
    assert "sk-env-secret" not in json.dumps(body)


def test_reset_falls_back(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/api/providers", json=VALID_CFG).status_code == 200
    assert (tmp_path / CONFIG_FILENAME).exists()
    r = client.post("/api/providers/reset")
    assert r.status_code == 200 and r.json()["source"] == "fallback"
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_effective_view_from_router_factory(tmp_path):
    router = LLMRouter.from_config  # 用真 router 装配有效视图
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "cfg.json"
        cfg_path.write_text(json.dumps(VALID_CFG))
        client = make_client(tmp_path, router_factory=lambda: router(cfg_path))
        body = client.get("/api/providers").json()
    eff = body["effective"]
    assert eff["default_provider"] == "dashscope"
    assert eff["role_map"] == {"research": "dashscope:kimi-k3"}
    prov = eff["providers"][0]
    assert prov["name"] == "dashscope" and prov["has_key"] is True
    assert set(prov["models"]) == {"kimi-k3", "GLM-5.3"}  # 别名键聚合成 models 列表
    assert "sk-real-secret" not in json.dumps(body)


def test_probe_success_and_failure(monkeypatch, tmp_path):
    import httpx

    class FakeResp:
        status_code = 200

        def __init__(self, ok=True):
            self._ok = ok
            self.text = ""

        def json(self):
            return {"choices": [{"message": {"content": "pong" if self._ok else ""}}]}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp(ok=True))
    client = make_client(tmp_path)
    r = client.post("/api/providers/test", json={
        "base_url": "https://example.com/v1", "model": "m", "api_key": "sk-x"})
    assert r.json()["ok"] is True and r.json()["latency_ms"] >= 0

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp(ok=False))
    r = client.post("/api/providers/test", json={
        "base_url": "https://example.com/v1", "model": "m", "api_key": "sk-x"})
    assert r.json()["ok"] is False and r.json()["error"]

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", boom)
    r = client.post("/api/providers/test", json={
        "base_url": "https://example.com/v1", "model": "m", "api_key": "sk-x"})
    assert r.json()["ok"] is False and "ConnectError" in r.json()["error"]


def test_probe_inherits_key_by_name(tmp_path, monkeypatch):
    import httpx

    seen = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "pong"}}]}

    def fake_post(url, headers=None, **k):
        seen["auth"] = (headers or {}).get("Authorization", "")
        return FakeResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    client = make_client(tmp_path)
    assert client.post("/api/providers", json=VALID_CFG).status_code == 200
    # KEY_MASK + name → 继承已存 key；且响应不回显
    r = client.post("/api/providers/test", json={
        "name": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "kimi-k3", "api_key": KEY_MASK})
    assert r.json()["ok"] is True
    assert seen["auth"] == "Bearer sk-real-secret"
    assert "sk-real-secret" not in r.text
