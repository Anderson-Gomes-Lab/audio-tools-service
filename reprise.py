"""Ce qui survit à une panne de lecture : le relevé de ce qui est payé, et les remarques du joueur.

Tout vit dans /media/lectures, que /nettoyer ne touche pas.
"""

import json
import logging
import os
import threading

import taches

journal = logging.getLogger(__name__)

verrou_note = threading.Lock()  # une note arrive sur le fil de Flask pendant que la tâche écrit la lecture


def chemin(dossier, video_id, suffixe="json"):
    return os.path.join(dossier, f"{video_id}.{suffixe}")


def lire_json(fichier):
    try:
        with open(fichier, encoding="utf-8") as lu:
            return json.load(lu)
    except FileNotFoundError:
        return None


class Releve:
    """Coût engagé sur tous les essais d'une vidéo, et chaque réponse payée, écrits après chaque appel.

    `cout_usd` est un cumul : il part de la lecture écrite s'il y en a une, sans la recompter.
    `annonce_usd` : la part déjà annoncée par une tâche, gardée sur le disque. Une tâche n'annonce que le reste,
    après un redémarrage comme après une lecture écrite : tant qu'il reste du coût à annoncer, le relevé reste.
    """

    def __init__(self, fichier, lecture=None):
        self.fichier = fichier
        deja = lecture["cout_usd"] if lecture else 0.0
        self.donnees = lire_json(fichier) or {"cout_usd": deja, "annonce_usd": deja, "reponses": {}}

    @property
    def cout_usd(self):
        return self.donnees["cout_usd"]

    def _ecrire(self):
        taches.ecrire_json(self.fichier, self.donnees)

    def payer(self, cout):
        self.donnees["cout_usd"] += cout
        self._ecrire()

    def deja(self, cle):
        return cle in self.donnees["reponses"]

    def une_fois(self, cle, appel):
        """La réponse déjà payée, sinon l'appel, gardé tout de suite."""
        if not self.deja(cle):
            self.donnees["reponses"][cle] = appel()
            self._ecrire()
        return self.donnees["reponses"][cle]

    def _du(self):
        return round(self.cout_usd - self.donnees["annonce_usd"], 6)

    def annoncer(self):
        """Ce que la tâche en échec révèle. Le relevé reste, pour la reprise."""
        du = self._du()
        if du:
            self.donnees["annonce_usd"] = self.cout_usd
            try:
                self._ecrire()
            except OSError as erreur:  # disque plein : le coût part quand même vers n8n
                journal.error("relevé non écrit : %s", erreur)
        return du

    def clore(self):
        """Lecture écrite et plus rien à jouer : plus rien à reprendre. Rend ce qui reste à annoncer."""
        du = self._du()
        if os.path.isfile(self.fichier):
            os.remove(self.fichier)
        return du


def mettre_en_attente(video_id, note, dossier):
    """Écrit la note avant toute chose, à la suite des autres ; une note renvoyée telle quelle n'est pas doublée.

    Vrai si elle attend la fin de la 1re passe ; faux si la lecture existe et que la 2e passe peut partir.
    """
    with verrou_note:
        os.makedirs(dossier, exist_ok=True)
        if f"\n{note}\n" not in f"\n{notes(video_id, dossier)}":
            with open(chemin(dossier, video_id, "note.txt"), "a", encoding="utf-8") as fichier:
                fichier.write(note + "\n")
                fichier.flush()
                os.fsync(fichier.fileno())
        return not os.path.isfile(chemin(dossier, video_id))


def notes(video_id, dossier):
    """Le texte exact des notes en attente, vide s'il n'y en a pas."""
    try:
        with open(chemin(dossier, video_id, "note.txt"), encoding="utf-8") as fichier:
            return fichier.read()
    except FileNotFoundError:
        return ""


def retirer_notes(video_id, dossier, jouees):
    """Retire les notes jouées ; celles reçues entre-temps restent pour la 2e passe suivante."""
    fichier = chemin(dossier, video_id, "note.txt")
    with verrou_note:
        reste = notes(video_id, dossier).removeprefix(jouees)
        if not reste:
            os.remove(fichier)
            return
        with open(f"{fichier}.tmp", "w", encoding="utf-8") as temporaire:
            temporaire.write(reste)
        os.replace(f"{fichier}.tmp", fichier)
