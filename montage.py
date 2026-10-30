"""Montage d'un Short depuis un moment du plan : coupe, image carrée sur fond noir, son, textes.

Pas de zoom ni d'effet : l'intérêt est dans le choix du moment et du texte.
"""
import json
import math
import os
import subprocess
import tempfile
import time

from PIL import Image

import texte

LARGEUR, HAUTEUR = 1080, 1920
# Format validé le 31/10 : image 1:1, texte dans la bande noire du haut
COTE_IMAGE = 1080
Y_IMAGE_MIN, Y_IMAGE_MAX = 420, 560
ECART_TEXTE = 24
TEXTES_MAX = 3
DELAI_RENDU_S = 1800

# Son du jeu gardé, volume harmonisé à -14 LUFS (niveau des plateformes)
# (loudnorm travaille en 192 kHz : on revient à 48 kHz, la norme vidéo)
SON_NORMALISE = "[0:a]loudnorm=I=-14:TP=-1.5,aresample=48000[a]"
# Clip muet : piste silencieuse, certaines plateformes refusent une vidéo sans son
SON_SILENCE = "[1:a]anull[a]"
ENTREE_SILENCE = ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
ENCODAGE = ['-r', '30', '-c:v', 'libx264', '-preset', 'medium', '-crf', '21',
            '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart']


class ErreurMontage(Exception):
    """Plan inutilisable pour ce moment."""


def nombre(valeur, minimum):
    """Nombre fini >= minimum, sinon None (texte, null, NaN, négatif...)."""
    if isinstance(valeur, bool):
        return None
    try:
        x = float(valeur)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= minimum else None


def duree_video(chemin):
    sortie = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', chemin],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return float(sortie.strip())


def a_du_son(chemin):
    sortie = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index',
         '-of', 'csv=p=0', chemin],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return bool(sortie.strip())


def dimensions(chemin):
    sortie = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
         '-of', 'json', chemin],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    flux = json.loads(sortie)['streams'][0]
    return flux['width'], flux['height']


def cadre(largeur, hauteur, x_centre):
    """Plus grand carré (largeur, hauteur, x, y) centré sur x_centre, dans l'image."""
    cote = min(largeur, hauteur) // 2 * 2
    x = max(0, min(round(x_centre * largeur - cote / 2), largeur - cote)) // 2 * 2
    return cote, cote, x, (hauteur - cote) // 2


def place(hauteur_texte):
    """(y de l'image, y du texte) : l'image laisse la place au texte, qui reste dans la bande."""
    sous_le_texte = texte.HAUT + hauteur_texte + ECART_TEXTE
    y_image = max(Y_IMAGE_MIN, min(sous_le_texte, Y_IMAGE_MAX)) // 2 * 2
    return y_image, min(texte.HAUT, y_image - hauteur_texte - ECART_TEXTE)


def horaires(textes, longueur):
    """[(texte, début, fin)] dans l'ordre, en secondes depuis le début du clip.

    Un texte s'arrête quand le suivant commence : ils occupent la même place.
    """
    textes = [t for t in textes or [] if texte.nettoyer(t.get("texte", ""))]
    if len(textes) > TEXTES_MAX:
        raise ErreurMontage(f"{len(textes)} textes, {TEXTES_MAX} au plus")
    lus = []
    for t in textes:
        debut, fin = nombre(t.get("debut_s"), 0), nombre(t.get("fin_s"), 0)
        if debut is None or fin is None or fin <= debut or debut >= longueur:
            raise ErreurMontage(f"instants du texte invalides pour un clip de {longueur:.1f} s : {t}")
        lus.append((t["texte"], debut, fin))
    lus.sort(key=lambda t: t[1])
    suivants = [t[1] for t in lus[1:]] + [longueur]
    resultat = [(t, debut, min(fin, suivant, longueur)) for (t, debut, fin), suivant in zip(lus, suivants)]
    return [t for t in resultat if t[2] > t[1]]


def _bornes(moment, total):
    debut, fin = nombre(moment.get("debut_s"), 0), nombre(moment.get("fin_s"), 0)
    if debut is None or fin is None or fin <= debut or debut >= total:
        raise ErreurMontage(f"bornes invalides pour une vidéo de {total:.1f} s : {debut} -> {fin}")
    return debut, min(fin, total) - debut


def _filtre_image(zone, y_image, incrustations, premiere_entree):
    """Filtre vidéo : cadence, carré sur fond noir, puis chaque PNG à son instant.

    Fin exclue : la dernière image d'un texte n'est jamais la première du suivant.
    """
    l_cadre, h_cadre, x, y = zone
    filtre = (f"[0:v]fps=30,crop={l_cadre}:{h_cadre}:{x}:{y},scale={COTE_IMAGE}:{COTE_IMAGE},setsar=1,"
              f"pad={LARGEUR}:{HAUTEUR}:0:{y_image}:black[i0]")
    for i, (px, py, debut, fin) in enumerate(incrustations):
        filtre += (f";[i{i}][{premiere_entree + i}:v]"
                   f"overlay={px}:{py}:enable='gte(t,{debut})*lt(t,{fin})'[i{i + 1}]")
    return filtre + f";[i{len(incrustations)}]format=yuv420p[v]"


def _x_centre(moment):
    x = nombre(moment.get("x_centre"), float("-inf"))
    return 0.5 if x is None else min(max(x, 0), 1)


def monter(brut, moment, sortie):
    """Écrit le Short dans sortie et rend {"duree_s", "rendu_s"}."""
    debut, longueur = _bornes(moment, duree_video(brut))
    textes = horaires(moment.get("textes"), longueur)
    zone = cadre(*dimensions(brut), _x_centre(moment))
    son = a_du_son(brut)
    partiel = sortie + ".part"
    os.makedirs(os.path.dirname(sortie), exist_ok=True)

    with tempfile.TemporaryDirectory() as dossier:
        entrees, images = [], []
        for i, (chaine, debut_texte, fin_texte) in enumerate(textes):
            png = os.path.join(dossier, f"{i}.png")
            px, _ = texte.rendre(chaine, png)
            with Image.open(png) as rendu:
                images.append((px, rendu.height, debut_texte, fin_texte))
            entrees += ['-i', png]
        # L'image ne bouge pas de tout le clip : sa place dépend du texte le plus haut
        y_image, y_texte = place(max((h for _, h, _, _ in images), default=0))
        incrustations = [(px, y_texte, debut_texte, fin_texte) for px, _, debut_texte, fin_texte in images]
        filtre = (_filtre_image(zone, y_image, incrustations, 1 if son else 2) + ';'
                  + (SON_NORMALISE if son else SON_SILENCE))
        commande = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-ss', str(debut), '-t', str(longueur), '-i', brut,
                    *([] if son else ENTREE_SILENCE), *entrees,
                    '-filter_complex', filtre, '-map', '[v]', '-map', '[a]', '-t', str(longueur),
                    *ENCODAGE, '-f', 'mp4', partiel]
        depart = time.monotonic()
        try:
            subprocess.run(commande, check=True, capture_output=True, text=True, timeout=DELAI_RENDU_S)
            os.replace(partiel, sortie)
        except subprocess.CalledProcessError as e:
            raise ErreurMontage(f"ffmpeg a échoué : {e.stderr[-300:]}") from None
        finally:
            if os.path.exists(partiel):
                os.remove(partiel)
    return {"duree_s": round(duree_video(sortie), 2), "rendu_s": round(time.monotonic() - depart, 1)}
