import json
import shutil
import sys
import threading
import types

import pytest

import fetch
import taches

ID = "_1PH44fjxNg"


def plan(moments=None):
    return {"video_id": ID, "cout_usd": 0.18, "rejetes": [], "moments": moments if moments is not None else [
        {"n": 3, "debut_s": 2.0, "fin_s": 5.0, "x_centre": 0.5, "textes": []}]}


def poser_brut(media, modele):
    (media / "bruts").mkdir(exist_ok=True)
    shutil.copy(modele, media / "bruts" / f"{ID}.mp4")


def poser_plan(media, contenu=None):
    (media / "plans").mkdir(exist_ok=True)
    (media / "plans" / f"{ID}.json").write_text(json.dumps(contenu or plan()))


@pytest.fixture
def api(atelier, auth):
    client = atelier.app.test_client()

    def post(route, corps):
        return client.post(route, json=corps, headers=auth)
    return post


@pytest.fixture
def ia(monkeypatch):
    """Faux analyse.py et tri.py : compte les appels."""
    appels = []

    def analyser(chemin, video_id):
        appels.append((chemin, video_id))
        return {**plan([{"n": 1}, {"n": 2}]), "rejetes": [{"n": 9}]}

    monkeypatch.setitem(sys.modules, "analyse", types.SimpleNamespace(analyser=analyser))
    monkeypatch.setitem(sys.modules, "tri", types.SimpleNamespace(trier=lambda m: (m[:1], m[1:])))
    return appels


def test_sante(atelier, auth):
    reponse = atelier.app.test_client().get("/sante", headers=auth).get_json()
    assert reponse["disque_libre_go"] > 0
    assert reponse["tache_en_cours"] is None


def test_tache_inconnue(atelier, auth):
    assert atelier.app.test_client().get("/taches/inconnue", headers=auth).status_code == 404


@pytest.mark.parametrize("mauvais", ["court", "a" * 12, "a" * 10 + "/", "../../etc/p", "a" * 11 + "\n", 12, None])
@pytest.mark.parametrize("route", ["/fetch", "/analyse", "/monter", "/nettoyer"])
def test_video_id_refuse(api, route, mauvais):
    assert api(route, {"video_id": mauvais, "moment": 1}).status_code == 400


def test_corps_absent_refuse(atelier, auth):
    assert atelier.app.test_client().post("/fetch", headers=auth).status_code == 400


@pytest.mark.parametrize("moment", [None, "1", True, 1.5])
def test_monter_refuse_un_moment_invalide(api, moment):
    assert api("/monter", {"video_id": ID, "moment": moment}).status_code == 400


def test_fetch_lance_une_tache(api, attendre, monkeypatch, atelier):
    monkeypatch.setattr(fetch, "recuperer", lambda video_id, dossier: {"file_path": f"bruts/{video_id}.mp4"})
    reponse = api("/fetch", {"video_id": ID})
    assert reponse.status_code == 202
    assert attendre(reponse.get_json()["tache"])["resultat"] == {"file_path": f"bruts/{ID}.mp4"}


def test_analyse_ecrit_le_plan_trie(api, attendre, ia, tmp_path, brut_modele):
    poser_brut(tmp_path, brut_modele)
    etat = attendre(api("/analyse", {"video_id": ID}).get_json()["tache"])
    assert etat["etat"] == "reussie"
    assert etat["resultat"]["moments"] == [{"n": 1}]
    assert etat["resultat"]["rejetes"] == [{"n": 9}, {"n": 2}]
    assert etat["resultat"]["cout_usd"] == 0.18
    assert json.loads((tmp_path / "plans" / f"{ID}.json").read_text()) == etat["resultat"]


def test_analyse_ne_repaie_pas(api, attendre, ia, tmp_path):
    poser_plan(tmp_path)
    etat = attendre(api("/analyse", {"video_id": ID}).get_json()["tache"])
    assert etat["resultat"]["moments"][0]["n"] == 3
    assert ia == []


def test_analyse_sans_brut_echoue(api, attendre, ia):
    assert attendre(api("/analyse", {"video_id": ID}).get_json()["tache"])["etat"] == "echouee"
    assert ia == []


def test_monter_cherche_le_moment_par_son_numero(api, attendre, tmp_path, brut_modele):
    poser_brut(tmp_path, brut_modele)
    poser_plan(tmp_path)
    etat = attendre(api("/monter", {"video_id": ID, "moment": 3}).get_json()["tache"])
    assert etat["etat"] == "reussie", etat
    assert etat["resultat"]["file_path"] == f"shorts/{ID}_3.mp4"
    assert abs(etat["resultat"]["duree_s"] - 3) < 0.2
    assert etat["resultat"]["rendu_s"] > 0
    assert (tmp_path / "shorts" / f"{ID}_3.mp4").is_file()


def test_monter_moment_absent(api, attendre, tmp_path, brut_modele):
    poser_brut(tmp_path, brut_modele)
    poser_plan(tmp_path)
    etat = attendre(api("/monter", {"video_id": ID, "moment": 1}).get_json()["tache"])
    assert etat["etat"] == "echouee"
    assert "1" in etat["erreur"]


