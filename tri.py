"""Tri des moments proposés par l'analyse. Fonction pure, sans réseau."""

SCORE_SEUIL = 7
MAX_MOMENTS = 2
ECART_MIN_S = 20


def _ecart(a, b):
    """Secondes entre deux moments ; négatif s'ils se chevauchent."""
    return max(a["debut_s"], b["debut_s"]) - min(a["fin_s"], b["fin_s"])


def _raison_de_rejet(moment, gardes):
    if moment.get("score", 0) < SCORE_SEUIL:
        return "score"
    if len(gardes) >= MAX_MOMENTS:
        return "quota"
    if any(g["registre"] == moment["registre"] for g in gardes):
        return "registre"
    if any(_ecart(g, moment) < ECART_MIN_S for g in gardes):
        return "ecart"
    return None


def trier(moments):
    """Rend (gardes, rejetes), du meilleur score au moins bon.

    Ne modifie pas la liste reçue. Les gardés sont numérotés `n` à partir de 1,
    chaque rejeté porte sa `raison`. Zéro moment gardé est un résultat valable.
    """
    gardes, rejetes = [], []
    for moment in sorted(moments, key=lambda m: -m.get("score", 0)):
        raison = _raison_de_rejet(moment, gardes)
        if raison is None:
            gardes.append({**moment, "n": len(gardes) + 1})
        else:
            rejetes.append({**moment, "raison": raison})
    return gardes, rejetes
