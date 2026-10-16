"""Lote de estrutura E10: pesquisa de nichos (inteligencia/tendencias.py) com
Google Trends e Mercado Livre falsos. Termos com teto, eventos no nível do
painel e sem a mensagem crua das exceções."""
import sys
import types

import pandas as pd
import pytest
import requests

from apoio import cabecalho
from db import conectar
from inteligencia import tendencias


class Resp:
    def __init__(self, status, corpo=None, sem_json=False):
        self.status_code = status
        self._corpo = corpo
        self._sem_json = sem_json

    def json(self):
        if self._sem_json:
            raise ValueError("Expecting value")
        return self._corpo


def trends_falso(series, erro=None):
    """Instala um pytrends.request falso com as séries dadas."""
    class TrendReq:
        def __init__(self, **_):
            pass

        def build_payload(self, lote, **_):
            self.lote = lote

        def interest_over_time(self):
            if erro:
                raise erro
            return pd.DataFrame({t: series[t] for t in self.lote if t in series})

    modulo = types.ModuleType("pytrends.request")
    modulo.TrendReq = TrendReq
    return modulo


@pytest.fixture(autouse=True)
def sem_pausa(monkeypatch):
    monkeypatch.setattr(tendencias.time, "sleep", lambda s: None)


@pytest.fixture
def mercado(monkeypatch):
    respostas = {}

    def get(url, params=None, **_):
        termo = (params or {}).get("q")
        return respostas.get(termo, Resp(200, {"paging": {"total": 0}, "results": []}))
    monkeypatch.setattr(tendencias.requests, "get", get)
    return respostas


def eventos():
    with conectar() as conn:
        return [tuple(l) for l in conn.execute("SELECT nivel, mensagem FROM eventos"
                                              " WHERE origem = 'tendencias' ORDER BY id")]


def test_pipeline_ordena_por_score_e_guarda(monkeypatch, mercado):
    monkeypatch.setitem(sys.modules, "pytrends.request", trends_falso({
        "garrafa termica": [10, 10, 20, 40, 60, 80],   # subindo
        "capa de chuva": [80, 80, 60, 40, 20, 10],     # caindo
    }))
    mercado["garrafa termica"] = Resp(200, {"paging": {"total": 120},
                                            "results": [{"price": 50}, {"price": 70}, {"price": "x"}]})
    mercado["capa de chuva"] = Resp(200, {"paging": {"total": 50000}, "results": [{"price": 30}]})

    ops = tendencias.analisar([" Garrafa  Termica ", "capa de chuva", "garrafa termica"], nicho="casa")

    assert [o.termo for o in ops] == ["garrafa termica", "capa de chuva"]
    primeira = ops[0]
    assert (primeira.concorrencia, primeira.preco_mediano, primeira.nicho) == (120, 60.0, "casa")
    assert primeira.tendencia_pct > 0 > ops[1].tendencia_pct
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM oportunidades").fetchone()[0] == 2
    assert "subindo" in primeira.resumo()


def test_serie_e_score():
    assert tendencias._analisar_serie([]) == (0.0, 0.0)
    assert tendencias._analisar_serie([10, 20]) == (15, 0.0)
    assert tendencias._analisar_serie([0, 0, 50, 50, 100, 100])[1] == 9900.0  # começo zero vira 1
    assert tendencias._score(0, 50, 10) == 0.0
    assert tendencias._score(50, 500, 10) == 100.0          # crescimento limitado a 2 x
    assert tendencias._score(50, -500, 10) == 25.0          # queda limitada a 0,5 x
    assert tendencias._score(50, 0, 100) == 25.0            # log10(100) = 2


@pytest.mark.parametrize("termos, erro", [
    ("garrafa", "lista"),
    ([1, 2], "texto"),
    (["x" * (tendencias.MAX_CARACTERES + 1)], "longo demais"),
    ([f"termo {i}" for i in range(tendencias.MAX_TERMOS + 1)], "No máximo"),
])
def test_termos_invalidos(termos, erro):
    with pytest.raises(ValueError, match=erro):
        tendencias.validar_termos(termos)


