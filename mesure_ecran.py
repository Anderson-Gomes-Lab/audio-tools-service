"""Lit l'écran du jeu sans IA : vie, alerte, lame, commandes, arme rangée, monstres vaincus.

ffmpeg envoie les images une à une par des tubes : seule la dernière reste en mémoire, avec les
mesures déjà prises. La mémoire ne dépend donc pas de la durée de la vidéo.

Les positions valent pour la capture PS5 avec l'interface par défaut, en 1080p.
La lame n'a de sens qu'à l'épée longue.
"""
import os
import subprocess
import unicodedata
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from math import inf
from pathlib import Path

import numpy as np
from PIL import Image

MODELE_ARME_RANGEE = Path(__file__).parent / "lecture" / "connaissance" / "signes" / "sortir-l-arme.png"
EPEE_LONGUE = "epee longue"

# YouTube rend la 1080p de la PS5 en 1920x1072 ou 1920x1080 : les zones ci-dessous tiennent dans les deux.
TAILLES_LISIBLES = ("1920x1080", "1920x1072")
LARGEUR, HAUTEUR, FPS = 1920, 240, 5
VIE_DEBUT, VIE_FIN = 88, 610
ARME_Y, ARME_DROITE = 30, 1800
SEUIL_LISTE, SEUIL_VERT, SEUIL_VAGUE = 150, 120, 230
DUREE_MIN_S, ALERTE_MIN_S, PERTE_MIN_S, PERTE_MAX_S, ARME_MIN_S = 0.6, 0.8, 2.0, 8.0, 0.2
TROU_VIE = 20
CHUTE_COUP, VIE_NULLE, VIE_BASSE, ECART_COUPS_S = 0.25, 0.03, 0.10, 4.0
COCHES_X, COCHES_Y, COCHES_LARGEUR, COCHES_HAUTEUR, COCHES_PAS = 1778, 212, 18, 190, 38
FPS_COCHES, COCHE_MIN, COCHE_TENUE = 2, 15, 6

CONSIGNES = (
    "Pour chaque moment sans commande, écris une ligne. Regarde l'image. Chasseur bloqué par un cri ou une secousse, "
    "projeté ou au sol : `subi`, avec la cause. S'il saute, plane ou se déplace : `neutre`, avec ce qu'il fait.\n"
    "Une arme rangée juste après un Rengainage spécial est un rengainage perdu : écris une ligne `rate`.\n"
    "Au début ou à la fin de la vidéo, ranger l'arme n'est pas un raté.\n"
    "Chaque coup reçu est une ligne `subi`, même si le chasseur continue son attaque : dis ce qui l'a frappé.\n"
    "Hors de ces coups, une baisse de vie ne prouve rien : écris `subi` seulement si tu vois le chasseur bousculé, "
    "projeté ou au sol. Sans cause visible, écris que la cause n'est pas visible.\n"
)


def verifier_taille(chemin):
    """Zones fixes : une autre taille donnerait des mesures fausses, sans que rien ne le montre."""
    commande = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(chemin)]
    sonde = subprocess.run(commande, capture_output=True, text=True)
    taille = sonde.stdout.strip()
    if sonde.returncode or not taille:
        raise ValueError(f"vidéo illisible : {chemin}")
    if taille not in TAILLES_LISIBLES:
        raise ValueError(f"{chemin} : {taille}, il faut la 1080p ({' ou '.join(TAILLES_LISIBLES)})")


def commande_ffmpeg(chemin, sortie_coches):
    bandeau = f"crop={LARGEUR}:{HAUTEUR}:0:0,fps={FPS}"
    coches = f"crop={COCHES_LARGEUR}:{COCHES_HAUTEUR}:{COCHES_X}:{COCHES_Y},fps={FPS_COCHES}"
    return ["ffmpeg", "-nostdin", "-v", "error", "-i", str(chemin),
            "-filter_complex", f"[0:v:0]split[a][b];[a]{bandeau}[bandeau];[b]{coches}[coches]",
            "-map", "[bandeau]", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            "-map", "[coches]", "-f", "rawvideo", "-pix_fmt", "rgb24", f"pipe:{sortie_coches}"]


