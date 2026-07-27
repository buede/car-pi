"""Evaluating inspection rules against a completed scan.

The important behaviour here is what happens to a rule whose facts are missing. It is
recorded as **skipped**, never as passed. A car that would not answer a question has
not answered it favourably, and a report that quietly converts silence into a clean
result is worse than no report -- somebody buys a car on the strength of it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from carpi.core.database import SEVERITY_ORDER, Database, Rule
from carpi.core.expr import ExpressionError, MissingFact

__all__ = ["Evaluation", "Finding", "SkippedRule", "evaluate", "flatten_facts"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finding:
    """A rule that fired."""

    rule_id: str
    title: str
    severity: str
    explain: str
    confidence: str
    # The facts the rule actually looked at, so the report can show the buyer the
    # numbers behind the verdict instead of asking them to take it on trust.
    evidence: dict[str, Any] = field(default_factory=dict)
    references: tuple[str, ...] = ()

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER[self.severity]


@dataclass(frozen=True)
class SkippedRule:
    """A rule that could not be evaluated, and why."""

    rule_id: str
    title: str
    missing: tuple[str, ...]
    reason: str = "facts unavailable"


@dataclass(frozen=True)
class Evaluation:
    """The outcome of running every rule."""

    findings: tuple[Finding, ...] = ()
    passed: tuple[str, ...] = ()
    skipped: tuple[SkippedRule, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def worst_severity(self) -> str | None:
        """Severity of the most serious finding, or ``None`` if nothing fired."""
        if not self.findings:
            return None
        return min(self.findings, key=lambda f: f.rank).severity

    def by_severity(self, severity: str) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == severity)


def flatten_facts(prefix: str, value: Any, into: dict[str, Any]) -> None:
    """Flatten nested decoder output into dotted fact keys.

    ``o2_b1s1 -> {"voltage": 0.45}`` becomes ``pid.o2_b1s1.voltage``. Rules reference
    these flat keys, so a decoder that grows a field automatically becomes available
    to rules without any plumbing.
    """
    if isinstance(value, Mapping):
        for key, nested in value.items():
            flatten_facts(f"{prefix}.{key}", nested, into)
        return
    if isinstance(value, list | tuple):
        # Lists are exposed by length only. A rule wanting element access would be
        # reaching past what the expression language deliberately permits.
        into[f"{prefix}.count"] = len(value)
        return
    into[prefix] = value


def _applies(rule: Rule, facts: Mapping[str, Any]) -> tuple[bool, str]:
    """Whether *rule*'s ``applies_to`` filter matches this vehicle."""
    if not rule.applies_to:
        return True, ""
    for key, expected in rule.applies_to.items():
        actual = facts.get(f"vehicle.{key}") if key != "ignition" else facts.get("status.ignition")
        if actual is None:
            return False, f"vehicle {key} unknown"
        if str(actual).lower() != str(expected).lower():
            return False, f"{key} is {actual!r}, rule is for {expected!r}"
    return True, ""


def evaluate(database: Database, facts: Mapping[str, Any]) -> Evaluation:
    """Run every rule in *database* against *facts*."""
    findings: list[Finding] = []
    passed: list[str] = []
    skipped: list[SkippedRule] = []
    errors: list[tuple[str, str]] = []

    for rule in database.rules_by_severity():
        applicable, why_not = _applies(rule, facts)
        if not applicable:
            skipped.append(
                SkippedRule(rule_id=rule.id, title=rule.title, missing=(), reason=why_not)
            )
            continue

        missing = tuple(sorted(name for name in rule.required_facts if name not in facts))
        if missing:
            skipped.append(SkippedRule(rule_id=rule.id, title=rule.title, missing=missing))
            continue

        try:
            fired = bool(rule.when.evaluate(facts))
        except MissingFact as exc:
            # Should be unreachable given the check above; treat as skipped rather
            # than let a database bug read as a passing check.
            skipped.append(SkippedRule(rule_id=rule.id, title=rule.title, missing=(exc.name,)))
            continue
        except (ExpressionError, TypeError) as exc:
            log.warning("rule %s could not be evaluated: %s", rule.id, exc)
            errors.append((rule.id, str(exc)))
            continue

        if fired:
            findings.append(
                Finding(
                    rule_id=rule.id,
                    title=rule.title,
                    severity=rule.severity,
                    explain=rule.explain,
                    confidence=rule.confidence,
                    evidence={name: facts[name] for name in sorted(rule.when.names)},
                    references=rule.references,
                )
            )
        else:
            passed.append(rule.id)

    findings.sort(key=lambda f: (f.rank, f.rule_id))
    return Evaluation(
        findings=tuple(findings),
        passed=tuple(passed),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )
