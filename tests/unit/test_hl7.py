"""HL7 v2 parsing and the ORU^R01 QC profile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lab_quality_control_agents.domain.errors import MessageFormatError
from lab_quality_control_agents.integrations import hl7

UTC_TZ = ZoneInfo("UTC")


def group(
    material: str,
    lot: str,
    value: str,
    *,
    set_id: str = "1",
    role: str = "Q",
    analyte: str = "2345-7^Glucose^LN",
    status: str = "F",
    vtype: str = "NM",
    when: str = "202610010815-0500",
    instrument: str = "CHEM-01",
    reagent: str | None = "R-100",
) -> list[str]:
    lines = [
        f"OBR|{set_id}||QC{set_id}|GLUQC^Glucose QC^L|||202610010800",
        f"OBX|1|{vtype}|{analyte}||{value}|mg/dL^mg/dL^UCUM|||||{status}|||{when}||||{instrument}",
        f"SPM|{set_id}|{material}||QC^Control^HL70487|||||||{role}",
        f"INV|{material}^Control|OK^ok|CO|||||||||||||{lot}",
    ]
    if reagent:
        lines.append(f"INV|GLU-REAGENT^Glucose reagent|OK^ok|SR|||||||||||||{reagent}")
    return lines


def message(*groups: list[str], control_id: str = "MSG0001", pid: str | None = None) -> str:
    lines = [
        f"MSH|^~\\&|MIDDLEWARE|CORE-LAB|LABQC|CORE-LAB|20261001081600||ORU^R01^ORU_R01|{control_id}|P|2.5.1"
    ]
    if pid is not None:
        lines.append(pid)
    for g in groups:
        lines.extend(g)
    return "\r".join(lines) + "\r"


TWO_LEVELS = message(
    group("GLU-L1", "L1-2026A", "101.5", set_id="1"),
    group("GLU-L2", "L2-2026A", "296.0", set_id="2"),
)


class TestParsing:
    def test_segments_and_fields(self) -> None:
        msg = hl7.parse(TWO_LEVELS)
        assert [s.name for s in msg.segments][:3] == ["MSH", "OBR", "OBX"]
        assert msg.msh.get(1) == "|"
        assert msg.msh.get(2) == "^~\\&"
        assert msg.msh.get(9, 1) == "ORU"
        assert msg.msh.get(9, 3) == "ORU_R01"
        obx = msg.first("OBX")
        assert obx is not None
        assert obx.get(3, 2) == "Glucose"
        assert obx.get(6, 1) == "mg/dL"
        assert obx.get(99) == ""
        assert obx.get(3, 9) == ""
        assert obx.get(3, 1, 4) == ""
        assert obx.get(3, 1, 1, 3) == ""
        assert len(msg.all("INV")) == 4

    def test_repetitions_and_subcomponents(self) -> None:
        msg = hl7.parse("MSH|^~\\&|A|B|C|D|20261001||ORU^R01|1|P|2.5\rNTE|1||first~sec&ond^x")
        nte = msg.first("NTE")
        assert nte is not None
        assert nte.get(3) == "first"
        assert nte.get(3, 1, 1, 1) == "sec"
        assert nte.get(3, 1, 2, 1) == "ond"
        assert nte.get(3, 2, 1, 1) == "x"

    def test_escape_sequences(self) -> None:
        enc = hl7.Encoding()
        raw = "a\\F\\b\\S\\c\\T\\d\\R\\e\\E\\f\\X41\\g\\.br\\h"
        assert hl7.unescape(raw, enc) == "a|b^c&d~e\\fAg\nh"
        with pytest.raises(MessageFormatError, match="unterminated"):
            hl7.unescape("bad\\F", enc)
        with pytest.raises(MessageFormatError, match="unsupported"):
            hl7.unescape("\\Z99\\", enc)

    def test_escape_round_trip(self) -> None:
        enc = hl7.Encoding()
        text = "pipe| caret^ amp& tilde~ slash\\ line\nbreak"
        assert hl7.unescape(hl7.escape(text), enc) == text.replace("\n", " ")

    def test_custom_delimiters(self) -> None:
        msg = hl7.parse("MSH#*@!$#A#B#C#D#20261001##ORU*R01#9#P#2.5\rNTE#1##x*y")
        assert msg.msh.get(9, 2) == "R01"
        nte = msg.first("NTE")
        assert nte is not None
        assert nte.get(3, 2) == "y"

    def test_mllp_and_line_endings(self) -> None:
        framed = "\x0b" + TWO_LEVELS.replace("\r", "\r\n") + "\x1c\r"
        assert len(hl7.parse(framed).segments) == len(hl7.parse(TWO_LEVELS).segments)

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            ("PID|1", "must start with an MSH"),
            ("MSH|^^\\&|A", "invalid MSH delimiters"),
            ("MSH|^~\\&|A\rpid|1", "invalid segment name"),
            ("MSH|^~\\&|A\rPIDX1", "not followed by the field separator"),
        ],
    )
    def test_malformed(self, text: str, error: str) -> None:
        with pytest.raises(MessageFormatError, match=error):
            hl7.parse(text)


class TestTimestamps:
    def test_offset_is_honoured(self) -> None:
        ts = hl7.parse_timestamp("202610010815-0500", UTC_TZ)
        assert ts == datetime(2026, 10, 1, 13, 15, tzinfo=UTC)

    def test_site_zone_applies_without_offset(self) -> None:
        ts = hl7.parse_timestamp("20261001081530.25", ZoneInfo("America/Chicago"))
        assert ts.utcoffset() == timedelta(hours=-5)
        assert ts.second == 30
        assert ts.microsecond == 250000

    def test_positive_offset(self) -> None:
        ts = hl7.parse_timestamp("2026100108+0130", UTC_TZ)
        assert ts.tzinfo == timezone(timedelta(hours=1, minutes=30))

    @pytest.mark.parametrize("raw", ["20261001", "2026", "garbage", "2026133108"])
    def test_rejects_imprecise_or_invalid(self, raw: str) -> None:
        with pytest.raises(MessageFormatError):
            hl7.parse_timestamp(raw, UTC_TZ)


class TestQcProfile:
    def test_extracts_two_levels_with_lots(self) -> None:
        qc = hl7.extract_qc(TWO_LEVELS, default_tz=UTC_TZ)
        assert qc.control_id == "MSG0001"
        assert qc.sending_application == "MIDDLEWARE"
        assert qc.version == "2.5.1"
        first, second = qc.observations
        assert (first.material_id, first.control_lot, first.reagent_lot) == (
            "GLU-L1",
            "L1-2026A",
            "R-100",
        )
        assert (first.analyte_code, first.coding_system) == ("2345-7", "LN")
        assert first.value == 101.5
        assert first.units == "mg/dL"
        assert first.instrument_id == "CHEM-01"
        assert first.observed_at == datetime(2026, 10, 1, 13, 15, tzinfo=UTC)
        assert second.material_id == "GLU-L2"
        assert (first.set_id, second.set_id) == ("1.1", "2.1")
        assert qc.skipped == ()

    def test_obr_time_is_the_fallback(self) -> None:
        qc = hl7.extract_qc(message(group("GLU-L1", "L", "100", when="")), default_tz=UTC_TZ)
        assert qc.observations[0].observed_at == datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    def test_non_numeric_and_non_final_are_skipped(self) -> None:
        text = message(
            group("GLU-L1", "L", "100", vtype="ST"),
            group("GLU-L2", "L", "300", set_id="2", status="X"),
        )
        qc = hl7.extract_qc(text, default_tz=UTC_TZ)
        assert qc.observations == ()
        assert any("not NM" in s for s in qc.skipped)
        assert any("not final" in s for s in qc.skipped)

    def test_empty_pid_is_tolerated(self) -> None:
        qc = hl7.extract_qc(message(group("GLU-L1", "L", "100"), pid="PID|1"), default_tz=UTC_TZ)
        assert len(qc.observations) == 1

    @pytest.mark.parametrize(
        ("text", "error"),
        [
            (TWO_LEVELS.replace("ORU^R01^ORU_R01", "ADT^A01"), "expected ORU"),
            (TWO_LEVELS.replace("|MSG0001|", "||"), "MSH-10"),
            (TWO_LEVELS.replace("|2.5.1", "|2.2"), "unsupported HL7 version"),
            (
                message(group("GLU-L1", "L", "100"), pid="PID|1||MRN123^^^HOSP^MR||DOE^JANE"),
                "patient identifiers",
            ),
            (message(group("GLU-L1", "L", "100", role="P")), "not a control specimen"),
            (message(group("", "L", "100")), "SPM-2"),
            (message(group("GLU-L1", "L", "abc")), "not numeric"),
            (message(group("GLU-L1", "L", "100", instrument="")), "OBX-18"),
            (
                message(group("GLU-L1", "L", "100", when="")).replace("|202610010800", "|"),
                "no observation time",
            ),
            (
                "MSH|^~\\&|A|B|C|D|20261001||ORU^R01|1|P|2.5.1\rNTE|1",
                "no OBR order groups",
            ),
        ],
    )
    def test_profile_violations(self, text: str, error: str) -> None:
        with pytest.raises(MessageFormatError, match=error):
            hl7.extract_qc(text, default_tz=UTC_TZ)


class TestAck:
    def test_ack_swaps_parties_and_references_inbound_id(self) -> None:
        now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        ack = hl7.build_ack(TWO_LEVELS, code="AA", note="accepted", control_id="ACK1", now=now)
        parsed = hl7.parse(ack)
        assert parsed.msh.get(3) == "LABQC"
        assert parsed.msh.get(5) == "MIDDLEWARE"
        assert parsed.msh.get(9, 1) == "ACK"
        assert parsed.msh.get(10) == "ACK1"
        msa = parsed.first("MSA")
        assert msa is not None
        assert (msa.get(1), msa.get(2), msa.get(3)) == ("AA", "MSG0001", "accepted")

    def test_ack_for_unparseable_input_escapes_note(self) -> None:
        now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
        ack = hl7.build_ack("garbage", code="AR", note="bad|input^here", control_id="A2", now=now)
        msa = hl7.parse(ack).first("MSA")
        assert msa is not None
        assert msa.get(1) == "AR"
        assert msa.get(3) == "bad|input^here"
