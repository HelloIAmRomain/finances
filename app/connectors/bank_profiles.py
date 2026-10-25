"""Profils de budget DSP2 par banque (inv. 10, Q14).

**Hypothèse, pas une vérité** : la réglementation (RTS, art. 36(5)) autorise 4 accès par jour
sans l'utilisateur, mais chaque banque interprète le compteur à sa façon (par compte, par
endpoint, par consentement ; pages comprises ou non). On retient le plafond le plus prudent
connu : 4 requêtes par compte et **par endpoint** sur 24 h glissantes, toutes requêtes comprises
(synchros manuelles, reprises, pages de transactions).

Les limites réellement observées (réponses 429 ou refus de quota) sont journalisées
(`app.connectors.budget.observed_limits`, commande `limites-observees`) pour ajuster ensuite ces
profils, dans ce fichier ou par `FINANCES_BANK_BUDGETS` (JSON : {"banque": {"endpoint": n}}).
"""

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

DEFAULT_LIMIT = 4


@dataclass(frozen=True)
class BankProfile:
    name: str
    aliases: tuple[str, ...] = ()
    limits: Mapping[str, int] = field(default_factory=dict)
    default_limit: int = DEFAULT_LIMIT

    def limit(self, endpoint: str) -> int:
        return self.limits.get(endpoint, self.default_limit)


def normalize(name: str) -> str:
    """« Société Générale » → « societe generale » (le nom exact de l'ASPSP peut varier)."""
    decomposed = unicodedata.normalize("NFKD", name)
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).lower().split())


# Valeurs initiales identiques pour les deux banques, à ajuster d'après les limites observées.
BUILTIN_PROFILES = (
    BankProfile(
        "boursorama",
        aliases=("boursorama", "boursobank"),
        limits={"balances": 4, "transactions": 4},
    ),
    BankProfile(
        "societe generale",
        aliases=("societe generale",),
        limits={"balances": 4, "transactions": 4},
    ),
)
DEFAULT_PROFILE = BankProfile("défaut")

_profiles: tuple[BankProfile, ...] = BUILTIN_PROFILES


def configure_profiles(overrides: Mapping[str, Mapping[str, int]]) -> None:
    """Applique les limites de la configuration (au démarrage du web et du worker)."""
    global _profiles  # noqa: PLW0603 - configuration unique au démarrage, comme le trousseau
    profiles = {profile.name: profile for profile in BUILTIN_PROFILES}
    for bank, limits in overrides.items():
        if any(value < 1 for value in limits.values()):
            raise ValueError(f"plafond DSP2 invalide pour {bank} : {dict(limits)}")
        key = normalize(bank)
        base = next(
            (p for p in profiles.values() if key == p.name or key in p.aliases),
            BankProfile(key, aliases=(key,)),
        )
        profiles[base.name] = BankProfile(
            base.name,
            aliases=base.aliases,
            limits=MappingProxyType({**base.limits, **limits}),
            default_limit=base.default_limit,
        )
    _profiles = tuple(profiles.values())


def profile_for(institution_name: str | None) -> BankProfile:
    if institution_name is None:
        return DEFAULT_PROFILE
    key = normalize(institution_name)
    for profile in _profiles:
        if any(alias in key for alias in profile.aliases):
            return profile
    return DEFAULT_PROFILE
