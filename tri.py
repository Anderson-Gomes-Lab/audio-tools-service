"""Tri des moments proposés par l'analyse. Fonction pure, sans réseau."""

import math

SCORE_SEUIL = 7
MOMENTS_BASE = 2
MOMENTS_MAX = 8  # la passe 1 en liste 8 au plus
ECART_MIN_S = 20
PRIORITE = ["rire", "curiosite", "spectacle", "tension", "info", "detente", "maitrise"]  # à score égal, recherche 12


def quota(duree_s):
    """Moments à garder : 2 jusqu'à 10 min, puis 1 de plus par tranche de 10 min commencée, 8 au plus."""
    return min(MOMENTS_MAX, MOMENTS_BASE + max(0, math.ceil(duree_s / 600) - 1))


def _ecart(a, b):
    """Secondes entre deux moments ; négatif s'ils se chevauchent."""
    return max(a["debut_s"], b["debut_s"]) - min(a["fin_s"], b["fin_s"])


def _raison_de_rejet(moment, gardes, plafond):
    if moment.get("score", 0) < SCORE_SEUIL:
        return "score"
    if any(_ecart(g, moment) < ECART_MIN_S for g in gardes):
        return "ecart"
    if len(gardes) >= plafond:
        return "quota"
    return None


def _rang(moment):
    return -moment.get("score", 0), PRIORITE.index(moment["registre"])


def trier(moments, plafond=math.inf):
    """Rend (gardes, rejetes), du meilleur score au moins bon, le registre départageant les égalités.

    Ne modifie pas la liste reçue. Les gardés sont numérotés `n` à partir de 1,
    chaque rejeté porte sa `raison`. Zéro moment gardé est un résultat valable.
    Les rejetés pour `quota` ou `score` forment la réserve : de vrais moments, gardés pour plus tard.
    """
    gardes, rejetes = [], []
    for moment in sorted(moments, key=_rang):
        raison = _raison_de_rejet(moment, gardes, plafond)
        if raison is None:
            gardes.append({**moment, "n": len(gardes) + 1})
        else:
            rejetes.append({**moment, "raison": raison})
    return gardes, rejetes
