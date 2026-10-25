"""Profils CSV intégrés. Formats **supposés**, à valider avec un export réel de chaque banque
(action manuelle du PLAN, section 2.1) ; les profils personnalisés seront stockés en base."""

from app.connectors.file_import.csv_import import ColumnMap, CsvProfile

BOURSORAMA = CsvProfile(
    name="boursorama",
    institution="Boursorama",
    encoding="utf-8-sig",
    delimiter=";",
    decimal_sep=",",
    date_formats=("%Y-%m-%d", "%d/%m/%Y"),
    columns=ColumnMap(date="dateOp", value_date="dateVal", label=("label",), amount="amount"),
)

SOCIETE_GENERALE = CsvProfile(
    name="societe_generale",
    institution="Société Générale",
    encoding="cp1252",
    delimiter=";",
    decimal_sep=",",
    date_formats=("%d/%m/%Y",),
    columns=ColumnMap(
        date="Date de l'opération",
        label=("Détail de l'écriture", "Libellé"),
        amount="Montant de l'opération",
        currency="Devise",
    ),
)

BUILTIN_PROFILES = {profile.name: profile for profile in (BOURSORAMA, SOCIETE_GENERALE)}
