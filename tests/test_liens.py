import io
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import RequestEntityTooLarge

import liens

MP4 = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 4
ADRESSE = "https://clips.exemple.test"
ID = "_1PH44fjxNg"
JOUR_S = 24 * 3600


def envoyer(client, auth, contenu=MP4, nom="s.mp4", entetes=None):
    donnees = {"fichier": (io.BytesIO(contenu), nom)}
    return client.post("/liens", data=donnees, headers={**auth, **(entetes or {})}, content_type="multipart/form-data")


def chemin_du_lien(reponse):
    return urlsplit(reponse.get_json()["url"]).path


def fichier_du_lien(media, reponse):
    return media / "liens" / chemin_du_lien(reponse).removeprefix('/p/')


def vieillir(chemin, age_s):
    moment = time.time() - age_s
    os.utime(chemin, (moment, moment))


def contenu_de(media):
    dossier = media / "liens"
    return sorted(os.listdir(dossier)) if dossier.exists() else []


@pytest.fixture(autouse=True)
def adresse_publique(monkeypatch):
    monkeypatch.setenv("URL_PUBLIQUE", ADRESSE)


@pytest.fixture
def client(atelier):
    return atelier.app.test_client()


@pytest.fixture
def lien(client, auth):
    return envoyer(client, auth)


def test_envoi_rend_un_lien_de_24_h(client, auth, tmp_path):
    reponse = envoyer(client, auth)
    assert reponse.status_code == 200
    corps = reponse.get_json()
    assert sorted(corps) == ["expire_le", "url"]
    assert corps["url"].startswith(f"{ADRESSE}/p/")
    assert corps["url"].endswith(".mp4")
    expire = datetime.fromisoformat(corps["expire_le"])
    assert expire.utcoffset() == timedelta(0)
    assert abs((expire - datetime.now(timezone.utc)).total_seconds() - JOUR_S) < 5
    assert fichier_du_lien(tmp_path, reponse).read_bytes() == MP4
    assert len(contenu_de(tmp_path)) == 1


def test_vraie_video_acceptee(client, auth, brut_modele):
    assert envoyer(client, auth, brut_modele.read_bytes()).status_code == 200


def test_jeton_aleatoire_sans_rapport_avec_la_video(client, auth):
    une, deux = (envoyer(client, auth, nom=f"{ID}_1.mp4") for _ in range(2))
    jetons = [chemin_du_lien(r).removeprefix("/p/").removesuffix(".mp4") for r in (une, deux)]
    assert jetons[0] != jetons[1]
    assert all(len(jeton) == 43 and ID not in jeton for jeton in jetons)


def test_adresse_vient_de_la_variable_pas_de_la_requete(client, auth, monkeypatch):
    monkeypatch.setenv("URL_PUBLIQUE", "https://exemple.test/")
    reponse = envoyer(client, auth, entetes={"Host": "pirate.test", "X-Forwarded-Host": "pirate.test"})
    assert reponse.get_json()["url"].startswith("https://exemple.test/p/")
    assert "pirate" not in reponse.get_json()["url"]


def test_sans_adresse_publique_refus_clair_et_rien_d_ecrit(client, auth, monkeypatch, tmp_path):
    monkeypatch.delenv("URL_PUBLIQUE")
    reponse = envoyer(client, auth)
    assert reponse.status_code == 400 and "URL_PUBLIQUE" in reponse.get_json()["error"]
    assert not list((tmp_path / "liens").glob("*.mp4"))


def test_lien_sans_extension_refuse(client, lien):
    assert client.get(chemin_du_lien(lien).removesuffix(".mp4")).status_code == 404


def test_fichier_purge_entre_controle_et_envoi_rend_404(client, lien, monkeypatch, tmp_path):
    chemin = chemin_du_lien(lien)
    monkeypatch.setattr(liens, "chemin_valide", lambda jeton, dossier: str(tmp_path / "parti.mp4"))
    assert client.get(chemin).status_code == 404


