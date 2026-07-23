from app.__main__ import power_on_self_test


def test_power_on_self_test():
    status = power_on_self_test()
    assert status["app"] == "moa-gateway"
    assert status["version"] == "0.1.0"
    assert status["guard_default"] in {True, False}
    assert isinstance(status["redis_url_set"], bool)
