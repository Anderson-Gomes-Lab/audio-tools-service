"""Liens publics temporaires : un Short lisible sans jeton d'accès pendant 24 h, le temps que Buffer le récupère.

n8n reprend le Short dans Drive et l'envoie ici : /media/shorts a déjà été vidé. L'échéance d'un lien est la date du
fichier plus 24 h, lue sur le disque : elle survit à un redémarrage et il n'y a rien d'autre à tenir à jour.
"""
import os
import re
import secrets
import time
from contextlib import suppress
from datetime import datetime, timezone

from werkzeug.exceptions import RequestEntityTooLarge

VIE_S = 24 * 3600
TAILLE_MAX = 300_000_000  # limite d'Instagram, la plus basse des plateformes visées
CORPS_MAX = TAILLE_MAX + 1_000_000  # l'enveloppe multipart s'ajoute au fichier
# Forme exacte de secrets.token_urlsafe(32), et .mp4 : Buffer et les réseaux lisent mieux un tel lien
MOTIF_LIEN = re.compile(r"[A-Za-z0-9_-]{43}\.mp4")


class Refus(Exception):
    """Envoi refusé : le message part tel quel vers n8n."""


def _echeance(chemin):
    return os.path.getmtime(chemin) + VIE_S


def _adresse():
    """Adresse fixée par la configuration : l'en-tête Host d'une requête se falsifie."""
    adresse = os.environ.get("URL_PUBLIQUE", "").rstrip("/")
    if not adresse:
        raise Refus("URL_PUBLIQUE absente")
    return adresse


def purger(dossier):
    """Efface tout ce qui a expiré, y compris un .part laissé par une panne."""
    if not os.path.isdir(dossier):
        return
    for nom in os.listdir(dossier):
        chemin = os.path.join(dossier, nom)
        with suppress(FileNotFoundError):  # un autre envoi a pu passer juste avant
            if time.time() >= _echeance(chemin):
                os.remove(chemin)


def _verifier(fichier):
    if fichier is None:
        raise Refus("Missing fichier")
    flux = fichier.stream
    entete = flux.read(8)
    flux.seek(0, os.SEEK_END)
    taille = flux.tell()
    flux.seek(0)
    if taille > TAILLE_MAX:
        raise RequestEntityTooLarge()
    if entete[4:8] != b"ftyp":
        raise Refus("Not an MP4")


def creer(fichier, dossier):
    """Écrit le fichier sous un jeton aléatoire et rend son lien et son échéance en UTC."""
    _verifier(fichier)
    adresse = _adresse()
    os.makedirs(dossier, exist_ok=True)
    jeton = secrets.token_urlsafe(32)
    chemin = os.path.join(dossier, f"{jeton}.mp4")
    partiel = f"{chemin}.part"
    try:
        fichier.save(partiel)
        os.replace(partiel, chemin)
    finally:
        with suppress(FileNotFoundError):
            os.remove(partiel)
    echeance = datetime.fromtimestamp(_echeance(chemin), timezone.utc)
    return {"url": f"{adresse}/p/{jeton}.mp4", "expire_le": echeance.isoformat(timespec="seconds")}


def chemin_valide(jeton, dossier):
    """Chemin du fichier si le lien vit encore, sinon None : le motif interdit toute sortie du dossier."""
    if not MOTIF_LIEN.fullmatch(jeton):
        return None
    chemin = os.path.join(dossier, jeton)
    try:
        return chemin if time.time() < _echeance(chemin) else None
    except FileNotFoundError:
        return None
