import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

import montage


def dimensions(chemin):
    sortie = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate",
         "-of", "json", str(chemin)], check=True, capture_output=True, text=True).stdout
    return {f["codec_type"]: f for f in json.loads(sortie)["streams"]}


def image(video, instant, sortie):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(instant), "-i", str(video),
                    "-frames:v", "1", str(sortie)], check=True)
    return Image.open(sortie).convert("RGB")


def moment(**champs):
    return {"n": 1, "debut_s": 2.0, "fin_s": 5.0, "x_centre": 0.5, "textes": [], **champs}


@pytest.fixture
def sortie(tmp_path):
    return tmp_path / "s.mp4"


def test_format_de_sortie(brut_modele, sortie):
    resultat = montage.monter(str(brut_modele), moment(), str(sortie))
    flux = dimensions(sortie)
    assert (flux["video"]["width"], flux["video"]["height"]) == (1080, 1920)
    assert flux["video"]["codec_name"] == "h264"
    assert flux["video"]["r_frame_rate"] == "30/1"
    assert flux["audio"]["codec_name"] == "aac"
    assert flux["audio"]["sample_rate"] == "48000"
    assert abs(resultat["duree_s"] - 3) < 0.15
    assert resultat["rendu_s"] > 0
    assert not list(sortie.parent.glob("*.part"))


def test_moment_du_plan_exemple(brut_modele, sortie):
    plan = json.loads(Path("contrat/plan-exemple.json").read_text())
    m = {**plan["moments"][0], "debut_s": 0.0}
    assert abs(montage.monter(str(brut_modele), m, str(sortie))["duree_s"] - 12) < 0.2


def test_faststart(brut_modele, sortie):
    montage.monter(str(brut_modele), moment(), str(sortie))
    debut = sortie.read_bytes()
    assert debut.find(b"moov") < debut.find(b"mdat")


@pytest.mark.parametrize("taille,attendu", [
    ((1920, 1072), (602, 1070, 658, 1)),
    ((1920, 1080), (606, 1076, 656, 2)),
])
def test_cadre_au_centre_tolere_1072_et_1080(taille, attendu):
    assert montage.cadre(*taille, 0.5) == attendu


def test_cadre_borne_dans_l_image():
    assert montage.cadre(1920, 1080, 0)[2] == 0
    assert montage.cadre(1920, 1080, 1)[2] == 1920 - 606
    assert montage.cadre(1920, 1080, 0.01)[2] == 0
    assert montage.cadre(1920, 1080, 0.99)[2] == 1920 - 606


def test_cadre_source_etroite():
    largeur, hauteur, x, y = montage.cadre(1080, 1920, 0.5)
    assert (largeur, hauteur) == (1080, 1920) and (x, y) == (0, 0)


@pytest.mark.parametrize("x_centre,rouge", [(0, True), (1, False)])
def test_recadrage_suit_x_centre(brut_modele, sortie, tmp_path, x_centre, rouge):
    montage.monter(str(brut_modele), moment(x_centre=x_centre), str(sortie))
    r, _, b = image(sortie, 1, tmp_path / "i.png").getpixel((540, 960))
    assert (r > b) == rouge


def test_x_centre_absent_vaut_le_centre(brut_modele, sortie, tmp_path):
    m = moment()
    del m["x_centre"]
    montage.monter(str(brut_modele), m, str(sortie))
    vue = image(sortie, 1, tmp_path / "i.png")
    r_gauche, _, b_gauche = vue.getpixel((270, 960))
    r_droite, _, b_droite = vue.getpixel((810, 960))
    assert r_gauche > b_gauche and b_droite > r_droite


def test_texte_incruste_seulement_a_son_instant(brut_modele, sortie, tmp_path):
    textes = [{"texte": "parry diff", "debut_s": 1.0, "fin_s": 2.0}]
    montage.monter(str(brut_modele), moment(x_centre=0, textes=textes), str(sortie))

    def blancs(instant):
        vue = image(sortie, instant, tmp_path / "t.png")
        return sum(1 for p in vue.get_flattened_data() if min(p) > 230)

    assert blancs(0.5) == 0
    assert blancs(1.5) > 1000
    assert blancs(2.5) == 0


