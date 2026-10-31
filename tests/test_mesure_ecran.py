import json
import subprocess
import tracemalloc

import numpy as np
import pytest
from PIL import Image

import mesure_ecran

CLES = {"vie", "lame", "alerte", "perte", "arme_rangee", "hors_combat", "coups", "vaincus"}
DUREE = 12

# Une boîte pleine : x, y, largeur, hauteur, couleur, condition sur le temps.
BOITES = [
    (88, 50, 418, 30, "0x00ff00", "lt(t,6)"),  # vie à 80 %
    (88, 50, 157, 30, "0x00ff00", "gte(t,6)"),  # vie à 30 %
    (1400, 150, 20, 20, "white", "not(between(t,8,10.5))"),  # liste de commandes
    (300, 10, 460, 4, "0xff7800", "between(t,2,4)"),  # vague d'alerte
    (110, 157, 160, 6, "0xe6e6e6", "lt(t,4)"),  # lame blanche
    (110, 181, 160, 6, "0xe6e6e6", "lt(t,4)"),
    (110, 157, 160, 6, "red", "gte(t,4)"),  # lame rouge
    (110, 181, 160, 6, "red", "gte(t,4)"),
    (1780, 30, 20, 10, "white", "between(t,1,2)"),  # arme rangée
    (1778, 214, 18, 30, "0xffdc00", "1"),  # coche déjà là au début : rien à dater
    (1778, 252, 18, 30, "0xffdc00", "gte(t,5)"),  # coche posée à 5 s
]


def fabriquer(chemin, taille="1920x1080", duree=3, boites=()):
    filtres = [f"drawbox={x}:{y}:{large}:{haut}:{couleur}:t=fill:enable='{quand}'"
               for x, y, large, haut, couleur, quand in boites]
    source = f"color=black:s={taille}:r=30:d={duree}"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
         *(["-vf", ",".join(filtres)] if filtres else []),
         "-c:v", "libx264", "-crf", "12", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(chemin)],
        check=True)
    return chemin


@pytest.fixture(scope="session")
def dossier(tmp_path_factory):
    return tmp_path_factory.mktemp("ecran")


@pytest.fixture(scope="session")
def modele(dossier):
    chemin = dossier / "modele.png"
    Image.new("L", (20, 10), 255).save(chemin)
    return chemin


@pytest.fixture(scope="session")
def mesures(dossier, modele):
    video = fabriquer(dossier / "chasse.mp4", duree=DUREE, boites=BOITES)
    return mesure_ecran.mesurer(video, modele=modele)


@pytest.fixture
def noire(dossier):
    return fabriquer(dossier / "noire.mp4")


def test_memes_cles_et_une_mesure_tous_les_cinquiemes_de_seconde(mesures):
    assert set(mesures) == CLES
    assert len(mesures["vie"]) == len(mesures["lame"]) == DUREE * 5


def test_le_resultat_se_compare_directement_a_un_json_de_reference(mesures):
    assert json.loads(json.dumps(mesures)) == mesures


def test_vie_lue_avant_et_apres_le_coup(mesures):
    assert mesures["vie"][10] == pytest.approx(0.8, abs=0.01)
    assert mesures["vie"][50] == pytest.approx(0.3, abs=0.01)


def test_coup_date(mesures):
    # La vie lissée tombe 0,4 s après l'image : un flash isolé ne doit pas compter.
    assert mesures["coups"] == [pytest.approx(6.4, abs=0.2)]


def test_lame_suit_le_contour(mesures):
    assert mesures["lame"][5] == "blanc"
    assert mesures["lame"][40] == "rouge"


def test_alerte_datee(mesures):
    [[debut, fin]] = mesures["alerte"]
    assert (debut, fin) == (pytest.approx(2.0, abs=0.6), pytest.approx(4.0, abs=0.6))


def test_perte_de_commandes_datee(mesures):
    [[debut, fin]] = mesures["perte"]
    assert (debut, fin) == (pytest.approx(8.0, abs=0.3), pytest.approx(10.5, abs=0.3))


