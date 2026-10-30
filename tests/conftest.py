import importlib
import subprocess
import time

import pytest
import requests

ID_VIDEO = "mcKcHx3qbFQ"
LIEN_APIFY = "https://apify.dziura.online/te/x.mp4?sig=SECRET"
ELEMENT_FACTURE = "apify-default-dataset-item"
JETON_APIFY = "apify_api_jeton_de_test"


def video(chemin, *options):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=4", *options, str(chemin)],
        check=True,
    )


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    dossier = tmp_path_factory.mktemp("media")
    video(dossier / "clip.mp4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest")
    video(dossier / "muet.mp4")
    return dossier


@pytest.fixture
def module(media, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(media))
    monkeypatch.setenv("API_TOKEN", "t")
    import app
    return importlib.reload(app)


@pytest.fixture
def client(module):
    return module.app.test_client()


@pytest.fixture
def auth():
    return {"Authorization": "Bearer t"}


def brut(chemin, taille="1920x1072"):
    """Fausse chasse : moitié gauche rouge, moitié droite bleue, avec du son."""
    largeur, hauteur = (int(x) for x in taille.split("x"))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=red:s={largeur // 2}x{hauteur}:r=60:d=12",
         "-f", "lavfi", "-i", f"color=blue:s={largeur // 2}x{hauteur}:r=60:d=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-filter_complex", "[0:v][1:v]hstack,format=yuv420p[v]", "-map", "[v]", "-map", "2:a",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(chemin)],
        check=True,
    )


@pytest.fixture(scope="session")
def brut_modele(tmp_path_factory):
    chemin = tmp_path_factory.mktemp("modele") / "brut.mp4"
    brut(chemin)
    return chemin


@pytest.fixture
def atelier(tmp_path, monkeypatch):
    """Service complet sur un /media neuf, jeton défini."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.setenv("API_TOKEN", "t")
    import app
    return importlib.reload(app)


@pytest.fixture
def attendre(atelier, auth):
    """Sonde une tâche jusqu'à sa fin et rend son état."""
    def sonder(id_tache):
        client = atelier.app.test_client()
        for _ in range(600):
            etat = client.get(f"/taches/{id_tache}", headers=auth).get_json()
            if etat["etat"] in ("reussie", "echouee"):
                return etat
            time.sleep(0.05)
        raise AssertionError("tâche sans fin")
    return sonder


class Reponse:
    def __init__(self, json_=None, contenu=b"", code=200, entetes=None):
        self.json_, self.contenu, self.status_code = json_, contenu, code
        self.headers = entetes if entetes is not None else {"Content-Length": str(len(contenu))}

    def json(self):
        return self.json_

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden for url: {LIEN_APIFY}", response=self)

    def iter_content(self, chunk_size):
        yield self.contenu

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FauxApify:
    """Simule l'API Apify et le lien de téléchargement ; garde la trace des appels."""

    def __init__(self, contenu, statut_run="SUCCEEDED", items=None):
        self.contenu, self.statut_run = contenu, statut_run
        self.items = items
        self.appels, self.telechargements = [], []
        self.sondes = 0
        self.pannes_dataset = 0
        self.lectures_dataset = 0
        self.runs = 0
        self.factures_en_retard = 0  # sondes où le coût affiché n'a pas encore l'élément livré

    def request(self, methode, url, headers=None, json=None, timeout=None, params=None):
        assert timeout
        self.appels.append((methode, url, headers, json))
        if methode == "POST":
            self.runs += 1
            return Reponse({"data": {"id": f"run{self.runs}"}})
        if "/actor-runs/" in url:
            self.sondes += 1
            statut = "RUNNING" if self.sondes == 1 else self.statut_run
            run = {"id": "run1", "status": statut, "usageTotalUsd": 0.0712, "defaultDatasetId": "ds1"}
            if self.factures_en_retard:
                self.factures_en_retard -= 1
                run.update(usageTotalUsd=0.05, chargedEventCounts={ELEMENT_FACTURE: 0})
            return Reponse({"data": run})
        self.lectures_dataset += 1
        if self.pannes_dataset:
            self.pannes_dataset -= 1
            raise requests.ConnectionError("dataset injoignable")
        items = self.items if self.items is not None else [{
            "videoId": ID_VIDEO, "downloadUrl": LIEN_APIFY, "status": "completed", "fileSizeMB": "59.23"}]
        return Reponse(items)

    def get(self, url, headers=None, stream=None, timeout=None):
        assert stream and timeout
        self.telechargements.append((url, headers))
        return Reponse(contenu=self.contenu)


@pytest.fixture
def bruts(tmp_path):
    dossier = tmp_path / "bruts"
    dossier.mkdir()
    return dossier


@pytest.fixture
def apify(monkeypatch, brut_modele):
    import fetch
    monkeypatch.setenv("APIFY_TOKEN", JETON_APIFY)
    monkeypatch.setattr(fetch, "ATTENTE_S", 0)
    faux = FauxApify(brut_modele.read_bytes())
    monkeypatch.setattr(fetch.requests, "request", faux.request)
    monkeypatch.setattr(fetch.requests, "get", faux.get)
    return faux
