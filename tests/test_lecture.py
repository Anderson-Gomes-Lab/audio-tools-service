"""Chaîne de lecture : mesure, identification, passages, hors combat, synthèse, 2e passe. Gemini simulé."""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors

import analyse
import lecture
import montage
from conftest import poser_jour

ID = "abcdefghijk"
CLE = "AIzaCLEDETEST123"
DUREE = 180.0
MESURES = {"vie": [1.0] * 900, "lame": [None] * 900, "alerte": [], "perte": [], "arme_rangee": [],
           "hors_combat": [[100.0, 130.0]], "coups": [], "vaincus": [12.0]}
FLASH, LITE = "gemini-3.8-flash", "gemini-3.5-flash-lite"
IDENTIFICATION = {"armes": ["Épée longue"], "monstres": ["Uth Duna"], "lu_a_l_ecran": "Chasser : Uth Duna"}
RESUME = {"parties": [], "resume": "r", "moment_fort": {"debut": "00:01", "fin": "00:02", "pourquoi": "p"},
          "a_garder": [], "drole": [], "doute": "d"}


def ligne(t, ecran="vu"):
    return {"t": t, "ecran": ecran, "joueur": "j", "monstre": "m", "lame": "blanc", "vie": "haute", "alerte": False,
            "issue": "neutre", "preuve": "p", "position": "contact"}


def clip(debut, fin, titre="t"):
    return {"debut": debut, "fin": fin, "titre": titre, "pourquoi": "p"}


def point(debut="00:20", fin="00:30", deja_vu=False, remarque="il a raté"):
    return {"remarque": remarque, "deja_vu": deja_vu, "debut": debut, "fin": fin, "pourquoi": "script"}


class FauxGemini:
    """Remplace genai.Client : répond selon le schéma demandé et note chaque appel."""

    def __init__(self):
        self.appels, self.effaces, self.envois = [], [], 0
        self.usage = SimpleNamespace(prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=0)
        self.remarques = []
        self.pannes = {}  # rang de l'appel : exception levée
        self.vivant = True
        self.code_perdu = 404  # Gemini répond 403 pour un fichier parti
        self.panne_fichier = None  # exception passagère de files.get
        self.files = SimpleNamespace(upload=self._upload, get=self._get, delete=self.effaces.append)
        self.models = SimpleNamespace(generate_content=self._generer)

    def _upload(self, file, config):
        self.envois += 1
        return SimpleNamespace(name=f"files/n{self.envois}", uri="u", mime_type="video/mp4",
                               state=SimpleNamespace(name="ACTIVE"))

    def _get(self, name):
        if self.panne_fichier:
            raise self.panne_fichier
        if not self.vivant:
            raise errors.ClientError(self.code_perdu, {"error": {"message": "fichier disparu"}})
        return SimpleNamespace(name=name, uri="u", mime_type="video/mp4", state=SimpleNamespace(name="ACTIVE"))

    def _reponse(self, schema, video):
        if schema == lecture.SCHEMA_SYNTHESE:
            return RESUME
        if schema == lecture.SCHEMA_REMARQUES:
            return {"remarques": self.remarques}
        if schema == lecture.SCHEMA_PASSAGE:
            debut, fin = int(video.start_offset[:-1]), int(video.end_offset[:-1])
            return {"lignes": [ligne(lecture.mmss(debut), "relu"), ligne(lecture.mmss(fin - 1), "relu")],
                    "a_garder": [clip(lecture.mmss(debut), lecture.mmss(fin))], "drole": [], "doute": f"doute {debut}"}
        return IDENTIFICATION

    def _generer(self, model, contents, config):
        rang = len(self.appels)
        parts = [] if isinstance(contents, str) else contents
        video = next((p.video_metadata for p in parts if p.video_metadata), None)
        texte = contents if isinstance(contents, str) else contents[-1].text
        self.appels.append(SimpleNamespace(modele=model, video=video, texte=texte, config=config))
        if rang in self.pannes:
            raise self.pannes[rang]
        reponse = self._reponse(config.response_schema, video)
        return SimpleNamespace(text=json.dumps(reponse), usage_metadata=self.usage)

    def selon(self, schema):
        return [a for a in self.appels if a.config.response_schema == schema]


