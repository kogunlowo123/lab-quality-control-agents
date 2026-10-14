"""HL7 v2 parsing and the QC ORU^R01 interface profile.

Parsing follows HL7 v2.x encoding rules: segments separated by CR (LF and
CRLF tolerated), delimiters declared in MSH-1/MSH-2, field repetitions,
components and subcomponents, escape sequences (\\F\\ \\S\\ \\T\\ \\R\\ \\E\\
\\Xhh..\\ \\.br\\). MLLP framing (VT ... FS CR) is stripped when present.

QC profile (ORU^R01, v2.3.1-2.8):

| Element | Use |
|---|---|
| MSH-9 | ``ORU^R01`` |
| MSH-10 | message control ID; identifies the analytical run and keys idempotency |
| PID | must be absent or carry no identifiers; QC messages never carry patient data |
| OBR-7 | observation time (fallback for OBX-14) |
| OBX-2 | ``NM`` (numeric); other value types are skipped |
| OBX-3 | analyte: ``code^text^system`` (``LN`` = LOINC, otherwise local code) |
| OBX-5 / OBX-6 | value / units (``code^text``) |
| OBX-11 | result status; ``F`` and ``C`` are accepted |
| OBX-14 | observation date/time |
| OBX-18 | equipment instance identifier: instrument ID |
| SPM-2 | control material identifier (placer-assigned component) |
| SPM-11 | specimen role ``Q`` (control specimen, HL7 table 0369) is required |
| INV-3 / INV-16 | substance type ``CO`` (control) or ``SR``/``MR`` (reagent) / manufacturer lot |

Each OBR group contributes the observations that follow it; SPM and INV
segments anywhere in the group apply to every OBX of that group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

from lab_quality_control_agents.domain.errors import MessageFormatError

MLLP_START = "\x0b"
MLLP_END = "\x1c"
CONTROL_SPECIMEN_ROLE = "Q"
CONTROL_SUBSTANCE = "CO"
REAGENT_SUBSTANCES = frozenset({"SR", "MR"})
ACCEPTED_RESULT_STATUS = frozenset({"F", "C"})
SUPPORTED_VERSIONS = ("2.3", "2.4", "2.5", "2.6", "2.7", "2.8")


@dataclass(frozen=True)
class Encoding:
    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"


def unescape(value: str, enc: Encoding) -> str:
    """Decode HL7 escape sequences."""
    if enc.escape not in value:
        return value
    out: list[str] = []
    i = 0
    esc = enc.escape
    while i < len(value):
        char = value[i]
        if char != esc:
            out.append(char)
            i += 1
            continue
        end = value.find(esc, i + 1)
        if end == -1:
            raise MessageFormatError("unterminated HL7 escape sequence")
        token = value[i + 1 : end]
        mapping = {
            "F": enc.field,
            "S": enc.component,
            "T": enc.subcomponent,
            "R": enc.repetition,
            "E": enc.escape,
            ".br": "\n",
        }
        if token in mapping:
            out.append(mapping[token])
        elif token.startswith("X") and re.fullmatch(r"X(?:[0-9A-Fa-f]{2})+", token):
            out.append(bytes.fromhex(token[1:]).decode("latin-1"))
        else:
            raise MessageFormatError(f"unsupported HL7 escape sequence \\{token}\\")
        i = end + 1
    return "".join(out)


@dataclass(frozen=True)
class Segment:
    name: str
    fields: tuple[str, ...]  # fields[0] is the segment name
    enc: Encoding

    def raw(self, number: int) -> str:
        """Field ``number`` (1-based, HL7 numbering) as transmitted."""
        if self.name == "MSH":
            if number == 1:
                return self.enc.field
            index = number - 1  # fields[1] holds MSH-2
        else:
            index = number
        return self.fields[index] if index < len(self.fields) else ""

    def get(
        self, number: int, component: int = 1, subcomponent: int = 1, repetition: int = 0
    ) -> str:
        value = self.raw(number)
        if self.name == "MSH" and number in (1, 2):
            return value
        reps = value.split(self.enc.repetition)
        if repetition >= len(reps):
            return ""
        comps = reps[repetition].split(self.enc.component)
        if component > len(comps):
            return ""
        subs = comps[component - 1].split(self.enc.subcomponent)
        if subcomponent > len(subs):
            return ""
        return unescape(subs[subcomponent - 1], self.enc).strip()


@dataclass(frozen=True)
class Message:
    segments: tuple[Segment, ...]
    enc: Encoding

    def first(self, name: str) -> Segment | None:
        return next((s for s in self.segments if s.name == name), None)

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]

    @property
    def msh(self) -> Segment:
        return self.segments[0]


def parse(text: str) -> Message:
    body = text.strip().removeprefix(MLLP_START)
    body = body.split(MLLP_END, 1)[0].strip()
    if not body.startswith("MSH") or len(body) < 8:
        raise MessageFormatError("HL7 message must start with an MSH segment")
    field_sep = body[3]
    encoding_chars = body[4:8]
    if len(set(field_sep + encoding_chars)) != 5 or field_sep.isalnum():
        raise MessageFormatError("invalid MSH delimiters")
    enc = Encoding(
        field=field_sep,
        component=encoding_chars[0],
        repetition=encoding_chars[1],
        escape=encoding_chars[2],
        subcomponent=encoding_chars[3],
    )
    segments: list[Segment] = []
    for line in re.split(r"\r\n|\r|\n", body):
        if not line.strip():
            continue
        name = line[:3]
        if not re.fullmatch(r"[A-Z][A-Z0-9]{2}", name):
            raise MessageFormatError(f"invalid segment name {name!r}")
        if name == "MSH":
            parts = (name, *line[4:].split(field_sep))
        else:
            if len(line) > 3 and line[3] != field_sep:
                raise MessageFormatError(f"segment {name} is not followed by the field separator")
            parts = tuple(line.split(field_sep))
        segments.append(Segment(name=name, fields=parts, enc=enc))
    if segments[0].name != "MSH":
        raise MessageFormatError("first segment must be MSH")
    return Message(segments=tuple(segments), enc=enc)


_TS = re.compile(
    r"^(?P<y>\d{4})(?P<mo>\d{2})?(?P<d>\d{2})?(?P<h>\d{2})?(?P<mi>\d{2})?(?P<s>\d{2})?"
    r"(?:\.(?P<frac>\d{1,4}))?(?P<tz>[+-]\d{4})?$"
)


def parse_timestamp(value: str, default_tz: tzinfo) -> datetime:
    """HL7 DTM/TS: YYYY[MM[DD[HH[MM[SS[.S[S[S[S]]]]]]]]][+/-ZZZZ]."""
    match = _TS.match(value.strip())
    if not match or match.group("h") is None:
        raise MessageFormatError(f"timestamp {value!r} needs at least YYYYMMDDHH precision")
    frac = match.group("frac") or ""
    tz: tzinfo = default_tz
    if match.group("tz"):
        raw = match.group("tz")
        sign = 1 if raw[0] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(raw[1:3]), minutes=int(raw[3:5])))
    try:
        return datetime(
            int(match.group("y")),
            int(match.group("mo") or 1),
            int(match.group("d") or 1),
            int(match.group("h")),
            int(match.group("mi") or 0),
            int(match.group("s") or 0),
            int(frac.ljust(6, "0")) if frac else 0,
            tzinfo=tz,
        )
    except ValueError as exc:
        raise MessageFormatError(f"invalid timestamp {value!r}") from exc


@dataclass(frozen=True)
class QcObservation:
    """One QC measurement extracted from an instrument or LIS message."""

    set_id: str
    analyte_code: str
    coding_system: str | None
    value: float
    units: str
    observed_at: datetime
    instrument_id: str
    material_id: str
    control_lot: str | None
    reagent_lot: str | None


@dataclass(frozen=True)
class QcMessage:
    control_id: str
    sending_application: str
    sending_facility: str
    version: str
    observations: tuple[QcObservation, ...]
    skipped: tuple[str, ...] = field(default=())


def _group_attrs(group: list[Segment]) -> tuple[str | None, str | None, str | None, bool]:
    material: str | None = None
    control_lot: str | None = None
    reagent_lot: str | None = None
    is_control = False
    for seg in group:
        if seg.name == "SPM":
            material = seg.get(2, 1) or material
            role = seg.get(11, 1)
            is_control = is_control or role == CONTROL_SPECIMEN_ROLE
        elif seg.name == "INV":
            substance_type = seg.get(3, 1)
            lot = seg.get(16) or None
            if substance_type == CONTROL_SUBSTANCE:
                control_lot = lot
            elif substance_type in REAGENT_SUBSTANCES:
                reagent_lot = lot
    return material, control_lot, reagent_lot, is_control


def _validate_header(message: Message) -> tuple[str, str]:
    msh = message.msh
    if (msh.get(9, 1), msh.get(9, 2)) != ("ORU", "R01"):
        raise MessageFormatError(f"expected ORU^R01, got {msh.raw(9)!r}")
    control_id = msh.get(10)
    if not control_id:
        raise MessageFormatError("MSH-10 message control ID is required")
    version = msh.get(12)
    if not version.startswith(SUPPORTED_VERSIONS):
        raise MessageFormatError(f"unsupported HL7 version {version!r}")
    for pid in message.all("PID"):
        if any(pid.get(n) for n in (2, 3, 4, 5, 7)):
            raise MessageFormatError(
                "PID carries patient identifiers; QC messages must not contain patient data"
            )
    return control_id, version


def _order_groups(message: Message) -> list[list[Segment]]:
    groups: list[list[Segment]] = []
    for seg in message.segments:
        if seg.name == "OBR":
            groups.append([seg])
        elif groups:
            groups[-1].append(seg)
    if not groups:
        raise MessageFormatError("ORU^R01 contains no OBR order groups")
    return groups


def _observation(
    obx: Segment,
    set_id: str,
    *,
    obr_time: str,
    material: str,
    control_lot: str | None,
    reagent_lot: str | None,
    default_tz: tzinfo,
) -> QcObservation:
    raw_value = obx.get(5)
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise MessageFormatError(f"OBX-{set_id}: value {raw_value!r} is not numeric") from exc
    instrument = obx.get(18)
    if not instrument:
        raise MessageFormatError(f"OBX-{set_id}: OBX-18 instrument identifier is required")
    when = obx.get(14) or obr_time
    if not when:
        raise MessageFormatError(f"OBX-{set_id}: no observation time in OBX-14 or OBR-7")
    return QcObservation(
        set_id=set_id,
        analyte_code=obx.get(3, 1),
        coding_system=obx.get(3, 3) or None,
        value=value,
        units=obx.get(6, 1),
        observed_at=parse_timestamp(when, default_tz),
        instrument_id=instrument,
        material_id=material,
        control_lot=control_lot,
        reagent_lot=reagent_lot,
    )


def extract_qc(text: str, *, default_tz: tzinfo) -> QcMessage:
    message = parse(text)
    control_id, version = _validate_header(message)
    observations: list[QcObservation] = []
    skipped: list[str] = []
    for index, group in enumerate(_order_groups(message), start=1):
        order_id = group[0].get(1) or str(index)
        material, control_lot, reagent_lot, is_control = _group_attrs(group)
        if not is_control:
            raise MessageFormatError(
                "order group is not a control specimen (SPM-11 must be 'Q'); "
                "patient results are not accepted"
            )
        if not material:
            raise MessageFormatError("SPM-2 control material identifier is required")
        for obx in (s for s in group if s.name == "OBX"):
            # OBX set IDs restart in every order group: qualify them by OBR-1.
            set_id = f"{order_id}.{obx.get(1) or len(observations) + len(skipped) + 1}"
            if obx.get(2) != "NM":
                skipped.append(f"OBX-{set_id}: value type {obx.get(2) or 'missing'} is not NM")
                continue
            status = obx.get(11)
            if status not in ACCEPTED_RESULT_STATUS:
                skipped.append(f"OBX-{set_id}: result status {status or 'missing'} not final")
                continue
            observations.append(
                _observation(
                    obx,
                    set_id,
                    obr_time=group[0].get(7),
                    material=material,
                    control_lot=control_lot,
                    reagent_lot=reagent_lot,
                    default_tz=default_tz,
                )
            )
    msh = message.msh
    return QcMessage(
        control_id=control_id,
        sending_application=msh.get(3),
        sending_facility=msh.get(4),
        version=version,
        observations=tuple(observations),
        skipped=tuple(skipped),
    )


def escape(value: str, enc: Encoding | None = None) -> str:
    e = enc or Encoding()
    out = value.replace(e.escape, f"{e.escape}E{e.escape}")
    for char, code in (
        (e.field, "F"),
        (e.component, "S"),
        (e.repetition, "R"),
        (e.subcomponent, "T"),
    ):
        out = out.replace(char, f"{e.escape}{code}{e.escape}")
    return out.replace("\r", " ").replace("\n", " ")


def build_ack(
    text: str,
    *,
    code: str,
    note: str,
    control_id: str,
    now: datetime,
    application: str = "LABQC",
) -> str:
    """HL7 original-mode acknowledgement (AA accept, AE error, AR reject).

    Uses the inbound message's sending/receiving parties swapped. When the
    inbound message cannot be parsed, generic party names are used.
    """
    try:
        msh = parse(text).msh
        receiver_app, receiver_fac = msh.get(3), msh.get(4)
        sender_fac, inbound_id, version = msh.get(6), msh.get(10), msh.get(12) or "2.5.1"
    except MessageFormatError:
        receiver_app, receiver_fac, sender_fac, inbound_id, version = "", "", "", "", "2.5.1"
    stamp = now.strftime("%Y%m%d%H%M%S") + now.strftime("%z")
    return (
        "\r".join(
            (
                f"MSH|^~\\&|{application}|{escape(sender_fac)}|{escape(receiver_app)}|"
                f"{escape(receiver_fac)}|{stamp}||ACK^R01^ACK|{escape(control_id)}|P|{escape(version)}",
                f"MSA|{code}|{escape(inbound_id)}|{escape(note[:80])}",
            )
        )
        + "\r"
    )
