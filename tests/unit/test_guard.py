from app.guard.permission_guard import FailClosedPermissionGuard, GuardDecision


@pytest.mark.asyncio
async def test_guard_allows_basic_payload():
    guard = FailClosedPermissionGuard()
    decision = await guard.check("assistant", {"required_permissions": ["read"]})
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_guard_blocks_missing_schema():
    guard = FailClosedPermissionGuard()
    decision = await guard.check("assistant", {})
    assert decision.allowed is False
