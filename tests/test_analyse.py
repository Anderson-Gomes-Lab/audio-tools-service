import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import analyse
from conftest import poser_jour
import montage

PLAN_EXEMPLE = json.loads((Path(__file__).parent.parent / "contrat" / "plan-exemple.json").read_text(encoding="utf-8"))
CLE = "AIzaCLEDETEST123"
ID = "abcdefghijk"
USAGE = SimpleNamespace(prompt_token_count=1_000_000, candidates_token_count=100_000, thoughts_token_count=100_000)
FICHIER = SimpleNamespace(name="files/x", uri="u", mime_type="video/mp4")


def moment(debut, fin, registre="maitrise", score=8, **autres):
    return {"debut_s": debut, "pic_s": fin - 1, "fin_s": fin, "registre": registre, "format": "instant",
            "score": score, "monstre": "Rathalos", "arme": "Long Sword", "textes": [], "preuve": "vu", "autre": None,
            **autres}


def passe_2(debut=98.0, fin=110.0, trouve=True):
    return {"trouve": trouve, "debut_s": debut, "pic_s": fin - 3, "fin_s": fin, "x_centre": 0.4,
            "textes": [{"texte": "parry diff", "debut_s": 1.0, "fin_s": 5.0}]}


class FauxGemini:
    """Remplace genai.Client : réponses en file, appels notés."""

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = []
        self.files = SimpleNamespace(get=self._get)
        self.models = SimpleNamespace(generate_content=self._generer)

    def _get(self, name):
        raise RuntimeError("panne du suivi")

    def _generer(self, model, contents, config):
        self.appels.append(contents[0].video_metadata)
        reponse = self.reponses.pop(0)
        if isinstance(reponse, Exception):
            raise reponse
        texte = reponse if isinstance(reponse, str) else json.dumps(reponse)
        return SimpleNamespace(text=texte, usage_metadata=USAGE)


