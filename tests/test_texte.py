import pytest
from PIL import Image

import texte


def rendu(chaine, tmp_path):
    chemin = tmp_path / "t.png"
    x, y = texte.rendre(chaine, str(chemin))
    return Image.open(chemin), x, y


def test_nettoie_en_minuscules():
    assert texte.nettoyer("  PARRY   Diff\n") == "parry diff"


def test_accents_composes_ramenes_a_une_lettre():
    assert texte.nettoyer("été") == "été"


def test_garde_accents_et_apostrophes():
    chaine = "j'étais à fond, l’œuf où ça ? « gagné » … ç"
    assert texte.nettoyer(chaine) == chaine


def test_retire_ce_que_la_police_n_a_pas():
    assert texte.nettoyer("gg 🔥") == "gg"
    assert texte.nettoyer("🔥") == ""


def test_png_transparent_avec_contour(tmp_path):
    image, _, _ = rendu("parry diff", tmp_path)
    assert image.mode == "RGBA"
    assert image.getpixel((0, 0))[3] == 0
    couleurs = set(image.get_flattened_data())
    assert (255, 255, 255, 255) in couleurs
    assert any(c[:3] == (0, 0, 0) and c[3] == 255 for c in couleurs)


def test_tient_dans_la_zone_sure(tmp_path):
    for chaine in ("gg", "one more cart and we are done", "x" * 60):
        image, x, y = rendu(chaine, tmp_path)
        assert x >= 60 and x + image.width <= 1080 - 60
        assert y >= texte.HAUT - 1
        assert y + image.height <= 1920 * 0.45, chaine


def test_revient_a_la_ligne(tmp_path):
    court, _, _ = rendu("parry diff", tmp_path)
    long, _, _ = rendu("the plan after three seconds of pure chaos", tmp_path)
    assert long.height > court.height * 1.5


def test_mot_seul_trop_long_tient_quand_meme(tmp_path):
    image, _, _ = rendu("a" * 60, tmp_path)
    assert image.width <= 960


def test_texte_vide_refuse(tmp_path):
    with pytest.raises(ValueError):
        texte.rendre("🔥", str(tmp_path / "t.png"))
