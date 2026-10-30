import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import analyse
import montage

PLAN_EXEMPLE = json.loads((Path(__file__).parent.parent / "contrat" / "plan-exemple.json").read_text(encoding="utf-8"))
CLE = "AIzaCLEDETEST123"
ID = "abcdefghijk"
USAGE = SimpleNamespace(prompt_token_count=1_000_000, candidates_token_count=100_000, thoughts_token_count=100_000)
VRAIE_DUREE = analyse._duree


def moment(debut, fin, registre="maitrise", score=8, **autres):
    return {"debut_s": debut, "pic_s": fin - 1, "fin_s": fin, "registre": registre, "format": "instant",
            "score": score, "monstre": "Rathalos", "arme": "Long Sword", "textes": [], "preuve": "vu", "autre": None,
            **autres}


def mmss(secondes):
    return f"{int(secondes // 60):02d}:{int(secondes % 60):02d}"


def passe_1(*moments):
    ecrits = [{**m, "debut": mmss(m["debut_s"]), "pic": mmss(m["pic_s"]), "fin": mmss(m["fin_s"])} for m in moments]
    return {"titre_propose": "t", "description_proposee": "d", "moments": ecrits}


def passe_2(debut=98.0, fin=110.0, trouve=True):
    return {"trouve": trouve, "debut_s": debut, "pic_s": fin - 3, "fin_s": fin, "x_centre": 0.4,
            "textes": [{"texte": "parry diff", "debut_s": 1.0, "fin_s": 5.0}]}


class FauxGemini:
    """Remplace genai.Client : réponses en file, appels notés."""

    def __init__(self, reponses, envoi_echoue=False, etats=("ACTIVE",), suivi_echoue=False):
        self.reponses = list(reponses)
        self.envoi_echoue = envoi_echoue
        self.suivi_echoue = suivi_echoue
        self.etats = list(etats)
        self.appels = []
        self.effaces = []
        self.files = SimpleNamespace(upload=self._upload, get=self._get, delete=self._delete)
        self.models = SimpleNamespace(generate_content=self._generer)

    def _fichier(self):
        etat = self.etats.pop(0)
        return SimpleNamespace(name="files/x", uri="u", mime_type="video/mp4", state=SimpleNamespace(name=etat))

    def _upload(self, file, config):
        if self.envoi_echoue:
            raise RuntimeError(f"échec https://x.googleapis.com/v1?key={CLE} clé {CLE}")
        return self._fichier()

    def _get(self, name):
        if self.suivi_echoue:
            raise RuntimeError("panne du suivi")
        return self._fichier()

    def _delete(self, name):
        self.effaces.append(name)

    def _generer(self, model, contents, config):
        self.appels.append(contents[0].video_metadata)
        reponse = self.reponses.pop(0)
        if isinstance(reponse, Exception):
            raise reponse
        texte = reponse if isinstance(reponse, str) else json.dumps(reponse)
        return SimpleNamespace(text=texte, usage_metadata=USAGE)


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", CLE)
    monkeypatch.setattr(analyse, "_duree", lambda chemin: 600.0)
    monkeypatch.setattr(analyse, "PAUSE_FICHIER_S", 0)

    def installer(*reponses, **options):
        faux = FauxGemini(reponses, **options)
        monkeypatch.setattr(analyse.genai, "Client", lambda api_key: faux)
        return faux
    return installer


def echec():
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        analyse.analyser("v.mp4", "abc")
    return erreur.value


def test_plan_conforme_au_contrat(gemini):
    faux = gemini(passe_1(moment(100, 110)), passe_2())
    plan = analyse.analyser("v.mp4", "abc")
    assert faux.effaces == ["files/x"]
    assert set(plan) == set(PLAN_EXEMPLE)
    assert set(plan["moments"][0]) == set(PLAN_EXEMPLE["moments"][0])
    assert set(plan["moments"][0]["textes"][0]) == set(PLAN_EXEMPLE["moments"][0]["textes"][0])
    assert plan["video_id"] == "abc"
    assert plan["modele"] == PLAN_EXEMPLE["modele"]


