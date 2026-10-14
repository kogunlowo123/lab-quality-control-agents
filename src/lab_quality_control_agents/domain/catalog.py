"""Analyte catalog: canonical codes, interface code mapping, allowable error, calibration interval.

Instrument interfaces identify tests differently: HL7 OBX-3 usually carries a
LOINC code, ASTM R-3 carries the manufacturer's local test code. The catalog
maps every known identifier to one canonical analyte code so QC history is
never split across aliases.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab_quality_control_agents.domain.models import Code
from lab_quality_control_agents.domain.statistics import AllowableError


class AnalyteDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: Code
    name: str = Field(min_length=2)
    loinc: str | None = Field(default=None, pattern=r"^\d{1,7}-\d$")
    units: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    tea: AllowableError
    calibration_interval_days: int = Field(default=30, ge=1, le=365)

    @property
    def calibration_interval(self) -> timedelta:
        return timedelta(days=self.calibration_interval_days)


class AnalyteCatalog(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    analytes: tuple[AnalyteDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unambiguous(self) -> AnalyteCatalog:
        seen: dict[str, str] = {}
        for analyte in self.analytes:
            keys = [analyte.code.upper(), *(a.upper() for a in analyte.aliases)]
            if analyte.loinc:
                keys.append(f"LN:{analyte.loinc}")
            for key in keys:
                owner = seen.setdefault(key, analyte.code)
                if owner != analyte.code:
                    raise ValueError(f"identifier {key} maps to both {owner} and {analyte.code}")
        return self

    @classmethod
    def from_file(cls, path: Path) -> AnalyteCatalog:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def get(self, code: str) -> AnalyteDefinition | None:
        for analyte in self.analytes:
            if analyte.code == code:
                return analyte
        return None

    def resolve(
        self, identifier: str, coding_system: str | None = None
    ) -> AnalyteDefinition | None:
        """Map an interface identifier (LOINC, local code or alias) to its analyte."""
        ident = identifier.strip()
        system = (coding_system or "").strip().upper()
        for analyte in self.analytes:
            if system == "LN":
                if analyte.loinc == ident:
                    return analyte
                continue
            if ident.upper() == analyte.code.upper() or ident.upper() in {
                a.upper() for a in analyte.aliases
            }:
                return analyte
            if not system and analyte.loinc == ident:
                return analyte
        return None
