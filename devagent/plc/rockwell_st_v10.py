from __future__ import annotations

from devagent.plc import v2_guardrails as _guard
from devagent.plc import v2_semantics as _v2


# Studio 5000 Structured Text commonly uses IEC-style elementary conversions
# inside otherwise deterministic assignments. Treat these names like pure
# expression functions so they are not misclassified as unresolved instruction
# or AOI calls. This only improves read/write expression normalization; it does
# not claim conversion range/overflow behavior beyond the exported expression.
_TYPE_CONVERSIONS = {
    "BOOL",
    "SINT",
    "INT",
    "DINT",
    "LINT",
    "USINT",
    "UINT",
    "UDINT",
    "ULINT",
    "REAL",
    "LREAL",
    "TIME",
    "TIME32",
    "LTIME",
    "DATE",
    "DT",
    "LDT",
    "TOD",
    "STRING",
}
_INSTALLED = False


def install() -> None:
    """Extend only the proven-safe ST expression-function vocabulary.

    V2 guardrails deliberately keep ELSIF/ELSE/CASE, packed assignment lines,
    loops, indirect indexing, and unresolved calls PARTIAL until a dedicated
    control/expression AST proves their exact semantics. V10 must not bypass
    those guards merely because the source text can be tokenized.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _v2._ST_SAFE_FUNCTIONS.update(_TYPE_CONVERSIONS)
    _guard._SAFE_FUNCTIONS.update(_TYPE_CONVERSIONS)
    _INSTALLED = True


__all__ = ["install"]
