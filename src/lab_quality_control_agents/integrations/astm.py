"""ASTM E1381 low-level framing and ASTM E1394 (CLSI LIS2-A2) records.

Framing (E1381 / LIS1-A): each frame is ``STX FN text ETB|ETX C1 C2 CR LF``.
FN is the frame number 0-7 (first frame 1, incrementing modulo 8), ETB marks
an intermediate frame of a record split across frames, ETX the last frame.
C1 C2 is the checksum: the sum of every byte from FN through ETB/ETX
inclusive, modulo 256, as two uppercase hex digits. Frame text is at most
240 characters. ENQ/EOT/ACK line-control characters are ignored.

Records (E1394): delimiters come from the header ``H|\\^&``: field, repeat,
component, escape. Record types used here: H (header), P (patient), O (order),
R (result), C (comment), L (terminator).

QC profile:

| Element | Use |
|---|---|
| H-3 | message control ID (identifies the run); a content digest is used if absent |
| H-5 | sender name: instrument ID in component 1 (fallback for R-14) |
| H-12 | processing ID ``Q`` marks a QC transmission |
| P | must carry no patient identifiers (P-3, P-4, P-5, P-6) |
| O-3 | specimen ID: control material ID ``^`` control lot |
| O-12 | action code ``Q`` (quality control) |
| R-3 | universal test ID; manufacturer test code in component 4 |
| R-4 / R-5 | value / units |
| R-9 | result status (F, C accepted) |
| R-13 | date/time test completed (YYYYMMDDHHMMSS) |
| R-14 | instrument identification |
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo

from lab_quality_control_agents.domain.errors import MessageFormatError
from lab_quality_control_agents.integrations.hl7 import QcMessage, QcObservation

STX, ETX, ETB, EOT, ENQ, ACK, NAK = "\x02", "\x03", "\x17", "\x04", "\x05", "\x06", "\x15"
CR, LF = "\r", "\n"
MAX_FRAME_TEXT = 240
ACCEPTED_STATUS = frozenset({"F", "C", ""})


def checksum(body: str) -> str:
    """Checksum over frame number, text and ETB/ETX."""
    return f"{sum(body.encode('latin-1')) % 256:02X}"


def build_frames(records: list[str]) -> str:
    """Encode records as a framed transmission (ENQ ... EOT)."""
    frames: list[str] = []
    number = 1
    for record in records:
        text = record + CR
        chunks = [text[i : i + MAX_FRAME_TEXT] for i in range(0, len(text), MAX_FRAME_TEXT)]
        for index, chunk in enumerate(chunks):
            terminator = ETX if index == len(chunks) - 1 else ETB
            body = f"{number % 8}{chunk}{terminator}"
            frames.append(f"{STX}{body}{checksum(body)}{CR}{LF}")
            number += 1
    return ENQ + "".join(frames) + EOT


_FRAME = re.compile(
    "\x02(?P<fn>[0-7])(?P<text>[^\x02\x03\x17]*)(?P<term>[\x03\x17])(?P<cs>[0-9A-Fa-f]{2})\r\n"
)


def unframe(data: str) -> list[str]:
    """Validate frames (checksum, sequence, length) and reassemble records."""
    stream = data.replace(ENQ, "").replace(EOT, "").replace(ACK, "")
    position = 0
    expected = 1
    buffer = ""
    records: list[str] = []
    while position < len(stream):
        match = _FRAME.match(stream, position)
        if match is None:
            raise MessageFormatError(f"malformed ASTM frame at offset {position}")
        fn, text, term, cs = match.group("fn", "text", "term", "cs")
        if int(fn) != expected % 8:
            raise MessageFormatError(f"frame number {fn} out of sequence; expected {expected % 8}")
        if len(text) > MAX_FRAME_TEXT:
            raise MessageFormatError("ASTM frame text exceeds 240 characters")
        if checksum(f"{fn}{text}{term}") != cs.upper():
            raise MessageFormatError(f"checksum mismatch in frame {fn}")
        buffer += text
        if term == ETX:
            records.extend(r for r in buffer.split(CR) if r)
            buffer = ""
        expected += 1
        position = match.end()
    if buffer:
        raise MessageFormatError("transmission ended inside an intermediate (ETB) frame")
    return records


@dataclass(frozen=True)
class Delimiters:
    field: str
    repeat: str
    component: str
    escape: str


@dataclass(frozen=True)
class Record:
    type: str
    fields: tuple[str, ...]  # fields[0] is the record type
    delimiters: Delimiters

    def get(self, number: int, component: int = 1, repeat: int = 0) -> str:
        """Field ``number`` (1-based, ASTM numbering)."""
        if self.type == "H" and number == 2:
            return self.fields[1] if len(self.fields) > 1 else ""
        if number - 1 >= len(self.fields):
            return ""
        reps = self.fields[number - 1].split(self.delimiters.repeat)
        if repeat >= len(reps):
            return ""
        comps = reps[repeat].split(self.delimiters.component)
        if component > len(comps):
            return ""
        return _unescape(comps[component - 1], self.delimiters).strip()


def _unescape(value: str, d: Delimiters) -> str:
    e = d.escape
    for code, char in (("F", d.field), ("S", d.component), ("R", d.repeat), ("E", d.escape)):
        value = value.replace(f"{e}{code}{e}", char)
    return value


def parse_records(lines: list[str]) -> list[Record]:
    if not lines or not lines[0].startswith("H") or len(lines[0]) < 5:
        raise MessageFormatError("ASTM message must start with a header (H) record")
    header = lines[0]
    d = Delimiters(field=header[1], repeat=header[2], component=header[3], escape=header[4])
    if len({d.field, d.repeat, d.component, d.escape}) != 4:
        raise MessageFormatError("invalid ASTM delimiter definition")
    records: list[Record] = []
    for raw_line in lines:
        line = raw_line.strip(CR + LF)
        if not line:
            continue
        rtype = line[0]
        if rtype not in "HPORCLQM" or (len(line) > 1 and line[1] != d.field):
            raise MessageFormatError(f"invalid ASTM record {line[:10]!r}")
        if rtype == "H":
            rest = line[5:]
            parts = ("H", line[2:5], *(rest.split(d.field)[1:] if rest else ()))
        else:
            parts = tuple(line.split(d.field))
        records.append(Record(type=rtype, fields=parts, delimiters=d))
    if records[-1].type != "L":
        raise MessageFormatError("ASTM message must end with a terminator (L) record")
    return records


def parse_astm_timestamp(value: str, default_tz: tzinfo) -> datetime:
    if not re.fullmatch(r"\d{12}(\d{2})?", value):
        raise MessageFormatError(f"ASTM timestamp {value!r} must be YYYYMMDDHHMM[SS]")
    try:
        return datetime.strptime(value.ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=default_tz)
    except ValueError as exc:
        raise MessageFormatError(f"invalid ASTM timestamp {value!r}") from exc


def _result(
    record: Record, order: Record, seq: str, *, sender_instrument: str, default_tz: tzinfo
) -> QcObservation:
    raw = record.get(4)
    try:
        value = float(raw)
    except ValueError as exc:
        raise MessageFormatError(f"R-{seq}: value {raw!r} is not numeric") from exc
    test_code = record.get(3, 4) or record.get(3, 1)
    if not test_code:
        raise MessageFormatError(f"R-{seq}: universal test ID is empty")
    instrument = record.get(14, 1) or sender_instrument
    if not instrument:
        raise MessageFormatError(f"R-{seq}: no instrument identification (R-14 or H-5)")
    completed = record.get(13)
    if not completed:
        raise MessageFormatError(f"R-{seq}: R-13 date/time test completed is required")
    material = order.get(3, 1)
    if not material:
        raise MessageFormatError("O-3 specimen ID (control material) is required")
    return QcObservation(
        set_id=f"{order.get(2) or '1'}.{seq}",
        analyte_code=test_code,
        coding_system=None,
        value=value,
        units=record.get(5),
        observed_at=parse_astm_timestamp(completed, default_tz),
        instrument_id=instrument,
        material_id=material,
        control_lot=order.get(3, 2) or None,
        reagent_lot=None,
    )


def extract_qc(data: str, *, framed: bool, default_tz: tzinfo) -> QcMessage:
    lines = unframe(data) if framed else [ln for ln in re.split(r"\r\n|\r|\n", data) if ln]
    records = parse_records(lines)
    header = records[0]
    control_id = header.get(3) or "sha256:" + hashlib.sha256(data.encode()).hexdigest()[:32]
    qc_header = header.get(12) == "Q"
    sender_instrument = header.get(5, 1)

    observations: list[QcObservation] = []
    skipped: list[str] = []
    order: Record | None = None
    for record in records[1:]:
        if record.type == "P" and any(record.get(n) for n in (3, 4, 5, 6)):
            raise MessageFormatError(
                "patient record carries identifiers; QC messages must not contain patient data"
            )
        if record.type == "O":
            order = record
            if not qc_header and record.get(12) != "Q":
                raise MessageFormatError(
                    "order is not a QC order (O-12 action code 'Q' or H-12 processing ID 'Q')"
                )
        elif record.type == "R":
            if order is None:
                raise MessageFormatError("result record without a preceding order record")
            seq = record.get(2) or str(len(observations) + len(skipped) + 1)
            status = record.get(9)
            if status not in ACCEPTED_STATUS:
                skipped.append(f"R-{seq}: result status {status} not final")
                continue
            observations.append(
                _result(
                    record,
                    order,
                    seq,
                    sender_instrument=sender_instrument,
                    default_tz=default_tz,
                )
            )
    if not observations and not skipped:
        raise MessageFormatError("ASTM message contains no result records")
    return QcMessage(
        control_id=control_id,
        sending_application=sender_instrument,
        sending_facility="",
        version=header.get(13) or "E1394",
        observations=tuple(observations),
        skipped=tuple(skipped),
    )