def images(tube, largeur, hauteur):
    """Une image à la fois, en nombres signés : les calculs de couleur soustraient et ajoutent."""
    taille = largeur * hauteur * 3
    while len(bloc := tube.read(taille)) == taille:
        yield np.frombuffer(bloc, np.uint8).reshape(hauteur, largeur, 3).astype(np.int16)


def lire_coches(tube):
    with tube:
        return [coches_par_rang(image) for image in images(tube, COCHES_LARGEUR, COCHES_HAUTEUR)]


def lire(chemin, modele):
    """Relevés du bandeau et des coches, en un seul décodage : l'AV1 coûte cher au serveur.

    Deux tubes et stderr se lisent en même temps : un seul qui se remplit bloquerait ffmpeg.
    """
    tube_coches, sortie_coches = os.pipe()
    erreurs = deque(maxlen=5)
    commande = commande_ffmpeg(chemin, sortie_coches)
    with subprocess.Popen(commande, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          pass_fds=(sortie_coches,)) as ffmpeg, ThreadPoolExecutor(2) as fils:
        os.close(sortie_coches)
        fils.submit(erreurs.extend, ffmpeg.stderr)
        lecture_coches = fils.submit(lire_coches, open(tube_coches, "rb"))
        try:
            releves = [relever(image, modele) for image in images(ffmpeg.stdout, LARGEUR, HAUTEUR)]
        except BaseException:
            ffmpeg.kill()
            raise
        code = ffmpeg.wait()
    if code:
        raise RuntimeError(f"ffmpeg a échoué sur {chemin} : {b''.join(erreurs).decode(errors='replace')[-300:]}")
    return releves, lecture_coches.result()


def blanc(zone):
    return zone.min(axis=2) > 215


def texte_vert(zone):
    r, v, b = zone[..., 0], zone[..., 1], zone[..., 2]
    return ((v > 170) & (v > r + 25) & (v > b + 60)).sum()


def vague(image):
    """Colonnes traversées par une ligne orange fine : la vague d'alerte, pas le feu du décor."""
    zone = image[0:118, 300:760]
    r, v, b = zone[..., 0], zone[..., 1], zone[..., 2]
    par_colonne = ((r > 200) & (v > 60) & (v < 200) & (b < 110)).sum(axis=0)
    return ((par_colonne >= 2) & (par_colonne <= 12)).sum()


def vie(image):
    """Part de la barre encore verte. None quand la barre est repliée, hors combat."""
    zone = image[44:92, 0:640]
    r, v, b = zone[..., 0], zone[..., 1], zone[..., 2]
    vert = (v > 150) & (v > r + 40) & (v > b + 30)
    if vert[:, :50].any():
        return None
    colonnes = np.where(vert[:, VIE_DEBUT:].sum(axis=0) >= 2)[0]
    if not colonnes.size:
        return 0.0
    trous = np.where(np.diff(colonnes) > TROU_VIE)[0]
    fin = colonnes[trous[0]] if trous.size else colonnes[-1]
    return round(min(1.0, float(fin / (VIE_FIN - VIE_DEBUT))), 2)


def couleur_lame(image):
    """Le contour de la jauge d'esprit donne le niveau : rien, blanc, jaune ou rouge."""
    r, v, b = np.median(image[[160, 184], 110:270], axis=1).mean(axis=0)
    if r - v > 50:
        return "rouge"
    if r > 100 and b < 0.6 * r:
        return "jaune"
    return "blanc" if r > 100 else "vide"


def arme_rangee(image, modele):
    zone = image[ARME_Y:ARME_Y + modele.shape[0], ARME_DROITE - modele.shape[1]:ARME_DROITE]
    return (blanc(zone) & modele).sum() / modele.sum() > 0.6


