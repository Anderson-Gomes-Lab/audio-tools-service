"""Appels à Gemini : gardes de coût, fichier distant, et précision des moments pour le montage.

La chaîne de lecture est dans lecture.py. `_preciser` n'a plus d'appelant : il revient au lot 2, avec le plan.
"""

import json
import logging
import math
import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google import genai
from google.genai import errors, types

import montage
import taches
import texte
import tri
from tarifs import MODELE, MODELE_IDENTIFICATION, _cout, limite_video  # noqa: F401 (lecture.py les lit ici)

CONSIGNE = Path(__file__).with_name("consigne.md")
MARGE_S = 20
APRES_PIC_MIN_S = 3  # 2 % des gagnants du marché finissent à moins de 2 s du pic (recherche 12 v4)
FPS_PASSE_2 = 5
RELANCES_JSON = 2
TOLERANCE_ARRONDI_S = 1  # MM:SS arrondit la fin de vidéo à la seconde pleine
ATTENTE_FICHIER_S = 900
PAUSE_FICHIER_S = 5
DELAI_APPEL_MS = 600_000
RELANCES_RESEAU = types.HttpRetryOptions(  # 5, 10, 20 puis 40 s : de quoi passer une minute saturée
    attempts=5, initial_delay=5.0, max_delay=60.0, http_status_codes=[429, 500, 502, 503, 504])

VIE_FICHIER = timedelta(hours=46)  # Gemini l'efface seul à 48 h ; 2 h de marge, comptées depuis le début de l'envoi
FICHIER_PERDU = (403, 404)  # toute autre erreur de Gemini est passagère : le fichier est peut-être toujours là

REGISTRES = tri.PRIORITE
DUREES_S = {  # registre : (instant, séquence), chacun (min, max), recherche 12 v3
    "rire": ((8, 20), (20, 40)), "curiosite": ((10, 25), (25, 45)), "spectacle": ((15, 30), (30, 45)),
    "tension": ((12, 25), (30, 60)), "info": ((15, 30), (30, 60)), "detente": ((8, 20), (20, 30)),
    "maitrise": ((12, 25), (30, 60)),
}
CHAMPS_MOMENT = ["n", "debut_s", "fin_s", "registre", "format", "score", "monstre",
                 "arme", "x_centre", "textes", "autre"]

journal = logging.getLogger(__name__)

_TYPES = {"OBJECT": dict, "ARRAY": list, "STRING": str, "NUMBER": (int, float), "INTEGER": int, "BOOLEAN": bool}
_TEXTE = {
    "type": "OBJECT",
    "required": ["texte", "debut_s", "fin_s"],
    "properties": {
        "texte": {"type": "STRING"},
        "debut_s": {"type": "NUMBER"},
        "fin_s": {"type": "NUMBER"},
    },
}
_TEXTES = {"type": "ARRAY", "items": _TEXTE, "maxItems": montage.TEXTES_MAX}
SCHEMA_PASSE_2 = {
    "type": "OBJECT",
    "required": ["trouve", "debut_s", "pic_s", "fin_s", "x_centre", "textes"],
    "properties": {
        "trouve": {"type": "BOOLEAN"},
        "debut_s": {"type": "NUMBER"},
        "pic_s": {"type": "NUMBER"},
        "fin_s": {"type": "NUMBER"},
        "x_centre": {"type": "NUMBER"},
        "textes": _TEXTES,
    },
}


class ErreurAnalyse(RuntimeError):
    """Échec de l'analyse. Message sans secret. cout_usd : ce que cet échec révèle à n8n."""

    cout_usd = 0


class BrutRequis(ErreurAnalyse):
    """Rien à lire : n8n refait /fetch puis /analyse, note gardée. Le message commence toujours par `brut_requis:`."""


def _chemin_compteur():
    return os.path.join(os.environ.get("MEDIA_DIR", "/media"), "compteur-gemini.json")


def _depense_du_mois():
    try:
        with open(_chemin_compteur(), encoding="utf-8") as fichier:
            compteur = json.load(fichier)
    except FileNotFoundError:
        return 0.0
    return compteur["usd"] if compteur["mois"] == date.today().strftime("%Y-%m") else 0.0


