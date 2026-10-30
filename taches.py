"""Tâches longues : une à la fois, dans un fil d'exécution, état écrit sur le disque.

États : en_attente, en_cours, reussie (avec resultat), echouee (avec erreur, sans secret).
L'état survit à un redémarrage dans /media/taches/<id>.json ; un travail coupé par
le redémarrage ne reprendra pas : reprendre() le marque echouee pour que n8n le sache.
"""
import json
import logging
import os
import re
import threading
import uuid

journal = logging.getLogger(__name__)

_verrou = threading.Lock()  # une seule tâche longue à la fois
_etats = {}  # état de chaque tâche lancée depuis le démarrage
_en_cours = None

_MOTIF_ID = re.compile(r"[0-9a-f]{32}")
_NOM_SECRET = re.compile(r"TOKEN|KEY|SECRET|PASSWORD", re.IGNORECASE)
_MASQUES = (
    (re.compile(r"https?://\S+"), "[lien masqué]"),
    (re.compile(r"\burl:\s*\S+"), "url: [masquée]"),  # format des erreurs de connexion de requests
    (re.compile(r"\bhost='[^']*'"), "host='[masqué]'"),
    (re.compile(r"\bBearer\s+\S+", re.IGNORECASE), "Bearer [masqué]"),
    (re.compile(r"\bapify_api_\w+|\bAIza[\w-]+"), "[masqué]"),
    (re.compile(r"[?&][\w.-]+=\S+"), "?[masqué]"),
)


def sans_secret(message):
    """Retire d'un message d'erreur les clés et les liens signés, seuls à pouvoir fuiter."""
    for nom, valeur in os.environ.items():
        if _NOM_SECRET.search(nom) and len(valeur) >= 8:
            message = message.replace(valeur, "[masqué]")
    for motif, remplacement in _MASQUES:
        message = motif.sub(remplacement, message)
    return message[:500]


def ecrire_json(chemin, donnees):
    """Écriture atomique : un lecteur voit l'ancien fichier ou le nouveau, jamais un morceau."""
    temporaire = f"{chemin}.{uuid.uuid4().hex}.tmp"
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    try:
        with open(temporaire, "w", encoding="utf-8") as fichier:
            json.dump(donnees, fichier, ensure_ascii=False)
        os.replace(temporaire, chemin)
    finally:
        if os.path.exists(temporaire):
            os.remove(temporaire)


def _chemin(id_tache):
    return os.path.join(os.environ.get("MEDIA_DIR", "/media"), "taches", f"{id_tache}.json")


def _noter(etat):
    _etats[etat["id"]] = etat
    try:
        ecrire_json(_chemin(etat["id"]), etat)
    except OSError as e:  # disque plein : l'état reste lisible en mémoire
        journal.error("état de la tâche %s non écrit : %s", etat["id"], sans_secret(str(e)))


def _executer(id_tache, travail, video_id):
    global _en_cours
    with _verrou:
        _en_cours = id_tache
        _noter({"id": id_tache, "etat": "en_cours", "video_id": video_id})
        try:
            resultat = json.loads(json.dumps(travail()))
            fin = {"etat": "reussie", "resultat": resultat}
        except Exception as e:
            fin = {"etat": "echouee", "erreur": sans_secret(str(e)) or type(e).__name__}
            journal.error("tâche %s échouée : %s", id_tache, fin["erreur"])
        _noter({"id": id_tache, "video_id": video_id, **fin})
        _en_cours = None


def lancer(travail, video_id=None):
    """Démarre travail() dès que le verrou est libre et rend l'id de la tâche."""
    id_tache = uuid.uuid4().hex
    _noter({"id": id_tache, "etat": "en_attente", "video_id": video_id})
    threading.Thread(target=_executer, args=(id_tache, travail, video_id), daemon=True).start()
    return id_tache


def lire(id_tache):
    """État de la tâche, ou None si elle n'existe pas."""
    if not _MOTIF_ID.fullmatch(id_tache):
        return None
    if id_tache in _etats:
        return _etats[id_tache]
    try:
        with open(_chemin(id_tache), encoding="utf-8") as fichier:
            return json.load(fichier)
    except (OSError, ValueError):
        return None


def en_cours():
    return _en_cours


def video_occupee(video_id):
    """Vrai si une tâche attend ou travaille sur cette vidéo."""
    return any(e.get("video_id") == video_id and e["etat"] in ("en_attente", "en_cours")
               for e in list(_etats.values()))


def reprendre():
    """Au démarrage : rien ne tourne plus, ce qui attendait ou tournait est perdu."""
    dossier = os.path.dirname(_chemin("x"))
    if not os.path.isdir(dossier):
        return
    for nom in os.listdir(dossier):
        id_tache = nom.removesuffix(".json")
        etat = lire(id_tache) if nom.endswith(".json") and id_tache not in _etats else None
        if etat and etat.get("etat") in ("en_attente", "en_cours"):
            _noter({**etat, "etat": "echouee", "erreur": "interrompue par un redémarrage"})
