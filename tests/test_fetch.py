import shutil
import subprocess

import pytest
import requests

import fetch
import taches
from conftest import ID_VIDEO as ID
from conftest import JETON_APIFY as JETON
from conftest import LIEN_APIFY as LIEN
from conftest import Reponse, brut


def test_recupere_et_mesure(apify, bruts):
    resultat = fetch.recuperer(ID, str(bruts))
    assert resultat["file_path"] == f"bruts/{ID}.mp4"
    assert resultat["hauteur"] == 1072
    assert abs(resultat["duree_s"] - 12) < 0.2
    assert resultat["cout_usd"] == 0.0712
    assert resultat["taille_mo"] > 0
    assert (bruts / f"{ID}.mp4").is_file()
    assert not list(bruts.glob("*.part"))


def test_appel_apify_conforme(apify, bruts):
    fetch.recuperer(ID, str(bruts))
    methode, url, entetes, corps = apify.appels[0]
    assert methode == "POST"
    assert "/v2/acts/api-ninja~youtube-video-downloader/runs" in url
    assert corps == {"urls": [f"https://youtu.be/{ID}"], "format": "1080", "ttl": "none"}
    assert entetes["Authorization"] == f"Bearer {JETON}"


def test_le_jeton_ne_part_pas_vers_le_lien(apify, bruts):
    fetch.recuperer(ID, str(bruts))
    url, entetes = apify.telechargements[0]
    assert url == LIEN
    assert "Authorization" not in (entetes or {})


def test_deja_present_pas_de_nouvel_appel(apify, bruts, brut_modele):
    shutil.copy(brut_modele, bruts / f"{ID}.mp4")
    resultat = fetch.recuperer(ID, str(bruts))
    assert apify.appels == []
    assert resultat["cout_usd"] == 0


def test_brut_abime_retelecharge(apify, bruts):
    (bruts / f"{ID}.mp4").write_bytes(b"pas une video")
    fetch.recuperer(ID, str(bruts))
    assert len(apify.appels) >= 1


def test_run_en_echec(apify, bruts):
    apify.statut_run = "FAILED"
    with pytest.raises(fetch.ErreurFetch, match="FAILED"):
        fetch.recuperer(ID, str(bruts))


def test_item_sans_lien(apify, bruts):
    apify.items = [{"videoId": ID, "status": "failed"}]
    with pytest.raises(fetch.ErreurFetch):
        fetch.recuperer(ID, str(bruts))
    apify.items = []
    with pytest.raises(fetch.ErreurFetch):
        fetch.recuperer(ID, str(bruts))


def test_sans_cle(apify, bruts, monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN")
    with pytest.raises(fetch.ErreurFetch, match="APIFY_TOKEN"):
        fetch.recuperer(ID, str(bruts))


def test_hauteur_insuffisante_refusee(apify, bruts, tmp_path):
    petit = tmp_path / "petit.mp4"
    brut(petit, "1280x720")
    apify.contenu = petit.read_bytes()
    with pytest.raises(fetch.ErreurFetch, match="720"):
        fetch.recuperer(ID, str(bruts))
    assert list(bruts.glob("*.mp4*")) == []


def test_sans_son_refuse(apify, bruts, tmp_path):
    muet = tmp_path / "muet.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=red:s=1920x1080:r=30:d=1", str(muet)], check=True)
    apify.contenu = muet.read_bytes()
    with pytest.raises(fetch.ErreurFetch, match="son"):
        fetch.recuperer(ID, str(bruts))
    assert list(bruts.glob("*.mp4*")) == []


def test_telechargement_tronque_refuse(apify, bruts, monkeypatch):
    def tronque(url, headers=None, stream=None, timeout=None):
        return Reponse(contenu=apify.contenu[:1000], entetes={"Content-Length": str(len(apify.contenu))})
    monkeypatch.setattr(fetch.requests, "get", tronque)
    with pytest.raises(fetch.ErreurFetch, match="incomplet"):
        fetch.recuperer(ID, str(bruts))
    assert list(bruts.glob("*.mp4*")) == []


def test_lien_refuse_sans_fuite(apify, bruts, monkeypatch):
    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: Reponse(code=403))
    with pytest.raises(fetch.ErreurFetch) as e:
        fetch.recuperer(ID, str(bruts))
    assert "SECRET" not in str(e.value)
    assert "403" in str(e.value)


def test_coupure_reseau_sans_fuite(apify, bruts, monkeypatch):
    def coupe(*a, **k):
        raise requests.ConnectionError(
            f"HTTPSConnectionPool(host='apify.dziura.online', port=443): Max retries exceeded with url: {LIEN}")
    monkeypatch.setattr(fetch.requests, "get", coupe)
    with pytest.raises(fetch.ErreurFetch) as e:
        fetch.recuperer(ID, str(bruts))
    assert "SECRET" not in str(e.value) and "dziura" not in str(e.value)


def test_apify_passe_un_coup_de_mou_pendant_l_attente(apify, bruts, monkeypatch):
    vrai = apify.request
    pannes = [requests.ConnectionError("x")]

    def fragile(methode, url, **k):
        if "/actor-runs/" in url and pannes:
            raise pannes.pop()
        return vrai(methode, url, **k)
    monkeypatch.setattr(fetch.requests, "request", fragile)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.0712


def test_erreur_de_tache_ne_contient_pas_le_lien(apify, bruts, atelier, monkeypatch):
    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: Reponse(code=403))
    id_tache = taches.lancer(lambda: fetch.recuperer(ID, str(bruts)))
    for _ in range(200):
        etat = taches.lire(id_tache)
        if etat["etat"] == "echouee":
            break
        taches.threading.Event().wait(0.01)
    assert etat["etat"] == "echouee"
    assert "SECRET" not in etat["erreur"] and JETON not in etat["erreur"]
