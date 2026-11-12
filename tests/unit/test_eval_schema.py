from pathlib import Path
import pytest
import json

from eval.schema import load_cases


def test_minimal_valid_case_roundtrips(tmp_path):
    """A minimal valid case round-trips through load_cases."""
    row = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "script",
        }
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(row + "\n")
    cases = load_cases(str(path))
    assert len(cases) == 1
    c = cases[0]
    assert c.id == "tc1"
    assert c.class_ == "business"
    assert c.source == "repo"
    assert c.base_sha == "abc123"
    assert c.goal == "do something"
    assert c.allowed_paths == ("a.py",)
    assert c.oracle_type == "script"


def test_missing_id_raises_value_error_with_line(tmp_path):
    """A case missing id raises ValueError naming the line number and the field."""
    d = {
        "class_": "business",
        "source": "repo",
        "base_sha": "abc123",
        "goal": "do something",
        "allowed_paths": ["a.py"],
        "oracle_type": "script",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(d) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 1" in message
    assert "id" in message


def test_missing_class__raises_value_error_with_line(tmp_path):
    """A case missing class_ raises ValueError naming the line number and the field."""
    d = {
        "id": "tc1",
        "source": "repo",
        "base_sha": "abc123",
        "goal": "do something",
        "allowed_paths": ["a.py"],
        "oracle_type": "script",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(d) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 1" in message
    assert "class_" in message


def test_missing_source_raises_value_error_with_line(tmp_path):
    """A case missing source raises ValueError naming the line number and the field."""
    d = {
        "id": "tc1",
        "class_": "business",
        "base_sha": "abc123",
        "goal": "do something",
        "allowed_paths": ["a.py"],
        "oracle_type": "script",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(d) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 1" in message
    assert "source" in message


def test_missing_goal_raises_value_error_with_line(tmp_path):
    """A case missing goal raises ValueError naming the line number and the field."""
    d = {
        "id": "tc1",
        "class_": "business",
        "source": "repo",
        "base_sha": "abc123",
        "allowed_paths": ["a.py"],
        "oracle_type": "script",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(d) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 1" in message
    assert "goal" in message


def test_missing_base_sha_raises_value_error_with_line(tmp_path):
    """A case missing base_sha raises ValueError naming the line number and the field."""
    d = {
        "id": "tc1",
        "class_": "business",
        "source": "repo",
        "goal": "do something",
        "allowed_paths": ["a.py"],
        "oracle_type": "script",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(d) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 1" in message
    assert "base_sha" in message


def test_missing_allowed_paths_raises_value_error_with_line(tmp_path):
    """A case missing allowed_paths raises ValueError naming the line number and the field."""
    d = {
        "id": "tc1",
        "class_": "business",
        "source": "repo",
        "base_sha": "abc123",
        "goal": "do something",
        "oracle_type": "script",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(d) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 1" in message
    assert "allowed_paths" in message


def test_missing_oracle_type_raises_value_error_not_key_error(tmp_path):
    """A case missing oracle_type raises ValueError (NOT KeyError)."""
    d = {
        "id": "tc1",
        "class_": "business",
        "source": "repo",
        "base_sha": "abc123",
        "goal": "do something",
        "allowed_paths": ["a.py"],
    }
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(d) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 1" in message
    assert "oracle_type" in message


def test_unknown_class__raises_value_error(tmp_path):
    """An unknown class_ value raises ValueError."""
    row = json.dumps(
        {
            "id": "tc1",
            "class_": "invalid",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "script",
        }
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(row + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Unknown class_:" in message


def test_unknown_oracle_type_raises_value_error(tmp_path):
    """An unknown oracle_type value raises ValueError."""
    row = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "invalid_type",
        }
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(row + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Unknown oracle_type:" in message


def test_blank_lines_skipped_and_bad_row_reports_line_3(tmp_path):
    """Blank lines between rows are skipped, and a bad row is reported with its PHYSICAL line number."""
    valid = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "script",
        }
    )
    missing_goal = json.dumps(
        {
            "id": "tc2",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            # goal omitted
        }
    )
    lines = [valid, "", missing_goal]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 3" in message
    assert "goal" in message


def test_invalid_json_on_line_2_raises_value_error(tmp_path):
    """A line of invalid JSON on physical line 2 reports Line 2."""
    valid = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "script",
        }
    )
    bad_json = "{{{ not json }"
    lines = [valid, bad_json]
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError) as exc_info:
        load_cases(str(path))
    message = str(exc_info.value)
    assert "Line 2" in message


def test_non_empty_values_survive_for_all_six_tuple_fields(tmp_path):
    """Non-empty values survive for all six tuple[str, ...] fields."""
    row = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "fixtures": ["f1"],
            "forbidden_paths": ["/no"],
            "verify_commands": ["pytest", "-q"],
            "oracle_type": "script",
            "oracle_checks": ["x"],
            "expected_failure_to_avoid": "expected",
            "tags": ["t1", "t2"],
        }
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(row + "\n")
    cases = load_cases(str(path))
    assert len(cases) == 1
    c = cases[0]
    assert c.allowed_paths == ("a.py",)
    assert c.fixtures == ("f1",)
    assert c.forbidden_paths == ("/no",)
    assert c.verify_commands == ("pytest", "-q")
    assert c.oracle_checks == ("x",)
    assert c.tags == ("t1", "t2")


def test_optional_fields_default_to_empty_and_empty_string(tmp_path):
    """Optional fields default to empty tuples and ''."""
    row = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "script",
        }
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(row + "\n")
    cases = load_cases(str(path))
    assert len(cases) == 1
    c = cases[0]
    assert c.fixtures == ()
    assert c.env == ()
    assert c.forbidden_paths == ()
    assert c.verify_commands == ()
    assert c.oracle_checks == ()
    assert c.expected_failure_to_avoid == ""


def test_env_given_as_json_arrays_becomes_tuple_of_tuples(tmp_path):
    """env given as JSON arrays becomes tuple of tuples, and inner element is not a list."""
    row = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "script",
            "env": [["A", "1"], ["B", "2"]],
        }
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(row + "\n")
    cases = load_cases(str(path))
    assert len(cases) == 1
    c = cases[0]
    assert c.env == (("A", "1"), ("B", "2"))
    assert not isinstance(c.env[0], list)


def test_frozen_dataclass_rejects_attribute_assignment(tmp_path):
    """The frozen dataclass rejects attribute assignment (FrozenInstanceError)."""
    row = json.dumps(
        {
            "id": "tc1",
            "class_": "business",
            "source": "repo",
            "base_sha": "abc123",
            "goal": "do something",
            "allowed_paths": ["a.py"],
            "oracle_type": "script",
        }
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(row + "\n")
    cases = load_cases(str(path))
    assert len(cases) == 1
    c = cases[0]
    with pytest.raises(AttributeError):
        c.id = "changed"
