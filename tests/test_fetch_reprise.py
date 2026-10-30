"""Une panne après un run payé ne doit jamais faire repayer ni effacer ce qui est bon."""
import json
import shutil
import subprocess

import pytest
import requests

import fetch
from conftest import ID_VIDEO as ID
from conftest import brut


def fiche(bruts):
    return bruts / f"{ID}.run.json"


def vieillir(bruts, minutes):
    suivi = json.loads(fiche(bruts).read_text())
    suivi["cree_le"] -= minutes * 60
    fiche(bruts).write_text(json.dumps(suivi))


def coupure(*a, **k):
    raise requests.ConnectionError("coupure")


def echec_paye(bruts):
    with pytest.raises(fetch.ErreurFetch) as e:
        fetch.recuperer(ID, str(bruts))
    return e.value


def test_panne_passagere_du_dataset_rattrapee(apify, bruts):
    apify.pannes_dataset = fetch.PANNES_TOLEREES
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.0712
    assert apify.runs == 1


def test_panne_durable_du_dataset_bornee_puis_rejeu_sans_repayer(apify, bruts):
    apify.pannes_dataset = 99
    assert echec_paye(bruts).cout_usd == 0.0712
    assert apify.lectures_dataset == fetch.PANNES_TOLEREES + 1
    assert fiche(bruts).is_file()
    apify.pannes_dataset = 0
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0
    assert apify.runs == 1
    assert not fiche(bruts).exists()


def test_telechargement_coupe_puis_rejeu_sans_repayer(apify, bruts, monkeypatch):
    vrai = fetch.requests.get
    monkeypatch.setattr(fetch.requests, "get", coupure)
    assert echec_paye(bruts).cout_usd == 0.0712
    monkeypatch.setattr(fetch.requests, "get", vrai)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0
    assert apify.runs == 1
    assert not fiche(bruts).exists()


def test_fiche_ecrite_avec_son_instant(apify, bruts, monkeypatch):
    monkeypatch.setattr(fetch.requests, "get", coupure)
    avant = fetch.time.time()
    echec_paye(bruts)
    suivi = json.loads(fiche(bruts).read_text())
    assert suivi["id_run"] == "run1"
    assert avant <= suivi["cree_le"] <= fetch.time.time()


@pytest.mark.parametrize("minutes,runs,total", [(11.9, 1, 0.0712), (12, 2, 0.1424)])
def test_au_dela_de_12_min_nouveau_run_et_cumul_juste(apify, bruts, monkeypatch, minutes, runs, total):
    vrai = fetch.requests.get
    monkeypatch.setattr(fetch.requests, "get", coupure)
    payes = [echec_paye(bruts).cout_usd]
    vieillir(bruts, minutes)
    monkeypatch.setattr(fetch.requests, "get", vrai)
    payes.append(fetch.recuperer(ID, str(bruts))["cout_usd"])
    assert apify.runs == runs
    assert round(sum(payes), 4) == total


def test_service_coupe_pendant_l_attente_le_run_est_retrouve(apify, bruts, monkeypatch):
    vrai = apify.request

    def injoignable(methode, url, **k):
        if "/actor-runs/" in url:
            raise requests.ConnectionError("x")
        return vrai(methode, url, **k)
    monkeypatch.setattr(fetch.requests, "request", injoignable)
    assert echec_paye(bruts).cout_usd == 0  # prix pas encore connu
    monkeypatch.setattr(fetch.requests, "request", vrai)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.0712
    assert apify.runs == 1


def test_run_expire_de_prix_inconnu_compte_dans_le_cumul(apify, bruts, monkeypatch):
    vrai = apify.request

    def injoignable(methode, url, **k):
        if "/actor-runs/" in url:
            raise requests.ConnectionError("x")
        return vrai(methode, url, **k)
    monkeypatch.setattr(fetch.requests, "request", injoignable)
    echec_paye(bruts)
    vieillir(bruts, 13)
    monkeypatch.setattr(fetch.requests, "request", vrai)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.1424
    assert apify.runs == 2


def test_cout_non_annonce_avant_un_redemarrage_ressort_au_rejeu(apify, bruts):
    fiche(bruts).write_text(json.dumps(
        {"id_run": "run1", "cree_le": fetch.time.time(), "du_usd": 0.0712, "compte": True}))
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.0712
    assert apify.runs == 0


def test_run_en_echec_paye_visible_et_relance_au_rejeu(apify, bruts):
    apify.statut_run = "FAILED"
    assert echec_paye(bruts).cout_usd == 0.0712
    apify.statut_run = "SUCCEEDED"
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.0712
    assert apify.runs == 2


def test_run_sans_lien_paye_visible_et_relance_au_rejeu(apify, bruts):
    apify.items = []
    assert echec_paye(bruts).cout_usd == 0.0712
    apify.items = None
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.0712
    assert apify.runs == 2