@pytest.fixture
def gemini(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", CLE)
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.delenv("PLAFOND_GEMINI_MOIS_USD", raising=False)

    def installer(*reponses):
        return FauxGemini(reponses)
    return installer


def preciser(faux, *moments, duree=600.0, facture=None):
    return analyse._preciser(faux, FICHIER, "consigne", list(moments), duree, facture or analyse._Facture())


def precis(faux, *moments, duree=600.0):
    return preciser(faux, *moments, duree=duree)[0][0]


def test_moment_precise_conforme_au_plan(gemini):
    m = analyse._conforme(precis(gemini(passe_2()), moment(100, 110, n=1)))
    assert set(m) == set(PLAN_EXEMPLE["moments"][0])
    assert set(m["textes"][0]) == set(PLAN_EXEMPLE["moments"][0]["textes"][0])
    assert PLAN_EXEMPLE["modele"] == analyse.MODELE


@pytest.mark.parametrize("debut, fin", [(98.0, 110.0), (18.0, 30.0)])  # depuis la vidéo, depuis l'extrait
def test_bornes_de_la_passe_2_ramenees_a_la_video(gemini, debut, fin):
    faux = gemini(passe_2(debut=debut, fin=fin))
    m = precis(faux, moment(100, 110))
    assert (m["debut_s"], m["fin_s"]) == (98.0, 110.0)
    extrait = faux.appels[0]
    assert (extrait.fps, extrait.start_offset, extrait.end_offset) == (analyse.FPS_PASSE_2, "80.0s", "130.0s")


@pytest.mark.parametrize("reponse", [passe_2(debut=300.0, fin=310.0), passe_2(trouve=False)])
def test_moment_absent_de_l_extrait_rejete(gemini, reponse):
    gardes, ecartes = preciser(gemini(reponse), moment(100, 110))
    assert gardes == []
    assert ecartes[0]["raison"] == "passe 2"


def test_extrait_borne_par_la_video(gemini):
    faux = gemini(passe_2(debut=2.0, fin=12.0))
    preciser(faux, moment(5, 15))
    assert faux.appels[0].start_offset == "0.0s"


def test_cout_additionne_les_appels_reels(gemini):  # chacun : 1 M entrée à 0,75 $ + 200 k sortie à 3,75 $ = 1,50 $
    facture = analyse._Facture()
    preciser(gemini(passe_2(), passe_2(200.0, 210.0)), moment(100, 110), moment(200, 210), facture=facture)
    assert facture.usd == pytest.approx(3.0)


def test_tarif_change_apres_le_31_decembre():
    assert analyse._cout(USAGE, date(2026, 12, 31)) == pytest.approx(1.5)
    assert analyse._cout(USAGE, date(2027, 1, 1)) == pytest.approx(3.0)


@pytest.mark.parametrize("jour", [date(2026, 11, 1), date(2027, 6, 1)])
def test_tarif_de_flash_lite(jour):  # 1 M à 0,30 $ + 200 k à 2,50 $
    assert analyse._cout(USAGE, jour, analyse.MODELE_IDENTIFICATION) == pytest.approx(0.8)


def test_facture_compte_chaque_modele_a_son_tarif(gemini, monkeypatch):
    poser_jour(monkeypatch, date(2026, 11, 1))
    facture = analyse._Facture()
    facture.noter(USAGE, analyse.MODELE_IDENTIFICATION)
    facture.noter(USAGE, analyse.MODELE)
    assert facture.usd == pytest.approx(2.3)


def test_json_mal_forme_relance_puis_reussit(gemini):
    faux = gemini("pas du json", "{toujours pas", passe_2())
    facture = analyse._Facture()
    assert preciser(faux, moment(100, 110), facture=facture)[0]
    assert len(faux.appels) == 3
    assert facture.usd == pytest.approx(4.5)


@pytest.mark.parametrize("reponses", [("a", "b", "c"), ("[]", "{}", "null")])
def test_abandon_apres_deux_relances(gemini, reponses):
    faux = gemini(*reponses, "jamais lu")
    with pytest.raises(analyse.ErreurAnalyse, match="mal formé"):
        preciser(faux, moment(100, 110))
    assert len(faux.appels) == 3


def test_cle_absente(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(analyse.ErreurAnalyse, match="GEMINI_API_KEY absente"):
        analyse.client()


@pytest.mark.parametrize("etats, delai, attendu", [
    (("FAILED",), 900, "traiter la vidéo"),
    (("PROCESSING",), -1, "à temps"),
])
def test_traitement_distant_echoue(monkeypatch, etats, delai, attendu):
    monkeypatch.setattr(analyse, "ATTENTE_FICHIER_S", delai)
    faux = gemini_sans_reponse()
    fichier = SimpleNamespace(name="files/x", state=SimpleNamespace(name=etats[0]))
    with pytest.raises(analyse.ErreurAnalyse, match=attendu):
        analyse.attendre_traitement(faux, fichier)


def test_suivi_du_traitement_en_panne_remonte(monkeypatch):
    monkeypatch.setattr(analyse, "PAUSE_FICHIER_S", 0)
    fichier = SimpleNamespace(name="files/x", state=SimpleNamespace(name="PROCESSING"))
    with pytest.raises(RuntimeError, match="panne du suivi"):
        analyse.attendre_traitement(gemini_sans_reponse(), fichier)


def gemini_sans_reponse():
    return FauxGemini(())


@pytest.mark.parametrize("texte, attendu", [
    ("04:05", 245), ("29:44", 1784), ("120:00", 7200), ("405", -1), ("4:75", -1),
])
def test_secondes(texte, attendu):
    assert analyse.secondes(texte) == attendu


def test_analyse_n_importe_pas_flask():
    assert "flask" not in Path(analyse.__file__).read_text(encoding="utf-8").lower()


def ecrit(debut, fin, mot="parry diff"):
    return {"texte": mot, "debut_s": debut, "fin_s": fin}


def plan_avec(gemini, textes, debut=98.0, fin=110.0, registre="maitrise"):
    return preciser(gemini({**passe_2(debut, fin), "textes": textes}), moment(100, max(110, int(fin)), registre))[0]


@pytest.mark.parametrize("recus, attendus", [
    ([ecrit(100.0, 103.0)], [(2.0, 5.0)]),  # comptés depuis la vidéo
    ([ecrit(10.0, 20.0)], [(10.0, 12.0)]),  # fin ramenée au clip
    ([ecrit(412.0, 415.0)], []),
    ([ecrit(5.0, 5.0)], []),
    ([ecrit(1.0, 3.0), ecrit(13.0, 15.0)], [(1.0, 3.0)]),
    ([ecrit(1.0, 2.0, "   "), ecrit(3.0, 4.0)], [(3.0, 4.0)]),
    ([ecrit(i, i + 1.0) for i in range(4)], [(0, 1.0), (1, 2.0), (2, 3.0)]),
])
def test_textes_toujours_montables(gemini, recus, attendus):
    m = plan_avec(gemini, recus)[0]
    assert [(t["debut_s"], t["fin_s"]) for t in m["textes"]] == attendus
    assert len(montage.horaires(m["textes"], m["fin_s"] - m["debut_s"])) == len(attendus)


@pytest.mark.parametrize("registre, rendu, fin, debut, format_reel", [
    ("maitrise", 106, 110, 98, "instant"), ("maitrise", 126, 140, 126, "instant"),  # rallongé, gardé
    ("rire", 100, 400, 360, "sequence"), ("rire", 100, 113, 100, "instant"), ("maitrise", 100, 127, 102, "instant"),
    ("maitrise", 100, 200, 140, "sequence"), ("tension", 100, 200, 140, "sequence"),
    ("spectacle", 100, 160, 115, "sequence"), ("detente", 100, 140, 110, "sequence"),
    ("info", 100, 170, 110, "sequence"), ("curiosite", 100, 160, 115, "sequence"),
])
def test_duree_par_registre_et_format(gemini, registre, rendu, fin, debut, format_reel):
    m = plan_avec(gemini, [], float(rendu), float(fin), registre)[0]
    assert (m["debut_s"], m["fin_s"], m["format"]) == (debut, fin, format_reel)


def test_raccourcir_ne_coupe_jamais_le_pic(gemini):
    gardes, ecartes = preciser(gemini({**passe_2(100.0, 200.0), "pic_s": 110.0}), moment(100, 200, "rire"))
    assert gardes == [] and ecartes[0]["raison"] == "passe 2"


def test_la_fin_laisse_retomber_l_action(gemini):
    m = precis(gemini({**passe_2(100.0, 120.0), "pic_s": 119.5}), moment(100, 120, "spectacle"))
    assert m["fin_s"] == 122.5 and m["debut_s"] == 100.0


def test_une_fin_plus_tardive_que_le_minimum_est_gardee(gemini):
    assert precis(gemini({**passe_2(100.0, 128.0), "pic_s": 120.0}), moment(100, 120, "spectacle"))["fin_s"] == 128.0


def test_la_fin_s_arrete_a_la_fin_de_la_video(gemini):
    m = precis(gemini({**passe_2(100.0, 118.0), "pic_s": 117.5}), moment(100, 118, "spectacle"), duree=119.0)
    assert m["fin_s"] == 119.0


def test_rallonger_la_fin_garde_le_debut_et_l_accroche(gemini):
    faux = gemini({**passe_2(100.0, 130.0), "pic_s": 129.5, "textes": [ecrit(0.5, 3.0)]})
    m = precis(faux, moment(100, 130, "detente"))
    assert (m["debut_s"], m["fin_s"]) == (100.0, 132.5) and m["textes"] == [ecrit(0.5, 3.0)]


def test_recadrer_garde_l_instant_reel_des_textes(gemini):
    assert plan_avec(gemini, [ecrit(1.0, 2.0)], 106.0, 110.0)[0]["textes"] == [ecrit(9.0, 10.0)]  # rallongé de 8 s
    m = plan_avec(gemini, [ecrit(1.0, 5.0), ecrit(35.0, 40.0)], 100.0, 160.0, "rire")[0]  # 20 s retirées
    assert m["textes"] == [ecrit(15.0, 20.0)]


def test_consignes_et_plafonds():
    passe_a, passe_b = analyse._consignes()
    assert "maitrise" in passe_a and "{moment}" in passe_b
    assert analyse._TEXTES["maxItems"] == montage.TEXTES_MAX == 3
    for registre, (instant, sequence) in analyse.DUREES_S.items():
        assert f"| {registre} | {instant[0]}–{instant[1]} s | {sequence[0]}–{sequence[1]} s |" in passe_b


@pytest.mark.parametrize("mauvais", [
    {"trouve": True}, {**passe_2(), "x_centre": "milieu"}, {**passe_2(), "textes": None},
    {**passe_2(), "textes": [{"texte": "x"}]}, {**passe_2(), "trouve": "oui"}, {**passe_2(), "debut_s": True},
])
def test_forme_invalide_en_passe_2_relance(gemini, mauvais):
    faux = gemini(mauvais, passe_2())
    assert len(preciser(faux, moment(100, 110))[0]) == 1
    assert len(faux.appels) == 2


@pytest.mark.parametrize("reponses, cout", [
    (("a", "b", "c"), 4.5),
    (("a", RuntimeError("panne")), 1.5),
    ((RuntimeError("panne"),), 0),
])
def test_erreur_laisse_tout_le_cout_paye_sur_la_facture(gemini, reponses, cout):
    facture = analyse._Facture()
    with pytest.raises(Exception), analyse.facturation(facture):
        preciser(gemini(*reponses), moment(100, 110), facture=facture)
    assert facture.usd == pytest.approx(cout)


def test_facturation_porte_le_cout_sur_l_erreur():
    facture = analyse._Facture()
    facture.usd = 1.2345678
    with pytest.raises(analyse.ErreurAnalyse) as erreur, analyse.facturation(facture):
        raise RuntimeError("panne")
    assert erreur.value.cout_usd == 1.234568 and "RuntimeError: panne" in str(erreur.value)


def test_arrondi_de_fin_de_video_tolere_et_borne(gemini):
    m = precis(gemini(passe_2(debut=108.0, fin=120.0)), moment(108, 120), duree=119.96)
    assert (m["debut_s"], m["fin_s"]) == (108.0, 119.96)
    assert preciser(gemini(passe_2(debut=108.0, fin=120.0)), moment(108, 120), duree=118.9)[0] == []


def test_facture_sans_limite_ne_refuse_jamais(gemini):
    facture = analyse._Facture()
    facture.usd = 1000.0
    facture.verifier()


@pytest.mark.parametrize("jour, limite", [(date(2026, 12, 31), 3.0), (date(2027, 1, 1), 6.0)])
def test_limite_de_la_facture_vaut_dix_centimes_la_minute(gemini, monkeypatch, jour, limite):
    poser_jour(monkeypatch, jour)
    facture = analyse._Facture(1800)
    facture.usd = limite - 0.01
    facture.verifier()
    facture.usd = limite
    with pytest.raises(analyse.ErreurAnalyse, match=rf"{limite:.0f},00 \$"):
        facture.verifier()


def test_compteur_repart_de_zero_le_mois_suivant(gemini, tmp_path):
    (tmp_path / "compteur-gemini.json").write_text(json.dumps({"mois": "2020-01", "usd": 999.0}))
    facture = analyse._Facture()
    facture.verifier()
    facture.noter(USAGE)
    lu = json.loads((tmp_path / "compteur-gemini.json").read_text())
    assert lu == {"mois": analyse.date.today().strftime("%Y-%m"), "usd": pytest.approx(facture.usd)}


@pytest.mark.parametrize("plafond", [None, ""])  # variable absente, ou laissée vide dans Coolify
def test_sans_plafond_defini_le_compteur_compte_sans_bloquer(gemini, tmp_path, monkeypatch, plafond):
    if plafond is not None:
        monkeypatch.setenv("PLAFOND_GEMINI_MOIS_USD", plafond)
    mois = analyse.date.today().strftime("%Y-%m")
    (tmp_path / "compteur-gemini.json").write_text(json.dumps({"mois": mois, "usd": 5000.0}))
    facture = analyse._Facture()
    facture.verifier()
    facture.noter(USAGE)
    assert json.loads((tmp_path / "compteur-gemini.json").read_text())["usd"] == pytest.approx(5000.0 + facture.usd)


def test_plafond_defini_bloque(gemini, tmp_path, monkeypatch):
    monkeypatch.setenv("PLAFOND_GEMINI_MOIS_USD", "25")
    mois = analyse.date.today().strftime("%Y-%m")
    (tmp_path / "compteur-gemini.json").write_text(json.dumps({"mois": mois, "usd": 24.99}))
    facture = analyse._Facture()
    facture.verifier()
    (tmp_path / "compteur-gemini.json").write_text(json.dumps({"mois": mois, "usd": 25.0}))
    with pytest.raises(analyse.ErreurAnalyse, match=r"25,00 \$"):
        facture.verifier()


def test_passe_2_respecte_les_gardes(gemini):
    facture = analyse._Facture(60)
    facture.usd = 0.5
    faux = gemini(passe_2())
    with pytest.raises(analyse.ErreurAnalyse):
        preciser(faux, moment(100, 110), facture=facture)
    assert faux.appels == []
