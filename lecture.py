"""Lecture d'une chasse : mesure de l'écran, identification, passages, hors combat, synthèse, 2e passe.

La lecture et ce qui sert à la 2e passe vivent dans /media/lectures, que /nettoyer ne touche pas.
Une panne ne fait rien repayer et ne perd aucune note : voir reprise.py.
"""

import math
import os
import re
import subprocess
from pathlib import Path

from google.genai import types

import analyse
import montage
import reprise
import taches
from reprise import chemin as _chemin
from reprise import lire_json as _lire_json
from reprise import mettre_en_attente  # noqa: F401 (app.py l'appelle ici)

LECTURE = Path(__file__).with_name("lecture")
CONNAISSANCE = LECTURE / "connaissance"
COMMUNES = ("ecran.md", "jeu.md")
EXEMPLES = {"mcKcHx3qbFQ": "lecture-anderson.md", "vNhm1knSwUs": "lecture-anderson-5min.md"}
IMAGES_MIN, IMAGES_MAX, SECONDES_PAR_IMAGE = 8, 16, 120
PASSAGE_S, RECOUVREMENT_S, COMBAT_MIN_S, ELAN_S = 45, 6, 5, 3
IMAGES_PAR_S = 1
RELECTURE_MAX_S = 30  # la consigne de la 2e passe, tenue même si Gemini vise plus large
HAUT, BAS = types.MediaResolution.MEDIA_RESOLUTION_HIGH, types.MediaResolution.MEDIA_RESOLUTION_LOW
FAIBLE = types.ThinkingLevel.LOW

TEXTE = {"type": "STRING"}
LIGNE = {
    "type": "OBJECT",
    "properties": {
        "t": TEXTE, "ecran": TEXTE, "joueur": TEXTE, "monstre": TEXTE,
        "lame": {"type": "STRING", "enum": ["vide", "blanc", "jaune", "rouge", "illisible"]},
        "vie": {"type": "STRING", "enum": ["haute", "moyenne", "basse", "quasi nulle", "illisible"]},
        "alerte": {"type": "BOOLEAN"},
        "issue": {"type": "STRING", "enum": ["reussi", "rate", "subi", "neutre"]},
        "preuve": TEXTE,
        "position": {"type": "STRING", "enum": ["contact", "distance", "plus bas", "plus haut", "inconnue"]},
    },
    "required": ["t", "ecran", "joueur", "monstre", "lame", "vie", "alerte", "issue", "preuve", "position"],
}
CLIP = {
    "type": "OBJECT",
    "properties": {"debut": TEXTE, "fin": TEXTE, "titre": TEXTE, "pourquoi": TEXTE},
    "required": ["debut", "fin", "titre", "pourquoi"],
}
CLIPS = {"type": "ARRAY", "items": CLIP}
SCHEMA_PASSAGE = {
    "type": "OBJECT",
    "properties": {"lignes": {"type": "ARRAY", "items": LIGNE}, "a_garder": CLIPS, "drole": CLIPS, "doute": TEXTE},
    "required": ["lignes", "a_garder", "drole", "doute"],
}
PARTIE = {
    "type": "OBJECT",
    "properties": {
        "debut": TEXTE, "fin": TEXTE, "titre": TEXTE, "ce_qui_se_joue": TEXTE,
        "domine": {"type": "STRING", "enum": ["joueur", "monstre", "equilibre", "personne"]}, "issue": TEXTE,
    },
    "required": ["debut", "fin", "titre", "ce_qui_se_joue", "domine", "issue"],
}
SCHEMA_SYNTHESE = {
    "type": "OBJECT",
    "properties": {
        "parties": {"type": "ARRAY", "items": PARTIE},
        "resume": TEXTE,
        "moment_fort": {"type": "OBJECT", "properties": {"debut": TEXTE, "fin": TEXTE, "pourquoi": TEXTE},
                        "required": ["debut", "fin", "pourquoi"]},
        "a_garder": CLIPS, "drole": CLIPS, "doute": TEXTE,
    },
    "required": ["parties", "resume", "moment_fort", "a_garder", "drole", "doute"],
}
SCHEMA_REMARQUES = {
    "type": "OBJECT",
    "properties": {"remarques": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"remarque": TEXTE, "deja_vu": {"type": "BOOLEAN"}, "debut": TEXTE, "fin": TEXTE,
                       "pourquoi": TEXTE},
        "required": ["remarque", "deja_vu", "debut", "fin", "pourquoi"],
    }}},
    "required": ["remarques"],
}


