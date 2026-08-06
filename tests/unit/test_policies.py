from __future__ import annotations

from app.guard.policies import (
    InternalIpPolicy,
    NoPriceCommitmentPolicy,
    PolicyEngine,
    SecretLeakPolicy,
    policy_engine,
)


class TestInternalIpPolicy:
    def test_detects_private_ipv4_hits(self):
        policy = InternalIpPolicy()
        cases = {
            "服务器地址是 10.0.0.1": "10.0.0.1",
            "数据库放在 172.16.5.9 上": "172.16.5.9",
            "网关地址为 192.168.1.1": "192.168.1.1",
            "本机回环 127.0.0.1": "127.0.0.1",
            "链路本地地址 169.254.1.1": "169.254.1.1",
            "运营商 NAT 网段 100.64.0.1": "100.64.0.1",
        }
        for text, ip in cases.items():
            hits = policy.detect(text)
            assert len(hits) == 1
            assert hits[0].policy_id == "policy.security.internal_ip"
            assert hits[0].severity == "deny"
            assert hits[0].snippet == ip

    def test_detects_multiple_private_ips_in_one_text(self):
        policy = InternalIpPolicy()
        hits = policy.detect("主备服务器 10.0.0.1 与 192.168.1.1")
        assert len(hits) == 2
        assert {hit.snippet for hit in hits} == {"10.0.0.1", "192.168.1.1"}

    def test_ignores_public_ips(self):
        policy = InternalIpPolicy()
        assert policy.detect("公网 8.8.8.8 和 203.0.113.5") == []

    def test_ignores_teaching_examples(self):
        policy = InternalIpPolicy()
        assert policy.detect("内网地址一般写作 10.x.x.x 这种形式") == []

    def test_ignores_invalid_ipv4(self):
        policy = InternalIpPolicy()
        assert policy.detect("999.999.999.999 不是合法 IP") == []


class TestSecretLeakPolicy:
    def test_detects_openai_style_key(self):
        policy = SecretLeakPolicy()
        hits = policy.detect("sk-abc123XYZuvw4567890abcdef")
        assert len(hits) >= 1
        assert hits[0].policy_id == "policy.security.secret_leak"
        assert hits[0].severity == "deny"
        assert hits[0].detail != ""

    def test_detects_aws_access_key(self):
        policy = SecretLeakPolicy()
        hits = policy.detect("AKIA1234567890ABCDEF")
        assert len(hits) >= 1
        assert hits[0].policy_id == "policy.security.secret_leak"
        assert hits[0].detail != ""

    def test_detects_github_token(self):
        policy = SecretLeakPolicy()
        hits = policy.detect("ghp_abcdefghijklmnopqrstuvwxyz1234567890")
        assert len(hits) >= 1
        assert hits[0].policy_id == "policy.security.secret_leak"
        assert hits[0].detail != ""

    def test_detects_private_key_block(self):
        policy = SecretLeakPolicy()
        hits = policy.detect("-----BEGIN RSA PRIVATE KEY-----")
        assert len(hits) >= 1
        assert hits[0].policy_id == "policy.security.secret_leak"
        assert hits[0].detail != ""

    def test_detects_api_key_assignment(self):
        policy = SecretLeakPolicy()
        hits = policy.detect('api_key = "aGVsbG93b3JsZHdvcmxkMTIz"')
        assert len(hits) >= 1
        assert hits[0].policy_id == "policy.security.secret_leak"
        assert hits[0].detail != ""

    def test_ignores_key_mentions_without_secret(self):
        policy = SecretLeakPolicy()
        assert policy.detect("请保管好你的 API Key") == []

    def test_ignores_plain_text(self):
        policy = SecretLeakPolicy()
        assert policy.detect("今天天气不错, 适合写代码") == []


class TestNoPriceCommitmentPolicy:
    def test_detects_price_commitments(self):
        policy = NoPriceCommitmentPolicy()
        for text in ["优惠价只要 99 元", "报价 1200 美元", "限时促销 5.5 折 300 块", "每月 199 元/月"]:
            hits = policy.detect(text)
            assert len(hits) >= 1
            assert hits[0].policy_id == "policy.compliance.no_price_commitment"
            assert hits[0].severity == "review"
            assert hits[0].detail != ""

    def test_ignores_price_mentions_without_numbers(self):
        policy = NoPriceCommitmentPolicy()
        assert policy.detect("价格需以合同为准") == []

    def test_ignores_fee_mentions_without_numbers(self):
        policy = NoPriceCommitmentPolicy()
        assert policy.detect("具体费用请咨询销售") == []


class TestPolicyEngine:
    def test_register_deduplicates_same_policy_id(self):
        engine = PolicyEngine()
        engine.register(InternalIpPolicy())
        engine.register(InternalIpPolicy())
        assert len(engine.list()) == 1

    def test_list_preserves_registration_order(self):
        engine = PolicyEngine()
        engine.register(NoPriceCommitmentPolicy())
        engine.register(InternalIpPolicy())
        engine.register(SecretLeakPolicy())
        assert [p.policy_id for p in engine.list()] == [
            "policy.compliance.no_price_commitment",
            "policy.security.internal_ip",
            "policy.security.secret_leak",
        ]

    def test_check_aggregates_multiple_policy_hits(self):
        engine = PolicyEngine()
        engine.register(InternalIpPolicy())
        engine.register(SecretLeakPolicy())
        hits = engine.check("数据库服务器 10.0.0.1 使用密钥 sk-abc123XYZuvw4567890abcdef")
        assert {hit.policy_id for hit in hits} == {
            "policy.security.internal_ip",
            "policy.security.secret_leak",
        }

    def test_check_empty_for_clean_text(self):
        engine = PolicyEngine()
        engine.register(InternalIpPolicy())
        engine.register(SecretLeakPolicy())
        engine.register(NoPriceCommitmentPolicy())
        assert engine.check("今天天气不错, 适合写代码") == []


class TestPolicyEngineSingleton:
    def test_singleton_has_three_registered_policies(self):
        assert len(policy_engine.list()) == 3

    def test_singleton_has_builtin_policy_ids(self):
        assert {p.policy_id for p in policy_engine.list()} == {
            "policy.security.internal_ip",
            "policy.security.secret_leak",
            "policy.compliance.no_price_commitment",
        }