def test_trois_textes_ne_se_superposent_pas(brut_modele, sortie, tmp_path):
    textes = [{"texte": "un", "debut_s": 0, "fin_s": 2.0},
              {"texte": "deux", "debut_s": 1.0, "fin_s": 2.5},
              {"texte": "trois", "debut_s": 2.0, "fin_s": 9.0}]
    montage.monter(str(brut_modele), moment(textes=textes), str(sortie))
    assert sortie.is_file()
    assert montage.horaires(textes, 3.0) == [("un", 0.0, 1.0), ("deux", 1.0, 2.0), ("trois", 2.0, 3.0)]


def image_numero(video, numero, sortie):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                    "-vf", f"select=eq(n\\,{numero})", "-frames:v", "1", "-fps_mode", "passthrough", str(sortie)],
                   check=True)
    return Image.open(sortie).convert("RGB")


@pytest.mark.parametrize("frontiere", [1.0, 0.5])
def test_jamais_deux_textes_sur_la_meme_image(brut_modele, sortie, tmp_path, frontiere):
    textes = [{"texte": "MMMM", "debut_s": 0, "fin_s": frontiere},
              {"texte": "IIII", "debut_s": frontiere, "fin_s": 2.0}]
    montage.monter(str(brut_modele), moment(x_centre=0, textes=textes), str(sortie))

    def blancs(numero):
        vue = image_numero(sortie, numero, tmp_path / "f.png")
        return {(x, y) for y in range(vue.height) for x in range(0, vue.width)
                if min(vue.getpixel((x, y))) > 230}

    frontiere_n = round(frontiere * 30)
    premier, second = blancs(frontiere_n - 3), blancs(frontiere_n + 3)
    propres_premier, propres_second = premier - second, second - premier
    assert len(propres_premier) > 500 and len(propres_second) > 500
    for numero in range(frontiere_n - 1, frontiere_n + 2):
        vue = blancs(numero)
        assert not (len(vue & propres_premier) > 100 and len(vue & propres_second) > 100), numero


def test_textes_en_desordre_ranges():
    textes = [{"texte": "b", "debut_s": 2, "fin_s": 3}, {"texte": "a", "debut_s": 0, "fin_s": 1}]
    assert [t[0] for t in montage.horaires(textes, 5)] == ["a", "b"]


@pytest.mark.parametrize("textes", [
    [{"texte": "x", "debut_s": 400, "fin_s": 410}],  # instants absolus au lieu de relatifs au clip
    [{"texte": "x", "debut_s": 2, "fin_s": 1}],
    [{"texte": "x"}],
    [{"texte": "x", "debut_s": 0, "fin_s": 1}] * 4,
])
def test_textes_invalides_refuses(textes):
    with pytest.raises(montage.ErreurMontage):
        montage.horaires(textes, 5)


def test_texte_vide_ignore():
    assert montage.horaires([{"texte": "  ", "debut_s": 0, "fin_s": 1}], 5) == []
    assert montage.horaires(None, 5) == []


@pytest.mark.parametrize("champs", [
    {"debut_s": 5.0, "fin_s": 2.0},
    {"debut_s": 500.0, "fin_s": 510.0},
    {"debut_s": None},
    {"debut_s": -1.0},
    {"fin_s": "x"},
])
def test_bornes_invalides(brut_modele, sortie, champs):
    with pytest.raises(montage.ErreurMontage):
        montage.monter(str(brut_modele), moment(**champs), str(sortie))
    assert not sortie.exists()


def test_fin_bornee_a_la_duree_de_la_video(brut_modele, sortie):
    assert abs(montage.monter(str(brut_modele), moment(debut_s=10.0, fin_s=99.0), str(sortie))["duree_s"] - 2) < 0.2


def test_video_abimee_pas_de_fichier_a_moitie(tmp_path, sortie, brut_modele):
    faux = tmp_path / "faux.mp4"
    shutil.copy(brut_modele, faux)
    faux.write_bytes(faux.read_bytes()[:2000])
    with pytest.raises(subprocess.CalledProcessError):
        montage.monter(str(faux), moment(), str(sortie))
    assert list(tmp_path.glob("s.mp4*")) == []


def test_sans_son_piste_silencieuse(tmp_path, sortie):
    muet = tmp_path / "muet.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=red:s=1920x1080:r=30:d=4", str(muet)], check=True)
    textes = [{"texte": "ok", "debut_s": 0, "fin_s": 1}]
    montage.monter(str(muet), moment(debut_s=0, fin_s=2, textes=textes), str(sortie))
    assert "audio" in dimensions(sortie)
