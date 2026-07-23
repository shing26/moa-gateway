from app.evaluator.evaluator import RuleEvaluator, EvalResult


@pytest.mark.asyncio
async def test_evaluator_valid_output():
    evaluator = RuleEvaluator()
    result = await evaluator.score("All checks passed.", intent="coding")
    assert not result.need_human_review
    assert result.score == 1.0


@pytest.mark.asyncio
async def test_evaluator_invalid_json_flags_review():
    evaluator = RuleEvaluator()
    result = await evaluator.score("{broken json", intent="coding")
    assert result.need_human_review
    assert "invalid_json_payload" in result.issues
