"""Analyse d'une chasse par Gemini : rend le plan JSON de contrat/plan-exemple.json."""

import json
import logging
import math
import os
import re
import time
from datetime import date
from pathlib import Path

from google import genai
from google.genai import types

import montage
import taches
import texte
import tri

MODELE = "gemini-3.8-flash"
CONSIGNE = Path(__file__).with_name("consigne.md")
MARGE_S = 20
FPS_PASSE_1 = 1
FPS_PASSE_2 = 5
RELANCES_JSON = 2
TOLERANCE_ARRONDI_S = 1  # MM:SS arrondit la fin de vidéo à la seconde pleine
ATTENTE_FICHIER_S = 900
PAUSE_FICHIER_S = 5
DELAI_APPEL_MS = 600_000

FIN_TARIF_REDUIT = date(2026, 12, 31)
TARIF_REDUIT = (0.75, 3.75)  # $ par million de jetons : entrée, sortie
TARIF_NORMAL = (1.50, 7.50)

REGISTRES = ["maitrise", "rire", "tension", "spectacle", "detente", "info"]
DUREES_S = {  # registre : (instant, séquence), chacun (min, max), recherche 12 v2
    "maitrise": ((8, 15), (30, 60)), "rire": ((6, 12), (15, 30)), "tension": ((10, 20), (30, 90)),
    "spectacle": ((8, 15), (20, 40)), "detente": ((6, 15), (15, 30)), "info": ((15, 30), (30, 60)),
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
_MOMENT = {
    "type": "OBJECT",
    "required": ["debut", "pic", "fin", "registre", "format", "score", "textes", "preuve"],
    "properties": {
        "debut": {"type": "STRING"},
        "pic": {"type": "STRING"},
        "fin": {"type": "STRING"},
        "registre": {"type": "STRING", "enum": REGISTRES},
        "format": {"type": "STRING", "enum": ["instant", "sequence"]},
        "score": {"type": "INTEGER"},
        "monstre": {"type": "STRING", "nullable": True},
        "arme": {"type": "STRING", "nullable": True},
        "textes": _TEXTES,
        "preuve": {"type": "STRING"},
        "autre": {"type": "STRING", "nullable": True},
    },
}
SCHEMA_PASSE_1 = {
    "type": "OBJECT",
    "required": ["titre_propose", "description_proposee", "moments"],
    "properties": {
        "titre_propose": {"type": "STRING"},
        "description_proposee": {"type": "STRING"},
        "moments": {"type": "ARRAY", "items": _MOMENT},
    },
}
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
    """Échec de l'analyse. Message sans secret. cout_usd : tout ce qui a été facturé avant l'échec."""

    cout_usd = 0


class _Facture:
    """Cumule chaque appel facturé, y compris ceux qui finissent en échec."""

    def __init__(self):
        self.usd = 0.0

    def noter(self, usage):
        self.usd += _cout(usage, date.today())


def _sans_secret(erreur):
    return f"{type(erreur).__name__}: {taches.sans_secret(str(erreur))}"


def _tarif(jour):
    return TARIF_REDUIT if jour <= FIN_TARIF_REDUIT else TARIF_NORMAL


def _cout(usage, jour):
    entree, sortie = _tarif(jour)
    jetons_sortie = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    return ((usage.prompt_token_count or 0) * entree + jetons_sortie * sortie) / 1_000_000


def _consignes():
    texte = CONSIGNE.read_text(encoding="utf-8")
    _, passe_1, passe_2 = re.split(r"^# Passe \d\n", texte, flags=re.M)
    return passe_1.strip(), passe_2.strip()


def _duree(chemin):
    return montage.duree_video(chemin)


def _attendre_traitement(client, fichier):
    limite = time.monotonic() + ATTENTE_FICHIER_S
    while fichier.state is None or fichier.state.name != "ACTIVE":
        if fichier.state is not None and fichier.state.name == "FAILED":
            raise ErreurAnalyse("Gemini n'a pas pu traiter la vidéo")
        if time.monotonic() > limite:
            raise ErreurAnalyse("Gemini n'a pas fini de traiter la vidéo à temps")
        time.sleep(PAUSE_FICHIER_S)
        fichier = client.files.get(name=fichier.name)
    return fichier


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


def _appeler(client, fichier, consigne, schema, metadonnees, resolution, facture):
    """Rend le JSON de la forme attendue. Les réponses mal formées sont facturées, donc notées aussi."""
    video = types.Part(
        file_data=types.FileData(file_uri=fichier.uri, mime_type=fichier.mime_type),
        video_metadata=types.VideoMetadata(**metadonnees),
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        media_resolution=resolution,
        http_options=types.HttpOptions(timeout=DELAI_APPEL_MS),
    )
    for _ in range(1 + RELANCES_JSON):
        reponse = client.models.generate_content(
            model=MODELE, contents=[video, types.Part(text=consigne)], config=config,
        )
        if reponse.usage_metadata:
            facture.noter(reponse.usage_metadata)
        lu = _lire(reponse)
        if _de_la_forme(lu, schema):
            return lu
        journal.warning("réponse Gemini mal formée, relance")
    raise ErreurAnalyse(f"JSON Gemini mal formé après {RELANCES_JSON} relances")


def _secondes(temps):
    """Convertit "MM:SS" en secondes ; -1 si Gemini a rendu autre chose."""
    trouve = re.fullmatch(r"(\d{1,3}):([0-5]\d)", temps.strip())
    return int(trouve[1]) * 60 + int(trouve[2]) if trouve else -1.0


def _en_secondes(moment):
    """En passe 1, Gemini confond parfois 04:05 et 405 s s'il écrit des secondes : il écrit MM:SS, on convertit."""
    return {**moment, "debut_s": _secondes(moment["debut"]), "pic_s": _secondes(moment["pic"]),
            "fin_s": _secondes(moment["fin"])}


def _dans_la_video(moment, duree):
    """Rend le moment, sa fin bornée à la durée si elle ne la dépasse que d'un arrondi ; sinon None."""
    if moment["fin_s"] > duree + TOLERANCE_ARRONDI_S:
        return None
    borne = {**moment, "fin_s": min(moment["fin_s"], duree)}
    return borne if 0 <= borne["debut_s"] < borne["fin_s"] else None


def _passe_1(client, fichier, consigne, facture):
    return _appeler(client, fichier, consigne, SCHEMA_PASSE_1, {"fps": FPS_PASSE_1},
                    types.MediaResolution.MEDIA_RESOLUTION_LOW, facture)


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

    Le pic est à la fin : on rallonge ou on raccourcit par le début. Le format suit la durée réelle.
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
    clip_rendu = (cale["debut_s"], cale["fin_s"])
    return {**final, "textes": _textes_du_clip(reponse["textes"], clip_rendu, final["debut_s"], final["fin_s"])}


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


def _analyser(client, fichier, video_id, duree, facture):
    consigne_1, consigne_2 = _consignes()
    brut = _passe_1(client, fichier, consigne_1, facture)
    moments, hors_video = [], []
    for moment in map(_en_secondes, brut["moments"]):
        cale = _dans_la_video(moment, duree)
        if cale:
            moments.append(cale)
        else:
            hors_video.append({**moment, "raison": "hors vidéo"})
    candidats, rejetes = tri.trier(moments)
    precis, ecartes = _preciser(client, fichier, consigne_2, candidats, duree, facture)
    gardes, rejetes_2 = tri.trier(precis)
    return {
        "video_id": video_id,
        "modele": MODELE,
        "titre_propose": brut["titre_propose"],
        "description_proposee": brut["description_proposee"],
        "cout_usd": round(facture.usd, 6),
        "moments": [_conforme(m) for m in gardes],
        "rejetes": hors_video + rejetes + ecartes + rejetes_2,
    }


def analyser(chemin_video: str, video_id: str) -> dict:
    """Analyse une vidéo locale et rend son plan. Efface toujours le fichier distant."""
    cle = os.environ.get("GEMINI_API_KEY")
    if not cle:
        raise ErreurAnalyse("GEMINI_API_KEY absente")
    client = genai.Client(api_key=cle)
    fichier = None
    facture = _Facture()
    try:
        duree = _duree(chemin_video)
        fichier = client.files.upload(file=chemin_video, config={"mime_type": "video/mp4"})
        fichier = _attendre_traitement(client, fichier)
        return _analyser(client, fichier, video_id, duree, facture)
    except Exception as erreur:
        echec = erreur if isinstance(erreur, ErreurAnalyse) else ErreurAnalyse(_sans_secret(erreur))
        echec.cout_usd = round(facture.usd, 6)
        raise echec from None
    finally:
        if fichier is not None:
            try:
                client.files.delete(name=fichier.name)
            except Exception as erreur:
                journal.warning("fichier distant non effacé : %s", _sans_secret(erreur))