def test_envoi_exige_le_jeton(client, tmp_path):
    assert envoyer(client, {}).status_code == 401
    assert envoyer(client, {"Authorization": "Bearer faux"}).status_code == 401
    assert contenu_de(tmp_path) == []


def test_lecture_sans_jeton(client, lien):
    reponse = client.get(chemin_du_lien(lien))
    assert (reponse.status_code, reponse.mimetype, reponse.data) == (200, "video/mp4", MP4)


def test_head(client, lien):
    reponse = client.head(chemin_du_lien(lien))
    assert (reponse.status_code, reponse.mimetype, reponse.data) == (200, "video/mp4", b"")
    assert reponse.content_length == len(MP4)
    assert reponse.headers["Accept-Ranges"] == "bytes"


def test_range(client, lien):
    reponse = client.get(chemin_du_lien(lien), headers={"Range": "bytes=4-7"})
    assert (reponse.status_code, reponse.data) == (206, b"ftyp")
    assert reponse.headers["Content-Range"] == f"bytes 4-7/{len(MP4)}"
    assert client.get(chemin_du_lien(lien), headers={"Range": "bytes=-3"}).data == MP4[-3:]
    assert client.get(chemin_du_lien(lien), headers={"Range": f"bytes={len(MP4)}-"}).status_code == 416


def test_lien_encore_valide_juste_avant_24_h(client, lien, tmp_path):
    vieillir(fichier_du_lien(tmp_path, lien), JOUR_S - 60)
    assert client.get(chemin_du_lien(lien)).status_code == 200


def test_lien_expire_ne_sert_plus_meme_si_le_fichier_existe(client, lien, tmp_path):
    fichier = fichier_du_lien(tmp_path, lien)
    vieillir(fichier, JOUR_S + 1)
    assert client.get(chemin_du_lien(lien)).status_code == 404
    assert client.head(chemin_du_lien(lien)).status_code == 404
    assert client.get(chemin_du_lien(lien), headers={"Range": "bytes=0-1"}).status_code == 404
    assert fichier.is_file()


@pytest.mark.parametrize("contenu", [MP4, b"pas une video"], ids=["accepte", "refuse"])
def test_chaque_envoi_efface_les_expires_et_garde_le_reste(client, auth, lien, tmp_path, contenu):
    vieux = fichier_du_lien(tmp_path, lien)
    jeune = fichier_du_lien(tmp_path, envoyer(client, auth))
    panne = tmp_path / "liens" / "panne.mp4.part"
    panne.write_bytes(b"x")
    vieillir(vieux, JOUR_S + 1)
    vieillir(panne, JOUR_S + 1)
    vieillir(jeune, JOUR_S - 60)
    envoyer(client, auth, contenu)
    assert not vieux.exists() and not panne.exists()
    assert jeune.is_file()


def test_premier_envoi_sans_dossier(client, auth, tmp_path):
    assert not (tmp_path / "liens").exists()
    assert envoyer(client, auth).status_code == 200


@pytest.mark.parametrize("jeton", [
    "a" * 43, "a" * 42, "a" * 44, "court", "secret", "secret.mp4", "../secret", "../../etc/passwd",
    "..%2Fsecret", "%2e%2e%2fsecret", "a" * 43 + "%00", "a" * 43 + "/x", "a" * 42 + ".",
])
def test_jeton_faux_ou_traversee_404_sans_jeton_d_acces(client, tmp_path, jeton):
    (tmp_path / "secret").write_bytes(MP4)
    (tmp_path / "secret.mp4").write_bytes(MP4)
    (tmp_path / "liens").mkdir()
    (tmp_path / "liens" / "court.mp4").write_bytes(MP4)  # bien rangé, mais son nom n'est pas un jeton
    reponse = client.get(f"/p/{jeton}")
    assert reponse.status_code == 404
    assert MP4 not in reponse.data


def test_404_ne_dit_pas_pourquoi(client, lien, tmp_path):
    vieillir(fichier_du_lien(tmp_path, lien), JOUR_S + 1)
    reponses = [client.get(chemin) for chemin in (chemin_du_lien(lien), f"/p/{'a' * 43}", "/p/court")]
    assert {(r.status_code, r.data) for r in reponses} == {(404, reponses[0].data)}