@pytest.mark.parametrize("debut, fin", [(98.0, 110.0), (18.0, 30.0)])  # depuis la vidéo, depuis l'extrait
def test_bornes_de_la_passe_2_ramenees_a_la_video(gemini, debut, fin):
    faux = gemini(passe_1(moment(100, 110)), passe_2(debut=debut, fin=fin))
    m = analyse.analyser("v.mp4", "abc")["moments"][0]
    assert (m["debut_s"], m["fin_s"]) == (98.0, 110.0)
    assert faux.appels[0].fps == analyse.FPS_PASSE_1
    extrait = faux.appels[1]
    assert (extrait.fps, extrait.start_offset, extrait.end_offset) == (analyse.FPS_PASSE_2, "80.0s", "130.0s")


@pytest.mark.parametrize("reponse", [passe_2(debut=300.0, fin=310.0), passe_2(trouve=False)])
def test_moment_absent_de_l_extrait_rejete(gemini, reponse):
    gemini(passe_1(moment(100, 110)), reponse)
    plan = analyse.analyser("v.mp4", "abc")
    assert plan["moments"] == []
    assert plan["rejetes"][0]["raison"] == "passe 2"


def test_extrait_borne_par_la_video(gemini):
    faux = gemini(passe_1(moment(5, 15)), passe_2(debut=2.0, fin=12.0))
    analyse.analyser("v.mp4", "abc")
    assert faux.appels[1].start_offset == "0.0s"


def test_cout_additionne_les_appels_reels(gemini):  # chacun : 1 M entrée à 0,75 $ + 200 k sortie à 3,75 $ = 1,50 $
    gemini(passe_1(moment(100, 110)), passe_2())
    assert analyse.analyser("v.mp4", "abc")["cout_usd"] == pytest.approx(3.0)


def test_tarif_change_apres_le_31_decembre():
    assert analyse._cout(USAGE, date(2026, 12, 31)) == pytest.approx(1.5)
    assert analyse._cout(USAGE, date(2027, 1, 1)) == pytest.approx(3.0)


def test_json_mal_forme_relance_puis_reussit(gemini):
    faux = gemini("pas du json", "{toujours pas", passe_1())
    plan = analyse.analyser("v.mp4", "abc")
    assert len(faux.appels) == 3
    assert plan["moments"] == []
    assert plan["cout_usd"] == pytest.approx(4.5)


@pytest.mark.parametrize("reponses", [("a", "b", "c"), ("[]", "{}", "null")])
def test_abandon_apres_deux_relances(gemini, reponses):
    faux = gemini(*reponses, "jamais lu")
    echec()
    assert len(faux.appels) == 3
    assert faux.effaces == ["files/x"]


def test_erreur_sans_cle_ni_url(gemini):
    gemini(envoi_echoue=True)
    erreur = echec()
    assert CLE not in str(erreur)
    assert "googleapis" not in str(erreur)
    assert erreur.__cause__ is None and erreur.__suppress_context__