@pytest.fixture
def chaine(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", CLE)
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.delenv("PLAFOND_GEMINI_MOIS_USD", raising=False)
    monkeypatch.setattr(analyse, "PAUSE_FICHIER_S", 0)
    monkeypatch.setattr(montage, "duree_video", lambda chemin: DUREE)
    monkeypatch.setattr(lecture, "images_espacees", lambda brut, duree: [])
    mesures = []

    def mesurer(chemin):
        mesures.append(chemin)
        return MESURES

    def faits(valeurs, arme=None, debut=None, fin=None):
        return f"\n# Faits {arme} {debut} {fin}\n"
    monkeypatch.setitem(sys.modules, "mesure_ecran", SimpleNamespace(mesurer=mesurer, faits=faits))
    faux = FauxGemini()
    monkeypatch.setattr(analyse.genai, "Client", lambda api_key, http_options=None: faux)
    brut = tmp_path / "bruts" / f"{ID}.mp4"
    brut.parent.mkdir()
    brut.write_bytes(b"video")
    return SimpleNamespace(faux=faux, brut=brut, dossier=str(tmp_path / "lectures"), media=tmp_path, mesures=mesures)


def lire_video(c):
    return lecture.analyser(ID, str(c.brut), c.dossier)


def corriger(c, note="remarque"):
    lecture.mettre_en_attente(ID, note, c.dossier)
    return lecture.contexte(ID, str(c.brut), c.dossier)


def sur_disque(c, suffixe="json"):
    return json.loads((Path(c.dossier) / f"{ID}.{suffixe}").read_text(encoding="utf-8"))


def fichier_vieux_de(c, heures):
    date_envoi = datetime.now(timezone.utc) - timedelta(hours=heures)
    (Path(c.dossier) / f"{ID}.fichier.json").write_text(json.dumps({"nom": "files/n1", "date": date_envoi.isoformat()}))


@pytest.mark.parametrize("debut, fin, attendu", [
    (0, 84, [(0, 45), (39, 84)]),
    (10, 30, [(10, 30)]),
    (0, 100, [(0, 37), (31, 69), (63, 100)]),
])
def test_decouper(debut, fin, attendu):
    assert lecture.decouper(debut, fin) == attendu


def test_decouper_trente_minutes_reste_dans_les_reglages():
    passages = lecture.decouper(0, 1800)
    assert len(passages) in range(31, 47)
    assert all(b - a <= 45 for a, b in passages)
    assert all(suite[0] < avant[1] for avant, suite in zip(passages, passages[1:]))


def test_garder_centre_ne_compte_chaque_moment_qu_une_fois():
    lignes = [ligne(t) for t in ("00:10", "00:12", "00:13", "00:50", "00:52", "00:53")]
    assert [x["t"] for x in lecture.garder_centre(lignes, 10, 55, True, False)] == ["00:10", "00:12", "00:13", "00:50"]
    assert [x["t"] for x in lecture.garder_centre(lignes, 10, 55, False, True)] == ["00:13", "00:50", "00:52", "00:53"]


def test_garder_centre_ecarte_un_temps_illisible():
    assert lecture.garder_centre([ligne("bientôt")], 0, 45, True, True) == []


def test_plages_de_combat_entre_les_plages_hors_combat():
    deux = {"hors_combat": [[0.0, 69.2], [131.2, 159.4]]}
    assert lecture.plages_combat(deux, 299.8) == [(69.2, 131.2), (159.4, 299.8)]
    assert lecture.plages_combat({"hors_combat": []}, 134.4) == [(0.0, 134.4)]
    assert lecture.plages_combat({"hors_combat": [[3.0, 50.0]]}, 52.0) == []  # moins de 5 s : pas un combat


@pytest.mark.parametrize("texte, attendu", [
    ("04:05", 245), ("61:40", 3700), ("1:01:40", 3700), ("01:01:40", 3700), ("00:12.5", 12.5), ("2:00:00", 7200),
    ("1:75:00", -1), ("bientôt", -1),
])
def test_temps_lus_au_dela_d_une_heure(texte, attendu):
    assert analyse.secondes(texte) == attendu


def test_mmss_au_dela_d_une_heure_se_relit():
    assert lecture.mmss(3700) == "61:40"
    assert analyse.secondes(lecture.mmss(3700)) == 3700


@pytest.mark.parametrize("duree, attendu", [(60, 8), (960, 8), (1200, 10), (1800, 15), (7200, 16)])
def test_nombre_d_images_d_identification(duree, attendu):
    assert lecture.nombre_images(duree) == attendu


def test_images_vraiment_extraites(brut_modele):
    images = lecture.images_espacees(str(brut_modele), 12.0)
    assert len(images) == 8
    assert images[0].inline_data.mime_type == "image/jpeg" and images[0].inline_data.data[:2] == b"\xff\xd8"


def test_fichiers_embarques():
    armes, monstres = lecture.fiches_de("armes"), lecture.fiches_de("monstres")
    assert (len(armes), len(monstres)) == (14, 34)
    assert "Épée longue" in armes and "Uth Duna" in monstres
    for nom in ("identifier", "passage", "hors-combat", "synthese", "contexte"):
        assert (lecture.LECTURE / f"consigne-{nom}.md").is_file()
    for nom in ("ecran.md", "jeu.md", "commandes-par-arme.md", "signes/sortir-l-arme.png"):
        assert (lecture.CONNAISSANCE / nom).is_file()
    assert not list(lecture.LECTURE.rglob("*.py")) and not (lecture.LECTURE / "__init__.py").exists()
    assert "COPY lecture/ lecture/" in (Path(lecture.__file__).parent / "Dockerfile").read_text(encoding="utf-8")


def test_fiches_choisies_seulement_celles_de_la_video():
    texte = lecture.fiches_choisies(IDENTIFICATION)
    assert "# Épée longue" in texte and "# Uth Duna" in texte
    assert "# Rathalos" not in texte and "# Grande épée" not in texte
    assert all((lecture.CONNAISSANCE / f).read_text(encoding="utf-8") in texte for f in lecture.COMMUNES)


def test_exemple_ne_montre_jamais_la_video_analysee():
    autre, sienne = lecture.exemple("zzzzzzzzzzz"), lecture.exemple("mcKcHx3qbFQ")
    assert autre.count("|") > sienne.count("|") > 0
    assert "## Moment par moment" not in autre and "dictée" not in autre.lower()


def test_chaine_complete(chaine):
    sortie = lire_video(chaine)
    champs = {"video_id", "modele", "cout_usd", "identification", "lignes", "parties", "resume", "moment_fort",
              "a_garder", "drole", "doute", "doutes_lecture", "faits", "duree_s"}
    assert set(sortie) == champs and sortie["video_id"] == ID and sortie["modele"] == FLASH
    assert sortie["identification"] == IDENTIFICATION
    assert sortie == sur_disque(chaine)
    assert chaine.faux.envois == 1 and len(chaine.faux.appels) == 8  # identification, 5 passages, hors combat, synthèse
    temps = [analyse.secondes(x["t"]) for x in sortie["lignes"]]
    assert temps == sorted(temps)
    assert sortie["doutes_lecture"][-1] == "doute 97"
    assert len(sortie["a_garder"]) == 0  # c'est la synthèse qui tranche
    assert sortie["faits"].startswith("\n# Faits Épée longue None None")


def test_reglages_de_chaque_etape(chaine):
    lire_video(chaine)
    premier, *_ = chaine.faux.appels
    assert premier.modele == LITE and "commandes" in premier.texte.lower() and "Chasser" in premier.texte
    passages = chaine.faux.selon(lecture.SCHEMA_PASSAGE)
    combats, hors = passages[:5], passages[5]
    for appel in combats:
        assert appel.modele == FLASH and appel.video.fps == 1
        assert appel.config.media_resolution.name == "MEDIA_RESOLUTION_HIGH" and appel.config.thinking_config is None
        assert "# Fiches" in appel.texte and "# Exemple" in appel.texte
    debut, fin = combats[1].video.start_offset, combats[1].video.end_offset
    assert (debut, fin) == ("31s", "69s")
    assert "Le passage va de 00:31 à 01:09" in combats[1].texte and "# Faits Épée longue 31 69" in combats[1].texte
    assert (hors.video.start_offset, hors.video.end_offset) == ("97s", "130s")  # 3 s d'élan
    assert hors.config.media_resolution.name == "MEDIA_RESOLUTION_LOW"
    assert hors.config.thinking_config.thinking_level.name == "LOW"
    assert "# Fiches" not in hors.texte and "Le moment va de 01:37 à 02:10" in hors.texte
    synthese = chaine.faux.selon(lecture.SCHEMA_SYNTHESE)[0]
    assert synthese.video is None and synthese.config.thinking_config.thinking_level.name == "LOW"
    assert "# Faits Épée longue None None" in synthese.texte and "# Script" in synthese.texte


def test_lignes_du_recouvrement_ecartees_mais_pas_les_bords(chaine):
    temps = [x["t"] for x in lire_video(chaine)["lignes"]]
    assert "00:00" in temps and "02:59" in temps  # bords des plages de combat
    assert "00:36" not in temps  # fin du 1er passage, dans le recouvrement


def test_autre_arme_passee_a_la_mesure(chaine, monkeypatch):
    monkeypatch.setattr(lecture.analyse, "generer", lambda *a, **k: {**RESUME, "armes": ["Grande épée"], "monstres": [],
                                                                     "lu_a_l_ecran": "", "lignes": [], "a_garder": []})
    monkeypatch.setitem(MESURES, "hors_combat", [])
    assert lire_video(chaine)["faits"].startswith("\n# Faits Grande épée")


def test_lecture_deja_faite_ne_repaie_pas(chaine):
    premiere = lire_video(chaine)
    chaine.faux.appels.clear()
    chaine.brut.unlink()
    assert lire_video(chaine) == {**premiere, "cout_usd": 0}  # rien de neuf à annoncer
    assert sur_disque(chaine) == premiere  # le fichier garde le cumul
    assert chaine.faux.appels == [] and chaine.faux.envois == 1


def test_sans_brut_rien_n_est_depense(chaine):
    chaine.brut.unlink()
    lecture.mettre_en_attente(ID, "ma note", chaine.dossier)
    with pytest.raises(analyse.ErreurAnalyse, match=f"^brut_requis: brut absent pour {ID}"):
        lire_video(chaine)
    assert chaine.faux.appels == [] and chaine.mesures == []
    assert (Path(chaine.dossier) / f"{ID}.note.txt").exists()


def test_fichier_gemini_garde_avec_son_nom_et_sa_date(chaine):
    lire_video(chaine)
    note = sur_disque(chaine, "fichier.json")
    assert chaine.faux.effaces == [] and note["nom"] == "files/n1"
    assert datetime.now(timezone.utc) - datetime.fromisoformat(note["date"]) < timedelta(minutes=1)


def test_relance_apres_panne_reprend_le_fichier_et_les_mesures(chaine):
    chaine.faux.pannes[3] = RuntimeError("panne")
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    assert erreur.value.cout_usd > 0
    assert chaine.faux.effaces == []
    chaine.faux.pannes.clear()
    lire_video(chaine)
    assert chaine.faux.envois == 1 and chaine.mesures == [str(chaine.brut)]


def test_erreur_sans_cle_ni_url(chaine, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "jeton-tres-secret")
    chaine.faux.pannes[0] = RuntimeError(f"échec https://x.googleapis.com/v1?key={CLE} clé {CLE} jeton-tres-secret")
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    message = str(erreur.value)
    assert CLE not in message and "googleapis" not in message and "jeton-tres-secret" not in message
    assert erreur.value.__cause__ is None and erreur.value.__suppress_context__


def test_traitement_distant_echoue(chaine, monkeypatch):
    rate = SimpleNamespace(name="files/n1", state=SimpleNamespace(name="FAILED"))
    monkeypatch.setattr(chaine.faux.files, "upload", lambda file, config: rate)
    with pytest.raises(analyse.ErreurAnalyse, match="traiter la vidéo"):
        lire_video(chaine)
    assert not (Path(chaine.dossier) / f"{ID}.fichier.json").exists()


def test_lecture_non_ecrite_garde_le_cout(chaine, monkeypatch):
    vrai = lecture.taches.ecrire_json

    def plein(chemin, donnees):
        if chemin.endswith(f"{ID}.json"):
            raise OSError("disque plein")
        vrai(chemin, donnees)
    monkeypatch.setattr(lecture.taches, "ecrire_json", plein)
    with pytest.raises(analyse.ErreurAnalyse) as erreur:
        lire_video(chaine)
    assert erreur.value.cout_usd > 0


def test_temps_passe_une_heure_garde_dans_le_script(chaine, monkeypatch):
    monkeypatch.setattr(montage, "duree_video", lambda chemin: 3800.0)
    monkeypatch.setitem(MESURES, "hors_combat", [[0.0, 3700.0]])
    monkeypatch.setattr(lecture, "PASSAGE_S", 200)
    sortie = lire_video(chaine)
    assert "61:40" in [x["t"] for x in sortie["lignes"]]


@pytest.mark.parametrize("jour, cout", [(date(2026, 12, 31), 1.13), (date(2027, 1, 1), 2.18)])
def test_arret_a_dix_centimes_par_minute(chaine, monkeypatch, jour, cout):  # 10 min : 1,00 $, puis 2,00 $ en 2027
    poser_jour(monkeypatch, jour)
    monkeypatch.setattr(montage, "duree_video", lambda chemin: 600.0)
    chaine.faux.usage = SimpleNamespace(prompt_token_count=100_000, candidates_token_count=20_000,
                                        thoughts_token_count=0)
    with pytest.raises(analyse.ErreurAnalyse, match="limite") as erreur:
        lire_video(chaine)
    assert len(chaine.faux.appels) == 8
    assert erreur.value.cout_usd == pytest.approx(cout)
    assert not (Path(chaine.dossier) / f"{ID}.json").exists()


def test_plafond_du_mois_refuse_avant_de_depenser(chaine, monkeypatch):
    monkeypatch.setenv("PLAFOND_GEMINI_MOIS_USD", "10")
    mois = analyse.date.today().strftime("%Y-%m")
    (chaine.media / "compteur-gemini.json").write_text(json.dumps({"mois": mois, "usd": 10.0}))
    with pytest.raises(analyse.ErreurAnalyse, match="plafond") as erreur:
        lire_video(chaine)
    assert erreur.value.cout_usd == 0
    assert chaine.faux.appels == [] and chaine.faux.envois == 0 and chaine.mesures == []


def test_plafond_atteint_en_cours_de_route_arrete_la_chaine(chaine, monkeypatch):
    monkeypatch.setenv("PLAFOND_GEMINI_MOIS_USD", "0.0001")
    with pytest.raises(analyse.ErreurAnalyse, match="plafond") as erreur:
        lire_video(chaine)
    assert len(chaine.faux.appels) == 1 and erreur.value.cout_usd > 0


def test_compteur_du_mois_additionne_les_deux_passes(chaine):
    premiere = lire_video(chaine)
    seconde = corriger(chaine)
    compteur = json.loads((chaine.media / "compteur-gemini.json").read_text())
    assert compteur["mois"] == analyse.date.today().strftime("%Y-%m")
    assert compteur["usd"] == pytest.approx(premiere["cout_usd"] + seconde["cout_usd"])


def test_branchement_de_bout_en_bout(chaine, atelier, auth, attendre):
    client = atelier.app.test_client()

    def tache(route, **corps):
        return attendre(client.post(route, json={"video_id": ID, **corps}, headers=auth).get_json()["tache"])

    lue = tache("/analyse")["resultat"]
    assert client.get(f"/lectures/{ID}", headers=auth).get_json() == lue
    assert tache("/contexte", note="le cri")["etat"] == "reussie"
    assert client.post("/nettoyer", json={"video_id": ID}, headers=auth).status_code == 200
    assert not chaine.brut.exists()
    assert client.get(f"/lectures/{ID}", headers=auth).status_code == 200
    assert sur_disque(chaine, "fichier.json") and sur_disque(chaine, "mesures.json")
    assert "lot 2" in tache("/monter", moment=1)["erreur"]


def test_cout_d_un_echec_remonte_par_la_tache(chaine, atelier, auth, attendre):
    client = atelier.app.test_client()
    chaine.faux.pannes[2] = RuntimeError("panne")
    etat = attendre(client.post("/analyse", json={"video_id": ID}, headers=auth).get_json()["tache"])
    assert etat["etat"] == "echouee" and etat["cout_usd"] > 0