def mmss(temps):
    return f"{int(temps // 60):02d}:{int(temps % 60):02d}"


def _texte(chemin):
    return chemin.read_text(encoding="utf-8")


def fiches_de(dossier):
    """Le nom d'une fiche est sa première ligne, sans la parenthèse."""
    return {_texte(f).splitlines()[0][2:].split(" (")[0]: f for f in sorted((CONNAISSANCE / dossier).glob("*.md"))}


def fiches_choisies(trouve):
    armes, monstres = fiches_de("armes"), fiches_de("monstres")
    choisies = [CONNAISSANCE / f for f in COMMUNES]
    choisies += [armes[a] for a in trouve["armes"]] + [monstres[m] for m in trouve["monstres"]]
    return "\n\n".join(_texte(f) for f in choisies)


def exemple(video_id):
    """Ses lectures des autres vidéos montrent comment il juge, sans rien dire de celle-ci."""
    tableaux = [re.search(r"## Moment par moment\n(.*?)\n## ", _texte(LECTURE / f), re.S).group(1).strip()
                for cle, f in EXEMPLES.items() if cle != video_id]
    return ("\n\n# Exemple\n\nUn joueur expérimenté a lu une autre chasse comme ceci. Rien de cet exemple n'est dans "
            "ta vidéo : ne recopie aucun fait, reprends sa façon de juger.\n\n" + "\n\n".join(tableaux))


def plages_combat(mesures, duree):
    bornes = [0.0, *(b for plage in mesures["hors_combat"] for b in plage), duree]
    return [(a, b) for a, b in zip(bornes[::2], bornes[1::2]) if b - a >= COMBAT_MIN_S]


def decouper(debut, fin):
    """Des passages qui se chevauchent : une action coupée à la frontière reste entière dans l'autre."""
    nombre = max(1, math.ceil((fin - debut - RECOUVREMENT_S) / (PASSAGE_S - RECOUVREMENT_S)))
    longueur = (fin - debut + (nombre - 1) * RECOUVREMENT_S) / nombre
    pas = longueur - RECOUVREMENT_S
    return [(round(debut + i * pas), round(debut + i * pas + longueur)) for i in range(nombre)]


def garder_centre(lignes, debut, fin, premier, dernier):
    """Dans le recouvrement, chaque moment reste dans un seul passage."""
    bas = debut if premier else debut + RECOUVREMENT_S / 2
    haut = fin if dernier else fin - RECOUVREMENT_S / 2
    return [ligne for ligne in lignes if bas <= analyse.secondes(ligne["t"]) < haut]


def script_texte(lignes):
    return "\n".join(f"{x['t']} [{x['issue']}, {x['position']}, lame {x['lame']}, vie {x['vie']}] {x['ecran']} | "
                     f"joueur : {x['joueur']} | monstre : {x['monstre']} | preuve : {x['preuve']}" for x in lignes)


def clips_texte(clips):
    return "\n".join(f"{c['debut']}-{c['fin']} {c['titre']} : {c['pourquoi']}" for c in clips)


def nombre_images(duree):
    return min(IMAGES_MAX, max(IMAGES_MIN, int(duree / SECONDES_PAR_IMAGE)))


def images_espacees(brut, duree):
    nombre = nombre_images(duree)
    images = []
    for i in range(1, nombre + 1):
        image = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(duree * i / (nombre + 1)), "-i", brut, "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True, check=True, timeout=120).stdout
        images.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
    return images


