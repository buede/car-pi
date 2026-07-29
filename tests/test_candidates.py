"""Turning sweeps into candidate definitions.

The database is the product, and this is the step where a contribution is made. The tests
that matter here are the ones about what these functions refuse to conclude: the whole
value of the shipped database is that nothing entered it unconfirmed, and a tool that
guessed a name for an identifier would quietly undo that.
"""

from __future__ import annotations

import pytest
import yaml

from carpi.core.candidates import (
    DraftError,
    LeakedValue,
    compare_sweeps,
    draft_profile,
    dump_yaml,
    issue_url,
    observe,
)


def _sweep(observations: list[dict], module: str = "714/77E") -> dict:
    return {
        "schema": "carpi.didscan/1",
        "module": module,
        "observations": observations,
    }


def _data(did: str, raw: str, **extra) -> dict:
    return {"did": did, "status": "data", "raw": raw, **extra}


class TestComparingSweeps:
    def test_only_identifiers_that_changed_are_reported(self) -> None:
        before = _sweep([_data("0x2200", "0001"), _data("0x2201", "00FF")])
        after = _sweep([_data("0x2200", "0002"), _data("0x2201", "00FF")])
        assert [c.label for c in compare_sweeps(before, after)] == ["0x2200"]

    def test_the_scale_is_reported_rather_than_assumed(self) -> None:
        """1.2 km against 12 counts means tenths -- or a coincidence. Say which it implies."""
        before = _sweep([_data("0x2200", "023668")])
        after = _sweep([_data("0x2200", "023674")])
        candidate = compare_sweeps(before, after, expected=1.2)[0]
        assert candidate.delta == 12
        assert candidate.familiar_scale == pytest.approx(0.1)

    def test_a_plausible_scale_outranks_an_implausible_one(self) -> None:
        before = _sweep([_data("0x2200", "023668"), _data("0x2201", "0384")])
        after = _sweep([_data("0x2200", "023674"), _data("0x2201", "0381")])
        ranked = [c.label for c in compare_sweeps(before, after, expected=1.2)]
        assert ranked[0] == "0x2200"

    def test_an_identifier_that_changed_length_is_not_a_delta(self) -> None:
        """That is the module answering differently, not a value moving."""
        before = _sweep([_data("0x2200", "0001")])
        after = _sweep([_data("0x2200", "000001")])
        assert compare_sweeps(before, after) == []

    def test_a_redacted_payload_is_skipped_rather_than_misread(self) -> None:
        """Anonymising a sweep before sharing it is right, and it costs this comparison."""
        before = _sweep([_data("0xF190", "<redacted: contained the VIN>")])
        after = _sweep([_data("0xF190", "<redacted: contained the VIN>")])
        assert compare_sweeps(before, after) == []

    def test_the_wrong_kind_of_file_is_rejected_by_name(self) -> None:
        with pytest.raises(DraftError, match="carpi.didscan/1"):
            compare_sweeps({"schema": "carpi.inspection/1"}, _sweep([]))

    def test_no_expected_change_still_lists_what_moved(self) -> None:
        before = _sweep([_data("0x2200", "0001")])
        after = _sweep([_data("0x2200", "0009")])
        assert compare_sweeps(before, after)[0].delta == 8


class TestDraftingAProfile:
    """What a draft must never do is name something it has not identified."""

    def test_an_unknown_identifier_is_marked_todo(self) -> None:
        document = draft_profile(
            [_sweep([_data("0x2200", "023668")])],
            profile_id="x",
            make="Example",
            platform="Test",
        )
        read = document["ecus"][0]["reads"][0]
        assert read["id"].startswith("todo_")
        assert "TODO" in read["label"]
        assert read["confidence"] == "community"

    def test_a_standardised_identifier_may_be_named(self) -> None:
        """ISO 14229 names 0xF190, so calling it the VIN is a citation, not a guess."""
        document = draft_profile(
            [_sweep([_data("0xF190", "4142", standard_name="vin")])],
            profile_id="x",
            make="Example",
            platform="Test",
        )
        read = document["ecus"][0]["reads"][0]
        assert read["id"] == "vin"
        assert read["confidence"] == "official"

    def test_nothing_drafted_is_ever_verified(self) -> None:
        """`verified` means confirmed against a car whose true state was known.

        A sweep cannot establish that, so no path through this code may produce it.
        """
        document = draft_profile(
            [_sweep([_data("0x2200", "01"), _data("0xF190", "4142", standard_name="vin")])],
            profile_id="x",
            make="Example",
            platform="Test",
        )
        text = dump_yaml(document)
        assert "verified" not in text
        assert document["meta"]["confidence"] == "community"

    def test_the_note_says_it_is_unconfirmed(self) -> None:
        document = draft_profile(
            [_sweep([_data("0x2200", "01")])], profile_id="x", make="E", platform="T"
        )
        assert "DRAFT" in document["meta"]["note"]
        assert "second car" in document["meta"]["note"]

    def test_addresses_come_from_the_sweep(self) -> None:
        document = draft_profile(
            [_sweep([_data("0x2200", "01")], module="714/77E")],
            profile_id="x",
            make="E",
            platform="T",
        )
        ecu = document["ecus"][0]
        assert (ecu["request_id"], ecu["response_id"]) == (0x714, 0x77E)

    def test_a_sweep_of_a_named_module_cannot_be_drafted_from(self) -> None:
        """It records the name instead of the address pair, so the address is unknown."""
        with pytest.raises(DraftError, match="address pair"):
            draft_profile(
                [_sweep([_data("0x2200", "01")], module="Instrument cluster")],
                profile_id="x",
                make="E",
                platform="T",
            )

    def test_no_sweeps_is_an_error_rather_than_an_empty_profile(self) -> None:
        with pytest.raises(DraftError, match="no sweeps"):
            draft_profile([], profile_id="x", make="E", platform="T")


