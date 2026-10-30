import json
import threading

import taches

SECRET = "jeton-tres-secret-123"


def fini(id_tache):
    return taches.lire(id_tache)["etat"] in ("reussie", "echouee")


def attendre(id_tache):
    for _ in range(200):
        if fini(id_tache):
            return taches.lire(id_tache)
        threading.Event().wait(0.01)
    raise AssertionError("tâche sans fin")


def test_tache_reussie_ecrite_sur_le_disque(atelier, tmp_path):
    id_tache = taches.lancer(lambda: {"ok": 1})
    assert attendre(id_tache)["resultat"] == {"ok": 1}
    disque = json.loads((tmp_path / "taches" / f"{id_tache}.json").read_text())
    assert disque["etat"] == "reussie"
    assert not list((tmp_path / "taches").glob("*.tmp"))


def assert_plus_rien_en_cours():
    with taches._verrou:  # la tâche finie a rendu la main
        assert taches.en_cours() is None


def test_une_seule_tache_a_la_fois(atelier):
    libre = threading.Event()
    premiere = taches.lancer(libre.wait)
    deuxieme = taches.lancer(lambda: 2)
    for _ in range(200):
        if taches.lire(premiere)["etat"] == "en_cours":
            break
        threading.Event().wait(0.01)
    assert taches.lire(premiere)["etat"] == "en_cours"
    assert taches.lire(deuxieme)["etat"] == "en_attente"
    assert taches.en_cours() == premiere
    libre.set()
    assert attendre(deuxieme)["etat"] == "reussie"
    assert_plus_rien_en_cours()


def test_resultat_illisible_ne_bloque_pas_la_tache(atelier):
    assert attendre(taches.lancer(lambda: {"x": object()}))["etat"] == "echouee"


def test_echec_libere_la_place(atelier):
    def rate():
        raise RuntimeError("panne")

    assert attendre(taches.lancer(rate))["etat"] == "echouee"
    assert_plus_rien_en_cours()


def test_disque_plein_l_etat_reste_lisible_en_memoire(atelier, monkeypatch, tmp_path, caplog):
    def plein(*_):
        raise OSError("No space left on device")
    monkeypatch.setattr(taches, "ecrire_json", plein)
    id_tache = taches.lancer(lambda: {"ok": 1})
    assert attendre(id_tache)["resultat"] == {"ok": 1}
    assert not (tmp_path / "taches" / f"{id_tache}.json").exists()
    assert "non écrit" in caplog.text
    assert_plus_rien_en_cours()


def test_echec_sans_secret(atelier, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", SECRET)

    def rate():
        raise RuntimeError(
            f"refus {SECRET} https://apify.dziura.online/te?sig=abc Authorization: Bearer xyz987 "
            "HTTPSConnectionPool(host='a.b', port=443): Max retries exceeded with url: /te/x?sig=abc")

    erreur = attendre(taches.lancer(rate))["erreur"]
    for fuite in (SECRET, "sig=abc", "dziura", "xyz987", "/te/x"):
        assert fuite not in erreur
    assert "refus" in erreur


def test_redemarrage_marque_echouee_ce_qui_tournait(atelier, tmp_path):
    dossier = tmp_path / "taches"
    dossier.mkdir(exist_ok=True)
    for nom, etat in (("a" * 32, "en_cours"), ("b" * 32, "en_attente"), ("c" * 32, "reussie")):
        (dossier / f"{nom}.json").write_text(json.dumps({"id": nom, "etat": etat}))
    taches.reprendre()
    assert taches.lire("a" * 32)["etat"] == "echouee"
    assert taches.lire("a" * 32)["erreur"]
    assert taches.lire("b" * 32)["etat"] == "echouee"
    assert taches.lire("c" * 32)["etat"] == "reussie"


def test_identifiant_de_tache_verifie(atelier):
    assert taches.lire("../../etc/passwd") is None
    assert taches.lire("0" * 32) is None