def identifier(client, brut, duree, facture):
    """Quelques images suffisent. Une vidéo peut enchaîner plusieurs chasses, donc plusieurs armes."""
    schema = {
        "type": "OBJECT",
        "properties": {
            "armes": {"type": "ARRAY", "items": {"type": "STRING", "enum": sorted(fiches_de("armes"))}},
            "monstres": {"type": "ARRAY", "items": {"type": "STRING", "enum": sorted(fiches_de("monstres"))}},
            "lu_a_l_ecran": TEXTE,
        },
        "required": ["armes", "monstres", "lu_a_l_ecran"],
    }
    consigne = _texte(LECTURE / "consigne-identifier.md") + _texte(CONNAISSANCE / "commandes-par-arme.md")
    contenu = [*images_espacees(brut, duree), types.Part(text=consigne)]
    return analyse.generer(client, analyse.MODELE_IDENTIFICATION, contenu, schema, facture)


def _arme(trouve):
    """L'arme donnée à la mesure. Plusieurs armes ou aucune : None, la ligne Lame ne serait pas sûre."""
    armes = trouve["armes"]
    return armes[0] if len(armes) == 1 else None


def _lire_fenetre(client, fichier, debut, fin, consigne, facture, resolution, reflexion=None):
    video = analyse.partie_video(fichier, fps=IMAGES_PAR_S, start_offset=f"{debut}s", end_offset=f"{fin}s")
    return analyse.generer(client, analyse.MODELE, [video, types.Part(text=consigne)], SCHEMA_PASSAGE, facture,
                           resolution, reflexion)


def _bornes(consigne, debut, fin):
    return consigne.replace("{debut}", mmss(debut)).replace("{fin}", mmss(fin))


def lire_passages(client, fichier, plages, video_id, trouve, mesures, facture, suivi):
    """Les combats, à la suite. Chaque passage ne reçoit que les faits de sa fenêtre, et n'est payé qu'une fois."""
    import mesure_ecran
    fiches, modele, modele_exemple = fiches_choisies(trouve), _texte(LECTURE / "consigne-passage.md"), exemple(video_id)
    lu = {"lignes": [], "a_garder": [], "drole": [], "doutes": []}
    for i, (debut, fin) in enumerate(plages):
        consigne = (_bornes(modele, debut, fin) + "# Fiches\n\n" + fiches
                    + mesure_ecran.faits(mesures, _arme(trouve), debut, fin) + modele_exemple)
        sortie = suivi.une_fois(f"passage {debut}-{fin}", lambda: _lire_fenetre(
            client, fichier, debut, fin, consigne, facture, HAUT))
        seul_avant = i == 0 or plages[i - 1][1] <= debut
        seul_apres = i == len(plages) - 1 or plages[i + 1][0] >= fin
        lu["lignes"] += garder_centre(sortie["lignes"], debut, fin, seul_avant, seul_apres)
        _ajouter(lu, sortie)
    return lu


def _ajouter(lu, sortie):
    lu["a_garder"] += sortie["a_garder"]
    lu["drole"] += sortie["drole"]
    lu["doutes"].append(sortie["doute"])


def lire_hors_combat(client, fichier, plages, lu, facture, suivi):
    """Sans fiches et en basse qualité : il suffit de voir ce que fait le joueur."""
    modele = _texte(LECTURE / "consigne-hors-combat.md")
    for debut, fin in plages:
        debut, fin = max(0, round(debut) - ELAN_S), round(fin)
        sortie = suivi.une_fois(f"hors combat {debut}-{fin}", lambda: _lire_fenetre(
            client, fichier, debut, fin, _bornes(modele, debut, fin), facture, BAS, FAIBLE))
        lu["lignes"] += sortie["lignes"]
        _ajouter(lu, sortie)


