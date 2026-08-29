"""一键运行契约：API 与前端同源伺服（参考 dsh web）。

- / 与任意非 /api 路径 → index.html（SPA fallback）
- /api/* 正常投影
- 无构建产物 → API 仍可用，/ 返回构建提示
"""

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore


def make_client(tmp_path, with_static=True):
    static_dir = None
    if with_static:
        static_dir = tmp_path / "dist"
        (static_dir / "assets").mkdir(parents=True)
        (static_dir / "index.html").write_text("<html><body>finance-agent</body></html>")
        (static_dir / "assets" / "x.js").write_text("console.log(1)")
    app = create_app(
        kb=BitemporalStore(tmp_path / "kb.db"),
        events=EventStore(tmp_path / "e.db"),
        decisions=DecisionStore(tmp_path / "d.db"),
        evals_dir=tmp_path / "evals",
        static_dir=static_dir,
    )
    return TestClient(app)


def test_spa_served_at_root_and_fallback(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200 and "finance-agent" in resp.text
    # SPA fallback：前端路由路径回退到 index.html
    assert client.get("/sessions/abc").text == "<html><body>finance-agent</body></html>"
    # 静态资源直达
    assert client.get("/assets/x.js").status_code == 200


def test_api_still_works_with_static(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/sessions").json() == []
    # /api 前缀不被 SPA 吃掉
    assert client.get("/api/nonexistent").status_code == 404


def test_missing_dist_shows_build_hint(tmp_path):
    app = create_app(
        kb=BitemporalStore(tmp_path / "kb.db"),
        events=EventStore(tmp_path / "e.db"),
        decisions=DecisionStore(tmp_path / "d.db"),
        evals_dir=tmp_path / "evals",
        static_dir=tmp_path / "dist",  # 不存在
    )
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "npm run build" in resp.text  # 构建提示
    assert client.get("/api/sessions").json() == []  # API 不受影响
