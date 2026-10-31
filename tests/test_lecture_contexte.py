"""2e passe : note en attente, relecture des seuls points non vus, fichier Gemini de 48 h. Gemini simulé."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from google.genai import errors

import analyse
import lecture
import test_lecture as commun
from test_lecture import ID, IDENTIFICATION, corriger, fichier_vieux_de, lire_video, point, sur_disque

chaine = commun.chaine


def note_sur_disque(c):
    return (Path(c.dossier) / f"{ID}.note.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("armes, attendu", [
    (["Épée longue"], "Épée longue"), (["Grande épée"], "Grande épée"),
    (["Épée longue", "Grande épée"], None), ([], None),  # le fait de la lame ne serait pas sûr
])
def test_arme_donnee_a_la_mesure(armes, attendu):
    assert lecture._arme({"armes": armes}) == attendu


@pytest.mark.parametrize("erreur", [ValueError("taille 1280x720"), RuntimeError("ffmpeg a échoué")])
def test_erreur_de_mesure_traduite_sans_depense(chaine, monkeypatch, erreur):
    def rate(chemin):
        raise erreur
    monkeypatch.setattr(sys.modules["mesure_ecran"], "mesurer", rate)
    with pytest.raises(analyse.ErreurAnalyse, match=str(erreur)) as echec:
        lire_video(chaine)
    assert echec.value.cout_usd == 0 and chaine.faux.appels == [] and chaine.faux.envois == 0


def test_note_attend_la_fin_de_la_premiere_passe(chaine):
    assert lecture.mettre_en_attente(ID, "premier message", chaine.dossier) is True
    assert lecture.mettre_en_attente(ID, "second message", chaine.dossier) is True
    chaine.faux.remarques = [point(deja_vu=True)]
    sortie = lire_video(chaine)
    tri = chaine.faux.selon(lecture.SCHEMA_REMARQUES)[0]
    assert "premier message" in tri.texte and "second message" in tri.texte
    assert not (Path(chaine.dossier) / f"{ID}.note.txt").exists()
    assert len(chaine.faux.selon(lecture.SCHEMA_SYNTHESE)) == 2
    assert sortie == sur_disque(chaine)


def test_note_tardive_ecrite_avant_tout(chaine):
    lire_video(chaine)
    assert lecture.mettre_en_attente(ID, "tard", chaine.dossier) is False  # la 2e passe peut partir
    assert note_sur_disque(chaine) == "tard\n"


def test_note_ratee_garde_la_lecture_et_se_rejoue(chaine):
    lecture.mettre_en_attente(ID, "ma note", chaine.dossier)
    chaine.faux.pannes[8] = RuntimeError("panne")  # le tri de la 2e passe
    with pytest.raises(analyse.ErreurAnalyse, match="lecture conservée") as erreur:
        lire_video(chaine)
    premiere = sur_disque(chaine)
    assert erreur.value.cout_usd == premiere["cout_usd"]
    assert (Path(chaine.dossier) / f"{ID}.note.txt").exists()
    chaine.faux.pannes.clear()
    chaine.faux.appels.clear()
    chaine.faux.remarques = []
    rejouee = lire_video(chaine)
    assert len(chaine.faux.appels) == 2  # tri et synthèse, rien de la 1re passe
    # La tâche n'annonce que son tri et sa synthèse : l'échec a déjà annoncé la 1re passe, le fichier cumule.
    assert 0 < rejouee["cout_usd"] < premiere["cout_usd"]
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(premiere["cout_usd"] + rejouee["cout_usd"])
    assert not (Path(chaine.dossier) / f"{ID}.note.txt").exists()


def test_deja_vu_ne_relit_pas_la_video_et_se_passe_du_brut(chaine):
    lire_video(chaine)
    chaine.faux.appels.clear()
    chaine.brut.unlink()
    fichier_vieux_de(chaine, 45)
    chaine.faux.remarques = [point(deja_vu=True)]
    sortie = corriger(chaine, "il a tout bien fait {vraiment}")
    tri, synthese = chaine.faux.appels
    assert tri.video is None and tri.config.thinking_config.thinking_level.name == "LOW"
    assert "« il a tout bien fait {vraiment} »" in tri.texte and tri.texte.rstrip().endswith("preuve : p")
    assert "# Remarque du joueur" in synthese.texte and sortie["identification"] == IDENTIFICATION


def test_relecture_du_seul_point_non_vu(chaine):
    avant = lire_video(chaine)
    chaine.faux.appels.clear()
    chaine.faux.remarques = [point("00:20", "00:30"), point("01:00", "01:10", deja_vu=True)]
    sortie = corriger(chaine)
    relu = chaine.faux.selon(lecture.SCHEMA_PASSAGE)
    assert len(relu) == 1
    assert (relu[0].video.start_offset, relu[0].video.end_offset) == ("17s", "33s")
    assert relu[0].config.media_resolution.name == "MEDIA_RESOLUTION_HIGH"
    assert all(x in relu[0].texte for x in ("« il a raté »", "# Fiches", "# Faits Épée longue 17 33"))
    fenetre = [x for x in sortie["lignes"] if 17 <= analyse.secondes(x["t"]) <= 33]
    assert [x["ecran"] for x in fenetre] == ["relu", "relu"] and "doute 17" in sortie["doutes_lecture"]
    synthese = chaine.faux.selon(lecture.SCHEMA_SYNTHESE)[-1]
    assert "00:17-00:33" in synthese.texte  # le clip relu est proposé à la synthèse
    assert len(sortie["doutes_lecture"]) == len(avant["doutes_lecture"]) + 1


def test_relecture_limitee_a_trente_secondes_et_a_la_video(chaine):
    lire_video(chaine)
    chaine.faux.appels.clear()
    chaine.faux.remarques = [point("00:10", "02:50"), point("02:55", "03:10")]
    corriger(chaine)
    premier, second = chaine.faux.selon(lecture.SCHEMA_PASSAGE)
    assert (premier.video.start_offset, premier.video.end_offset) == ("7s", "43s")
    assert (second.video.start_offset, second.video.end_offset) == ("172s", "180s")


def test_point_illisible_devient_un_doute(chaine):
    lire_video(chaine)
    chaine.faux.remarques = [point("bientôt", "plus tard", remarque="le cri")]
    sortie = corriger(chaine)
    assert sortie["doutes_lecture"][-1] == "Remarque non localisée : le cri"


def test_cout_de_la_deuxieme_passe(chaine):
    premiere = lire_video(chaine)
    chaine.faux.remarques = [point()]
    sortie = corriger(chaine)
    assert 0 < sortie["cout_usd"] < premiere["cout_usd"]  # la tâche n'annonce que ce qu'elle a payé
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(premiere["cout_usd"] + sortie["cout_usd"])


def test_fichier_de_moins_de_46_h_reutilise_sans_brut(chaine):
    lire_video(chaine)
    chaine.brut.unlink()
    fichier_vieux_de(chaine, 45.9)
    chaine.faux.remarques = [point()]
    corriger(chaine)
    assert chaine.faux.envois == 1


def test_fichier_expire_renvoie_le_brut(chaine):
    lire_video(chaine)
    fichier_vieux_de(chaine, 46)
    chaine.faux.remarques = [point()]
    corriger(chaine)
    assert chaine.faux.envois == 2 and sur_disque(chaine, "fichier.json")["nom"] == "files/n2"


def test_date_du_fichier_comptee_depuis_le_debut_de_l_envoi(chaine, monkeypatch):
    vrai, debuts = chaine.faux.files.upload, []

    def envoi(file, config):
        debuts.append(datetime.now(timezone.utc))
        return vrai(file, config)
    monkeypatch.setattr(chaine.faux.files, "upload", envoi)
    lire_video(chaine)
    assert datetime.fromisoformat(sur_disque(chaine, "fichier.json")["date"]) <= debuts[0]


@pytest.mark.parametrize("heures, vivant, code", [(46, True, 404), (1, False, 404), (1, False, 403)])
def test_sans_brut_ni_fichier_rien_n_est_paye(chaine, heures, vivant, code):
    lire_video(chaine)
    chaine.faux.appels.clear()
    chaine.brut.unlink()
    fichier_vieux_de(chaine, heures)
    chaine.faux.vivant, chaine.faux.code_perdu = vivant, code
    chaine.faux.remarques = [point()]
    with pytest.raises(analyse.ErreurAnalyse, match=r"/fetch.*0,13 \$") as erreur:
        corriger(chaine, "ma note")
    assert str(erreur.value).startswith("brut_requis:")
    assert erreur.value.cout_usd == 0 and chaine.faux.appels == []  # vérifié avant le tri
    assert chaine.faux.envois == 1
    assert note_sur_disque(chaine) == "ma note\n"


@pytest.mark.parametrize("panne", [errors.ServerError(503, {"error": {"message": "indisponible"}}),
                                   httpx.ConnectError("réseau coupé"),
                                   errors.ClientError(429, {"error": {"message": "trop d'appels"}})])
@pytest.mark.parametrize("avec_brut", [True, False])
def test_panne_passagere_du_suivi_ne_demande_pas_de_fetch(chaine, panne, avec_brut):
    lire_video(chaine)
    chaine.faux.appels.clear()
    if not avec_brut:
        chaine.brut.unlink()
    chaine.faux.panne_fichier = panne
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        corriger(chaine, "ma note")
    assert "brut_requis" not in str(erreur.value) and "/fetch" not in str(erreur.value)
    assert erreur.value.cout_usd == 0 and chaine.faux.appels == [] and chaine.faux.envois == 1
    assert note_sur_disque(chaine) == "ma note\n"


def test_analyse_rejouee_garde_le_prefixe_brut_requis(chaine):
    lire_video(chaine)
    chaine.brut.unlink()
    fichier_vieux_de(chaine, 49)
    lecture.mettre_en_attente(ID, "ma note", chaine.dossier)
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    assert str(erreur.value).startswith("brut_requis:")


def test_brut_requis_par_la_tache_puis_fetch_et_contexte_jouent_la_note_une_fois(chaine, atelier, auth, attendre):
    client = atelier.app.test_client()
    lire_video(chaine)
    brut = chaine.brut.read_bytes()
    chaine.brut.unlink()
    fichier_vieux_de(chaine, 49)

    def remarque():
        return client.post("/contexte", json={"video_id": ID, "note": "le cri"}, headers=auth).get_json()["tache"]
    etat = attendre(remarque())
    assert etat["etat"] == "echouee" and etat["erreur"].startswith("brut_requis:") and "cout_usd" not in etat
    chaine.brut.write_bytes(brut)  # /fetch de n8n
    chaine.faux.appels.clear()
    assert attendre(remarque())["etat"] == "reussie"
    assert chaine.faux.appels[0].texte.count("le cri") == 1
    assert not (Path(chaine.dossier) / f"{ID}.note.txt").exists()


def test_toutes_les_notes_presentes_sont_jouees_ensemble(chaine):
    lire_video(chaine)
    chaine.faux.appels.clear()
    lecture.mettre_en_attente(ID, "premiere", chaine.dossier)
    lecture.mettre_en_attente(ID, "seconde", chaine.dossier)
    lecture.contexte(ID, str(chaine.brut), chaine.dossier)
    tri, synthese = chaine.faux.appels
    assert all("premiere" in x.texte and "seconde" in x.texte for x in (tri, synthese))
    assert not (Path(chaine.dossier) / f"{ID}.note.txt").exists()


def test_note_recue_pendant_la_deuxieme_passe_reste_pour_la_suivante(chaine, monkeypatch):
    lire_video(chaine)
    chaine.faux.appels.clear()
    vrai = chaine.faux.models.generate_content

    def pendant(model, contents, config):
        if config.response_schema == lecture.SCHEMA_SYNTHESE:
            assert lecture.mettre_en_attente(ID, "tardive", chaine.dossier) is False
        return vrai(model, contents, config)
    monkeypatch.setattr(chaine.faux.models, "generate_content", pendant)
    corriger(chaine, "premiere")
    assert note_sur_disque(chaine) == "tardive\n"  # seule la note jouée est retirée
    monkeypatch.setattr(chaine.faux.models, "generate_content", vrai)
    chaine.faux.appels.clear()
    lire_video(chaine)
    tri = chaine.faux.appels[0]
    assert "tardive" in tri.texte and "premiere" not in tri.texte
    assert not (Path(chaine.dossier) / f"{ID}.note.txt").exists()


@pytest.mark.parametrize("rang", [0, 1])  # le tri, ou la synthèse de la 2e passe
def test_panne_de_la_deuxieme_passe_ne_perd_pas_la_note(chaine, rang):
    lire_video(chaine)
    chaine.faux.appels.clear()
    chaine.faux.pannes[rang] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse):
        corriger(chaine, "ma note")
    assert note_sur_disque(chaine) == "ma note\n"
    chaine.faux.pannes.clear()
    chaine.faux.appels.clear()
    lire_video(chaine)  # après un redémarrage : seul le disque reste
    assert "ma note" in chaine.faux.appels[0].texte
    assert not (Path(chaine.dossier) / f"{ID}.note.txt").exists()


def test_lecture_corrigee_non_ecrite_garde_la_note(chaine, monkeypatch):
    lire_video(chaine)
    vrai = lecture.taches.ecrire_json

    def plein(chemin, donnees):
        if chemin.endswith(f"{ID}.json"):
            raise OSError("disque plein")
        vrai(chemin, donnees)
    monkeypatch.setattr(lecture.taches, "ecrire_json", plein)
    with pytest.raises(analyse.ErreurAnalyse, match="disque plein"):
        corriger(chaine, "ma note")
    assert note_sur_disque(chaine) == "ma note\n"


def test_note_ecrite_meme_si_la_tache_echoue(chaine, atelier, auth, attendre):
    lire_video(chaine)
    chaine.faux.appels.clear()
    chaine.faux.pannes[0] = RuntimeError("panne")
    corps = {"video_id": ID, "note": "le cri"}
    etat = attendre(atelier.app.test_client().post("/contexte", json=corps, headers=auth).get_json()["tache"])
    assert etat["etat"] == "echouee" and note_sur_disque(chaine) == "le cri\n"


def test_note_renvoyee_par_n8n_n_est_pas_doublee(chaine):
    assert lecture.mettre_en_attente(ID, "le cri", chaine.dossier) is True
    assert lecture.mettre_en_attente(ID, "autre", chaine.dossier) is True
    assert lecture.mettre_en_attente(ID, "le cri", chaine.dossier) is True
    assert note_sur_disque(chaine) == "le cri\nautre\n"


def test_tache_sans_note_restante_n_annonce_rien(chaine):
    premiere = lire_video(chaine)
    chaine.faux.appels.clear()
    sortie = lecture.contexte(ID, str(chaine.brut), chaine.dossier)
    assert chaine.faux.appels == [] and sortie["cout_usd"] == 0
    assert sur_disque(chaine) == premiere


def test_relecture_vide_garde_le_script_de_sa_fenetre(chaine, monkeypatch):
    avant = lire_video(chaine)
    vrai = chaine.faux._reponse
    monkeypatch.setattr(chaine.faux, "_reponse", lambda schema, video: (
        {**vrai(schema, video), "lignes": []} if schema == lecture.SCHEMA_PASSAGE else vrai(schema, video)))
    chaine.faux.remarques = [point("00:00", "00:05")]
    assert any(analyse.secondes(x["t"]) <= 8 for x in avant["lignes"])  # la fenêtre a bien du script à perdre
    assert corriger(chaine)["lignes"] == avant["lignes"]


@pytest.mark.parametrize("relues, attendu", [
    (["02:00", "bientôt"], None),  # rien dans la fenêtre : le script ne bouge pas
    (["00:20", "02:00"], ["00:20"]),  # les lignes hors fenêtre sont écartées
])
def test_relecture_filtree_comme_la_premiere_passe(chaine, monkeypatch, relues, attendu):
    avant = lire_video(chaine)
    vrai = chaine.faux._reponse
    monkeypatch.setattr(chaine.faux, "_reponse", lambda schema, video: (
        {**vrai(schema, video), "lignes": [commun.ligne(t, "relu") for t in relues]}
        if schema == lecture.SCHEMA_PASSAGE else vrai(schema, video)))
    chaine.faux.remarques = [point("00:20", "00:30")]
    sortie = corriger(chaine)
    if attendu is None:
        assert sortie["lignes"] == avant["lignes"]
    else:
        assert [x["t"] for x in sortie["lignes"] if x["ecran"] == "relu" and x not in avant["lignes"]] == attendu
        assert not any(analyse.secondes(x["t"]) == 120 and x not in avant["lignes"] for x in sortie["lignes"])
        assert not any(17 <= analyse.secondes(x["t"]) < 33 and x in avant["lignes"] for x in sortie["lignes"])