def relever(image, modele):
    """Ce qu'on garde d'une image : quelques valeurs simples, l'image peut partir."""
    return (bool(blanc(image[20:205, 1380:1800]).sum() >= SEUIL_LISTE),
            bool(texte_vert(image[96:140, 860:1780]) >= SEUIL_VERT),
            bool(arme_rangee(image, modele)),
            vie(image), couleur_lame(image), int(vague(image)))


def lissee(valeurs, rayon=3):
    """Valeur la plus fréquente autour de chaque image, pour écarter les flashs."""
    return [Counter(fenetre).most_common(1)[0][0]
            for fenetre in (valeurs[max(0, i - rayon):i + rayon + 1] for i in range(len(valeurs)))]


def vie_stable(valeurs, rayon=3):
    """La vie basse clignote : deuxième plus haut relevé autour de chaque image, un flash isolé ne compte pas."""
    resultat = []
    for i, valeur in enumerate(valeurs):
        fenetre = sorted(v for v in valeurs[max(0, i - rayon):i + rayon + 1] if v is not None)
        resultat.append(None if valeur is None or len(fenetre) < 2 else fenetre[-2])
    return resultat


def intervalles(actif, duree_min=DUREE_MIN_S):
    """Plages où le signe est présent, trous de moins de 0,4 s bouchés, plages trop courtes écartées."""
    actif = list(actif)
    for i in range(1, len(actif) - 2):
        if not actif[i] and actif[i - 1] and (actif[i + 1] or actif[i + 2]):
            actif[i] = True
    plages, debut = [], None
    for i, a in enumerate(actif + [False]):
        if a and debut is None:
            debut = i
        if not a and debut is not None:
            if (i - debut) / FPS >= duree_min:
                plages.append([debut / FPS, i / FPS])
            debut = None
    return plages


def tenue(valeurs, rayon=2):
    """Médiane sur 1 s : la vague dure, un éclat de feu non."""
    return [np.median(valeurs[max(0, i - rayon):i + rayon + 1]) for i in range(len(valeurs))]


def en_combat(vies, liste, actions, rayon=3 * FPS):
    """Vrai en chasse. Faux dans un menu, sans vie ni action, ou plus de 8 s sans aucune commande : camp, trajet."""
    present = np.array([v != 0.0 for v in vies]) | actions
    combat = np.array([present[max(0, i - rayon):i + rayon + 1].any() for i in range(len(present))])
    for debut, fin in intervalles(~liste & ~actions, PERTE_MAX_S):
        combat[int(debut * FPS):int(fin * FPS)] = False
    return combat


def coups_recus(vies):
    """Chutes brutales : 25 % ou plus en 2 s, tenues 1 s, ou vie tombée à presque rien."""
    coups = []
    for i, ici in enumerate(vies):
        avant = [v for v in vies[max(0, i - 2 * FPS):i] if v is not None]
        apres = vies[i + 1:i + 1 + FPS]
        if ici is None or not avant or None in apres or (coups and i / FPS - coups[-1] < ECART_COUPS_S):
            continue
        haut = max(avant)
        tenue_basse = apres and np.median(apres) <= haut - CHUTE_COUP
        if (haut - ici >= CHUTE_COUP and tenue_basse) or (ici <= VIE_NULLE and haut >= VIE_BASSE):
            coups.append(i / FPS)
    return coups


def coches_par_rang(image):
    r, v, b = image[..., 0], image[..., 1], image[..., 2]
    jaune = (r > 190) & (v > 150) & (b < 120)
    return [bool(jaune[rang:rang + COCHES_PAS].sum() >= COCHE_MIN) for rang in range(0, COCHES_HAUTEUR, COCHES_PAS)]


def vaincus(releves):
    """Coches jaunes de la liste de quête : une par monstre, elle reste une fois posée."""
    dates = []
    for cochee in np.array(releves).T:
        tenues = [i for i in range(len(cochee)) if cochee[i:i + COCHE_TENUE].mean() >= 0.7]
        if tenues and tenues[0] > 0:
            dates.append(tenues[0] / FPS_COCHES)
    return sorted(dates)


