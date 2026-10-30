"""Tri des moments : seuil, quota selon la durée, écart, priorité des registres."""

import json

import pytest

import analyse
import tri


def moment(debut, fin, registre="maitrise", score=8):
    return {"debut_s": debut, "fin_s": fin, "registre": registre, "score": score}


def test_tri_garde_les_meilleurs_numerotes_jusqu_au_plafond():
    moments = [moment(0, 10, "rire", 7), moment(100, 110, "maitrise", 9), moment(200, 210, "info", 8)]
    gardes, rejetes = tri.trier(moments, 2)
    assert [(m["n"], m["registre"]) for m in gardes] == [(1, "maitrise"), (2, "info")]
    assert [m["raison"] for m in rejetes] == ["quota"]


def test_tri_seuil_de_score():
    assert tri.trier([moment(0, 10, score=tri.SCORE_SEUIL)])[0]
    gardes, rejetes = tri.trier([moment(0, 10, score=tri.SCORE_SEUIL - 1)])
    assert gardes == [] and rejetes[0]["raison"] == "score"


def test_tri_garde_deux_moments_du_meme_registre():
    gardes, rejetes = tri.trier([moment(0, 10, score=9), moment(100, 110, score=8)])
    assert len(gardes) == 2 and rejetes == []


def test_tri_sans_plafond_ne_rejette_pas_pour_quota():
    moments = [moment(i * 100, i * 100 + 10) for i in range(6)]
    assert len(tri.trier(moments)[0]) == 6


@pytest.mark.parametrize("duree_s, attendu", [
    (134, 2), (300, 2), (600, 2), (601, 3), (900, 3), (1200, 3), (1500, 4), (1501, 4), (1800, 4), (2100, 5),
    (3600, 7), (4500, 8), (7200, 8),
])
def test_tri_quota_selon_la_duree(duree_s, attendu):
    assert tri.quota(duree_s) == attendu


@pytest.mark.parametrize("plafond", [1, 2])
def test_tri_un_doublon_n_entre_jamais_en_reserve(plafond):
    moments = [moment(0, 10, score=9), moment(5, 15, score=8)]
    assert [m["raison"] for m in tri.trier(moments, plafond)[1]] == ["ecart"]


def test_tri_a_score_egal_le_registre_departage():
    moments = [moment(0, 10, "maitrise", 8), moment(100, 110, "spectacle", 8), moment(200, 210, "rire", 8)]
    gardes, rejetes = tri.trier(moments, 2)
    assert [m["registre"] for m in gardes] == ["rire", "spectacle"]
    assert rejetes[0]["registre"] == "maitrise" and rejetes[0]["raison"] == "quota"


def test_tri_le_score_passe_avant_le_registre():
    assert tri.trier([moment(0, 10, "rire", 8), moment(100, 110, "maitrise", 9)], 1)[0][0]["registre"] == "maitrise"


def test_chaque_registre_a_sa_duree_et_son_rang():
    assert set(analyse.DUREES_S) == set(tri.PRIORITE) == set(analyse.REGISTRES)


@pytest.mark.parametrize("debut, garde", [(30, True), (29, False), (5, False)])
def test_tri_ecart_minimum(debut, garde):
    gardes, rejetes = tri.trier([moment(0, 10, "rire", 9), moment(debut, debut + 8, "maitrise", 8)])
    assert len(gardes) == (2 if garde else 1)
    assert garde or rejetes[0]["raison"] == "ecart"


def test_tri_accepte_zero_et_ne_modifie_pas_son_entree():
    assert tri.trier([]) == ([], [])
    entree = [moment(0, 10)]
    copie = json.loads(json.dumps(entree))
    tri.trier(entree)
    assert entree == copie
