"""供应商注册表：provider 语义的单一事实源。

此前 provider 名单散落两处——``provider.py`` 的 LiteLLM 前缀映射与
dashboard ops 表单的硬编码 ``<option>``，且已经实际漂移（表单选不了
mistral/cohere，配置层认识的 vllm/omniroute 无入口）。新增供应商现在
只改 ``PROVIDERS`` 这一张表：表单展示由 ``visible_options()`` 派生，
前缀映射由 ``kind``/``litellm_prefix`` 派生。

``hidden`` 条目是配置层认识的内部别名或技术供应商，不在表单展示。
"""

from __future__ import annotations

from dataclasses import dataclass

PROVIDER_KIND_DIRECT = "direct"
PROVIDER_KIND_LITELLM = "litellm"
PROVIDER_KIND_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_KIND_LOCAL = "local"


@dataclass(frozen=True)
class ProviderSpec:
    value: str                 # 配置与表单使用的 provider 名
    label: str                 # ops 表单显示名
    kind: str                  # direct | litellm | openai_compatible | local
    litellm_prefix: str = ""   # kind=litellm 时传给 LiteLLM 的 provider 前缀
    hidden: bool = False       # 配置层认识但不进 ops 表单的内部别名


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec("direct", "自动识别", PROVIDER_KIND_DIRECT),
    ProviderSpec("openai", "OpenAI", PROVIDER_KIND_LITELLM, "openai"),
    ProviderSpec("deepseek", "DeepSeek", PROVIDER_KIND_LITELLM, "deepseek"),
    ProviderSpec("anthropic", "Anthropic", PROVIDER_KIND_LITELLM, "anthropic"),
    ProviderSpec("gemini", "Google Gemini", PROVIDER_KIND_LITELLM, "gemini"),
    ProviderSpec("mistral", "Mistral", PROVIDER_KIND_LITELLM, "mistral", hidden=True),
    ProviderSpec("cohere", "Cohere", PROVIDER_KIND_LITELLM, "cohere", hidden=True),
    ProviderSpec("openrouter", "OpenRouter", PROVIDER_KIND_LITELLM, "openrouter"),
    ProviderSpec("nvidia", "NVIDIA", PROVIDER_KIND_OPENAI_COMPATIBLE, hidden=True),
    ProviderSpec("nvidia_nim", "NVIDIA NIM", PROVIDER_KIND_OPENAI_COMPATIBLE),
    ProviderSpec("vllm", "vLLM", PROVIDER_KIND_OPENAI_COMPATIBLE, hidden=True),
    ProviderSpec("hosted_vllm", "Hosted vLLM", PROVIDER_KIND_OPENAI_COMPATIBLE, hidden=True),
    ProviderSpec("lmstudio", "LM Studio", PROVIDER_KIND_OPENAI_COMPATIBLE, hidden=True),
    ProviderSpec("omniroute", "OmniRoute", PROVIDER_KIND_OPENAI_COMPATIBLE, hidden=True),
    ProviderSpec("openai_compatible", "其他 OpenAI-compatible 云服务", PROVIDER_KIND_OPENAI_COMPATIBLE),
    ProviderSpec("local", "本地 Ollama", PROVIDER_KIND_LOCAL),
    ProviderSpec("ollama", "Ollama", PROVIDER_KIND_LOCAL, hidden=True),
)

_BY_VALUE: dict[str, ProviderSpec] = {spec.value: spec for spec in PROVIDERS}


def get_provider(value: str) -> ProviderSpec | None:
    return _BY_VALUE.get((value or "").lower())


def visible_options() -> list[ProviderSpec]:
    """ops 表单下拉框的选项，按注册顺序。"""
    return [spec for spec in PROVIDERS if not spec.hidden]


def litellm_prefix_for(value: str) -> str:
    """kind=litellm 的 provider 返回其 LiteLLM 前缀，其余返回空串。"""
    spec = get_provider(value)
    if spec is not None and spec.kind == PROVIDER_KIND_LITELLM:
        return spec.litellm_prefix
    return ""


def is_openai_compatible(value: str) -> bool:
    """openai_compatible 与 local 两类都走 openai/ 前缀直连。"""
    spec = get_provider(value)
    return spec is not None and spec.kind in (
        PROVIDER_KIND_OPENAI_COMPATIBLE,
        PROVIDER_KIND_LOCAL,
    )


__all__ = [
    "PROVIDERS",
    "PROVIDER_KIND_DIRECT",
    "PROVIDER_KIND_LITELLM",
    "PROVIDER_KIND_LOCAL",
    "PROVIDER_KIND_OPENAI_COMPATIBLE",
    "ProviderSpec",
    "get_provider",
    "is_openai_compatible",
    "litellm_prefix_for",
    "visible_options",
]