class _Facture:
    """Cumule chaque appel facturé, y compris ceux qui finissent en échec, et tient le compteur du mois.

    `duree_s` : durée de la vidéo, d'où sa limite. Sans durée, pas de limite par vidéo.
    `suivi` : son relevé (voir reprise.py), qui porte la dépense d'un essai à l'autre. `usd` ne compte que cet essai.
    """

    def __init__(self, duree_s=None, suivi=None):
        self.usd = 0.0
        self.suivi = suivi
        self.avant_usd = suivi.cout_usd if suivi else 0.0
        self.limite_usd = math.inf if duree_s is None else limite_video(duree_s, date.today())

    def verifier(self):
        """Refuse avant de dépenser. Un appel déjà parti reste compté : il franchit la limite, le suivant s'arrête."""
        engage = self.avant_usd + self.usd
        if engage >= self.limite_usd:
            raise ErreurAnalyse(f"arrêt : {_dollars(engage)} engagés, limite de {_dollars(self.limite_usd)} "
                                "pour cette vidéo")
        plafond = os.environ.get("PLAFOND_GEMINI_MOIS_USD")  # sans lui, le compteur compte sans bloquer
        if plafond and _depense_du_mois() >= float(plafond):
            raise ErreurAnalyse(f"plafond Gemini du mois atteint : {_dollars(_depense_du_mois())} sur "
                                f"{_dollars(float(plafond))}, {_dollars(engage)} engagés sur cette vidéo")

    def noter(self, usage, modele=MODELE):
        cout = _cout(usage, date.today(), modele)
        self.usd += cout
        if self.suivi:
            self.suivi.payer(cout)
        taches.ecrire_json(_chemin_compteur(), {"mois": date.today().strftime("%Y-%m"),
                                                "usd": _depense_du_mois() + cout})

    def revele(self):
        """Ce qu'un échec annonce : sans suivi, cet essai ; avec, tout ce qu'aucune tâche n'a encore annoncé."""
        return self.suivi.annoncer() if self.suivi else round(self.usd, 6)


@contextmanager
def facturation(facture):
    """Toute erreur sort en ErreurAnalyse, sans secret, avec le coût déjà engagé."""
    try:
        yield
    except Exception as erreur:
        echec = erreur if isinstance(erreur, ErreurAnalyse) else ErreurAnalyse(_sans_secret(erreur))
        echec.cout_usd = facture.revele()
        raise echec from None


def _dollars(montant):
    return f"{montant:.2f} $".replace(".", ",")


def _sans_secret(erreur):
    return f"{type(erreur).__name__}: {taches.sans_secret(str(erreur))}"


def _consignes():
    texte = CONSIGNE.read_text(encoding="utf-8")
    _, passe_1, passe_2 = re.split(r"^# Passe \d\n", texte, flags=re.M)
    return passe_1.strip(), passe_2.strip()


def client():
    cle = os.environ.get("GEMINI_API_KEY")
    if not cle:
        raise ErreurAnalyse("GEMINI_API_KEY absente")
    return genai.Client(api_key=cle, http_options=types.HttpOptions(retry_options=RELANCES_RESEAU))


def attendre_traitement(client_gemini, fichier):
    limite = time.monotonic() + ATTENTE_FICHIER_S
    while fichier.state is None or fichier.state.name != "ACTIVE":
        if fichier.state is not None and fichier.state.name == "FAILED":
            raise ErreurAnalyse("Gemini n'a pas pu traiter la vidéo")
        if time.monotonic() > limite:
            raise ErreurAnalyse("Gemini n'a pas fini de traiter la vidéo à temps")
        time.sleep(PAUSE_FICHIER_S)
        fichier = client_gemini.files.get(name=fichier.name)
    return fichier


def _fichier_vivant(client_gemini, chemin_memoire):
    """Le fichier déjà envoyé, s'il n'est pas périmé et que Gemini l'a encore ; sinon None.

    Une panne passagère sort en erreur : elle ne dit rien du fichier, et ne doit pas demander de /fetch.
    """
    try:
        with open(chemin_memoire, encoding="utf-8") as fichier:
            memoire = json.load(fichier)
    except FileNotFoundError:
        return None
    if datetime.now(timezone.utc) - datetime.fromisoformat(memoire["date"]) >= VIE_FICHIER:
        return None
    try:
        distant = client_gemini.files.get(name=memoire["nom"])
    except errors.APIError as erreur:
        if erreur.code not in FICHIER_PERDU:
            raise
        journal.warning("fichier Gemini perdu : %s", _sans_secret(erreur))
        return None
    return distant if distant.state is not None and distant.state.name == "ACTIVE" else None


def fichier_disponible(client_gemini, brut, chemin_memoire):
    """Le fichier distant s'il vit encore, None si le brut peut le remplacer. Sans l'un ni l'autre : BrutRequis."""
    distant = _fichier_vivant(client_gemini, chemin_memoire)
    if distant is None and not os.path.isfile(brut):
        raise BrutRequis("brut_requis: fichier Gemini perdu ou périmé et brut absent, /fetch (0,13 $) puis /analyse")
    return distant


