"""Téléchargement d'une chasse YouTube par l'acteur Apify api-ninja.

Apify rend un lien MP4 valable 15 min : on le télécharge aussitôt, en flux, vers un
fichier .part renommé une fois complet. Présent = complet, donc rien à repayer.
Le jeton ne part que vers api.apify.com, jamais vers le lien de téléchargement.

Chaque run payé est noté dans bruts/<id>.run.json dès son lancement. Après une panne,
le rejeu reprend ce run tant que son lien vit, au lieu d'en payer un autre.
Chaque tâche annonce dans cout_usd ce qu'elle seule a révélé, réussie ou échouée :
la somme des tâches d'une vidéo donne ce qu'elle a coûté, sans compter deux fois.
"""
import contextlib
import json
import logging
import os
import subprocess
import time

import requests

import montage
import taches

API = "https://api.apify.com/v2"
ACTEUR = "api-ninja~youtube-video-downloader"
ATTENTE_S = 10
DELAI_RUN_S = 1200
PANNES_TOLEREES = 5  # le run est déjà payé : un trou réseau passager ne doit pas le perdre
VIE_LIEN_S = 12 * 60  # marge sur les 15 min du lien
HAUTEUR_MIN = 1070
FINS_EN_ECHEC = ("FAILED", "ABORTED", "TIMED-OUT")
BLOC = 1024 * 1024
FICHIER_INVALIDE = "Invalid data found"  # seule réponse de ffprobe qui accuse le fichier

journal = logging.getLogger(__name__)


class ErreurFetch(Exception):
    """Échec au message sûr : ni jeton, ni lien signé. cout_usd : payé, annoncé par cet échec."""

    cout_usd = 0


class PanneVerification(ErreurFetch):
    """Vérification impossible : le fichier n'est pas en cause, on le garde."""


def _apify(methode, chemin, jeton, **options):
    reponse = requests.request(methode, API + chemin, headers={"Authorization": f"Bearer {jeton}"},
                               timeout=30, **options)
    reponse.raise_for_status()
    return reponse.json()


def _attendre_run(id_run, jeton):
    """Sonde le run jusqu'à sa fin, réussie ou non, et le rend."""
    pannes = 0
    limite = time.monotonic() + DELAI_RUN_S + 60
    while time.monotonic() < limite:
        try:
            run = _apify("GET", f"/actor-runs/{id_run}", jeton)["data"]
            pannes = 0
        except requests.RequestException as e:
            pannes += 1
            if pannes > PANNES_TOLEREES:
                raise ErreurFetch(f"Apify injoignable pendant l'attente ({type(e).__name__})") from None
            time.sleep(ATTENTE_S)
            continue
        if run["status"] == "SUCCEEDED" or run["status"] in FINS_EN_ECHEC:
            return run
        time.sleep(ATTENTE_S)
    raise ErreurFetch("run Apify trop long")


def _erreur_apify(e):
    if isinstance(e, requests.HTTPError):
        return ErreurFetch(f"Apify a refusé l'appel (HTTP {e.response.status_code})")
    return ErreurFetch(f"Apify injoignable ({type(e).__name__})")


def _lire_items(id_dataset, jeton):
    """Lit le dataset d'un run payé, en résistant à un trou réseau passager."""
    for essai in range(PANNES_TOLEREES + 1):
        try:
            return _apify("GET", f"/datasets/{id_dataset}/items", jeton, params={"clean": "true"})
        except requests.RequestException as e:
            if essai == PANNES_TOLEREES:
                raise _erreur_apify(e) from None
            time.sleep(ATTENTE_S)


def chemin_suivi(dossier, video_id):
    return os.path.join(dossier, f"{video_id}.run.json")


def _lire_suivi(fiche):
    """Dernier run de la vidéo. du_usd : payé, pas encore annoncé. compte : son prix est dans du_usd."""
    vide = {"id_run": None, "cree_le": 0, "du_usd": 0, "compte": True}
    try:
        with open(fiche, encoding="utf-8") as fichier:
            lu = json.load(fichier)
    except (OSError, ValueError):
        return vide
    return {**vide, **lu} if isinstance(lu, dict) else vide


def _ecrire_suivi(fiche, suivi):
    try:
        taches.ecrire_json(fiche, suivi)
    except OSError as e:  # le run est payé : continuer vaut mieux que le perdre
        journal.error("suivi du run non écrit : %s", taches.sans_secret(str(e)))


def _lien_vivant(suivi):
    return bool(suivi["id_run"]) and 0 <= time.time() - suivi["cree_le"] < VIE_LIEN_S


def _compter_run_perdu(suivi, jeton):
    """Run abandonné avant que son prix soit connu : le demander, pour un cumul juste."""
    if suivi["id_run"] and not suivi["compte"]:
        with contextlib.suppress(requests.RequestException):
            run = _apify("GET", f"/actor-runs/{suivi['id_run']}", jeton)["data"]
            suivi.update(du_usd=suivi["du_usd"] + (run.get("usageTotalUsd") or 0), compte=True)


def _lancer_run(video_id, jeton, fiche, suivi):
    corps = {"urls": [f"https://youtu.be/{video_id}"], "format": "1080", "ttl": "none"}
    try:
        run = _apify("POST", f"/acts/{ACTEUR}/runs", jeton, json=corps, params={"timeout": DELAI_RUN_S})["data"]
    except requests.RequestException as e:
        raise _erreur_apify(e) from None
    suivi.update(id_run=run["id"], cree_le=time.time(), compte=False)
    _ecrire_suivi(fiche, suivi)