def test_cle_absente(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    echec()


@pytest.mark.parametrize("options, delai", [
    ({"etats": ("PROCESSING", "FAILED")}, 900),
    ({"etats": ("PROCESSING", "PROCESSING")}, -1),
    ({"etats": ("PROCESSING",), "suivi_echoue": True}, 900),
])
def test_traitement_distant_echoue(gemini, monkeypatch, options, delai):
    faux = gemini(**options)
    monkeypatch.setattr(analyse, "ATTENTE_FICHIER_S", delai)
    echec()
    assert faux.effaces == ["files/x"]


@pytest.mark.parametrize("texte, attendu", [
    ("04:05", 245), ("29:44", 1784), ("120:00", 7200), ("405", -1), ("4:75", -1),
])
def test_secondes(texte, attendu):
    assert analyse._secondes(texte) == attendu


@pytest.mark.parametrize("brut", [
    {**passe_1(moment(100, 110)), "moments": [{**passe_1(moment(100, 110))["moments"][0], "debut": "cent"}]},
    passe_1(moment(700, 710)),
])
def test_moment_hors_video_rejete_sans_passe_2(gemini, brut):
    faux = gemini(brut)
    plan = analyse.analyser("v.mp4", "abc")
    assert plan["moments"] == [] and plan["rejetes"][0]["raison"] == "hors vidéo"
    assert len(faux.appels) == 1


def test_pas_de_passe_2_pour_un_score_faible(gemini):
    faux = gemini(passe_1(moment(100, 110, score=3)))
    plan = analyse.analyser("v.mp4", "abc")
    assert len(faux.appels) == 1
    assert plan["rejetes"][0]["raison"] == "score"


def test_analyse_n_importe_pas_flask():
    assert "flask" not in Path(analyse.__file__).read_text(encoding="utf-8").lower()


def ecrit(debut, fin, mot="parry diff"):
    return {"texte": mot, "debut_s": debut, "fin_s": fin}


def plan_avec(gemini, textes, debut=98.0, fin=110.0, registre="maitrise"):
    gemini(passe_1(moment(100, max(110, int(fin)), registre)), {**passe_2(debut, fin), "textes": textes})
    return analyse.analyser("v.mp4", "abc")["moments"]


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
    gemini(passe_1(moment(100, 200, "rire")), {**passe_2(100.0, 200.0), "pic_s": 110.0})
    plan = analyse.analyser("v.mp4", "abc")
    assert plan["moments"] == [] and plan["rejetes"][0]["raison"] == "passe 2"


def test_la_fin_laisse_retomber_l_action(gemini):
    gemini(passe_1(moment(100, 120, "spectacle")), {**passe_2(100.0, 120.0), "pic_s": 119.5})
    m = analyse.analyser("v.mp4", "abc")["moments"][0]
    assert m["fin_s"] == 122.5 and m["debut_s"] == 100.0


def test_une_fin_plus_tardive_que_le_minimum_est_gardee(gemini):
    gemini(passe_1(moment(100, 120, "spectacle")), {**passe_2(100.0, 128.0), "pic_s": 120.0})
    assert analyse.analyser("v.mp4", "abc")["moments"][0]["fin_s"] == 128.0


def test_la_fin_s_arrete_a_la_fin_de_la_video(gemini, monkeypatch):
    gemini(passe_1(moment(100, 118, "spectacle")), {**passe_2(100.0, 118.0), "pic_s": 117.5})
    monkeypatch.setattr(analyse, "_duree", lambda chemin: 119.0)
    assert analyse.analyser("v.mp4", "abc")["moments"][0]["fin_s"] == 119.0


def test_rallonger_la_fin_garde_le_debut_et_l_accroche(gemini):
    gemini(passe_1(moment(100, 130, "detente")),
           {**passe_2(100.0, 130.0), "pic_s": 129.5, "textes": [ecrit(0.5, 3.0)]})
    m = analyse.analyser("v.mp4", "abc")["moments"][0]
    assert (m["debut_s"], m["fin_s"]) == (100.0, 132.5) and m["textes"] == [ecrit(0.5, 3.0)]


def test_l_analyse_applique_le_quota(gemini):
    faux = gemini(passe_1(moment(100, 120), moment(200, 220), moment(300, 320)),
                  passe_2(100.0, 120.0), passe_2(200.0, 220.0))
    plan = analyse.analyser("v.mp4", "abc")
    assert [m["n"] for m in plan["moments"]] == [1, 2]
    assert [r["raison"] for r in plan["rejetes"]] == ["quota"] and len(faux.appels) == 3


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


ECRIT = passe_1(moment(1, 9))["moments"][0]


@pytest.mark.parametrize("mauvais", [
    "[]", "null", "{}", {**passe_1(), "moments": "aucun"}, {**passe_1(), "moments": [{"debut": "00:01"}]},
    {**passe_1(), "moments": [{**ECRIT, "debut": None}]}, {**passe_1(), "moments": [{**ECRIT, "registre": "autre"}]},
    {**passe_1(), "moments": [{**ECRIT, "score": "8"}]},
])
def test_forme_invalide_en_passe_1_relance(gemini, mauvais):
    faux = gemini(mauvais, passe_1())
    assert analyse.analyser("v.mp4", "abc")["moments"] == []
    assert len(faux.appels) == 2


@pytest.mark.parametrize("mauvais", [
    {"trouve": True}, {**passe_2(), "x_centre": "milieu"}, {**passe_2(), "textes": None},
    {**passe_2(), "textes": [{"texte": "x"}]}, {**passe_2(), "trouve": "oui"}, {**passe_2(), "debut_s": True},
])
def test_forme_invalide_en_passe_2_relance(gemini, mauvais):
    faux = gemini(passe_1(moment(100, 110)), mauvais, passe_2())
    assert len(analyse.analyser("v.mp4", "abc")["moments"]) == 1
    assert len(faux.appels) == 3


@pytest.mark.parametrize("reponses, cout", [
    (("a", "b", "c"), 4.5),
    ((passe_1(moment(100, 110)), "a", "b", "c"), 6.0),
    ((passe_1(moment(100, 110)), RuntimeError("panne")), 1.5),
    (("a", RuntimeError("panne")), 1.5),
    ((RuntimeError("panne"),), 0),
])
def test_erreur_porte_tout_le_cout_paye(gemini, reponses, cout):
    faux = gemini(*reponses)
    assert echec().cout_usd == pytest.approx(cout)
    assert faux.effaces == ["files/x"]


def test_arrondi_de_fin_de_video_tolere_et_borne(gemini, monkeypatch):
    gemini(passe_1(moment(108, 120)), passe_2(debut=108.0, fin=120.0))
    monkeypatch.setattr(analyse, "_duree", lambda chemin: 119.96)
    m = analyse.analyser("v.mp4", "abc")["moments"][0]
    assert (m["debut_s"], m["fin_s"]) == (108.0, 119.96)
    gemini(passe_1(moment(108, 120)))  # au-delà de la tolérance
    monkeypatch.setattr(analyse, "_duree", lambda chemin: 118.9)
    assert analyse.analyser("v.mp4", "abc")["rejetes"][0]["raison"] == "hors vidéo"


def test_duree_passe_par_le_montage(monkeypatch):
    monkeypatch.setattr(montage, "duree_video", lambda chemin: 42.0)
    assert analyse._duree("v.mp4") == 42.0


def test_erreur_masque_les_autres_secrets(gemini, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "jeton-tres-secret")
    gemini(RuntimeError("refus jeton-tres-secret"))
    message = str(echec())
    assert "jeton-tres-secret" not in message and "RuntimeError" in message


@pytest.fixture
def service(atelier, auth, attendre, gemini, monkeypatch, tmp_path, brut_modele):
    """Le vrai service : seule l'API Gemini est simulée."""
    monkeypatch.setattr(analyse, "_duree", VRAIE_DUREE)
    (tmp_path / "bruts").mkdir()
    (tmp_path / "bruts" / f"{ID}.mp4").write_bytes(brut_modele.read_bytes())
    client = atelier.app.test_client()

    def tache(route, **corps):
        return attendre(client.post(route, json={"video_id": ID, **corps}, headers=auth).get_json()["tache"])
    return tache


def test_branchement_app_analyse_tri_montage(service, gemini, tmp_path):
    gemini(passe_1(moment(1, 2, "rire", 3), moment(2, 10)), passe_2(debut=2.0, fin=10.0))
    plan = service("/analyse")["resultat"]
    assert [(m["n"], m["debut_s"], m["fin_s"]) for m in plan["moments"]] == [(1, 0.0, 10.0)]
    assert [r["raison"] for r in plan["rejetes"]] == ["score"]
    assert json.loads((tmp_path / "plans" / f"{ID}.json").read_text(encoding="utf-8")) == plan
    assert service("/monter", moment=1)["etat"] == "reussie"


def test_cout_d_un_echec_remonte_et_ne_se_recompte_pas(service, gemini, atelier, auth, attendre):
    gemini("a", "b", "c")
    rate = service("/analyse")
    assert (rate["etat"], rate["cout_usd"]) == ("echouee", pytest.approx(4.5))
    atelier.taches._etats.clear()
    assert atelier.app.test_client().get(f"/taches/{rate['id']}", headers=auth).get_json()["cout_usd"] == 4.5

    gemini(passe_1())
    assert service("/analyse")["resultat"]["cout_usd"] == pytest.approx(1.5)
    faux = gemini()
    assert service("/analyse")["resultat"]["cout_usd"] == pytest.approx(1.5)
    assert faux.appels == []


def test_plan_non_ecrit_garde_le_cout(service, gemini, atelier, monkeypatch):
    monkeypatch.setattr(atelier, "chemin_plan", lambda video_id: "/dev/null/plans/x.json")
    gemini(passe_1())
    assert service("/analyse")["cout_usd"] == pytest.approx(1.5)
