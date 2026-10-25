"""Conditions d'une règle de catégorisation (ANA-01).

Toutes les conditions renseignées doivent être vraies ; `label_contains` est vrai si l'un des
fragments figure dans le libellé nettoyé (sans tenir compte de la casse).

Expression régulière : moteur `regex` avec délai maximal. Une expression saisie à la main ou
proposée par l'IA ne doit jamais pouvoir bloquer le serveur (retour arrière exponentiel) :
elle est refusée si elle s'emballe sur des libellés piégés, et chaque recherche est bornée.
"""

import logging
from datetime import timedelta
from typing import Literal, Self

import regex
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

LOGGER = logging.getLogger(__name__)
MAX_REGEX_LENGTH = 200
# Délais en timedelta (aucun littéral décimal dans app/, inv. 2) ; regex les veut en secondes.
MATCH_TIMEOUT = timedelta(milliseconds=20).total_seconds()  # une recherche normale : quelques µs
PROBE_TIMEOUT = timedelta(milliseconds=50).total_seconds()
# Libellés piégés : longues répétitions suivies d'un caractère qui fait échouer la fin du motif.
PROBES = tuple(unit * (60 // len(unit)) + "!" for unit in ("a", "A1", "ab ", "1", " ", "a-", "x.y"))


class RuleConditions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label_contains: list[str] | None = None
    label_regex: str | None = None
    counterparty_contains: str | None = None
    amount_min: int | None = None  # centimes, bornes incluses
    amount_max: int | None = None
    direction: Literal["debit", "credit"] | None = None
    account_ids: list[int] | None = None

    @field_validator("label_regex")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if len(value) > MAX_REGEX_LENGTH:
            raise ValueError(f"expression régulière trop longue ({MAX_REGEX_LENGTH} caractères)")
        try:
            compiled = regex.compile(value, regex.IGNORECASE)
        except regex.error as exc:
            raise ValueError(f"expression régulière invalide : {exc}") from None
        try:
            for probe in PROBES:
                compiled.search(probe, timeout=PROBE_TIMEOUT)
        except TimeoutError:
            raise ValueError(
                "expression régulière trop coûteuse à évaluer : simplifiez-la (évitez les"
                " répétitions imbriquées ou les alternatives qui se chevauchent, comme (a|aa)+)"
            ) from None
        return value

    @model_validator(mode="after")
    def _at_least_one(self) -> Self:
        if not any(getattr(self, name) not in (None, []) for name in type(self).model_fields):
            raise ValueError("une règle doit avoir au moins une condition")
        return self


def matches(
    conditions: RuleConditions,
    *,
    label: str,
    amount: int,
    account_id: int,
    counterparty: str | None,
) -> bool:
    upper = label.upper()
    checks = (
        conditions.label_contains is None
        or any(fragment.upper() in upper for fragment in conditions.label_contains),
        conditions.label_regex is None or _search(conditions.label_regex, label),
        conditions.counterparty_contains is None
        or (counterparty or "").upper().find(conditions.counterparty_contains.upper()) >= 0,
        conditions.amount_min is None or amount >= conditions.amount_min,
        conditions.amount_max is None or amount <= conditions.amount_max,
        conditions.direction is None or (amount < 0) == (conditions.direction == "debit"),
        conditions.account_ids is None or account_id in conditions.account_ids,
    )
    return all(checks)


def _search(pattern: str, label: str) -> bool:
    """Recherche bornée : au-delà du délai, la règle ne s'applique pas à ce libellé."""
    try:
        return regex.search(pattern, label, regex.IGNORECASE, timeout=MATCH_TIMEOUT) is not None
    except TimeoutError:
        LOGGER.warning("expression régulière trop lente sur un libellé : règle ignorée")
        return False