def mesurer(chemin_video, modele=MODELE_ARME_RANGEE):
    """Tous les faits de la vidéo. ValueError si la vidéo est illisible ou n'est pas en 1080p."""
    verifier_taille(chemin_video)
    forme = np.array(Image.open(modele)) > 0
    releves, coches = lire(chemin_video, forme)
    if not releves:
        raise ValueError(f"aucune image lue : {chemin_video}")
    liste, vert, rangee, vies, lames, vagues = zip(*releves)
    liste, vert = np.array(liste), np.array(vert)
    combat = en_combat(vies, liste, vert)
    mesures = {
        "vie": vie_stable([v if c else None for v, c in zip(vies, combat)]),
        "lame": lissee([couleur if c else None for couleur, c in zip(lames, combat)]),
        "alerte": intervalles(np.array(tenue(vagues)) >= SEUIL_VAGUE, ALERTE_MIN_S),
        "perte": intervalles(~liste & ~vert & combat, PERTE_MIN_S),
        "arme_rangee": intervalles(rangee, ARME_MIN_S),
        "hors_combat": intervalles(~combat, PERTE_MIN_S),
    }
    mesures["coups"] = coups_recus(mesures["vie"])
    mesures["vaincus"] = vaincus(coches)
    return mesures


def mmss(s):
    return f"{int(s) // 60:02d}:{int(s) % 60:02d}"


def plages_texte(plages, debut, fin):
    """Plages coupées aux bornes de la fenêtre."""
    gardees = [(max(d, debut), min(f, fin)) for d, f in plages if d < fin and f > debut]
    return ", ".join(f"{mmss(d)} à {mmss(f)}" for d, f in gardees) or "aucune"


def dates_texte(dates, debut, fin):
    return ", ".join(mmss(t) for t in dates if debut <= t <= fin) or "aucun"


def est_epee_longue(arme):
    sans_accent = unicodedata.normalize("NFD", arme or "").encode("ascii", "ignore").decode()
    return sans_accent.casefold().replace("-", " ").strip() == EPEE_LONGUE


def ligne_lame(lames, debut, fin):
    connues = [(i / FPS, v) for i, v in enumerate(lames) if v is not None and debut <= i / FPS <= fin]
    changements = [c for i, c in enumerate(connues) if i == 0 or c[1] != connues[i - 1][1]]
    return "- Lame : " + (", ".join(f"{mmss(t)} {v}" for t, v in changements) or "aucune") + ".\n"


def faits(mesures, arme=None, debut=None, fin=None):
    """Le texte donné à Gemini, sur toute la vidéo ou sur une fenêtre en secondes.

    La ligne Lame n'apparaît que si `arme` nomme l'épée longue : sans arme connue, elle n'est pas sûre.
    """
    debut, fin = 0 if debut is None else debut, inf if fin is None else fin
    lame = ligne_lame(mesures["lame"], debut, fin) if est_epee_longue(arme) else ""
    releves = range(0, len(mesures["vie"]), 5 * FPS)
    vies = ", ".join(f"{mmss(i / FPS)} {round(mesures['vie'][i] * 100)} %" for i in releves
                     if mesures["vie"][i] is not None and debut <= i / FPS <= fin) or "aucune"
    return f"""
# Faits mesurés à l'écran

Un programme a lu ces faits sur l'écran, image par image. Ils sont exacts et passent avant ce que tu crois voir.

{lame}- Vie, part de la barre encore verte : {vies}.
- Coups reçus, chute brutale de la vie : {dates_texte(mesures["coups"], debut, fin)}.
- Monstres vaincus, coche de la liste de quête, une date par monstre : {dates_texte(mesures["vaincus"], debut, fin)}.
- Alerte, vague orange, plages sûres, il peut y en avoir d'autres : {plages_texte(mesures["alerte"], debut, fin)}.
- Sans aucune commande à l'écran, 2 s ou plus : {plages_texte(mesures["perte"], debut, fin)}.
- Arme rangée : {plages_texte(mesures["arme_rangee"], debut, fin)}.
- Hors combat, menus, camp ou trajet : {plages_texte(mesures["hors_combat"], debut, fin)}.

{CONSIGNES}"""