def test_arme_rangee_datee(mesures):
    [[debut, fin]] = mesures["arme_rangee"]
    assert (debut, fin) == (pytest.approx(1.0, abs=0.3), pytest.approx(2.0, abs=0.3))


def test_seule_la_coche_posee_pendant_la_video_compte(mesures):
    assert mesures["vaincus"] == [pytest.approx(5.0, abs=0.6)]


def test_ecran_vide_est_hors_combat(noire, modele):
    mesures = mesure_ecran.mesurer(noire, modele=modele)
    assert mesures["hors_combat"] == [[0.0, 3.0]]
    assert set(mesures["vie"]) == {None}
    assert set(mesures["lame"]) == {None}
    assert mesures["coups"] == mesures["vaincus"] == []


def test_la_1072_de_youtube_passe(dossier, modele):
    video = fabriquer(dossier / "yt.mp4", taille="1920x1072")
    assert len(mesure_ecran.mesurer(video, modele=modele)["vie"]) == 15


@pytest.mark.parametrize("taille", ["1280x720", "2560x1440", "1920x800", "1080x1920"])
def test_autre_taille_refusee_clairement(dossier, modele, taille):
    video = fabriquer(dossier / f"{taille}.mp4", taille=taille, duree=1)
    with pytest.raises(ValueError, match=taille):
        mesure_ecran.mesurer(video, modele=modele)


def test_fichier_absent_erreur_claire(dossier, modele):
    with pytest.raises(ValueError, match="absent.mp4"):
        mesure_ecran.mesurer(dossier / "absent.mp4", modele=modele)


def test_fichier_qui_n_est_pas_une_video(dossier, modele):
    faux = dossier / "faux.mp4"
    faux.write_bytes(b"pas une video")
    with pytest.raises(ValueError, match="faux.mp4"):
        mesure_ecran.mesurer(faux, modele=modele)


def test_echec_de_ffmpeg_remonte_son_message(noire, modele, monkeypatch):
    monkeypatch.setattr(mesure_ecran, "LARGEUR", 4000)  # zone plus large que l'image : ffmpeg refuse
    with pytest.raises(RuntimeError, match="ffmpeg a échoué.*[Ii]nvalid"):
        mesure_ecran.mesurer(noire, modele=modele)


def test_erreur_en_cours_de_lecture_arrete_ffmpeg(noire, modele, monkeypatch):
    def plante(*_):
        raise KeyError("relevé impossible")

    monkeypatch.setattr(mesure_ecran, "relever", plante)
    with pytest.raises(KeyError):
        mesure_ecran.mesurer(noire, modele=modele)


def test_video_sans_image_erreur_claire(noire, modele, monkeypatch):
    monkeypatch.setattr(mesure_ecran, "lire", lambda *_: ([], []))
    with pytest.raises(ValueError, match="aucune image"):
        mesure_ecran.mesurer(noire, modele=modele)


def test_la_memoire_ne_croit_pas_avec_la_duree(dossier, modele):
    court = fabriquer(dossier / "court.mp4", duree=2)
    long = fabriquer(dossier / "long.mp4", duree=20)

    def pic(video):
        tracemalloc.start()
        mesure_ecran.mesurer(video, modele=modele)
        _, haut = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return haut

    pic(court)  # premier passage : caches de numpy et de Pillow
    assert pic(long) - pic(court) < 1_000_000


def mesures_faites():
    return {
        "vie": [None, *[0.9] * 49, *[0.5] * 50, *[0.2] * 50],
        "lame": [None, *["blanc"] * 49, *["rouge"] * 50, *["jaune"] * 50],
        "alerte": [[5.0, 8.0], [12.0, 14.0]],
        "perte": [[1.0, 3.5], [21.0, 25.0]],
        "arme_rangee": [],
        "hors_combat": [],
        "coups": [11.0, 22.0],
        "vaincus": [13.0],
    }