def test_monter_sans_plan(api, attendre, tmp_path, brut_modele):
    poser_brut(tmp_path, brut_modele)
    assert attendre(api("/monter", {"video_id": ID, "moment": 1}).get_json()["tache"])["etat"] == "echouee"


def test_short_se_telecharge_avec_le_jeton(atelier, auth, tmp_path):
    (tmp_path / "shorts").mkdir(exist_ok=True)
    (tmp_path / "shorts" / f"{ID}_2.mp4").write_bytes(b"video")
    client = atelier.app.test_client()
    reponse = client.get(f"/shorts/{ID}/2", headers=auth)
    assert (reponse.status_code, reponse.data, reponse.mimetype) == (200, b"video", "video/mp4")
    assert client.get(f"/shorts/{ID}/2").status_code == 401
    assert client.get(f"/shorts/{ID}/3", headers=auth).status_code == 404
    assert client.get("/shorts/pas%20bon/2", headers=auth).status_code == 400


def test_nettoyer_efface_tout_et_rien_que_ca(api, tmp_path):
    for dossier in ("bruts", "plans", "shorts"):
        (tmp_path / dossier).mkdir()
    (tmp_path / "bruts" / f"{ID}.mp4").write_bytes(b"x" * 2_000_000)
    (tmp_path / "bruts" / f"{ID}.mp4.part").write_bytes(b"x")
    (tmp_path / "bruts" / f"{ID}.run.json").write_text("{}")
    (tmp_path / "plans" / f"{ID}.json").write_text("{}")
    (tmp_path / "shorts" / f"{ID}_1.mp4").write_bytes(b"x" * 1_000_000)
    (tmp_path / "shorts" / f"{ID}_12.mp4").write_bytes(b"x")
    (tmp_path / "shorts" / f"{ID}_1.mp4.part").write_bytes(b"x")
    autre = tmp_path / "shorts" / "autrevideo1_1.mp4"
    autre.write_bytes(b"x")
    assert api("/nettoyer", {"video_id": ID}).get_json()["efface_mo"] == 3.0
    assert sorted(p.name for p in tmp_path.rglob("*.*")) == ["autrevideo1_1.mp4"]
    assert api("/nettoyer", {"video_id": ID}).get_json() == {"efface_mo": 0}


@pytest.mark.parametrize("etat", ["en_cours", "en_attente"])
def test_nettoyer_refuse_pendant_une_tache_sur_la_video(api, attendre, monkeypatch, tmp_path, etat):
    libre = threading.Event()
    monkeypatch.setattr(fetch, "recuperer", lambda video_id, dossier: libre.wait(5))
    premiere = api("/fetch", {"video_id": ID if etat == "en_cours" else "autrevideo1"}).get_json()["tache"]
    while taches.lire(premiere)["etat"] != "en_cours":
        threading.Event().wait(0.01)
    ids = [premiere]
    if etat == "en_attente":
        ids.append(api("/fetch", {"video_id": ID}).get_json()["tache"])
    assert taches.lire(ids[-1])["etat"] == etat
    assert api("/nettoyer", {"video_id": ID}).status_code == 409
    libre.set()
    for id_tache in ids:
        attendre(id_tache)
    assert api("/nettoyer", {"video_id": ID}).status_code == 200


def lancer_fetch_paye(api, monkeypatch, cout):
    def rate(video_id, dossier):
        erreur = fetch.ErreurFetch("téléchargement coupé")
        erreur.cout_usd = cout
        raise erreur
    monkeypatch.setattr(fetch, "recuperer", rate)
    return api("/fetch", {"video_id": ID}).get_json()["tache"]


def test_tache_echouee_porte_le_cout_paye(api, attendre, monkeypatch):
    etat = attendre(lancer_fetch_paye(api, monkeypatch, 0.0712))
    assert etat["etat"] == "echouee"
    assert etat["cout_usd"] == 0.0712
    assert etat["erreur"] == "téléchargement coupé"


def test_cout_de_la_tache_echouee_survit_a_un_redemarrage(api, attendre, atelier, auth, monkeypatch):
    id_tache = lancer_fetch_paye(api, monkeypatch, 0.0712)
    attendre(id_tache)
    atelier.taches._etats.clear()
    assert atelier.app.test_client().get(f"/taches/{id_tache}", headers=auth).get_json()["cout_usd"] == 0.0712


def test_tache_echouee_sans_paiement_sans_cout(api, attendre, monkeypatch):
    assert "cout_usd" not in attendre(lancer_fetch_paye(api, monkeypatch, 0))


def test_cout_non_ecrit_ne_masque_pas_l_erreur(api, attendre, atelier, monkeypatch):
    vrai = atelier.taches.ecrire_json

    def plein(chemin, donnees):
        if chemin.endswith(".cout.json"):
            raise OSError("disque plein")
        vrai(chemin, donnees)
    monkeypatch.setattr(atelier.taches, "ecrire_json", plein)
    id_tache = lancer_fetch_paye(api, monkeypatch, 0.0712)
    assert attendre(id_tache)["erreur"] == "téléchargement coupé"
