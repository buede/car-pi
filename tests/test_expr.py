"""The expression language.

Definition files can arrive via pull request, so the interesting tests here are the
rejections. A construct that is merely unusual must still be refused, because the
whole safety argument rests on the allowlist being exhaustive.
"""

from __future__ import annotations

import pytest

from carpi.core.expr import ExpressionError, MissingFact, compile_expression


class TestArithmetic:
    @pytest.mark.parametrize(
        ("source", "facts", "expected"),
        [
            ("U / 4", {"U": 4000}, 1000),
            ("A - 40", {"A": 123}, 83),
            ("A * 100 / 128 - 100", {"A": 128}, 0),
            ("S / 4", {"S": -4}, -1),
            ("abs(A - B)", {"A": 3, "B": 11}, 8),
            ("max(A, B)", {"A": 3, "B": 11}, 11),
            ("2 ** 8", {}, 256),
            ("-A", {"A": 5}, -5),
        ],
    )
    def test_evaluates(self, source: str, facts: dict, expected: float) -> None:
        assert compile_expression(source).evaluate(facts) == expected

    def test_division_by_zero_is_an_expression_error(self) -> None:
        expression = compile_expression("A / B")
        with pytest.raises(ExpressionError, match="division by zero"):
            expression.evaluate({"A": 1, "B": 0})

    def test_exponent_is_capped(self) -> None:
        expression = compile_expression("2 ** A")
        with pytest.raises(ExpressionError, match="exceeds the limit"):
            expression.evaluate({"A": 10_000})


class TestBooleans:
    @pytest.mark.parametrize(
        ("source", "facts", "expected"),
        [
            ("dtc.permanent_count > 0", {"dtc.permanent_count": 1}, True),
            ("dtc.permanent_count > 0", {"dtc.permanent_count": 0}, False),
            ("a > 1 and b < 2", {"a": 5, "b": 1}, True),
            ("a > 1 or b < 2", {"a": 0, "b": 9}, False),
            ("not a", {"a": False}, True),
            ("1 < a < 10", {"a": 5}, True),
            ("1 < a < 10", {"a": 50}, False),
        ],
    )
    def test_evaluates(self, source: str, facts: dict, expected: bool) -> None:
        assert compile_expression(source).evaluate(facts) is expected

    @pytest.mark.parametrize("literal", ["true", "false", "null", "True", "False", "None"])
    def test_yaml_style_literals_are_not_treated_as_facts(self, literal: str) -> None:
        """`false` in a YAML rule must be the boolean, not a fact named "false".

        Python's parser sees a bare `false` as a name. Treating it as a fact would make
        the rule permanently inapplicable, so a real check would silently never run --
        which is exactly the failure mode this tool must not have.
        """
        expression = compile_expression(f"x == {literal}")
        assert expression.names == frozenset({"x"})

    def test_lowercase_false_compares_as_boolean(self) -> None:
        expression = compile_expression("status.mil_on == false")
        assert expression.evaluate({"status.mil_on": False}) is True
        assert expression.evaluate({"status.mil_on": True}) is False


class TestNameCollection:
    def test_collects_dotted_names(self) -> None:
        expression = compile_expression("pid.ltft_bank1 - pid.ltft_bank2 > vehicle.limit")
        assert expression.names == {"pid.ltft_bank1", "pid.ltft_bank2", "vehicle.limit"}

    def test_function_names_are_not_facts(self) -> None:
        assert compile_expression("abs(pid.x)").names == {"pid.x"}

    def test_applicability_checks_every_referenced_name(self) -> None:
        expression = compile_expression("a > 1 and b > 2")
        assert expression.is_applicable({"a": 1, "b": 2})
        assert not expression.is_applicable({"a": 1})

    def test_missing_fact_raises_rather_than_defaulting(self) -> None:
        """A fact the vehicle never reported must never evaluate as zero or false."""
        expression = compile_expression("pid.absent > 0")
        with pytest.raises(MissingFact) as info:
            expression.evaluate({})
        assert info.value.name == "pid.absent"


class TestRejections:
    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os').system('true')",
            "open('/etc/passwd').read()",
            "().__class__.__bases__",
            "[x for x in range(10)]",
            "lambda: 1",
            "a if b else c()",
            "print(1)",
            "a[0]",
            "{'a': 1}",
            "{1, 2}",
            "(1, 2)",
            "a := 1",
            "f'{a}'",
            "a.b(c)",
            "min(a, key=len)",
            "import os",
            "a = 1",
            "",
            "   ",
            "a +",
        ],
    )
    def test_rejected(self, source: str) -> None:
        with pytest.raises(ExpressionError):
            compile_expression(source)

    def test_bare_function_name_without_call_is_rejected(self) -> None:
        with pytest.raises(ExpressionError, match="must be called"):
            compile_expression("abs > 1")

    def test_conditional_expression_is_allowed(self) -> None:
        """IfExp is on the allowlist; the rejection above is for the *call* in it."""
        expression = compile_expression("a if b else c")
        assert expression.evaluate({"a": 1, "b": True, "c": 2}) == 1