def fichier_gemini(client_gemini, brut, chemin_memoire):
    """Réutilise le fichier distant, sinon renvoie le brut. Rien n'est effacé : la 2e passe en a besoin."""
    distant = fichier_disponible(client_gemini, brut, chemin_memoire)
    if distant is not None:
        return distant
    debut = datetime.now(timezone.utc)  # l'horloge de Gemini part de l'envoi, pas de la fin du traitement
    envoi = client_gemini.files.upload(file=brut, config={"mime_type": "video/mp4"})
    distant = attendre_traitement(client_gemini, envoi)
    taches.ecrire_json(chemin_memoire, {"nom": distant.name, "date": debut.isoformat()})
    return distant


def _de_la_forme(valeur, schema):
    """Vrai si la valeur suit le schéma envoyé à Gemini (hors maxItems : un texte de trop n'empêche rien)."""
    if valeur is None:
        return schema.get("nullable", False)
    genre = schema["type"]
    if not isinstance(valeur, _TYPES[genre]) or (isinstance(valeur, bool) and genre != "BOOLEAN"):
        return False
    if isinstance(valeur, float) and not math.isfinite(valeur):
        return False
    if genre == "OBJECT":
        return all(c in valeur for c in schema["required"]) and all(
            _de_la_forme(valeur[c], sous) for c, sous in schema["properties"].items() if c in valeur)
    if genre == "ARRAY":
        return all(_de_la_forme(v, schema["items"]) for v in valeur)
    return valeur in schema.get("enum", [valeur])


def _lire(reponse):
    try:
        return json.loads(reponse.text)
    except (TypeError, ValueError):
        return None


def generer(client_gemini, modele, contenu, schema, facture, resolution=None, reflexion=None):
    """Rend le JSON de la forme attendue. Les réponses mal formées sont facturées, donc notées aussi."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        media_resolution=resolution,
        thinking_config=types.ThinkingConfig(thinking_level=reflexion) if reflexion else None,
        http_options=types.HttpOptions(timeout=DELAI_APPEL_MS),
    )
    for _ in range(1 + RELANCES_JSON):
        facture.verifier()
        reponse = client_gemini.models.generate_content(model=modele, contents=contenu, config=config)
        if reponse.usage_metadata:
            facture.noter(reponse.usage_metadata, modele)
        lu = _lire(reponse)
        if _de_la_forme(lu, schema):
            return lu
        journal.warning("réponse Gemini mal formée, relance")
    raise ErreurAnalyse(f"JSON Gemini mal formé après {RELANCES_JSON} relances")


def partie_video(fichier, **metadonnees):
    return types.Part(
        file_data=types.FileData(file_uri=fichier.uri, mime_type=fichier.mime_type),
        video_metadata=types.VideoMetadata(**metadonnees),
    )


def _appeler(client_gemini, fichier, consigne, schema, metadonnees, resolution, facture):
    contenu = [partie_video(fichier, **metadonnees), types.Part(text=consigne)]
    return generer(client_gemini, MODELE, contenu, schema, facture, resolution)


def secondes(temps):
    """Convertit "MM:SS", "H:MM:SS" ou "MM:SS.s" en secondes ; -1 si Gemini a rendu autre chose.

    Au-delà d'une heure, nos consignes écrivent "61:40" ; Gemini peut répondre "1:01:40".
    """
    trouve = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,3}):([0-5]\d(?:\.\d+)?)", temps.strip())
    if not trouve or (trouve[1] and int(trouve[2]) > 59):
        return -1.0
    total = int(trouve[1] or 0) * 3600 + int(trouve[2]) * 60 + float(trouve[3])
    return int(total) if total == int(total) else total


def _dans_la_video(moment, duree):
    """Rend le moment, sa fin bornée à la durée si elle ne la dépasse que d'un arrondi ; sinon None."""
    if moment["fin_s"] > duree + TOLERANCE_ARRONDI_S:
        return None
    borne = {**moment, "fin_s": min(moment["fin_s"], duree)}
    return borne if 0 <= borne["debut_s"] < borne["fin_s"] else None