class TestTheDraftIsUsableAsIs:
    def test_addresses_stay_in_hex_and_parse_as_integers(self) -> None:
        """The schema wants integers; a person wants hex. YAML gives both at once."""
        document = draft_profile(
            [_sweep([_data("0x2200", "01")])], profile_id="x", make="E", platform="T"
        )
        text = dump_yaml(document)
        assert "request_id: 0x714" in text
        assert yaml.safe_load(text)["ecus"][0]["request_id"] == 0x714

    def test_it_validates_against_the_shipped_vehicle_schema(self) -> None:
        """A draft nobody can load is not a starting point."""
        from pathlib import Path

        from carpi.core.database import _validate, defs_root  # noqa: PLC2701

        document = draft_profile(
            [_sweep([_data("0x2200", "01"), _data("0xF190", "4142", standard_name="vin")])],
            profile_id="example-draft",
            make="Example",
            platform="Draft",
        )
        # Round-tripped through YAML, because that is how it will actually be loaded, and
        # `0x714` only becomes an integer on the way back in.
        _validate(
            yaml.safe_load(dump_yaml(document)),
            defs_root() / "schema" / "vehicle.schema.json",
            Path("draft.yaml"),
        )

    def test_the_decode_length_reflects_what_was_observed(self) -> None:
        document = draft_profile(
            [_sweep([_data("0x2200", "023668")])], profile_id="x", make="E", platform="T"
        )
        assert document["ecus"][0]["reads"][0]["decode"] == {"type": "uint", "length": 3}

    def test_printable_payloads_are_drafted_as_text(self) -> None:
        document = draft_profile(
            [_sweep([_data("0x2200", "4142", text="AB")])],
            profile_id="x",
            make="E",
            platform="T",
        )
        assert document["ecus"][0]["reads"][0]["decode"] == {"type": "ascii"}


def _report(**overrides) -> dict:
    """An inspection report carrying everything that identifies one physical car."""
    document = {
        "schema": "carpi.inspection/1",
        "scan": {"vin": "WVWZZZ1KZAW123456", "claimed_odometer_km": 145000.0},
        "odometer_by_module": {"Instrument cluster": 145000.0},
        "ecus": [
            {
                "address": {"label": "7E0/7E8"},
                "ecu_name": "SIM-ECM-0042",
                "supported_pids": ["0x01", "0x0C"],
                "calibration_ids": ["CAL-SECRET-001"],
                "calibration_verification_numbers": ["DEADBEEF"],
                "uds_vin": "WVWZZZ1KZAW123456",
                "readings": {"engine_rpm": {"value": 760.5, "raw": "0BE8"}},
            }
        ],
        "module_readings": [
            {
                "ecu": "Instrument cluster",
                "address": "Instrument cluster",
                "request_id": "0x714",
                "response_id": "0x77E",
                "reached": True,
                "values": {"odometer_km": 145000.0, "part_number": "CLU-9988776"},
                "raw": {"odometer_km": "023668", "part_number": "434c552d39393838373736"},
            }
        ],
    }
    document.update(overrides)
    return document


class TestAnObservationCarriesNoVehicleContent:
    """The check that matters. A contribution is published under a licence that cannot be
    withdrawn, so a value surviving the reduction is not a bug somebody apologises for.

    Values are dropped rather than scrubbed. Removing a VIN still leaves the part numbers,
    the serial numbers and the programming dates, and those together identify one car.
    """

    @pytest.mark.parametrize(
        "secret",
        [
            "WVWZZZ1KZAW123456",  # the VIN
            "145000",  # the odometer, on the report and in the claim
            "CLU-9988776",  # a module part number
            "CAL-SECRET-001",  # a calibration id
            "SIM-ECM-0042",  # the module's own name
            "023668",  # raw bytes
            "434c552d39393838373736",
        ],
    )
    def test_no_identifying_value_survives(self, secret: str) -> None:
        import json

        published = json.dumps(observe([_report()]))
        assert secret not in published

    def test_the_vin_is_reduced_to_a_platform_prefix(self) -> None:
        """Characters 9 onward narrow towards one car, so none of them are kept."""
        observation = observe([_report()])
        assert observation["vin_prefix"] == "WVWZZZ1K"
        assert len(observation["vin_prefix"]) == 8

    def test_a_missing_vin_is_not_invented(self) -> None:
        observation = observe([_report(scan={"vin": None})])
        assert observation["vin_prefix"] is None

    def test_the_leak_guard_actually_fires(self) -> None:
        """Proving the net catches something, so it is not decorative."""
        from carpi.core.candidates import _reject_leaks  # noqa: PLC2701

        with pytest.raises(LeakedValue, match="survived"):
            _reject_leaks({"note": "WVWZZZ1KZAW123456"}, [_report()])

    def test_short_values_do_not_trip_the_guard(self) -> None:
        """A payload length of 2 must not be mistaken for a leaked value.

        A check that cries wolf gets an exception added to it, which is how it stops working.
        """
        observe([_report()])  # contains small numbers throughout; must not raise