@pytest.mark.parametrize("contenu", [b"", b"ftyp", b"pas une video " * 10, b"<html>ftyp</html>"])
def test_mauvais_fichier_refuse_malgre_l_extension(client, auth, tmp_path, contenu):
    reponse = envoyer(client, auth, contenu, "s.mp4")
    assert reponse.status_code == 400
    assert "error" in reponse.get_json()
    assert contenu_de(tmp_path) == []


def test_champ_absent_refuse(client, auth, tmp_path):
    donnees = {"autre": (io.BytesIO(MP4), "s.mp4")}
    assert client.post("/liens", data=donnees, headers=auth, content_type="multipart/form-data").status_code == 400
    assert client.post("/liens", headers=auth).status_code == 400
    assert contenu_de(tmp_path) == []


def test_taille_limite_acceptee_un_octet_de_plus_refuse(client, auth, tmp_path, monkeypatch):
    monkeypatch.setattr(liens, "TAILLE_MAX", len(MP4))
    assert envoyer(client, auth).status_code == 200
    avant = contenu_de(tmp_path)
    reponse = envoyer(client, auth, MP4 + b"x")
    assert reponse.status_code == 413
    assert "error" in reponse.get_json()
    assert contenu_de(tmp_path) == avant


def test_limite_reelle_300_mo(tmp_path):
    with open(tmp_path / "gros", "wb+") as flux:
        flux.write(MP4)
        flux.truncate(300_000_001)  # fichier creux : rien n'est écrit sur le disque
        flux.seek(0)
        with pytest.raises(RequestEntityTooLarge):
            liens.creer(FileStorage(stream=flux), str(tmp_path / "liens"))
    assert not (tmp_path / "liens").exists()


def test_corps_annonce_trop_gros_refuse_sans_le_lire(client, auth, monkeypatch):
    monkeypatch.setattr(liens, "CORPS_MAX", 10)
    reponse = envoyer(client, auth)
    assert reponse.status_code == 413
    assert "error" in reponse.get_json()


def test_corps_sans_longueur_annoncee_limite_aussi(client, auth, tmp_path, monkeypatch):
    monkeypatch.setattr(liens, "CORPS_MAX", 1000)
    corps = b"--b\r\nContent-Disposition: form-data; name=\"fichier\"; filename=\"s.mp4\"\r\n\r\n"
    corps += MP4 * 10 + b"\r\n--b--\r\n"
    reponse = client.post("/liens", input_stream=io.BytesIO(corps), headers=auth,
                          content_type="multipart/form-data; boundary=b",
                          environ_overrides={"wsgi.input_terminated": True})
    assert reponse.status_code == 413
    assert contenu_de(tmp_path) == []


def test_ecriture_coupee_ne_laisse_rien(client, auth, tmp_path, monkeypatch):
    def plein(*_):
        raise OSError("disque plein")
    monkeypatch.setattr(liens.os, "replace", plein)
    assert envoyer(client, auth).status_code == 500
    assert contenu_de(tmp_path) == []


def test_nettoyer_ne_touche_pas_aux_liens(client, auth, lien, tmp_path):
    fichier = fichier_du_lien(tmp_path, lien)
    assert client.post("/nettoyer", json={"video_id": ID}, headers=auth).status_code == 200
    assert fichier.is_file()


def test_seule_p_est_ouverte_en_plus_de_split_et_duration(atelier):
    client = atelier.app.test_client()
    ouvertes = set()
    for regle in atelier.app.url_map.iter_rules():
        chemin = regle.build({nom: "1" for nom in regle.arguments})[1]
        methode = next(iter(regle.methods - {"HEAD", "OPTIONS"}))
        if client.open(chemin, method=methode).status_code != 401:
            ouvertes.add(regle.endpoint)
    assert ouvertes == {"split_audio", "get_duration", "lire_lien"}
