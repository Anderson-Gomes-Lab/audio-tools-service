"""Une panne ne fait rien repayer : relevé après chaque appel, garde de coût cumulée, coût annoncé. Gemini simulé."""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import analyse
import lecture
import montage
import reprise
import test_lecture as commun
from conftest import poser_jour
from test_lecture import FLASH, ID, LITE, corriger, fichier_vieux_de, lire_video, point, sur_disque

chaine = commun.chaine
GROS = SimpleNamespace(prompt_token_count=100_000, candidates_token_count=20_000, thoughts_token_count=0)  # 0,08 ; 0,15


def cout(c, appels):
    """Le prix des `appels` premiers appels de la 1re passe : l'identification, puis du 3.8 Flash."""
    prix = [analyse._cout(c.faux.usage, analyse.date.today(), m) for m in (LITE, FLASH)]
    return prix[0] + (appels - 1) * prix[1] if appels else 0


def releve(c):
    return Path(c.dossier) / f"{ID}.releve.json"


def compteur(c):
    return json.loads((c.media / "compteur-gemini.json").read_text())["usd"]


def repartir(c):
    c.faux.pannes.clear()
    c.faux.appels.clear()


@pytest.mark.parametrize("panne", [1, 3, 6, 7])  # 1er passage, milieu des passages, hors combat, synthèse
def test_reprise_ne_repaie_que_ce_qui_manque(chaine, panne):
    chaine.faux.pannes[panne] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    assert erreur.value.cout_usd == pytest.approx(cout(chaine, panne))
    assert json.loads(releve(chaine).read_text())["cout_usd"] == pytest.approx(cout(chaine, panne))

    repartir(chaine)
    sortie = lire_video(chaine)
    assert len(chaine.faux.appels) == 8 - panne
    assert sortie["cout_usd"] == pytest.approx(cout(chaine, 8) - cout(chaine, panne))  # ce que la tâche révèle
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8))  # le fichier cumule
    assert compteur(chaine) == pytest.approx(cout(chaine, 8))
    assert chaine.faux.envois == 1 and not releve(chaine).exists()


def test_chaque_appel_paye_est_sauve_tout_de_suite(chaine):
    chaine.faux.pannes[7] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse):
        lire_video(chaine)
    reponses = json.loads(releve(chaine).read_text())["reponses"]
    assert len(reponses) == 7  # identification, 5 passages, hors combat


def test_lecture_reprise_identique_a_une_lecture_sans_panne(chaine):
    entiere = lire_video(chaine)
    (Path(chaine.dossier) / f"{ID}.json").unlink()
    chaine.faux.pannes[len(chaine.faux.appels) + 4] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse):
        lire_video(chaine)
    repartir(chaine)
    assert lire_video(chaine)["lignes"] == entiere["lignes"]


def test_lecture_non_ecrite_se_reprend_sans_aucun_appel(chaine, monkeypatch):
    vrai = lecture.taches.ecrire_json
    plein = [True]

    def ecrire(chemin, donnees):
        if chemin.endswith(f"{ID}.json") and plein[0]:
            raise OSError("disque plein")
        vrai(chemin, donnees)
    monkeypatch.setattr(lecture.taches, "ecrire_json", ecrire)
    with pytest.raises(analyse.ErreurAnalyse):
        lire_video(chaine)
    assert releve(chaine).exists()  # effacé seulement après l'écriture de la lecture
    plein[0] = False
    repartir(chaine)
    assert lire_video(chaine)["cout_usd"] == 0
    assert chaine.faux.appels == [] and sur_disque(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8))


def test_reponse_mal_formee_comptee_dans_le_releve(chaine, monkeypatch):
    vrai = chaine.faux._reponse
    monkeypatch.setattr(chaine.faux, "_reponse", lambda schema, video: (
        {} if schema == lecture.SCHEMA_PASSAGE else vrai(schema, video)))
    with pytest.raises(analyse.ErreurAnalyse, match="mal formé"):
        lire_video(chaine)
    assert json.loads(releve(chaine).read_text())["cout_usd"] == pytest.approx(cout(chaine, 4))
    monkeypatch.setattr(chaine.faux, "_reponse", vrai)
    repartir(chaine)
    lire_video(chaine)
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(cout(chaine, 4 + 7))
    assert compteur(chaine) == pytest.approx(cout(chaine, 4 + 7))