def _demander_lien(video_id, jeton, fiche, suivi):
    """Rend l'élément du dataset, du run encore vivant s'il y en a un, sinon d'un nouveau."""
    if not _lien_vivant(suivi):
        _compter_run_perdu(suivi, jeton)
        _lancer_run(video_id, jeton, fiche, suivi)
    fin = _attendre_run(suivi["id_run"], jeton)
    if not suivi["compte"]:
        suivi.update(du_usd=suivi["du_usd"] + (fin.get("usageTotalUsd") or 0), compte=True)
        _ecrire_suivi(fiche, suivi)
    if fin["status"] != "SUCCEEDED":
        suivi["id_run"] = None  # rien à relire : le prochain essai relance
        raise ErreurFetch(f"run Apify en échec : {fin['status']}")
    items = _lire_items(fin["defaultDatasetId"], jeton)
    item = next((i for i in items if i.get("downloadUrl") and i.get("status") == "completed"), None)
    if item is None:
        suivi["id_run"] = None
        raise ErreurFetch(f"Apify n'a pas rendu de lien pour {video_id}")
    return item


def _telecharger(lien, cible):
    """Écrit le lien dans cible ; refuse un fichier plus court qu'annoncé."""
    try:
        with requests.get(lien, headers={"Accept-Encoding": "identity"}, stream=True,
                          timeout=(15, 120)) as reponse:
            reponse.raise_for_status()
            attendu = int(reponse.headers.get("Content-Length") or 0)
            with open(cible, "wb") as fichier:
                for bloc in reponse.iter_content(chunk_size=BLOC):
                    fichier.write(bloc)
    except requests.HTTPError as e:
        raise ErreurFetch(f"téléchargement refusé (HTTP {e.response.status_code})") from None
    except requests.RequestException as e:
        raise ErreurFetch(f"téléchargement coupé ({type(e).__name__})") from None
    if attendu and os.path.getsize(cible) != attendu:
        raise ErreurFetch("téléchargement incomplet")


def _defaut(chemin):
    """Ce qui rend le fichier inutilisable, ou None. Lève PanneVerification si on ne peut pas le dire."""
    try:
        _, hauteur = montage.dimensions(chemin)
        if hauteur < HAUTEUR_MIN:
            return f"hauteur {hauteur} trop basse (minimum {HAUTEUR_MIN})"
        if not montage.a_du_son(chemin):
            return "vidéo sans son"
    except IndexError:
        return "pas de piste vidéo"
    except subprocess.CalledProcessError as e:
        if FICHIER_INVALIDE in (e.stderr or ""):
            return "fichier vidéo illisible"
        raise PanneVerification(f"vérification impossible (ffprobe, code {e.returncode})") from None
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
        raise PanneVerification(f"vérification impossible ({type(e).__name__})") from None
    return None


def _bilan(video_id, chemin, cout):
    return {
        "file_path": f"bruts/{video_id}.mp4",
        "taille_mo": round(os.path.getsize(chemin) / 1e6, 1),
        "duree_s": round(montage.duree_video(chemin), 1),
        "hauteur": montage.dimensions(chemin)[1],
        "cout_usd": cout,
    }


def _installer(lien, chemin):
    """Télécharge et vérifie. En cas de panne de vérification, le fichier complet reste, à revérifier."""
    partiel = chemin + ".part"
    try:
        _telecharger(lien, partiel)
        defaut = _defaut(partiel)
        if defaut:
            raise ErreurFetch(defaut)
        os.replace(partiel, chemin)
    except PanneVerification:
        os.replace(partiel, chemin)
        raise
    finally:
        if os.path.exists(partiel):
            os.remove(partiel)


def _obtenir(video_id, chemin, fiche, suivi):
    """Rend la vidéo à chemin, complète et bonne, en payant le moins possible."""
    if os.path.isfile(chemin):
        defaut = _defaut(chemin)
        if defaut is None:
            return
        os.remove(chemin)
    jeton = os.environ.get("APIFY_TOKEN")
    if not jeton:
        raise ErreurFetch("APIFY_TOKEN non défini")
    item = _demander_lien(video_id, jeton, fiche, suivi)
    _installer(item["downloadUrl"], chemin)


def recuperer(video_id, dossier):
    """Met la vidéo dans dossier/<id>.mp4, sans rien repayer si elle y est déjà ou si son run vit encore."""
    os.makedirs(dossier, exist_ok=True)
    chemin = os.path.join(dossier, f"{video_id}.mp4")
    fiche = chemin_suivi(dossier, video_id)
    suivi = _lire_suivi(fiche)
    try:
        _obtenir(video_id, chemin, fiche, suivi)
        bilan = _bilan(video_id, chemin, round(suivi["du_usd"], 6))
    except Exception as e:
        erreur = e if isinstance(e, ErreurFetch) else ErreurFetch(f"panne ({type(e).__name__})")
        erreur.cout_usd = round(suivi["du_usd"], 6)
        suivi["du_usd"] = 0
        if os.path.isfile(fiche) or suivi["id_run"]:
            _ecrire_suivi(fiche, suivi)
        raise erreur from None
    with contextlib.suppress(OSError):
        os.remove(fiche)
    return bilan
