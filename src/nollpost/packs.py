"""Built-in opt-in regional and domain rule packs for nollpost."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable


@dataclass(frozen=True)
class RulePack:
    """Declarative regional/domain classification and link-scoring rules."""

    name: str
    description: str
    bill_keywords: tuple[str, ...] = ()
    explicit_bill_senders: tuple[str, ...] = ()
    transactional_domains: tuple[str, ...] = ()
    transactional_text_re: re.Pattern[str] | None = None
    security_text_re: re.Pattern[str] | None = None
    retail_promo_re: re.Pattern[str] | None = None
    critical_bill_terms_re: re.Pattern[str] | None = None
    gov_domain_token: str = ""
    gov_subcategory: str = ""
    gov_badge: str = ""
    gov_app_host: str = ""
    gov_cta_re: re.Pattern[str] | None = None
    gov_noise_path_re: re.Pattern[str] | None = None


SWEDISH_PACK = RulePack(
    name="se",
    description="Swedish billing, digital mailbox (Kivra), and parcel delivery rules",
    bill_keywords=(
        "kivra",
        "fortnox",
        "bankgirot",
        "skatteverket",
        "faktura",
        "utbetalning",
        "förfallodatum",
        "betalning",
        "påminnelse",
        "inkasso",
        "ocr",
    ),
    explicit_bill_senders=("kivra", "fortnox", "bankgirot", "skatteverket", "faktura"),
    transactional_domains=(
        "airmee.com",
        "postnord.se",
        "postnord.com",
        "budbee.com",
        "instabox.se",
    ),
    transactional_text_re=re.compile(
        r"\b(?:leverans|skickas\s+nu|beställning\s+är\s+packad|tack\s+för\s+din\s+beställning)\b",
        re.I,
    ),
    security_text_re=re.compile(
        r"\b(?:säkerhetsvarning|app[\s‑-]specifikt\s+lösenord|engångskod|logga\s+in\s+från)\b",
        re.I,
    ),
    retail_promo_re=re.compile(
        r"\b(?:rabatt|erbjudande|fynd|rea\b|nyhetsbrev)\b",
        re.I,
    ),
    critical_bill_terms_re=re.compile(
        r"\b(?:kivra|fortnox|bankgirot|skatteverket|faktura|påminnelse|inkasso|förfallodatum|betalning|utbetalning)\b",
        re.I,
    ),
    gov_domain_token="kivra",
    gov_subcategory="Gov / Kivra",
    gov_badge="Kivra",
    gov_app_host="app.kivra.com",
    gov_cta_re=re.compile(r"logga\s+in|öppna|visa|meddelande|läsa", re.I),
    gov_noise_path_re=re.compile(r"/om-kivra|/about|/faq|/hjalp", re.I),
)

BUILTIN_PACKS: dict[str, RulePack] = {
    "se": SWEDISH_PACK,
}


@lru_cache(maxsize=32)
def _resolve_packs_cached(pack_names: tuple[str, ...]) -> tuple[RulePack, ...]:
    resolved: list[RulePack] = []
    seen: set[str] = set()
    for raw in pack_names:
        name = str(raw).strip().lower()
        if not name or name in seen:
            continue
        pack = BUILTIN_PACKS.get(name)
        if pack is None:
            available = ", ".join(sorted(BUILTIN_PACKS))
            raise ValueError(f"Unknown rule pack {raw!r}; available packs: {available}")
        seen.add(name)
        resolved.append(pack)
    return tuple(resolved)


def resolve_packs(pack_names: Iterable[str]) -> tuple[RulePack, ...]:
    """Validate and resolve enabled rule pack names in order without duplicates."""
    if isinstance(pack_names, (str, bytes)):
        raise ValueError("enabled_packs must be a list of pack names (e.g. [\"se\"]), not a string")
    return _resolve_packs_cached(tuple(str(p) for p in pack_names))