def test_faits_a_l_epee_longue_sans_fenetre():
    texte = mesure_ecran.faits(mesures_faites(), "Épée longue")
    assert "- Lame : 00:00 blanc, 00:10 rouge, 00:20 jaune.\n" in texte
    assert "encore verte : 00:05 90 %, 00:10 50 %, 00:15 50 %, 00:20 20 %, 00:25 20 %.\n" in texte
    assert "- Coups reçus, chute brutale de la vie : 00:11, 00:22.\n" in texte
    assert "une date par monstre : 00:13.\n" in texte
    assert "il peut y en avoir d'autres : 00:05 à 00:08, 00:12 à 00:14.\n" in texte
    assert "- Sans aucune commande à l'écran, 2 s ou plus : 00:01 à 00:03, 00:21 à 00:25.\n" in texte
    assert "- Arme rangée : aucune.\n" in texte
    assert "- Hors combat, menus, camp ou trajet : aucune.\n" in texte


@pytest.mark.parametrize("arme", ["Épée longue", "épée longue", "epee-longue"])
def test_ligne_lame_gardee_a_l_epee_longue(arme):
    assert "- Lame : " in mesure_ecran.faits(mesures_faites(), arme)


@pytest.mark.parametrize("arme", ["Grande épée", "Lames doubles", None])
def test_ligne_lame_retiree_sinon(arme):
    texte = mesure_ecran.faits(mesures_faites(), arme)
    assert "Lame" not in texte
    assert "- Vie, part" in texte


def test_fenetre_limite_tous_les_faits():
    texte = mesure_ecran.faits(mesures_faites(), "Épée longue", debut=12, fin=23)
    assert "- Lame : 00:12 rouge, 00:20 jaune.\n" in texte
    assert "encore verte : 00:15 50 %, 00:20 20 %.\n" in texte
    assert "chute brutale de la vie : 00:22.\n" in texte
    assert "une date par monstre : 00:13.\n" in texte
    assert "il peut y en avoir d'autres : 00:12 à 00:14.\n" in texte


def test_fenetre_coupe_les_plages_a_ses_bornes():
    texte = mesure_ecran.faits(mesures_faites(), debut=6, fin=23)
    assert "il peut y en avoir d'autres : 00:06 à 00:08, 00:12 à 00:14.\n" in texte
    assert "2 s ou plus : 00:21 à 00:23.\n" in texte


def test_fenetre_ouverte_d_un_seul_cote():
    assert "chute brutale de la vie : 00:22.\n" in mesure_ecran.faits(mesures_faites(), debut=20)
    assert "chute brutale de la vie : 00:11.\n" in mesure_ecran.faits(mesures_faites(), fin=20)


def test_fenetre_vide_reste_lisible():
    texte = mesure_ecran.faits(mesures_faites(), "Épée longue", debut=100, fin=110)
    assert "- Lame : aucune.\n" in texte
    assert "encore verte : aucune.\n" in texte
    assert "chute brutale de la vie : aucun.\n" in texte


def test_faits_ne_modifie_pas_les_mesures():
    mesures = mesures_faites()
    mesure_ecran.faits(mesures, "Épée longue", debut=12, fin=23)
    assert mesures == mesures_faites()


def test_le_modele_est_cherche_a_cote_du_service():
    attendu = mesure_ecran.Path(mesure_ecran.__file__).parent / "lecture" / "connaissance" / "signes"
    assert mesure_ecran.MODELE_ARME_RANGEE == attendu / "sortir-l-arme.png"


def test_vie_lit_la_part_de_barre_verte():
    image = np.zeros((240, 1920, 3), np.int16)
    image[50:80, 88:349, 1] = 255
    assert mesure_ecran.vie(image) == 0.5


def test_vie_replie_hors_combat():
    image = np.zeros((240, 1920, 3), np.int16)
    image[50:80, 10:30, 1] = 255
    assert mesure_ecran.vie(image) is None