def synthese(client, lu, trouve, faits, facture, ajout=""):
    lignes = sorted(lu["lignes"], key=lambda ligne: analyse.secondes(ligne["t"]))
    texte = (_texte(LECTURE / "consigne-synthese.md") + faits + ajout + "\n\n# Script\n\n" + script_texte(lignes)
             + "\n\n# Clips proposés\n\n" + clips_texte(lu["a_garder"])
             + "\n\n# Moments drôles proposés\n\n" + clips_texte(lu["drole"])
             + "\n\n# Doutes de la lecture\n\n" + "\n".join(lu["doutes"]))
    sortie = analyse.generer(client, analyse.MODELE, texte, SCHEMA_SYNTHESE, facture, reflexion=FAIBLE)
    return {**sortie, "identification": trouve, "lignes": lignes, "doutes_lecture": lu["doutes"]}


def lire(video_id, dossier):
    """La lecture écrite, ou None."""
    return _lire_json(_chemin(dossier, video_id))


def _mesures(video_id, brut, dossier):
    """Longue mais gratuite, et sans brut impossible à refaire : gardée pour une relance et la 2e passe."""
    chemin = _chemin(dossier, video_id, "mesures.json")
    mesures = _lire_json(chemin)
    if mesures is None:
        import mesure_ecran
        mesures = mesure_ecran.mesurer(brut)
        taches.ecrire_json(chemin, mesures)
    return mesures


def _premiere_passe(video_id, brut, dossier):
    """Chaque appel payé est gardé dans le relevé : une reprise saute ce qui est déjà lu."""
    import mesure_ecran
    duree = montage.duree_video(brut)
    client = analyse.client()
    suivi = reprise.Releve(_chemin(dossier, video_id, "releve.json"))
    facture = analyse._Facture(duree, suivi)
    with analyse.facturation(facture):
        if not suivi.deja("synthese"):  # la dernière réponse gardée : plus rien à payer, rien à refuser
            facture.verifier()
        mesures = _mesures(video_id, brut, dossier)
        trouve = suivi.une_fois("identification", lambda: identifier(client, brut, duree, facture))
        fichier = analyse.fichier_gemini(client, brut, _chemin(dossier, video_id, "fichier.json"))
        plages = [p for a, b in plages_combat(mesures, duree) for p in decouper(a, b)]
        lu = lire_passages(client, fichier, plages, video_id, trouve, mesures, facture, suivi)
        lire_hors_combat(client, fichier, mesures["hors_combat"], lu, facture, suivi)
        faits = mesure_ecran.faits(mesures, _arme(trouve))
        sortie = suivi.une_fois("synthese", lambda: synthese(client, lu, trouve, faits, facture))
        lecture = {"video_id": video_id, "modele": analyse.MODELE, "duree_s": duree, "faits": faits, **sortie,
                   "cout_usd": round(suivi.cout_usd, 6)}
        with reprise.verrou_note:
            taches.ecrire_json(_chemin(dossier, video_id), lecture)


def analyser(video_id, brut, dossier):
    """Lit la vidéo une fois pour toutes, puis joue les notes qui attendaient.

    Le relevé n'est clos qu'à la toute fin : tomber entre les deux ne perd aucun coût non annoncé.
    Une note ratée laisse la lecture et la note sur le disque : /analyse rejouée ne repaie que la note.
    """
    if lire(video_id, dossier) is None:
        if not os.path.isfile(brut):
            raise analyse.BrutRequis(f"brut_requis: brut absent pour {video_id}, /fetch (0,13 $) puis /analyse")
        _premiere_passe(video_id, brut, dossier)
    try:
        return contexte(video_id, brut, dossier)
    except analyse.BrutRequis:
        raise
    except analyse.ErreurAnalyse as erreur:
        echec = analyse.ErreurAnalyse(f"lecture conservée, remarque à rejouer par /analyse : {erreur}")
        echec.cout_usd = erreur.cout_usd
        raise echec from None


def _fenetre(point, duree):
    """Où relire : le point et son élan, dans la vidéo. None si Gemini n'a pas donné de temps utilisable."""
    debut, fin = analyse.secondes(point["debut"]), analyse.secondes(point["fin"])
    if debut < 0 or fin < debut:
        return None
    debut = max(0, round(debut) - ELAN_S)
    fin = min(round(fin) + ELAN_S, debut + RELECTURE_MAX_S + 2 * ELAN_S, round(duree))
    return (debut, fin) if debut < fin else None