def test_redemarrage_la_tache_suivante_revele_tout_ce_qui_n_a_pas_ete_annonce(chaine):
    chaine.faux.pannes[3] = KeyboardInterrupt()  # le processus tombe : aucun échec n'a annoncé le coût
    with pytest.raises(KeyboardInterrupt):
        lire_video(chaine)
    repartir(chaine)
    sortie = lire_video(chaine)
    assert len(chaine.faux.appels) == 5
    assert sortie["cout_usd"] == pytest.approx(cout(chaine, 8))


def test_appel_paye_mais_inutilisable_compte_meme_si_le_processus_tombe(chaine, monkeypatch):
    vrai = chaine.faux._reponse
    monkeypatch.setattr(chaine.faux, "_reponse", lambda schema, video: (
        {} if schema == lecture.SCHEMA_PASSAGE else vrai(schema, video)))
    chaine.faux.pannes[2] = KeyboardInterrupt()  # la relance de la réponse mal formée
    with pytest.raises(KeyboardInterrupt):
        lire_video(chaine)
    assert json.loads(releve(chaine).read_text())["cout_usd"] == pytest.approx(cout(chaine, 2))


def test_second_echec_n_annonce_que_le_sien(chaine):
    for panne in (3, 1):  # puis le 2e appel de la reprise
        chaine.faux.pannes[panne] = RuntimeError("panne")
        with pytest.raises(analyse.ErreurAnalyse) as erreur:
            lire_video(chaine)
        repartir(chaine)
    assert erreur.value.cout_usd == pytest.approx(cout(chaine, 4) - cout(chaine, 3))


@pytest.mark.parametrize("jour, duree, engage, appels", [  # 0,08 $ puis 0,15 $ par appel, 0,30 $ en 2027
    (date(2026, 12, 31), 180.0, 0.53, 4),  # 0,30 $ sans plancher
    (date(2027, 1, 1), 134.0, 0.68, 3),  # 0,45 $ sans plancher
])
def test_plancher_d_une_video_courte(chaine, monkeypatch, jour, duree, engage, appels):
    poser_jour(monkeypatch, jour)
    monkeypatch.setattr(montage, "duree_video", lambda chemin: duree)
    chaine.faux.usage = GROS
    with pytest.raises(analyse.ErreurAnalyse, match=r"limite de 0,50 \$") as erreur:
        lire_video(chaine)
    assert len(chaine.faux.appels) == appels and erreur.value.cout_usd == pytest.approx(engage)


def test_garde_cumulee_sur_tous_les_essais(chaine):
    chaine.faux.usage = GROS
    chaine.faux.pannes[2] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    assert erreur.value.cout_usd == pytest.approx(0.23)

    repartir(chaine)
    with pytest.raises(analyse.ErreurAnalyse, match=r"0,53 \$") as erreur:
        lire_video(chaine)
    assert len(chaine.faux.appels) == 2 and erreur.value.cout_usd == pytest.approx(0.30)

    repartir(chaine)
    with pytest.raises(analyse.ErreurAnalyse, match=r"0,53 \$") as erreur:
        lire_video(chaine)
    assert chaine.faux.appels == [] and erreur.value.cout_usd == 0  # refusé avant tout appel
    assert compteur(chaine) == pytest.approx(0.53)  # chaque dépense comptée une seule fois


def test_deuxieme_passe_comptee_dans_la_garde(chaine, monkeypatch):
    monkeypatch.setattr(montage, "duree_video", lambda chemin: 180.0)
    premiere = lire_video(chaine)
    (Path(chaine.dossier) / f"{ID}.json").write_text(json.dumps({**premiere, "cout_usd": 0.5}))
    repartir(chaine)
    with pytest.raises(analyse.ErreurAnalyse, match="limite"):
        corriger(chaine)
    assert chaine.faux.appels == []
    assert (Path(chaine.dossier) / f"{ID}.note.txt").exists()


