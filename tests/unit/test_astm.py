"""ASTM E1381 framing and E1394 QC records."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from lab_quality_control_agents.domain.errors import MessageFormatError
from lab_quality_control_agents.integrations import astm

UTC_TZ = ZoneInfo("UTC")

RECORDS = [
    "H|\\^&|RUN-0042||CHEM-01^1.2|||||||Q|LIS2-A2|20261001081600",
    "P|1",
    "O|1|GLU-L1^L1-2026A||^^^GLU|R||||||Q",
    "R|1|^^^GLU|101.5|mg/dL||N||F||tech||20261001081500|CHEM-01",
    "O|2|GLU-L2^L2-2026A||^^^GLU|R||||||Q",
    "R|1|^^^GLU|296.0|mg/dL||N||F||tech||20261001081500|CHEM-01",
    "L|1|N",
]


def manual_checksum(body: str) -> str:
    total = 0
    for char in body:
        total = (total + ord(char)) % 256
    return format(total, "02X")


class TestFraming:
    def test_checksum_matches_independent_sum(self) -> None:
        body = "1H|\\^&\r\x03"
        assert astm.checksum(body) == manual_checksum(body)

    def test_frame_layout(self) -> None:
        data = astm.build_frames(["H|\\^&", "L|1|N"])
        assert data.startswith(astm.ENQ)
        assert data.endswith(astm.EOT)
        first = data[1 : data.index("\n") + 1]
        body = "1H|\\^&\r\x03"
        assert first == f"\x02{body}{manual_checksum(body)}\r\n"

    def test_round_trip(self) -> None:
        assert astm.unframe(astm.build_frames(RECORDS)) == RECORDS

    def test_long_record_spans_intermediate_frames(self) -> None:
        long_record = "C|1|I|" + "x" * 600 + "|G"
        data = astm.build_frames(["H|\\^&", long_record, "L|1|N"])
        assert data.count(astm.ETB) == 2
        assert astm.unframe(data) == ["H|\\^&", long_record, "L|1|N"]

    def test_frame_numbers_wrap_modulo_eight(self) -> None:
        records = ["H|\\^&"] + [f"C|{i}|I|note|G" for i in range(10)] + ["L|1|N"]
        assert astm.unframe(astm.build_frames(records)) == records

    def test_bad_checksum(self) -> None:
        data = astm.build_frames(["H|\\^&", "L|1|N"])
        index = data.index("\x03") + 1
        corrupted = (
            data[:index] + ("00" if data[index : index + 2] != "00" else "01") + data[index + 2 :]
        )
        with pytest.raises(MessageFormatError, match="checksum"):
            astm.unframe(corrupted)

    def test_out_of_sequence(self) -> None:
        body = "2H|\\^&\r\x03"
        with pytest.raises(MessageFormatError, match="out of sequence"):
            astm.unframe(f"\x02{body}{astm.checksum(body)}\r\n")

    def test_frame_too_long(self) -> None:
        body = "1" + "x" * 241 + "\x03"
        with pytest.raises(MessageFormatError, match="240"):
            astm.unframe(f"\x02{body}{astm.checksum(body)}\r\n")

    def test_garbage_and_truncated(self) -> None:
        with pytest.raises(MessageFormatError, match="malformed"):
            astm.unframe("not a frame")
        body = "1H|\\^&\x17"
        with pytest.raises(MessageFormatError, match="intermediate"):
            astm.unframe(f"\x02{body}{astm.checksum(body)}\r\n")


class TestRecords:
    def test_header_fields(self) -> None:
        records = astm.parse_records(RECORDS)
        header = records[0]
        assert header.get(2) == "\\^&"
        assert header.get(3) == "RUN-0042"
        assert header.get(5, 1) == "CHEM-01"
        assert header.get(5, 2) == "1.2"
        assert header.get(12) == "Q"
        assert header.get(40) == ""
        result = records[3]
        assert result.get(3, 4) == "GLU"
        assert result.get(3, 9) == ""
        assert result.get(3, 1, 2) == ""

    def test_escapes(self) -> None:
        records = astm.parse_records(["H|\\^&", "C|1|I|a&F&b&S&c&R&d&E&e|G", "L|1|N"])
        assert records[1].get(4) == "a|b^c\\d&e"

    @pytest.mark.parametrize(
        ("lines", "error"),
        [
            ([], "must start with a header"),
            (["P|1"], "must start with a header"),
            (["H|||&"], "invalid ASTM delimiter"),
            (["H|\\^&", "X|1", "L|1"], "invalid ASTM record"),
            (["H|\\^&", "P|1"], "terminator"),
        ],
    )
    def test_malformed(self, lines: list[str], error: str) -> None:
        with pytest.raises(MessageFormatError, match=error):
            astm.parse_records(lines)

    def test_blank_lines_are_ignored(self) -> None:
        assert len(astm.parse_records(["H|\\^&", "\r", "L|1|N"])) == 2

    def test_timestamps(self) -> None:
        assert astm.parse_astm_timestamp("202610010815", UTC_TZ) == datetime(
            2026, 10, 1, 8, 15, tzinfo=UTC
        )
        with pytest.raises(MessageFormatError, match="must be"):
            astm.parse_astm_timestamp("20261001", UTC_TZ)
        with pytest.raises(MessageFormatError, match="invalid"):
            astm.parse_astm_timestamp("20261301081500", UTC_TZ)


class TestQcProfile:
    def test_framed_extraction(self) -> None:
        qc = astm.extract_qc(astm.build_frames(RECORDS), framed=True, default_tz=UTC_TZ)
        assert qc.control_id == "RUN-0042"
        assert qc.version == "LIS2-A2"
        first, second = qc.observations
        assert (first.material_id, first.control_lot, first.analyte_code) == (
            "GLU-L1",
            "L1-2026A",
            "GLU",
        )
        assert first.value == 101.5
        assert first.observed_at == datetime(2026, 10, 1, 8, 15, tzinfo=UTC)
        assert first.set_id == "1.1"
        assert second.set_id == "2.1"
        assert second.instrument_id == "CHEM-01"

    def test_bare_records_and_order_action_code(self) -> None:
        records = [RECORDS[0].replace("|Q|LIS2-A2", "|P|LIS2-A2"), *RECORDS[1:]]
        qc = astm.extract_qc("\r\n".join(records), framed=False, default_tz=UTC_TZ)
        assert len(qc.observations) == 2

    def test_control_id_digest_when_header_has_none(self) -> None:
        records = [RECORDS[0].replace("RUN-0042", ""), *RECORDS[1:]]
        qc = astm.extract_qc("\r".join(records), framed=False, default_tz=UTC_TZ)
        assert qc.control_id.startswith("sha256:")

    def test_sender_instrument_is_the_fallback(self) -> None:
        records = [*RECORDS[:3], RECORDS[3].replace("|CHEM-01", "|"), RECORDS[-1]]
        qc = astm.extract_qc("\r".join(records), framed=False, default_tz=UTC_TZ)
        assert qc.observations[0].instrument_id == "CHEM-01"

    def test_non_final_results_are_skipped(self) -> None:
        records = [*RECORDS[:3], RECORDS[3].replace("|F|", "|X|"), RECORDS[-1]]
        qc = astm.extract_qc("\r".join(records), framed=False, default_tz=UTC_TZ)
        assert qc.observations == ()
        assert qc.skipped == ("R-1: result status X not final",)

    @pytest.mark.parametrize(
        ("records", "error"),
        [
            ([RECORDS[0], "P|1||MRN123||DOE^JANE", *RECORDS[2:]], "patient record"),
            (
                [
                    RECORDS[0].replace("|Q|", "|P|"),
                    RECORDS[1],
                    RECORDS[2].replace("|Q", "|N"),
                    *RECORDS[3:],
                ],
                "not a QC order",
            ),
            ([RECORDS[0], RECORDS[3], RECORDS[-1]], "without a preceding order"),
            ([*RECORDS[:3], RECORDS[3].replace("101.5", "high"), RECORDS[-1]], "not numeric"),
            ([*RECORDS[:3], RECORDS[3].replace("^^^GLU", ""), RECORDS[-1]], "universal test ID"),
            (
                [
                    RECORDS[0].replace("CHEM-01^1.2", ""),
                    *RECORDS[1:3],
                    RECORDS[3].replace("|CHEM-01", "|"),
                    RECORDS[-1],
                ],
                "no instrument",
            ),
            (
                [*RECORDS[:3], RECORDS[3].replace("20261001081500", ""), RECORDS[-1]],
                "R-13",
            ),
            (
                [RECORDS[0], RECORDS[2].replace("GLU-L1^L1-2026A", ""), RECORDS[3], RECORDS[-1]],
                "O-3",
            ),
            ([RECORDS[0], RECORDS[-1]], "no result records"),
        ],
    )
    def test_profile_violations(self, records: list[str], error: str) -> None:
        with pytest.raises(MessageFormatError, match=error):
            astm.extract_qc("\r".join(records), framed=False, default_tz=UTC_TZ)
