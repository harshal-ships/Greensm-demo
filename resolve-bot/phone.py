"""Shared phone normalization for ride/WA matching."""

from __future__ import annotations

import re


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("00"):
        digits = digits[2:]
    return digits
