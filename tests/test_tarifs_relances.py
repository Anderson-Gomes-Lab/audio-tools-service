"""Limite de dépense par vidéo, et relances réseau prouvées sur le vrai client du SDK, réseau simulé."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from google.genai import _api_client, errors

import analyse

CLE = "AIzaCLEDETEST123"


@pytest.fixture
def gemini(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))


@pytest.mark.parametrize("duree, jour, limite", [
    (1800, date(2026, 12, 31), 3.0), (1800, date(2027, 1, 1), 6.0),  # le double de l'estimation suit le tarif
    (134, date(2026, 10, 31), 0.5), (134, date(2027, 1, 1), 0.5),  # 0,16 $ réels : le plancher évite un arrêt à tort
    (300, date(2026, 10, 31), 0.5), (600, date(2026, 10, 31), 1.0), (600, date(2027, 6, 1), 2.0),
])
def test_limite_par_video_suit_le_tarif(duree, jour, limite):
    assert analyse.limite_video(duree, jour) == pytest.approx(limite)


class SdkReel:
    """Le vrai client du SDK, servi par un faux réseau : prouve les relances, pas seulement le réglage."""

    REUSSI = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}],
              "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200}}

    def __init__(self, monkeypatch, *codes):
        self.codes, self.requetes, self.attentes = list(codes), 0, []
        monkeypatch.setenv("GEMINI_API_KEY", CLE)
        monkeypatch.setattr(httpx.Client, "send", self._envoyer)
        monkeypatch.setattr("tenacity.nap.time.sleep", self.attentes.append)
        self.client = analyse.client()

    def _envoyer(self, requete, **_):
        self.requetes += 1
        code = self.codes.pop(0)
        contenu = self.REUSSI if code == 200 else {"error": {"code": code, "message": "panne", "status": "X"}}
        return httpx.Response(code, json=contenu, request=requete)

    def generer(self):
        facture = analyse._Facture()
        schema = {"type": "OBJECT", "required": [], "properties": {}}
        return analyse.generer(self.client, analyse.MODELE, "texte", schema, facture), facture


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_panne_reseau_relancee_par_le_sdk_sans_facturer_deux_fois(gemini, monkeypatch, code):
    sdk = SdkReel(monkeypatch, code, code, 200)
    lu, facture = sdk.generer()
    assert lu == {} and sdk.requetes == 3
    assert facture.usd == pytest.approx(analyse._cout(SimpleNamespace(
        prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=0), analyse.date.today()))


def test_relances_reseau_a_attente_croissante_puis_abandon(gemini, monkeypatch):
    sdk = SdkReel(monkeypatch, *[503] * 5)
    with pytest.raises(errors.ServerError):
        sdk.generer()
    assert sdk.requetes == 5
    assert len(sdk.attentes) == 4 and sdk.attentes == sorted(sdk.attentes)


@pytest.mark.parametrize("code", [400, 403, 404])
def test_refus_definitif_jamais_relance(gemini, monkeypatch, code):
    sdk = SdkReel(monkeypatch, code)
    with pytest.raises(errors.ClientError):
        sdk.generer()
    assert sdk.requetes == 1


def test_contrat_dit_la_limite_fixe_de_l_envoi():
    contrat = (Path(__file__).parents[2] / "docs" / "contrat-chaine.md").read_text(encoding="utf-8")
    assert f"Envoi de la vidéo : {_api_client.MAX_RETRY_COUNT} tentatives par morceau" in contrat