def test_echec_de_la_deuxieme_passe_garde_son_cout_pour_le_cumul(chaine):
    lire_video(chaine)
    avant = compteur(chaine)
    repartir(chaine)
    chaine.faux.remarques = [point()]
    chaine.faux.pannes[1] = RuntimeError("panne")  # la relecture, après un tri payé
    prix = analyse._cout(chaine.faux.usage, analyse.date.today(), FLASH)
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        corriger(chaine)
    assert erreur.value.cout_usd == pytest.approx(prix)

    repartir(chaine)
    rejouee = lire_video(chaine)
    assert len(chaine.faux.appels) == 3 and rejouee["cout_usd"] == pytest.approx(3 * prix)
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8) + 4 * prix)
    assert compteur(chaine) == pytest.approx(avant + 4 * prix)
    assert not releve(chaine).exists()


def test_releve_oublie_apres_la_lecture_ne_se_recompte_pas(chaine):
    premiere = lire_video(chaine)  # un relevé oublié entre l'écriture de la lecture et son effacement
    releve(chaine).write_text(json.dumps({"cout_usd": premiere["cout_usd"], "annonce_usd": premiere["cout_usd"],
                                          "reponses": {}}))
    prix = analyse._cout(chaine.faux.usage, analyse.date.today(), FLASH)
    assert corriger(chaine)["cout_usd"] == pytest.approx(2 * prix)
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(premiere["cout_usd"] + 2 * prix)


def test_cout_d_un_echec_remonte_et_ne_se_recompte_pas(chaine, atelier, auth, attendre):
    client = atelier.app.test_client()

    def analyser():
        return attendre(client.post("/analyse", json={"video_id": ID}, headers=auth).get_json()["tache"])

    chaine.faux.pannes[2] = RuntimeError("panne")
    rate = analyser()
    assert (rate["etat"], rate["cout_usd"]) == ("echouee", pytest.approx(cout(chaine, 2)))
    atelier.taches._etats.clear()
    assert client.get(f"/taches/{rate['id']}", headers=auth).get_json()["cout_usd"] == rate["cout_usd"]

    repartir(chaine)
    reussi = analyser()["resultat"]["cout_usd"]
    assert reussi == pytest.approx(cout(chaine, 8) - cout(chaine, 2))
    assert rate["cout_usd"] + reussi == pytest.approx(cout(chaine, 8))  # n8n additionne : rien de compté deux fois

    repartir(chaine)
    rejouee = analyser()
    assert rejouee["cout_usd"] == 0 and rejouee["resultat"]["cout_usd"] == 0  # rejouée : rien de neuf à annoncer
    assert client.get(f"/lectures/{ID}", headers=auth).get_json()["cout_usd"] == pytest.approx(cout(chaine, 8))
    assert chaine.faux.appels == []


def test_tache_reussie_annonce_a_la_racine_comme_un_echec(chaine, atelier, auth, attendre):
    client = atelier.app.test_client()
    etat = attendre(client.post("/analyse", json={"video_id": ID}, headers=auth).get_json()["tache"])
    assert etat["cout_usd"] == pytest.approx(cout(chaine, 8))  # le seul champ que n8n lit


def tomber_a_la_cloture(monkeypatch):
    """Le processus tombe après l'écriture de la lecture, avant la fin de la tâche."""
    vrai = reprise.Releve.clore

    def tombe(suivi):
        raise KeyboardInterrupt
    monkeypatch.setattr(reprise.Releve, "clore", tombe)
    return vrai


