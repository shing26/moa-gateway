from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyHit:
    policy_id: str
    severity: str
    snippet: str
    detail: str = ""


class Policy:
    def __init__(self, policy_id: str, name: str, description: str, severity: str) -> None:
        self.policy_id = policy_id
        self.name = name
        self.description = description
        self.severity = severity

    def detect(self, text: str) -> list[PolicyHit]:
        raise NotImplementedError


class PolicyEngine:
    def __init__(self) -> None:
        self._policies: list[Policy] = []

    def register(self, policy: Policy) -> None:
        if any(p.policy_id == policy.policy_id for p in self._policies):
            return
        self._policies.append(policy)

    def list(self) -> list[Policy]:
        return list(self._policies)

    def check(self, text: str) -> list[PolicyHit]:
        hits: list[PolicyHit] = []
        for policy in self._policies:
            hits.extend(policy.detect(text))
        return hits


_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


def _parse_ipv4(candidate: str) -> tuple[int, int, int, int] | None:
    parts = candidate.split(".")
    if len(parts) != 4:
        return None
    try:
        first, second, third, fourth = (int(p) for p in parts)
    except ValueError:
        return None
    if 0 <= first <= 255 and 0 <= second <= 255 and 0 <= third <= 255 and 0 <= fourth <= 255:
        return (first, second, third, fourth)
    return None


class InternalIpPolicy(Policy):
    def __init__(self) -> None:
        super().__init__(
            policy_id="policy.security.internal_ip",
            name="内网 IP 泄露",
            description="检测输出中泄露内网、回环、链路本地及运营商 NAT 地址",
            severity="deny",
        )

    def detect(self, text: str) -> list[PolicyHit]:
        hits: list[PolicyHit] = []
        for m in _IPV4_RE.finditer(text):
            octets = _parse_ipv4(m.group(0))
            if octets is None:
                continue
            if self._is_private(octets):
                hits.append(PolicyHit(self.policy_id, self.severity, m.group(0)[:120]))
        return hits

    @staticmethod
    def _is_private(octets: tuple[int, int, int, int]) -> bool:
        first, second, _, _ = octets
        if first == 10:
            return True
        if first == 172 and 16 <= second <= 31:
            return True
        if first == 192 and second == 168:
            return True
        if first == 127:
            return True
        if first == 169 and second == 254:
            return True
        if first == 100 and second == 64:
            return True
        return False


_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "OpenAI 风格密钥"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}"), "AWS Access Key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "GitHub Personal Access Token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥块"),
    (re.compile(r"api[_-]?key[\"']?\s*[:=]\s*[\"'][A-Za-z0-9+/=]{16,}"), "API Key"),
]


class SecretLeakPolicy(Policy):
    def __init__(self) -> None:
        super().__init__(
            policy_id="policy.security.secret_leak",
            name="密钥泄露",
            description="检测 OpenAI、AWS、GitHub 密钥、私钥及 API Key 泄露",
            severity="deny",
        )

    def detect(self, text: str) -> list[PolicyHit]:
        hits: list[PolicyHit] = []
        for pattern, detail in _SECRET_PATTERNS:
            for m in pattern.finditer(text):
                hits.append(PolicyHit(self.policy_id, self.severity, m.group(0)[:120], detail))
        return hits


_PRICE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(价格|报价|收费|费用|定价|优惠|折扣|促销|限时)[^\n。]{0,15}?\d+(\.\d+)?\s*(元|块|美元|美金|RMB|CNY|USD|\$|¥)", re.IGNORECASE), "含金额的价格表述"),
    (re.compile(r"\d+(\.\d+)?\s*(元/月|元/年|元/次|美元|美金)"), "周期性价格"),
]


class NoPriceCommitmentPolicy(Policy):
    def __init__(self) -> None:
        super().__init__(
            policy_id="policy.compliance.no_price_commitment",
            name="价格承诺",
            description="检测输出中的价格承诺表述, 命中后需人工复核",
            severity="review",
        )

    def detect(self, text: str) -> list[PolicyHit]:
        hits: list[PolicyHit] = []
        for pattern, detail in _PRICE_PATTERNS:
            for m in pattern.finditer(text):
                hits.append(PolicyHit(self.policy_id, self.severity, m.group(0)[:120], detail))
        return hits


policy_engine = PolicyEngine()
policy_engine.register(InternalIpPolicy())
policy_engine.register(SecretLeakPolicy())
policy_engine.register(NoPriceCommitmentPolicy())

__all__ = [
    "Policy",
    "PolicyEngine",
    "PolicyHit",
    "InternalIpPolicy",
    "SecretLeakPolicy",
    "NoPriceCommitmentPolicy",
    "policy_engine",
]
