import json
import os
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


def poser_lecture(media, contenu):
    (media / "lectures").mkdir(exist_ok=True)
    (media / "lectures" / f"{ID}.json").write_text(json.dumps(contenu))


def lire_json(chemin):
    if not os.path.isfile(chemin):
        return None
    with open(chemin, encoding="utf-8") as fichier:
        return json.load(fichier)


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
    """Faux lecture.py : compte les appels."""
    appels = []

    def analyser(video_id, brut, dossier):
        appels.append(("analyser", video_id, brut, dossier))
        return {"video_id": video_id, "cout_usd": 1.5}

    def contexte(video_id, brut, dossier):
        appels.append(("contexte", video_id))
        return {"video_id": video_id, "cout_usd": 0.2}

    faux = types.SimpleNamespace(analyser=analyser, contexte=contexte, attente=[])

    def mettre_en_attente(video_id, note, dossier):
        faux.attente.append(note)
        return not os.path.isfile(os.path.join(dossier, f"{video_id}.json"))

    faux.mettre_en_attente = mettre_en_attente
    faux.lire = lambda video_id, dossier: lire_json(os.path.join(dossier, f"{video_id}.json"))
    monkeypatch.setitem(sys.modules, "lecture", faux)
    faux.appels = appels
    return faux


def test_sante(atelier, auth):
    reponse = atelier.app.test_client().get("/sante", headers=auth).get_json()
    assert reponse["disque_libre_go"] > 0
    assert reponse["tache_en_cours"] is None


def test_tache_inconnue(atelier, auth):
    assert atelier.app.test_client().get("/taches/inconnue", headers=auth).status_code == 404


@pytest.mark.parametrize("mauvais", ["court", "a" * 12, "a" * 10 + "/", "../../etc/p", "a" * 11 + "\n", 12, None])
@pytest.mark.parametrize("route", ["/fetch", "/analyse", "/contexte", "/monter", "/nettoyer"])
def test_video_id_refuse(api, route, mauvais):
    assert api(route, {"video_id": mauvais, "moment": 1, "note": "n"}).status_code == 400


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


def test_analyse_passe_par_la_lecture(api, attendre, ia, tmp_path):
    etat = attendre(api("/analyse", {"video_id": ID}).get_json()["tache"])
    assert etat["resultat"] == {"video_id": ID, "cout_usd": 1.5}
    assert ia.appels == [("analyser", ID, str(tmp_path / "bruts" / f"{ID}.mp4"), str(tmp_path / "lectures"))]


def test_analyse_n_ecrit_plus_de_plan(api, attendre, ia, tmp_path):
    attendre(api("/analyse", {"video_id": ID}).get_json()["tache"])
    assert not (tmp_path / "plans").exists()


def test_cout_d_une_lecture_echouee_gardee(api, attendre, ia, monkeypatch):
    def rate(video_id, brut, dossier):
        erreur = RuntimeError("panne")
        erreur.cout_usd = 0.42
        raise erreur
    monkeypatch.setattr(ia, "analyser", rate)
    etat = attendre(api("/analyse", {"video_id": ID}).get_json()["tache"])
    assert (etat["etat"], etat["cout_usd"]) == ("echouee", 0.42)


@pytest.mark.parametrize("note", [None, "", "   ", 12])
def test_contexte_refuse_une_note_vide(api, ia, note):
    assert api("/contexte", {"video_id": ID, "note": note}).status_code == 400
    assert ia.attente == [] and ia.appels == []


def test_contexte_avant_la_lecture_attend(api, ia):
    reponse = api("/contexte", {"video_id": ID, "note": "  le cri  "})
    assert (reponse.status_code, reponse.get_json()) == (200, {"en_attente": True})
    assert ia.attente == ["le cri"] and ia.appels == []


def test_contexte_apres_la_lecture_lance_une_tache(api, attendre, ia, tmp_path):
    poser_lecture(tmp_path, {"video_id": ID})
    reponse = api("/contexte", {"video_id": ID, "note": "le cri"})
    assert reponse.status_code == 202
    assert attendre(reponse.get_json()["tache"])["resultat"]["cout_usd"] == 0.2
    assert ia.attente == ["le cri"] and ia.appels == [("contexte", ID)]  # la note est sur le disque avant la tâche


def test_lire_une_lecture(atelier, auth, ia, tmp_path):
    client = atelier.app.test_client()
    assert client.get(f"/lectures/{ID}", headers=auth).status_code == 404
    poser_lecture(tmp_path, {"video_id": ID, "resume": "r"})
    assert client.get(f"/lectures/{ID}", headers=auth).get_json() == {"video_id": ID, "resume": "r"}
    assert client.get(f"/lectures/{ID}").status_code == 401
    assert client.get("/lectures/pas%20bon", headers=auth).status_code == 400


def test_monter_cherche_le_moment_par_son_numero(api, attendre, tmp_path, brut_modele):
    poser_brut(tmp_path, brut_modele)
    poser_plan(tmp_path)
    etat = attendre(api("/monter", {"video_id": ID, "moment": 3}).get_json()["tache"])
    assert etat["etat"] == "reussie", etat
    assert etat["resultat"]["file_path"] == f"shorts/{ID}_3.mp4"
    assert etat["cout_usd"] == 0  # une tâche gratuite annonce 0, sans cas particulier
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
    etat = attendre(api("/monter", {"video_id": ID, "moment": 1}).get_json()["tache"])
    assert etat["etat"] == "echouee" and "plan absent" in etat["erreur"] and "lot 2" in etat["erreur"]


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


def test_nettoyer_garde_lecture_note_fichier_et_compteur(api, tmp_path):
    (tmp_path / "lectures").mkdir()
    fins = ("json", "note.txt", "fichier.json", "mesures.json", "releve.json")
    gardes = [tmp_path / "lectures" / f"{ID}.{fin}" for fin in fins]
    gardes.append(tmp_path / "compteur-gemini.json")
    for chemin in gardes:
        chemin.write_text("{}")
    api("/nettoyer", {"video_id": ID})
    assert all(chemin.is_file() for chemin in gardes)


@pytest.mark.parametrize("etat", ["en_cours", "en_attente"])
def test_nettoyer_refuse_pendant_une_tache_sur_la_video(api, attendre, monkeypatch, tmp_path, etat):
    libre = threading.Event()
    monkeypatch.setattr(fetch, "recuperer", lambda video_id, dossier: {"fini": libre.wait(5)})
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
