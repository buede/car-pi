"""Validate the CAN interface netlist.

A schematic is only as good as its review, and a wiring error is paid for in hardware and
an evening. So the netlist is machine-checked: every pin of every part is accounted for
exactly once, either connected or explicitly marked no-connect, and the connections that
matter most are asserted against the datasheet pinout directly.

The MCP2515 pin numbers here were taken from Microchip DS20001801K, 18-lead PDIP. That
matters because a web search for the same information returned a pinout that listed SCK on
two different pins and no RESET at all -- confidently wrong, and exactly the sort of thing
that gets soldered before anyone notices.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

NETLIST = Path(__file__).resolve().parents[1] / "hardware" / "carpi-can.net"

# Pin counts by package, so a missing or invented pin is caught.
PIN_COUNTS = {
    "U1": 18,  # MCP2515, 18-lead PDIP
    "U2": 8,  # SN65HVD230, SOIC-8
    "Y1": 2,
    "C1": 2,
    "C2": 2,
    "C3": 2,
    "C4": 2,
    "R1": 2,
    "R2": 2,
    "D1": 3,  # PESD1CAN, SOT-23
    "J1": 3,  # OBD-II harness
    "J2": 7,  # Pi header
}


@pytest.fixture(scope="module")
def netlist() -> str:
    assert NETLIST.is_file(), f"{NETLIST} is missing"
    return NETLIST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def nets(netlist: str) -> dict[str, list[tuple[str, str]]]:
    """Parse into ``{net name: [(ref, pin), ...]}``."""
    parsed: dict[str, list[tuple[str, str]]] = {}
    for block in re.finditer(
        r'\(net \(code "\d+"\) \(name "([^"]+)"\)(.*?)(?=\n    \(net |\)\)\s*$)',
        netlist,
        re.DOTALL,
    ):
        name = block.group(1)
        nodes = re.findall(r'\(node \(ref "([^"]+)"\) \(pin "([^"]+)"\)', block.group(2))
        parsed[name] = nodes
    return parsed


class TestStructure:
    def test_parentheses_balance(self, netlist: str) -> None:
        """An unbalanced file is one KiCad will refuse to import."""
        depth = 0
        in_string = False
        for char in netlist:
            if char == '"':
                in_string = not in_string
            elif not in_string:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    assert depth >= 0, "closing parenthesis with nothing open"
        assert depth == 0, f"{depth} unclosed parenthesis/es"

    def test_every_declared_component_is_present(self, netlist: str) -> None:
        refs = set(re.findall(r'\(comp \(ref "([^"]+)"\)', netlist))
        assert refs == set(PIN_COUNTS), f"unexpected: {refs ^ set(PIN_COUNTS)}"

    def test_every_component_has_a_footprint(self, netlist: str) -> None:
        """Without one, a netlist cannot be imported into a board layout."""
        components = re.findall(
            r'\(comp \(ref "([^"]+)"\)(.*?)(?=\n    \(comp |\n  \)\n)', netlist, re.DOTALL
        )
        for ref, body in components:
            assert "(footprint " in body, f"{ref} has no footprint"

    def test_all_nets_parsed(self, nets: dict) -> None:
        assert len(nets) == 22, f"parsed {len(nets)} nets, expected 22"


class TestCompleteness:
    def test_every_pin_appears_exactly_once(self, nets: dict) -> None:
        """The check that matters: a pin on two nets is a short, on none is a float."""
        seen: Counter[tuple[str, str]] = Counter()
        for nodes in nets.values():
            seen.update(nodes)

        duplicated = [pin for pin, count in seen.items() if count > 1]
        assert duplicated == [], f"these pins are on more than one net: {duplicated}"

        missing: list[str] = []
        for ref, count in PIN_COUNTS.items():
            for pin in range(1, count + 1):
                if (ref, str(pin)) not in seen:
                    missing.append(f"{ref}-{pin}")
        assert missing == [], f"these pins are on no net at all: {missing}"

    def test_single_node_nets_are_explicit_no_connects(self, nets: dict) -> None:
        """A one-node net is either a deliberate no-connect or a mistake. Say which."""
        for name, nodes in nets.items():
            if len(nodes) == 1:
                assert name.startswith("unconnected-"), (
                    f"net {name!r} has only one node but is not marked unconnected"
                )
            else:
                assert not name.startswith("unconnected-"), (
                    f"net {name!r} is marked unconnected but has {len(nodes)} nodes"
                )


class TestDatasheetPinout:
    """Asserted against Microchip DS20001801K, 18-lead PDIP, rather than from memory."""

    @pytest.mark.parametrize(
        ("net", "pin", "signal"),
        [
            ("+3V3", "18", "VDD"),
            ("GND", "9", "VSS"),
            ("TXCAN", "1", "TXCAN"),
            ("RXCAN", "2", "RXCAN"),
            ("OSC2", "7", "OSC2"),
            ("OSC1", "8", "OSC1"),
            ("MCP_INT", "12", "INT"),
            ("SPI_SCLK", "13", "SCK"),
            ("SPI_MOSI", "14", "SI"),
            ("SPI_MISO", "15", "SO"),
            ("SPI_CS", "16", "CS"),
            ("MCP_RESET", "17", "RESET"),
        ],
    )
    def test_mcp2515_pin(self, nets: dict, net: str, pin: str, signal: str) -> None:
        assert ("U1", pin) in nets[net], f"U1 pin {pin} ({signal}) is not on {net}"

    def test_oscillator_pins_are_not_swapped(self, nets: dict) -> None:
        """Pin 7 is OSC2 and pin 8 is OSC1 -- the opposite of what is easy to assume.

        A crystal is symmetric enough to oscillate either way, so this survives a bench
        test and then bites whoever later fits an external oscillator, where OSC1 is the
        input and the distinction is real.
        """
        assert ("U1", "7") in nets["OSC2"]
        assert ("U1", "8") in nets["OSC1"]

    @pytest.mark.parametrize(
        ("net", "pin", "signal"),
        [
            ("TXCAN", "1", "D"),
            ("GND", "2", "GND"),
            ("+3V3", "3", "VCC"),
            ("RXCAN", "4", "R"),
            ("CAN_L", "6", "CANL"),
            ("CAN_H", "7", "CANH"),
            ("TRANSCEIVER_RS", "8", "RS"),
        ],
    )
    def test_transceiver_pin(self, nets: dict, net: str, pin: str, signal: str) -> None:
        assert ("U2", pin) in nets[net], f"U2 pin {pin} ({signal}) is not on {net}"


class TestDesignRules:
    def test_no_terminator_is_fitted(self, netlist: str) -> None:
        """A vehicle bus is already terminated at both ends; a third causes bus errors."""
        assert "120" not in re.sub(r"\(comment[^\n]*\n", "", netlist), (
            "something in the netlist looks like a 120 ohm terminator"
        )

    def test_the_can_pair_is_protected(self, nets: dict) -> None:
        """It is going in a car."""
        assert ("D1", "1") in nets["CAN_H"]
        assert ("D1", "2") in nets["CAN_L"]

    def test_both_chips_are_decoupled(self, nets: dict) -> None:
        assert ("C3", "1") in nets["+3V3"] and ("C3", "2") in nets["GND"]
        assert ("C4", "1") in nets["+3V3"] and ("C4", "2") in nets["GND"]

    def test_reset_is_pulled_up_not_left_floating(self, nets: dict) -> None:
        assert ("R1", "2") in nets["MCP_RESET"]
        assert ("R1", "1") in nets["+3V3"]

    def test_transceiver_is_in_high_speed_mode(self, nets: dict) -> None:
        """RS to ground selects high speed; left floating the transceiver may not drive."""
        assert ("R2", "1") in nets["TRANSCEIVER_RS"]
        assert ("R2", "2") in nets["GND"]

    def test_the_harness_has_no_twelve_volt_line(self, netlist: str) -> None:
        """J1962 pin 16 is deliberately not wired: the Pi runs off a battery bank.

        Bringing +12 V onto the board is an unnecessary path from the car into the
        electronics, and it is what turns a wiring slip into a dead Pi.
        """
        assert "+12V" not in netlist
        assert "DO NOT wire J1962 pin 16" in netlist


class TestDocumentation:
    def test_the_netlist_says_it_is_unbuilt(self, netlist: str) -> None:
        """Nobody should discover that from a smell of hot plastic."""
        assert "NOT YET BUILT" in netlist

    def test_the_readme_covers_the_crystal_trap(self) -> None:
        readme = (NETLIST.parent / "README.md").read_text(encoding="utf-8")
        assert "crystal" in readme.lower()
        assert "oscillator=16000000" in readme

    def test_the_readme_warns_off_the_cheap_module(self) -> None:
        readme = (NETLIST.parent / "README.md").read_text(encoding="utf-8")
        assert "MCP2551" in readme, "the 5V-transceiver trap should be documented"