def test_fiche_impossible_a_ecrire_ne_perd_pas_le_run(apify, bruts, monkeypatch):
    def plein(*a):
        raise OSError("disque plein")
    monkeypatch.setattr(fetch.taches, "ecrire_json", plein)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0.0712


def test_disque_plein_au_telechargement_cout_visible(apify, bruts, monkeypatch):
    def plein(*a, **k):
        raise OSError("No space left on device")
    monkeypatch.setattr(fetch, "_telecharger", plein)
    assert echec_paye(bruts).cout_usd == 0.0712


PANNES_FFPROBE = [
    subprocess.TimeoutExpired("ffprobe", 60),
    subprocess.CalledProcessError(-9, "ffprobe", stderr=""),
    subprocess.CalledProcessError(1, "ffprobe", stderr="x.mp4: Cannot allocate memory"),
    OSError("ffprobe absent"),
    ValueError("sortie illisible"),
]


@pytest.mark.parametrize("panne", PANNES_FFPROBE, ids=lambda p: type(p).__name__ + str(getattr(p, "returncode", "")))
def test_panne_ffprobe_garde_le_brut_et_ne_repaie_pas(apify, bruts, brut_modele, monkeypatch, panne):
    shutil.copy(brut_modele, bruts / f"{ID}.mp4")
    vrai = fetch.montage.dimensions

    def en_panne(chemin):
        raise panne
    monkeypatch.setattr(fetch.montage, "dimensions", en_panne)
    assert "vérification impossible" in str(echec_paye(bruts))
    assert (bruts / f"{ID}.mp4").is_file()
    assert apify.appels == []
    monkeypatch.setattr(fetch.montage, "dimensions", vrai)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0
    assert apify.appels == []


def test_panne_ffprobe_sur_le_son_garde_aussi_le_brut(apify, bruts, brut_modele, monkeypatch):
    shutil.copy(brut_modele, bruts / f"{ID}.mp4")

    def en_panne(chemin):
        raise subprocess.TimeoutExpired("ffprobe", 60)
    monkeypatch.setattr(fetch.montage, "a_du_son", en_panne)
    echec_paye(bruts)
    assert (bruts / f"{ID}.mp4").is_file()


def test_panne_ffprobe_juste_apres_achat_le_fichier_paye_reste(apify, bruts, monkeypatch):
    vrai = fetch.montage.dimensions

    def en_panne(chemin):
        raise subprocess.TimeoutExpired("ffprobe", 60)
    monkeypatch.setattr(fetch.montage, "dimensions", en_panne)
    assert echec_paye(bruts).cout_usd == 0.0712
    assert (bruts / f"{ID}.mp4").is_file() and not list(bruts.glob("*.part"))
    vieillir(bruts, 60)  # même lien expiré : le fichier suffit
    monkeypatch.setattr(fetch.montage, "dimensions", vrai)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0
    assert apify.runs == 1
    assert not fiche(bruts).exists()


def test_panne_ffprobe_au_bilan_cout_visible_et_rejeu_gratuit(apify, bruts, monkeypatch):
    vrai = fetch.montage.duree_video

    def en_panne(chemin):
        raise subprocess.TimeoutExpired("ffprobe", 60)
    monkeypatch.setattr(fetch.montage, "duree_video", en_panne)
    assert echec_paye(bruts).cout_usd == 0.0712
    assert (bruts / f"{ID}.mp4").is_file()
    monkeypatch.setattr(fetch.montage, "duree_video", vrai)
    assert fetch.recuperer(ID, str(bruts))["cout_usd"] == 0
    assert apify.runs == 1


def test_brut_sans_video_efface(apify, bruts, tmp_path):
    son = tmp_path / "son.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", str(son)], check=True)
    shutil.copy(son, bruts / f"{ID}.mp4")
    fetch.recuperer(ID, str(bruts))
    assert apify.runs == 1


def test_brut_trop_petit_efface(apify, bruts, tmp_path):
    brut(bruts / f"{ID}.mp4", "1280x720")
    assert fetch.recuperer(ID, str(bruts))["hauteur"] == 1072
    assert apify.runs == 1


def test_run_perdu_compte_une_seule_fois_si_la_relance_echoue(apify, bruts, monkeypatch):
    vrai = apify.request

    def injoignable(methode, url, **k):
        if "/actor-runs/" in url:
            raise requests.ConnectionError("x")
        return vrai(methode, url, **k)

    def refus(methode, url, **k):
        if methode == "POST":
            raise requests.ConnectionError("x")
        return vrai(methode, url, **k)
    monkeypatch.setattr(fetch.requests, "request", injoignable)
    payes = [echec_paye(bruts).cout_usd]
    vieillir(bruts, 13)
    monkeypatch.setattr(fetch.requests, "request", refus)
    payes.append(echec_paye(bruts).cout_usd)
    monkeypatch.setattr(fetch.requests, "request", vrai)
    payes.append(fetch.recuperer(ID, str(bruts))["cout_usd"])
    assert apify.runs == 2
    assert round(sum(payes), 4) == 0.1424
