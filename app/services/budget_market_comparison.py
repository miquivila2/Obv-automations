"""Market-rate comparison (Axo Capital budget format, §4 "What the same work
costs elsewhere") — reinstated on explicit request after the user shared the
real Axo Capital reference document. Was previously deferred (docs
ARCHITECTURE.md §9.12); that decision is now reversed.

WHY THESE SPECIFIC BANDS: they're the exact categories and USD/hr ranges cited
in Oblivion's real, already-delivered Axo Capital budget (sourced there from
published rate guides — Curotec/index.dev for LATAM, South for US freelance,
Volpis/Chop Dawg for US agencies). Reusing them as the default comparison set
is not fabrication — they're real, previously-sourced numbers — but they ARE
a point-in-time snapshot, not a live feed, so they should be refreshed
periodically rather than treated as permanently current.

WHY USD ONLY: this system has a locked decision (docs §5.2) that a budget is
priced in ONE currency start to finish and nothing ever converts between
currencies. These bands are sourced in USD; showing them against an MXN
budget would require a conversion this system deliberately never does. So
`compute_market_comparison` returns None for any non-USD currency rather than
silently reaching for an exchange rate.
"""
from __future__ import annotations

# (label, low $/hr, high $/hr, short note) — order matches the Axo reference.
_MARKET_RATE_BANDS_USD: list[tuple[str, float, float, str]] = [
    ("A software agency (US), same scope", 150.0, 300.0, "US agencies"),
    ("A US senior freelancer, same scope", 85.0, 150.0, "US senior freelance"),
    ("A LATAM senior freelance team, same scope", 65.0, 100.0, "LATAM senior freelance"),
]


def compute_market_comparison(total_hours: float, currency: str) -> list[dict] | None:
    """Scale the published rate bands by this project's real total hours.
    Returns None when currency != 'USD' (no currency conversion, ever)."""
    if currency != "USD" or total_hours <= 0:
        return None

    return [
        {
            "label": label,
            "rate_range": f"US${low:,.0f} to US${high:,.0f}/hr",
            "price_low": round(total_hours * low, 2),
            "price_high": round(total_hours * high, 2),
            "note": note,
        }
        for label, low, high, note in _MARKET_RATE_BANDS_USD
    ]