def _temps_absolus(reponse, debut, fin):
    """Rend (debut, pic, fin) en secondes de vidéo.

    Malgré `start_offset`, Gemini compte depuis le début de la vidéo (mesuré le 30/10/2026).
    Une réponse comptée depuis l'extrait reste acceptée ; toute autre est refusée.
    """
    temps = [reponse["debut_s"], reponse["pic_s"], reponse["fin_s"]]
    if all(debut <= t <= fin + TOLERANCE_ARRONDI_S for t in temps):
        return tuple(temps)
    if all(0 <= t <= fin - debut + TOLERANCE_ARRONDI_S for t in temps):
        return tuple(debut + t for t in temps)
    return None


def _selon_le_registre(moment):
    """Rend le moment à la durée de son registre, ou None si le raccourcir perdrait le pic.

    La fin ne bouge pas : on rallonge ou on raccourcit par le début. Le format suit la durée réelle.
    """
    instant, sequence = DUREES_S[moment["registre"]]
    fin = moment["fin_s"]
    debut = min(moment["debut_s"], max(0.0, fin - instant[0]))
    format_reel, duree_max = ("sequence", sequence[1]) if fin - debut >= sequence[0] else ("instant", instant[1])
    debut = round(max(debut, fin - duree_max), 1)
    return {**moment, "debut_s": debut, "format": format_reel} if debut <= moment["pic_s"] else None


def _textes_du_clip(textes, clip_rendu, debut, fin):
    """Textes en secondes depuis `debut`, seulement ceux qui tombent dans le clip final.

    Gemini les compte depuis son clip `clip_rendu` (debut, fin) ou, parfois, depuis le début de la vidéo.
    Un plan écrit est donc toujours montable (voir montage.horaires).
    """
    longueur_rendue = clip_rendu[1] - clip_rendu[0]
    gardes = []
    for t in sorted(textes, key=lambda t: t["debut_s"]):
        origine = clip_rendu[0] if t["debut_s"] < longueur_rendue else 0
        depart = round(t["debut_s"] + origine - debut, 1)
        arrivee = min(round(t["fin_s"] + origine - debut, 1), fin - debut)
        if 0 <= depart < arrivee and texte.nettoyer(t["texte"]):
            gardes.append({"texte": t["texte"], "debut_s": depart, "fin_s": arrivee})
    return gardes[:montage.TEXTES_MAX]


def _passe_2(client, fichier, consigne, moment, duree, facture):
    """Cale un moment sur ±MARGE_S. Rend le moment précisé, ou None s'il n'est pas utilisable."""
    debut = max(0.0, moment["debut_s"] - MARGE_S)
    fin = min(duree, moment["fin_s"] + MARGE_S)
    metadonnees = {"fps": FPS_PASSE_2, "start_offset": f"{debut:.1f}s", "end_offset": f"{fin:.1f}s"}
    resume = json.dumps({k: moment.get(k) for k in ("registre", "format", "preuve", "textes")}, ensure_ascii=False)
    demande = consigne.replace("{moment}", resume).replace("{debut}", f"{debut:.1f}").replace("{fin}", f"{fin:.1f}")
    reponse = _appeler(client, fichier, demande, SCHEMA_PASSE_2, metadonnees,
                       types.MediaResolution.MEDIA_RESOLUTION_HIGH, facture)
    temps = _temps_absolus(reponse, debut, fin) if reponse["trouve"] else None
    if temps is None:
        return None
    cale = _dans_la_video({
        **moment,
        "debut_s": round(temps[0], 1),
        "pic_s": round(temps[1], 1),
        "fin_s": round(temps[2], 1),
        "x_centre": round(min(1.0, max(0.0, reponse["x_centre"])), 2),
    }, duree)
    final = _selon_le_registre(cale) if cale else None
    if final is None:
        return None
    # Rallongé après le calage sur le registre, pour ne pas manger le début du clip.
    fin_clip = max(final["fin_s"], min(round(final["pic_s"] + APRES_PIC_MIN_S, 1), duree))
    clip_rendu = (cale["debut_s"], cale["fin_s"])
    return {**final, "fin_s": fin_clip,
            "textes": _textes_du_clip(reponse["textes"], clip_rendu, final["debut_s"], fin_clip)}


def _preciser(client, fichier, consigne, candidats, duree, facture):
    """Passe 2 sur chaque candidat. Rend (précis, écartés)."""
    precis, ecartes = [], []
    for moment in candidats:
        resultat = _passe_2(client, fichier, consigne, moment, duree, facture)
        if resultat is None:
            ecartes.append({**moment, "raison": "passe 2"})
        else:
            precis.append(resultat)
    return precis, ecartes


def _conforme(moment):
    """Réduit un moment gardé aux champs du contrat."""
    return {champ: moment.get(champ) for champ in CHAMPS_MOMENT}
