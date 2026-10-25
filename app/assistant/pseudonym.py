"""Pseudonymisation de tout ce que reçoit l'IA (inv. 8, NF-SEC-07, docs/spec-mcp.md §5).

Les banques ne transmettent pas le nom du tiers à part : il est repéré dans le libellé.

1. Identifiants retirés : IBAN, e-mails, liens, téléphones, fin de numéro de carte, et toute
   suite de 5 chiffres ou plus (`#`). Les dates restent.
2. Noms de personnes abrégés en initiales (« Jean Dupont » → « J. D. ») :
   - mots des noms connus (liste de la page Sécurité, payeurs des baux) partout ;
   - après une civilité (M, MME…), jusqu'à trois mots ;
   - dans un **virement**, tout mot hors vocabulaire bancaire courant et hors émetteurs non
     personnels (employeur, CAF…) : mieux vaut abréger un nom de société que laisser passer
     un nom de personne.
3. Noms de commerçants et d'organismes conservés (décision Q-B1) ; troncature à 80 caractères.

Garde de sortie : `scrub` abrège tout nom connu resté en toutes lettres (nom de catégorie, de
règle…), puis `check` refuse une réponse où subsisterait un IBAN ou une suite de 5 chiffres.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

MAX_LABEL = 80
NAME_WORDS = 2  # prénom et nom : les mots suivants sont souvent le motif (« LOYER », « RESTO »)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+33\s?|0)[1-9](?:[ .-]?\d{2}){4}(?!\d)")
_CARD = re.compile(r"\*\d{4}\b")
_DIGITS = re.compile(r"\d{5,}")
_PLACEHOLDERS = frozenset(("[lien]", "[e-mail]"))
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")
_OPAQUE = re.compile(r"^[a-z]_[a-z2-7]{26}$")
_NAME_LIKE = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*$")
_EDGES = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)
_PSEUDONYMIZED = re.compile(r"(?:^|\s|\W)[A-Z]\.(?:$|\s|-)|#")

TITLES = frozenset("M MR MME MM MMES MLLE MONSIEUR MADAME MADEMOISELLE MESSIEURS MESDAMES".split())
_STOP_AFTER_TITLE = frozenset("OU ET DE DU POUR PAR".split())
_TRANSFER = frozenset("VIR VIREMENT VIRT VIREMENTS".split())
_NOT_TRANSFER = frozenset("PRLV PRELEVEMENT CARTE CB RETRAIT CHQ CHEQUE".split())
_MONTHS = (
    "JANVIER FEVRIER MARS AVRIL MAI JUIN JUILLET AOUT SEPTEMBRE OCTOBRE NOVEMBRE DECEMBRE "
    "JANV FEV FEVR AVR JUIL SEPT OCT NOV DEC"
)
VOCABULARY = frozenset(
    (
        _MONTHS
        + " VIR VIREMENT VIREMENTS VIRT INST INSTANTANE SEPA RECU RECUS EMIS PERMANENT PERM WEB"
        " DE DU DES D LA LE LES L A AU AUX POUR PAR ET OU EN SUR SANS AVEC CHEZ MON MA MES SON"
        " SA SES NOTRE VOTRE REF REFERENCE MOTIF DATE BENEF BENEFICIAIRE ORDRE DONNEUR EMETTEUR"
        " ID MDT RUM ICS ECH LIB LIBELLE INFO REMISE CHEQUE CHQ INTERNE EXTERNE TRANSFERT"
        " LOYER LOYERS CHARGES CHARGE PRET PRETS REMBOURSEMENT REMBOURSEMENTS REMBT REMB RBT"
        " SALAIRE SALAIRES PAIE PRIME PRIMES CAF IMPOT IMPOTS DGFIP TRESOR PUBLIC FINANCES URSSAF"
        " CPAM AMELI MUTUELLE EPARGNE LIVRET LDDS LEP PEL CEL ASSURANCE VIE COMPTE COMPTES"
        " COURANT JOINT FACTURE CADEAU CADEAUX PARTICIPATION COURSES FRAIS VACANCES NOEL"
        " ANNIVERSAIRE PART MOIS ANNUEL MENSUEL MENSUALITE ACOMPTE SOLDE CAUTION DEPOT GARANTIE"
        " TRAVAUX ELECTRICITE GAZ EAU TELEPHONE ABONNEMENT CANTINE ECOLE CRECHE RESTAURANT"
        " DINER DEJEUNER WEEKEND SOIREE AVANCE DETTE RETOUR APPORT VERSEMENT ALLOCATION"
        " ALLOCATIONS PRESTATIONS PENSION RETRAITE INDEMNITE INDEMNITES NOTE REPAS SORTIE"
        " SAS SASU SARL SA SCI EURL SNC SCP SELARL ASSOCIATION ASSO BANQUE BANK BOURSORAMA"
        " BOURSOBANK SOCIETE GENERALE SG LCL BNP CIC CREDIT AGRICOLE MUTUEL CAISSE POSTALE"
        " RECU RECUE ENVOI ENVOYE MERCI BONNE ANNEE"
    ).split()
)


class LeakError(ValueError):
    """Une donnée personnelle a survécu à la pseudonymisation : l'envoi est refusé."""


