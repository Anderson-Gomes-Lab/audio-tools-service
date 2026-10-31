"""Modèles et tarifs de Gemini, et limite de dépense par vidéo qui en découle."""

from datetime import date

MODELE = "gemini-3.8-flash"
MODELE_IDENTIFICATION = "gemini-3.5-flash-lite"

FIN_TARIF_REDUIT = date(2026, 12, 31)
TARIF_REDUIT = (0.75, 3.75)  # $ par million de jetons : entrée, sortie
TARIF_NORMAL = (1.50, 7.50)
TARIF_IDENTIFICATION = (0.30, 2.50)

LIMITE_PAR_MINUTE_USD = 0.10  # au tarif réduit, le double de l'estimation : contre un emballement, pas la qualité
LIMITE_MIN_USD = 0.50  # une vidéo de 2 min 14 coûte 0,16 $ : sans plancher, elle serait arrêtée à tort


def _tarif(jour, modele=MODELE):
    if modele == MODELE_IDENTIFICATION:
        return TARIF_IDENTIFICATION
    return TARIF_REDUIT if jour <= FIN_TARIF_REDUIT else TARIF_NORMAL


def _cout(usage, jour, modele=MODELE):
    entree, sortie = _tarif(jour, modele)
    jetons_sortie = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    return ((usage.prompt_token_count or 0) * entree + jetons_sortie * sortie) / 1_000_000


def limite_video(duree_s, jour):
    """Deux fois l'estimation, qui double avec le tarif, et jamais sous le plancher."""
    hausse = _tarif(jour)[0] / TARIF_REDUIT[0]
    return max(LIMITE_MIN_USD, duree_s / 60 * LIMITE_PAR_MINUTE_USD * hausse)
