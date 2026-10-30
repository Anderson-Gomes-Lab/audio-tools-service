"""Textes des Shorts : PNG transparents dessinés avec Pillow, posés ensuite par ffmpeg.

Haut de l'écran seulement : le bas est couvert par l'interface des plateformes.
"""
import functools
import os
import unicodedata

from PIL import Image, ImageDraw, ImageFont

POLICE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polices", "Inter-ExtraBold.ttf")
LARGEUR_ECRAN = 1080
LARGEUR_TEXTE = 920
HAUT = 300  # en dessous des barres du haut de TikTok, Reels et Shorts
TAILLES = range(96, 47, -4)  # du plus grand au plus petit
LIGNES_MAX = 3
INTERLIGNE = 1.16
CONTOUR = 0.075  # épaisseur rapportée à la taille


@functools.lru_cache
def _police(taille):
    return ImageFont.truetype(POLICE, taille)


def _dessin(lettre):
    masque = _police(32).getmask(lettre)
    return masque.size, bytes(masque)


def _dessinable(lettre):
    """Faux pour un caractère absent de la police : il sortirait en carré vide."""
    return lettre.isspace() or _dessin(lettre) != _dessin(chr(0xFFFF))


def nettoyer(brut):
    """Minuscules, accents en une seule lettre, espaces simples, rien que la police ne sache écrire."""
    chaine = unicodedata.normalize("NFC", str(brut)).lower()
    return " ".join("".join(c for c in chaine if _dessinable(c)).split())


def _lignes(chaine, police):
    """Répartit les mots en lignes ; un mot trop long pour une ligne est coupé."""
    lignes, ligne = [], ""
    for mot in chaine.split():
        essai = f"{ligne} {mot}".strip()
        if police.getlength(essai) <= LARGEUR_TEXTE:
            ligne = essai
            continue
        if ligne:
            lignes.append(ligne)
        ligne = ""
        for lettre in mot:
            if police.getlength(ligne + lettre) > LARGEUR_TEXTE:
                lignes.append(ligne)
                ligne = ""
            ligne += lettre
    return lignes + [ligne] if ligne else lignes


def _mise_en_page(chaine):
    """Plus grande taille où le texte tient sans couper de mot, sinon la plus petite."""
    for taille in TAILLES:
        police = _police(taille)
        lignes = _lignes(chaine, police)
        if len(lignes) <= LIGNES_MAX and len(lignes) <= len(chaine.split()):
            return police, lignes
    return police, _lignes(chaine, police)[:LIGNES_MAX]


def rendre(brut, chemin):
    """Écrit le PNG et rend sa position (x, y) dans l'image 1080x1920."""
    chaine = nettoyer(brut)
    if not chaine:
        raise ValueError("texte vide")
    police, lignes = _mise_en_page(chaine)
    taille = police.size
    contour = round(taille * CONTOUR)
    pas = round(taille * INTERLIGNE)
    largeur = round(max(police.getlength(ligne) for ligne in lignes)) + 2 * contour + 4
    hauteur = pas * len(lignes) + 2 * contour

    image = Image.new("RGBA", (largeur, hauteur), (0, 0, 0, 0))
    dessin = ImageDraw.Draw(image)
    for i, ligne in enumerate(lignes):
        dessin.text((largeur / 2, contour + pas * (i + 0.5)), ligne, font=police, anchor="mm",
                    fill="white", stroke_width=contour, stroke_fill="black")
    image.save(chemin)
    return (LARGEUR_ECRAN - largeur) // 2, HAUT