@pytest.mark.parametrize("rejeu", [lire_video, lambda c: lecture.contexte(ID, str(c.brut), c.dossier)])
def test_releve_reste_avec_la_lecture_annonce_son_reste_une_fois(chaine, monkeypatch, rejeu):
    chaine.faux.pannes[3] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    annonce = erreur.value.cout_usd
    repartir(chaine)
    vrai = tomber_a_la_cloture(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        lire_video(chaine)
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8)) and releve(chaine).exists()
    assert json.loads(releve(chaine).read_text())["annonce_usd"] == pytest.approx(annonce)  # survit sur le disque

    monkeypatch.setattr(reprise.Releve, "clore", vrai)
    repartir(chaine)
    assert rejeu(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8) - annonce)
    assert not releve(chaine).exists() and chaine.faux.appels == []
    assert rejeu(chaine)["cout_usd"] == 0
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8))


def test_coupure_pendant_la_note_de_la_meme_tache_ne_perd_pas_la_premiere_passe(chaine):
    lecture.mettre_en_attente(ID, "ma note", chaine.dossier)
    chaine.faux.pannes[8] = KeyboardInterrupt()  # /analyse tombe entre sa 1re passe et le tri de la note
    with pytest.raises(KeyboardInterrupt):
        lire_video(chaine)
    assert releve(chaine).exists()
    repartir(chaine)
    prix = analyse._cout(chaine.faux.usage, analyse.date.today(), FLASH)
    assert lire_video(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8) + 2 * prix)  # tri et synthèse de la note
    assert sur_disque(chaine)["cout_usd"] == pytest.approx(cout(chaine, 8) + 2 * prix)


def test_echec_de_la_note_de_la_meme_tache_annonce_toute_la_premiere_passe(chaine):
    lecture.mettre_en_attente(ID, "ma note", chaine.dossier)
    chaine.faux.pannes[8] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    assert erreur.value.cout_usd == pytest.approx(cout(chaine, 8))
    repartir(chaine)
    with pytest.raises(analyse.ErreurAnalyse) as seconde:  # brut et fichier perdus : rien de neuf à annoncer
        chaine.brut.unlink()
        fichier_vieux_de(chaine, 49)
        lire_video(chaine)
    assert str(seconde.value).startswith("brut_requis:") and seconde.value.cout_usd == 0


@pytest.mark.parametrize("plafond", [None, "0.0001"])
def test_refus_de_cout_sort_avant_le_controle_du_brut(chaine, monkeypatch, plafond):
    premiere = lire_video(chaine)
    if plafond:
        monkeypatch.setenv("PLAFOND_GEMINI_MOIS_USD", plafond)
    else:
        (Path(chaine.dossier) / f"{ID}.json").write_text(json.dumps({**premiere, "cout_usd": 0.5}))
    repartir(chaine)
    chaine.brut.unlink()
    fichier_vieux_de(chaine, 49)
    chaine.faux.panne_fichier = RuntimeError("le fichier ne devait pas être consulté")
    with pytest.raises(analyse.ErreurAnalyse, match="limite|plafond") as erreur:
        corriger(chaine)
    assert "brut_requis" not in str(erreur.value) and chaine.faux.appels == []


def test_reprise_sans_appel_a_payer_finit_au_dessus_de_la_limite(chaine, monkeypatch):
    vrai = lecture.taches.ecrire_json

    def plein(chemin, donnees):
        if chemin.endswith(f"{ID}.json"):
            raise OSError("disque plein")
        vrai(chemin, donnees)
    monkeypatch.setattr(lecture.taches, "ecrire_json", plein)
    with pytest.raises(analyse.ErreurAnalyse):
        lire_video(chaine)
    monkeypatch.setattr(lecture.taches, "ecrire_json", vrai)
    garde = json.loads(releve(chaine).read_text())
    releve(chaine).write_text(json.dumps({**garde, "cout_usd": 0.6, "annonce_usd": 0.6}))  # au-dessus de 0,50 $
    repartir(chaine)
    assert lire_video(chaine)["cout_usd"] == 0
    assert chaine.faux.appels == [] and sur_disque(chaine)["cout_usd"] == 0.6 and not releve(chaine).exists()
