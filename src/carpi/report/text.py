"""Terminal and JSON renderings of an inspection.

Two principles shape the layout. Findings come first and worst-first, because someone
standing next to a car they might buy needs the verdict before the data. And the
checks that could not be run are shown explicitly, because "we could not test this"
and "this tested fine" must never look the same.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from carpi.core.rules import Evaluation
from carpi.core.scan import ScanResult, build_facts

__all__ = ["render_json", "render_text", "to_dict"]

_WIDTH = 78
_INDENT = "    "

_SEVERITY_LABEL = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
    "info": "INFO",
}


def _rule(title: str) -> list[str]:
    return ["", title, "-" * len(title)]


def _wrap(text: str, indent: str = _INDENT) -> str:
    return textwrap.fill(
        " ".join(text.split()),
        width=_WIDTH,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def _number(value: Any) -> str:
    """Render one scalar for a human rather than for a debugger."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, list | tuple):
        # A list of flags is a presence map -- report which positions are set, since
        # "[True, True, False, False]" tells a reader nothing useful.
        if value and all(isinstance(item, bool) for item in value):
            present = [str(index + 1) for index, item in enumerate(value) if item]
            return ",".join(present) if present else "none"
        return ", ".join(_number(item) for item in value)
    return str(value)


def _reading_line(reading: Any) -> str | None:
    """One line for a reading, or ``None`` if a dedicated section already covers it.

    Decoders that return nested structures -- the readiness monitors especially -- get
    their own section, and dumping the raw mapping here as well would bury the numbers
    a reader actually wants in a wall of Python syntax.
    """
    value = reading.value
    if isinstance(value, dict):
        if any(isinstance(nested, dict) for nested in value.values()):
            return None
        parts = [
            f"{key}={_number(nested)}"
            for key, nested in value.items()
            # `raw` is the undecoded byte, useful in JSON but noise in prose.
            if nested is not None and key != "raw"
        ]
        if not parts:
            return None
        return f"{reading.definition.label}: {', '.join(parts)}"
    return str(reading)


def render_text(result: ScanResult, evaluation: Evaluation, *, verbose: bool = False) -> str:
    """Render a full inspection as plain text."""
    lines: list[str] = []
    lines.extend(_header(result))
    lines.extend(_findings(evaluation))
    lines.extend(_not_assessed(evaluation))
    lines.extend(_fault_codes(result))
    lines.extend(_monitors(result))
    if verbose:
        lines.extend(_live_data(result))
        lines.extend(_monitor_tests(result))
    lines.extend(_footer(result, evaluation))
    return "\n".join(lines)


def _header(result: ScanResult) -> list[str]:
    lines = ["", "=" * _WIDTH, "car-pi vehicle inspection".center(_WIDTH), "=" * _WIDTH]
    lines.append(f"Scanned:   {result.started_at}")
    lines.append(f"VIN:       {result.vin or 'not reported'}")
    if result.claimed_odometer_km is not None:
        lines.append(f"Advertised: {result.claimed_odometer_km:,.0f} km")
    modules = ", ".join(
        f"{ecu.address.label}{f' ({ecu.ecu_name})' if ecu.ecu_name else ''}" for ecu in result.ecus
    )
    lines.append(f"Modules:   {modules or 'none responded'}")
    for note in result.notes:
        lines.append("")
        lines.append(_wrap(note, indent=""))
    return lines


def _findings(evaluation: Evaluation) -> list[str]:
    lines = _rule("Findings")
    if not evaluation.findings:
        lines.append("")
        lines.append(_wrap("No findings. Every check that could be run came back clean."))
        return lines

    for finding in evaluation.findings:
        label = _SEVERITY_LABEL[finding.severity]
        lines.append("")
        lines.append(f"[{label}] {finding.title}")
        lines.append(_wrap(finding.explain))
        if finding.evidence:
            evidence = ", ".join(
                f"{name} = {_number(value)}" for name, value in sorted(finding.evidence.items())
            )
            lines.append(_wrap(f"Evidence: {evidence}"))
        if finding.confidence != "official":
            lines.append(
                _wrap(
                    f"Confidence: {finding.confidence} -- this check has not been "
                    f"confirmed against a known-good reference vehicle."
                )
            )
    return lines


def _not_assessed(evaluation: Evaluation) -> list[str]:
    """Checks that could not run. Never fold these into the passing set."""
    unavailable = [s for s in evaluation.skipped if s.missing]
    if not unavailable:
        return []
    lines = _rule("Not assessed")
    lines.append("")
    lines.append(
        _wrap(
            "These checks could not be run because the vehicle did not report the data "
            "they need. That is not the same as passing them.",
            indent="",
        )
    )
    for skipped in unavailable:
        lines.append("")
        lines.append(f"  - {skipped.title}")
        lines.append(_wrap(f"missing: {', '.join(skipped.missing)}", indent=_INDENT + "  "))
    return lines


def _fault_codes(result: ScanResult) -> list[str]:
    lines = _rule("Fault codes")
    any_codes = False
    for ecu in result.ecus:
        groups = (
            ("permanent (cannot be cleared by any tool)", ecu.permanent_dtcs),
            ("stored", ecu.stored_dtcs),
            ("pending", ecu.pending_dtcs),
        )
        if not any(codes for _, codes in groups):
            continue
        any_codes = True
        lines.append("")
        lines.append(f"  {ecu.address.label} {ecu.ecu_name or ecu.address.label}")
        for label, codes in groups:
            if codes:
                lines.append(f"{_INDENT}{label}: {', '.join(codes)}")
    if not any_codes:
        lines.append("")
        lines.append(f"{_INDENT}None reported by any module.")
    return lines


