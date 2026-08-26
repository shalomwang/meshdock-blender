from __future__ import annotations

TRIPO_CN = "tripo_cn"
TRIPO_GLOBAL = "tripo_global"
HUNYUAN_DIRECT = "hunyuan_direct"
TOKENHUB_CN = "tokenhub_cn"
TOKENHUB_GLOBAL = "tokenhub_global"
COMPARE_CN = "compare_cn"
COMPARE_GLOBAL = "compare_global"
COMPARE_LEGACY = "compare_legacy"
MOCK = "mock"

TRIPO_PROVIDERS = (TRIPO_CN, TRIPO_GLOBAL)
HUNYUAN_PROVIDERS = (HUNYUAN_DIRECT,)
TOKENHUB_PROVIDERS = (TOKENHUB_CN, TOKENHUB_GLOBAL)
COMPARE_PROVIDERS = (COMPARE_CN, COMPARE_GLOBAL)
REAL_PROVIDERS = (*TRIPO_PROVIDERS, *HUNYUAN_PROVIDERS, *TOKENHUB_PROVIDERS)
GENERATION_PROVIDERS = (*REAL_PROVIDERS, *COMPARE_PROVIDERS, MOCK)

LEGACY_PROVIDER_IDS = {
    "tripo": TRIPO_GLOBAL,
    # 0.7.x called the mainland TokenHub transport "hunyuan".  Mapping it
    # to direct Hunyuan would silently bind old jobs to a different account
    # and wire protocol, so recovery deliberately targets TokenHub China.
    "hunyuan": TOKENHUB_CN,
    "hunyuan_cn": TOKENHUB_CN,
    "hunyuan_global": TOKENHUB_GLOBAL,
    "compare": COMPARE_LEGACY,
}


def canonical_provider_id(provider: str | None) -> str | None:
    if provider is None:
        return None
    value = str(provider)
    return LEGACY_PROVIDER_IDS.get(value, value)


def provider_family(provider: str) -> str:
    value = canonical_provider_id(provider)
    if value in TRIPO_PROVIDERS:
        return "tripo"
    if value in HUNYUAN_PROVIDERS:
        return "hunyuan_direct"
    if value in TOKENHUB_PROVIDERS:
        return "tokenhub"
    if value in {*COMPARE_PROVIDERS, COMPARE_LEGACY}:
        return "compare"
    return str(value)


def option_namespace(provider: str) -> str:
    family = provider_family(provider)
    return family if family in {"tripo", "hunyuan_direct", "tokenhub"} else str(provider)


def same_provider_family(left: str, right: str) -> bool:
    return provider_family(left) == provider_family(right)
