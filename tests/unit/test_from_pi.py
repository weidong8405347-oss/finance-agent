"""LLMRouter.from_pi：直接复用 pi 的 provider 配置（~/.pi/agent/）作为单一真相源。"""

import json

import pytest

from finance_agent.llm.router import LLMRouter, ProviderConfigError


def make_pi_dir(tmp_path):
    pi = tmp_path / ".pi" / "agent"
    pi.mkdir(parents=True)
    (pi / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "novita-gpt": {
                        "api": "openai-completions",
                        "baseUrl": "https://api.novita.ai/openai/v1",
                        "models": [{"id": "pa/gpt-5.6-sol"}],
                    },
                    "dashscope": {
                        "api": "openai-completions",
                        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        "models": [
                            {"id": "kimi-k3"},
                            {"id": "ZHIPU/GLM-5.3"},
                            {"id": "deepseek-v4-pro-0813"},
                        ],
                    },
                    "novita-claude": {
                        "api": "anthropic-messages",  # 非 OpenAI 协议 → 跳过
                        "baseUrl": "https://api.novita.ai/anthropic",
                        "models": [{"id": "anthropic/claude-opus-5"}],
                    },
                }
            }
        )
    )
    (pi / "auth.json").write_text(
        json.dumps(
            {
                "novita-gpt": {"type": "api_key", "key": "sk-novita"},
                "dashscope": {"type": "api_key", "key": "sk-dash"},
                "novita-claude": {"type": "api_key", "key": "sk-claude"},
            }
        )
    )
    return pi


def test_from_pi_loads_openai_compatible_providers(tmp_path):
    router = LLMRouter.from_pi(pi_dir=make_pi_dir(tmp_path), default_provider="novita-gpt")

    # 默认角色路由（2026-09 裁决：kimi-k3@max 效果超 pa/gpt-5.6-sol，效果优先）：
    # research → dashscope:kimi-k3 @ max effort + 分角色 timeout
    kimi = router.get("research")
    assert kimi.spec.model == "kimi-k3"
    assert kimi.spec.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert kimi.spec.api_key == "sk-dash"
    assert kimi.spec.effort == "max"
    assert kimi._timeout == 300.0  # noqa: SLF001

    # provider 名直取不受角色路由影响；未配置 effort 的不下发该参数
    gpt = router.get("novita-gpt")
    assert gpt.spec.base_url == "https://api.novita.ai/openai/v1"
    assert gpt.spec.model == "pa/gpt-5.6-sol"
    assert gpt.spec.api_key == "sk-novita"
    assert gpt.spec.effort is None


def test_from_pi_multi_model_provider_exposes_aliases(tmp_path):
    router = LLMRouter.from_pi(pi_dir=make_pi_dir(tmp_path), default_provider="novita-gpt")
    # dashscope 多模型 → 每个模型一个别名键
    # fast 角色默认路由 GLM-5.3（2026-09 裁决）；kimi-k3 仍可经别名直取
    fast = router.get("fast")
    assert fast.spec.model == "ZHIPU/GLM-5.3"
    assert fast.spec.api_key == "sk-dash"
    kimi = router.get("dashscope:kimi-k3")
    assert kimi.spec.model == "kimi-k3"
    glm = router.get("dashscope:ZHIPU/GLM-5.3")
    assert glm.spec.model == "ZHIPU/GLM-5.3"


def test_from_pi_skips_non_openai_protocol(tmp_path):
    router = LLMRouter.from_pi(pi_dir=make_pi_dir(tmp_path), default_provider="novita-gpt")
    # anthropic-messages 协议的 provider 不被收录（fail-closed，不会误用）
    assert "novita-claude" not in router.providers()
    assert "novita-gpt" in router.providers()


def test_from_pi_missing_dir_fail_closed(tmp_path):
    router = LLMRouter.from_pi(pi_dir=tmp_path / "nonexistent", default_provider="novita-gpt")
    assert router.providers() == []
    with pytest.raises(ProviderConfigError):
        router.get("research")