def _monitors(result: ScanResult) -> list[str]:
    facts = build_facts(result)
    if "readiness.supported_count" not in facts:
        return []
    lines = _rule("Emissions self-test readiness")
    lines.append("")
    lines.append(
        f"{_INDENT}{facts['readiness.complete_count']} of "
        f"{facts['readiness.supported_count']} complete"
    )
    incomplete = sorted(
        name.split(".")[1]
        for name, value in facts.items()
        if name.startswith("readiness.") and name.endswith(".complete") and value is False
    )
    if incomplete:
        lines.append(f"{_INDENT}not complete: {', '.join(incomplete)}")
    return lines


def _live_data(result: ScanResult) -> list[str]:
    primary = result.primary
    if primary is None or not primary.readings:
        return []
    lines = _rule("Live data")
    lines.append("")
    for name in sorted(primary.readings):
        line = _reading_line(primary.readings[name])
        if line is not None:
            lines.append(f"{_INDENT}{line}")
    return lines


def _monitor_tests(result: ScanResult) -> list[str]:
    tests = [(ecu, test) for ecu in result.ecus for test in ecu.monitor_results]
    if not tests:
        return []
    lines = _rule("On-board monitor test results (Mode 06)")
    lines.append("")
    lines.append(
        _wrap(
            "Values are raw counts. Because a test value and its limits share one "
            "scaling factor, the position within the allowed range is exact even "
            "though the engineering unit is not yet decoded.",
            indent=_INDENT,
        )
    )
    for ecu, test in tests:
        lines.append(f"{_INDENT}{ecu.address.label}  {test}")
    return lines


def _footer(result: ScanResult, evaluation: Evaluation) -> list[str]:
    lines = ["", "-" * _WIDTH]
    worst = evaluation.worst_severity
    if worst is None:
        verdict = "no findings"
    else:
        counts = ", ".join(
            f"{len(evaluation.by_severity(level))} {level}"
            for level in ("critical", "high", "medium", "low", "info")
            if evaluation.by_severity(level)
        )
        verdict = counts
    unavailable = len([s for s in evaluation.skipped if s.missing])
    lines.append(
        f"{verdict}; {len(evaluation.passed)} checks passed; {unavailable} could not be run"
    )
    errors = [error for ecu in result.ecus for error in ecu.errors]
    if errors:
        lines.append(f"{len(errors)} request(s) failed during the scan; re-run with -v for detail")
    lines.append("")
    return lines


def to_dict(result: ScanResult, evaluation: Evaluation) -> dict[str, Any]:
    """A JSON-serialisable view of the whole inspection.

    The raw payload of every reading is included as hex. That is what makes a scan
    re-analysable later when a definition is corrected, and it is what someone else
    needs to verify a finding rather than take it on trust.
    """
    return {
        "schema": "carpi.inspection/1",
        "scan": {
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "transport": result.transport,
            "vin": result.vin,
            "claimed_odometer_km": result.claimed_odometer_km,
            "notes": list(result.notes),
        },
        "ecus": [
            {
                "address": {
                    "tx_id": ecu.address.tx_id,
                    "rx_id": ecu.address.rx_id,
                    "extended": ecu.address.extended,
                    "label": ecu.address.label,
                },
                "ecu_name": ecu.ecu_name,
                "supported_pids": [f"0x{pid:02X}" for pid in ecu.supported_pids],
                "vin": ecu.vin,
                "calibration_ids": list(ecu.calibration_ids),
                "calibration_verification_numbers": list(ecu.calibration_verification_numbers),
                "dtcs": {
                    "stored": list(ecu.stored_dtcs),
                    "pending": list(ecu.pending_dtcs),
                    "permanent": list(ecu.permanent_dtcs),
                },
                "readings": {
                    name: {
                        "label": reading.definition.label,
                        "value": reading.value,
                        "unit": reading.unit,
                        "raw": reading.raw.hex(),
                        "plausible": reading.plausible,
                        "confidence": reading.definition.confidence,
                    }
                    for name, reading in ecu.readings.items()
                },
                "freeze_frame": {
                    name: {"value": reading.value, "raw": reading.raw.hex()}
                    for name, reading in ecu.freeze_frame.items()
                },
                "monitor_tests": [
                    {
                        "monitor_id": test.monitor_id,
                        "test_id": test.test_id,
                        "unit_and_scaling_id": test.unit_and_scaling_id,
                        "value": test.value,
                        "minimum": test.minimum,
                        "maximum": test.maximum,
                        "passed": test.passed,
                        "margin": test.margin,
                    }
                    for test in ecu.monitor_results
                ],
                "unsupported": list(ecu.unsupported),
                "errors": list(ecu.errors),
            }
            for ecu in result.ecus
        ],
        "facts": build_facts(result),
        "findings": [
            {
                "rule_id": finding.rule_id,
                "title": finding.title,
                "severity": finding.severity,
                "explain": finding.explain,
                "confidence": finding.confidence,
                "evidence": finding.evidence,
                "references": list(finding.references),
            }
            for finding in evaluation.findings
        ],
        "passed": list(evaluation.passed),
        "not_assessed": [
            {"rule_id": s.rule_id, "title": s.title, "missing": list(s.missing), "reason": s.reason}
            for s in evaluation.skipped
        ],
        "rule_errors": [{"rule_id": rid, "error": msg} for rid, msg in evaluation.errors],
    }


def render_json(result: ScanResult, evaluation: Evaluation, *, indent: int = 2) -> str:
    """Render the inspection as JSON."""
    return json.dumps(to_dict(result, evaluation), indent=indent, sort_keys=False, default=str)