def _relire(client, fichier, point, debut, fin, base, mesures, facture):
    import mesure_ecran
    trouve = base["identification"]
    consigne = (_bornes(_texte(LECTURE / "consigne-passage.md"), debut, fin)
                + f"\n# Remarque du joueur\n\n« {point['remarque']} »\n\n"
                + "Vérifie-la à l'image. S'il a raison, écris-le avec la preuve. Si tu ne le vois pas, écris ce que "
                + "tu vois et dis-le dans `doute`.\n\n# Fiches\n\n" + fiches_choisies(trouve)
                + mesure_ecran.faits(mesures, _arme(trouve), debut, fin))
    return _lire_fenetre(client, fichier, debut, fin, consigne, facture, HAUT)


def _relire_points(client, fichier, points, base, brut, dossier, facture):
    """Relit les seuls points non vus. Une relecture sans ligne dans sa fenêtre ne touche pas au script."""
    lu = {"lignes": list(base["lignes"]), "a_garder": list(base["a_garder"]), "drole": list(base["drole"]),
          "doutes": list(base["doutes_lecture"])}
    video_id = base["video_id"]
    for point in (p for p in points if not p["deja_vu"]):
        fenetre = _fenetre(point, base["duree_s"])
        if fenetre is None:
            lu["doutes"].append(f"Remarque non localisée : {point['remarque']}")
            continue
        debut, fin = fenetre
        fichier = fichier or analyse.fichier_gemini(client, brut, _chemin(dossier, video_id, "fichier.json"))
        mesures = _lire_json(_chemin(dossier, video_id, "mesures.json"))
        sortie = _relire(client, fichier, point, debut, fin, base, mesures, facture)
        relues = garder_centre(sortie["lignes"], debut, fin, True, True)
        if relues:
            lu["lignes"] = [x for x in lu["lignes"] if not debut <= analyse.secondes(x["t"]) < fin] + relues
        _ajouter(lu, sortie)
    return lu


def contexte(video_id, brut, dossier):
    """2e passe : joue toutes les notes présentes, seuls les points non vus sont relus, puis la synthèse est refaite.

    La limite passe d'abord, puis l'accès à la vidéo, vérifié avant le tri : rien n'est payé pour rien. Les notes
    jouées ne sont retirées qu'après l'écriture de la lecture corrigée.
    `cout_usd` de la tâche : ce qui est payé et jamais annoncé. Le fichier garde le cumul.
    """
    base = lire(video_id, dossier)
    jouees = reprise.notes(video_id, dossier)
    suivi = reprise.Releve(_chemin(dossier, video_id, "releve.json"), base)
    if not jouees.strip():  # rien à jouer : seul un relevé resté sur le disque peut encore avoir du coût à annoncer
        return {**base, "cout_usd": suivi.clore()}
    note = jouees.strip()
    client = analyse.client()
    facture = analyse._Facture(base["duree_s"], suivi)
    with analyse.facturation(facture):
        facture.verifier()
        fichier = analyse.fichier_disponible(client, brut, _chemin(dossier, video_id, "fichier.json"))
        consigne = _texte(LECTURE / "consigne-contexte.md").replace("{note}", note) + script_texte(base["lignes"])
        points = analyse.generer(client, analyse.MODELE, consigne, SCHEMA_REMARQUES, facture, reflexion=FAIBLE)
        lu = _relire_points(client, fichier, points["remarques"], base, brut, dossier, facture)
        ajout = (f"\n\n# Remarque du joueur\n\n« {note} »\n\nElle oriente la synthèse. Ce qui passe avant, c'est ce "
                 "que le script relu montre. Dans `doute`, dis ce qui dans sa remarque n'a pas été vu.")
        corrigee = {**base, **synthese(client, lu, base["identification"], base["faits"], facture, ajout),
                    "cout_usd": round(suivi.cout_usd, 6)}
        taches.ecrire_json(_chemin(dossier, video_id), corrigee)
        reprise.retirer_notes(video_id, dossier, jouees)
        return {**corrigee, "cout_usd": suivi.clore()}