class TestWhatAnObservationKeeps:
    def test_it_keeps_which_identifiers_exist_and_their_shape(self) -> None:
        sweep = _sweep([_data("0x2203", "023668"), _data("0xF190", "4142", text="AB")])
        module = observe([sweep])["modules"][0]
        assert module["identifiers"]["0x2203"] == {
            "status": "data",
            "length": 3,
            "type": "uint",
        }

    def test_a_protected_identifier_is_kept_as_a_finding(self) -> None:
        """It exists and is locked. That is information, not a miss."""
        sweep = _sweep([{"did": "0x2203", "status": "protected"}])
        module = observe([sweep])["modules"][0]
        assert module["identifiers"]["0x2203"]["status"] == "protected"

    def test_an_absent_identifier_is_not_recorded(self) -> None:
        sweep = _sweep([{"did": "0x2203", "status": "unsupported"}])
        assert observe([sweep])["modules"][0]["identifiers"] == {}

    def test_module_addresses_are_pairs_not_names(self) -> None:
        """A name means nothing to somebody reaching the same module on their own car."""
        addresses = [module["address"] for module in observe([_report()])["modules"]]
        assert addresses == ["0x714/0x77E"]

    def test_the_same_module_from_two_sources_is_one_entry(self) -> None:
        """A sweep says `714/77E` and a report says `0x714/0x77E`. Both are one module.

        Left unnormalised it appears twice, inflating the count and splitting identifiers
        that belong together -- worse than useless to whoever reads the contribution.
        """
        observation = observe([_report(), _sweep([_data("0x2203", "01")], module="714/77E")])
        addresses = [module["address"] for module in observation["modules"]]
        assert addresses == ["0x714/0x77E"]
        # Both sources' identifiers, merged rather than split across two entries.
        assert set(observation["modules"][0]["identifiers"]) == {
            "0x2203",
            "odometer_km",
            "part_number",
        }

    def test_a_module_named_by_a_profile_keeps_its_name(self) -> None:
        """There is no address pair to canonicalise, so it is left alone rather than mangled."""
        report = _report()
        del report["module_readings"][0]["request_id"]
        del report["module_readings"][0]["response_id"]
        assert observe([report])["modules"][0]["address"] == "Instrument cluster"

    def test_a_definition_name_is_not_labelled_as_an_iso_name(self) -> None:
        """`odometer_km` is a contributor's name. Calling it standard would be a claim."""
        identifiers = observe([_report()])["modules"][0]["identifiers"]
        assert identifiers["odometer_km"] == {"status": "data", "read_id": "odometer_km"}

    def test_an_iso_name_is_labelled_as_one(self) -> None:
        report = _report()
        report["module_readings"][0]["values"] = {"ecu_serial_number": "X"}
        identifiers = observe([report])["modules"][0]["identifiers"]
        assert "standard_name" in identifiers["ecu_serial_number"]

    def test_supported_parameters_are_platform_data_and_are_kept(self) -> None:
        assert observe([_report()])["obd_modules"][0]["supported_pids"] == ["0x01", "0x0C"]

    def test_sweeps_and_reports_can_be_combined(self) -> None:
        observation = observe([_report(), _sweep([_data("0x2203", "01")])])
        assert set(observation["sources"]) == {"inspection", "sweep"}

    def test_the_wrong_kind_of_file_is_named_in_the_error(self) -> None:
        with pytest.raises(DraftError, match="carpi.didscan/1"):
            observe([{"schema": "carpi.discovery/1"}])

    def test_no_files_is_an_error(self) -> None:
        with pytest.raises(DraftError, match="no files"):
            observe([])


class TestTheIssueLink:
    def test_it_targets_the_project_and_asks_for_review(self) -> None:
        url = issue_url(observe([_report()]))
        assert url.startswith("https://github.com/buede/car-pi/issues/new?")
        assert "vehicle-data" in url

    def test_it_carries_a_summary_rather_than_the_whole_document(self) -> None:
        """A full sweep would overflow a URL, and the file is attached anyway."""
        sweep = _sweep([_data(f"0x{did:04X}", "01") for did in range(0x2200, 0x2280)])
        assert len(issue_url(observe([sweep]))) < 4000

    def test_it_does_not_carry_a_value(self) -> None:
        assert "145000" not in issue_url(observe([_report()]))

    def test_it_states_that_nothing_is_confirmed(self) -> None:
        assert "confirmed" in issue_url(observe([_report()]))