def test_repetidos_e_vazios_nao_contam_para_o_teto():
    termos = ["Igual"] * 100 + ["  "] * 10
    assert tendencias.validar_termos(termos) == ["igual"]


def test_falha_do_trends_registra_so_o_tipo(monkeypatch, mercado):
    monkeypatch.setitem(sys.modules, "pytrends.request", trends_falso(
        {}, erro=RuntimeError("https://trends.google.com/?q=segredo-da-resposta 429")))
    tendencias.analisar(["garrafa"])
    assert eventos() == [("atencao", "Falha no Google Trends para 1 termo(s) (RuntimeError)")]


def test_sem_pytrends_avisa_e_segue(monkeypatch, mercado):
    monkeypatch.setitem(sys.modules, "pytrends.request", None)  # import levanta ImportError
    ops = tendencias.analisar(["garrafa"], persistir=False)
    assert ops[0].demanda == 0
    assert eventos() == [("atencao", "pytrends não instalado; pulando Google Trends")]


def test_mercado_livre_fora_do_ar_nao_leva_termo_nem_url(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytrends.request", trends_falso({}))

    def caiu(url, params=None, **_):
        raise requests.ConnectionError(f"{url}?q={params['q']}&token=segredo")
    monkeypatch.setattr(tendencias.requests, "get", caiu)
    assert tendencias._concorrencia_ml("garrafa termica") == (0, 0.0)
    monkeypatch.setattr(tendencias.requests, "get", lambda *a, **k: Resp(403, {}))
    assert tendencias._concorrencia_ml("garrafa termica") == (0, 0.0)
    monkeypatch.setattr(tendencias.requests, "get", lambda *a, **k: Resp(200, sem_json=True))
    assert tendencias._concorrencia_ml("garrafa termica") == (0, 0.0)

    registro = "\n".join(m for _, m in eventos())
    assert "falha de rede (ConnectionError)" in registro and "HTTP 403" in registro
    assert "segredo" not in registro and "garrafa" not in registro
    assert {n for n, _ in eventos()} == {"atencao"}


def test_termos_em_alta(monkeypatch):
    monkeypatch.setattr(tendencias.requests, "get", lambda *a, **k: Resp(
        200, [{"keyword": "a"}, {"url": "sem palavra"}, {"keyword": "b"}]))
    assert tendencias.termos_em_alta_ml() == ["a", "b"]
    monkeypatch.setattr(tendencias.requests, "get", lambda *a, **k: Resp(500, {}))
    assert tendencias.termos_em_alta_ml() == []
    monkeypatch.setattr(tendencias.requests, "get", lambda *a, **k: Resp(200, sem_json=True))
    assert tendencias.termos_em_alta_ml() == []


@pytest.mark.parametrize("corpo, status", [
    ({"termos": [f"t{i}" for i in range(tendencias.MAX_TERMOS + 1)]}, 422),
    ({"termos": "garrafa"}, 422),
    ({"termos": [1]}, 422),
    ({"termos": ["  "]}, 400),
    (["garrafa"], 422),
])
def test_api_de_nichos_valida_os_termos(sessao, corpo, status):
    cliente, csrf = sessao
    assert cliente.post("/api/nichos", json=corpo, headers=cabecalho(csrf)).status_code == status


def test_api_de_nichos_responde(sessao, monkeypatch, mercado):
    monkeypatch.setitem(sys.modules, "pytrends.request", trends_falso({"garrafa": [50] * 6}))
    cliente, csrf = sessao
    r = cliente.post("/api/nichos", json={"termos": ["garrafa"]}, headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["oportunidades"][0]["termo"] == "garrafa"


def test_state_recusado_aparece_como_atencao():
    from core import seguranca
    with pytest.raises(seguranca.StateInvalido):
        seguranca.consumir_state_oauth("inventado", "mercadolivre", "sessao-x")
    with conectar() as conn:
        nivel = conn.execute("SELECT nivel FROM eventos WHERE mensagem LIKE 'Retorno de autorização recusado%'"
                             " ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert nivel == "atencao"