def normalize(text: str) -> str:
    """Majuscules sans accents (comparaisons seulement)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).upper()


def initials(word: str) -> str:
    """« DUPONT » → « D. », « Jean-Pierre » → « J.-P. »."""
    parts = [part for part in word.split("-") if part]
    return "-".join(normalize(part)[:1] + "." for part in parts)


def looks_pseudonymized(text: str) -> bool:
    """Vrai si le texte contient des initiales ou `#` : motif inutilisable pour une règle."""
    return bool(_PSEUDONYMIZED.search(text))


def _strip_identifiers(text: str) -> str:
    text = _URL.sub("[lien]", text)
    text = _EMAIL.sub("[e-mail]", text)
    text = _IBAN.sub("#", text)
    text = _PHONE.sub("#", text)
    text = _CARD.sub("*#", text)
    return _DIGITS.sub("#", text)


def _name_words(names: Iterable[str]) -> frozenset[str]:
    words: set[str] = set()
    for name in names:
        for word in re.split(r"[\s,;:/()]+", normalize(name)):
            core = word.strip(".'-")
            if len(core) >= 2 and core not in TITLES and core not in VOCABULARY:  # noqa: PLR2004
                words.add(core)
    return frozenset(words)


class Pseudonymizer:
    """Nettoyage des textes envoyés à l'IA, à partir des noms connus et des émetteurs gardés."""

    def __init__(self, *, persons: Iterable[str], emitters: Iterable[str]) -> None:
        self.person_words = _name_words(persons)
        keep: set[str] = set()
        for emitter in emitters:
            keep.update(w for w in re.split(r"\W+", normalize(emitter)) if w)
        self.kept_words = frozenset(keep) - self.person_words

    # --- Nettoyage -----------------------------------------------------------------------------

    def label(self, raw: str) -> str:
        """Libellé bancaire tel que l'IA le reçoit."""
        return self._clean(raw, transfer_rule=True)[:MAX_LABEL].rstrip()

    def text(self, value: str) -> str:
        """Texte libre saisi par moi (nom de compte, de bail, motif de règle) : noms connus et
        noms après une civilité abrégés, identifiants retirés ; la casse est conservée."""
        return self._clean(value, transfer_rule=False)

    def names(self, raw: str) -> list[str]:
        """Noms repérés dans un libellé (suites de mots abrégés), en clair : pour que le
        serveur, et lui seul, en tire le payeur d'un bail ou le motif d'une règle."""
        found: list[list[str]] = []
        previous = False
        for _, core, abbreviated in self._tokens(raw, transfer_rule=True):
            if abbreviated:
                if not previous:
                    found.append([])
                found[-1].append(normalize(core))
            previous = abbreviated
        return [" ".join(words[:NAME_WORDS]) for words in found]

    def _clean(self, value: str, *, transfer_rule: bool) -> str:
        return " ".join(shown for shown, _, _ in self._tokens(value, transfer_rule=transfer_rule))

    def _tokens(self, value: str, *, transfer_rule: bool) -> list[tuple[str, str, bool]]:
        """(texte montré, mot d'origine, abrégé ?) pour chaque mot."""
        tokens = _strip_identifiers(" ".join(value.split())).split(" ")
        cores = [_EDGES.match(token) for token in tokens]
        normalized = [normalize(m.group(2)) if m else "" for m in cores]
        is_transfer = transfer_rule and _is_transfer(normalized)
        out: list[tuple[str, str, bool]] = []
        after_title = 0
        for token, match, word in zip(tokens, cores, normalized, strict=True):
            if match is None or not word or token.strip(".,;:") in _PLACEHOLDERS:
                out.append((token, token, False))
                after_title = 0
                continue
            head, core, tail = match.groups()
            name_like = bool(_NAME_LIKE.match(core)) and len(core) > 1
            if word in TITLES:
                out.append((token, core, False))
                after_title = 3
                continue
            abbreviate = name_like and (
                word in self.person_words
                or any(part in self.person_words for part in word.split("-"))
                or (after_title > 0 and word not in _STOP_AFTER_TITLE and word not in VOCABULARY)
                or (is_transfer and word not in VOCABULARY and word not in self.kept_words)
            )
            if after_title > 0:
                after_title = after_title - 1 if abbreviate else 0
            out.append((head + initials(core) + tail if abbreviate else token, core, abbreviate))
        return out

    # --- Garde de sortie -----------------------------------------------------------------------

    def scrub(self, payload: Any) -> Any:
        """Copie de la réponse où chaque nom connu encore en toutes lettres est abrégé et chaque
        identifiant resté visible (texte saisi : nom de catégorie, de règle…) masqué."""
        if isinstance(payload, str):
            if _OPAQUE.match(payload):
                return payload
            text = _DIGITS.sub("#", _IBAN.sub("#", payload))
            return _WORD.sub(self._abbreviate_known, text) if self.person_words else text
        if isinstance(payload, dict):
            return {key: self.scrub(value) for key, value in payload.items()}
        if isinstance(payload, list | tuple):
            return [self.scrub(value) for value in payload]
        return payload

    def _abbreviate_known(self, match: re.Match[str]) -> str:
        word = match.group(0)
        known = normalize(word) in self.person_words or any(
            normalize(part) in self.person_words for part in word.split("-")
        )
        return initials(word) if known and len(word) > 1 else word

    def check(self, payload: Any) -> None:
        """Refuse une réponse où subsiste un IBAN ou une suite de 5 chiffres (après `scrub`)."""
        for text in _strings(payload):
            if _OPAQUE.match(text):
                continue
            if _IBAN.search(text) or _DIGITS.search(text):
                raise LeakError("identifiant non masqué")


def _is_transfer(words: list[str]) -> bool:
    significant = [w for w in words if w and not w.isdigit()]
    if not significant or significant[0] in _NOT_TRANSFER:
        return False
    return any(w in _TRANSFER for w in significant[:3])


def _strings(payload: Any) -> Iterable[str]:
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(payload, list | tuple):
        for value in payload:
            yield from _strings(value)
