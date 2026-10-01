"""Lote 2: state do OAuth preso à sessão, escape e mensagens fixas.

O endpoint de token do Mercado Livre é trocado por um falso: conta as
chamadas e devolve a resposta que o teste escolher.
"""
import base64
import hashlib
import json
import time
import types
import urllib.parse

import pytest

from apoio import cabecalho
from core import cofre, seguranca
from db import conectar
from painel import app as modulo_app
from painel import configurar

SID_A = "sessao-a-" + "a" * 40
SID_B = "sessao-b-" + "b" * 40
RECUSA = "Clique em autorizar de novo"
# Escrito por extenso, e não seguranca.STATE_VALIDADE_S: se a constante mudar,
# o teste reprova. RFC 9700, seção 2.1.
DEZ_MINUTOS = 10 * 60


class Resposta:
    def __init__(self, status, corpo):
        self.status_code = status
        self.text = corpo if isinstance(corpo, str) else json.dumps(corpo)

    def json(self):
        return json.loads(self.text)


class TokenFalso:
    def __init__(self):
        self.chamadas = []
        self.resposta = Resposta(200, {"refresh_token": "rt-falso", "access_token": "at-falso",
                                       "user_id": 4242, "expires_in": 21600})

    def post(self, url, data=None, timeout=None, **_):
        self.chamadas.append({"url": url, **(data or {})})
        return self.resposta


@pytest.fixture
def token(monkeypatch):
    falso = TokenFalso()
    monkeypatch.setattr(configurar, "requests", types.SimpleNamespace(post=falso.post))
    return falso


def parametros(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


def novo_state(sid=SID_A):
    return parametros(configurar.ml_url_autorizacao(sid))["state"][0]


def retorno(state, code="CODIGO-TESTE"):
    return "https://localhost:8777/oauth/ml/retorno?" + urllib.parse.urlencode(
        {"code": code, "state": state})


def tentar(token, entrada, sid):
    """Devolve (tipo do resultado, mensagem, chamadas ao marketplace)."""
    antes = len(token.chamadas)
    try:
        configurar.ml_trocar_code(entrada, sid)
        tipo, mensagem = "ok", ""
    except seguranca.StateInvalido as e:
        tipo, mensagem = "state", str(e)
    except ValueError as e:
        tipo, mensagem = "valor", str(e)
    return tipo, mensagem, len(token.chamadas) - antes


def adiantar_relogio(monkeypatch, segundos):
    """O state passa a ter `segundos` de idade a mais para core.seguranca."""
    monkeypatch.setattr(seguranca, "time", types.SimpleNamespace(
        monotonic=lambda: time.monotonic() + segundos))


def recusas_registradas():
    with conectar() as conn:
        return [m[0] for m in conn.execute(
            "SELECT mensagem FROM eventos WHERE origem = 'seguranca'"
            " AND mensagem LIKE 'Retorno de autorização recusado%'")]


# ------------------------------------------------------------------ estrutura

def test_rota_de_retorno_do_ml_existe_uma_vez():
    rotas = [r for r in modulo_app.app.routes if getattr(r, "path", "") == "/oauth/ml/retorno"]
    assert len(rotas) == 1


# ------------------------------------------------------------ state e troca

def test_state_valido_troca_uma_vez_com_pkce(token, isolamento):
    url = configurar.ml_url_autorizacao(SID_A)
    state = parametros(url)["state"][0]
    assert len(state) >= 43  # 256 bits em base64url

    assert tentar(token, retorno(state), SID_A)[::2] == ("ok", 1)

    verifier = token.chamadas[-1]["code_verifier"]
    desafio = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert desafio == parametros(url)["code_challenge"][0]
    assert parametros(url)["code_challenge_method"] == ["S256"]
    # Os tokens novos foram para o cofre; o .env recebe só o que não é segredo.
    assert cofre.ler("mercadolivre", "refresh_token") == "rt-falso"
    assert cofre.ler("mercadolivre", "access_token") == "at-falso"
    env = (isolamento / ".env").read_text(encoding="utf-8")
    assert "rt-falso" not in env and "at-falso" not in env
    assert "ML_SELLER_ID=4242" in env


def test_state_reutilizado_e_recusado(token):
    state = novo_state()
    assert tentar(token, retorno(state), SID_A)[0] == "ok"
    assert tentar(token, retorno(state), SID_A)[::2] == ("state", 0)


def test_so_o_codigo_sem_state_e_recusado(token):
    novo_state()
    assert tentar(token, "CODIGO-TESTE", SID_A)[::2] == ("valor", 0)


def test_state_forjado_e_recusado(token):
    novo_state()
    assert tentar(token, retorno("forjado-" + "z" * 40), SID_A)[::2] == ("state", 0)


def test_state_de_outra_sessao_e_recusado_e_deixa_de_valer(token):
    state = novo_state(SID_A)
    assert tentar(token, retorno(state), SID_B)[::2] == ("state", 0)
    # Uso único mesmo quando quem tentou foi outra sessão.
    assert tentar(token, retorno(state), SID_A)[::2] == ("state", 0)


def test_retorno_sem_sessao_e_recusado(token):
    state = novo_state()
    assert tentar(token, retorno(state), None)[::2] == ("state", 0)


def test_state_vencido_e_recusado(token, monkeypatch):
    state = novo_state()
    adiantar_relogio(monkeypatch, DEZ_MINUTOS + 1)
    assert tentar(token, retorno(state), SID_A)[::2] == ("state", 0)


def test_state_vale_10_minutos_e_nao_mais(token, monkeypatch):
    dentro, fora = novo_state(), novo_state()
    adiantar_relogio(monkeypatch, 9 * 60)
    assert tentar(token, retorno(dentro), SID_A)[::2] == ("ok", 1)
    adiantar_relogio(monkeypatch, DEZ_MINUTOS + 1)
    assert tentar(token, retorno(fora), SID_A)[::2] == ("state", 0)


def test_state_de_outro_provedor_e_recusado(token):
    state = seguranca.criar_state_oauth("amazon", SID_A)
    assert tentar(token, retorno(state), SID_A)[::2] == ("state", 0)


def test_sem_sessao_nao_inicia_autorizacao():
    with pytest.raises(seguranca.StateInvalido):
        configurar.ml_url_autorizacao(None)
    assert seguranca._states == {}


def test_autorizacoes_pendentes_tem_limite():
    primeiro = seguranca.criar_state_oauth("amazon", SID_A)
    for _ in range(seguranca.STATE_PENDENTES_MAX + 20):
        ultimo = seguranca.criar_state_oauth("amazon", SID_A)
    assert len(seguranca._states) == seguranca.STATE_PENDENTES_MAX
    assert primeiro not in seguranca._states  # sai o mais antigo
    assert ultimo in seguranca._states


# ------------------------------------------------- nada de fora volta na tela

def test_erro_em_html_na_url_colada_nao_volta_na_mensagem(token):
    carga = "<img src=x onerror=alert(1)>"
    tipo, mensagem, chamadas = tentar(
        token, "https://localhost:8777/oauth/ml/retorno?error=" + urllib.parse.quote(carga), SID_A)
    assert (tipo, chamadas) == ("valor", 0)
    assert "<" not in mensagem and "onerror" not in mensagem
    assert "(sem_codigo)" in mensagem


def test_corpo_html_do_marketplace_nao_volta_na_mensagem(token):
    token.resposta = Resposta(400, "<html><script>alert(1)</script> access_token=VAZADO</html>")
    tipo, mensagem, chamadas = tentar(token, retorno(novo_state()), SID_A)
    assert (tipo, chamadas) == ("valor", 1)
    assert "<" not in mensagem and "VAZADO" not in mensagem


def test_recusa_oauth_mostra_o_codigo_e_esconde_a_descricao(token):
    token.resposta = Resposta(400, {"error": "invalid_grant", "error_description": "<b>fora</b>"})
    tipo, mensagem, _ = tentar(token, retorno(novo_state()), SID_A)
    assert tipo == "valor"
    assert "invalid_grant" in mensagem
    assert "<b>" not in mensagem and "fora" not in mensagem


def test_recusas_sao_registradas_sem_o_valor_do_state(token, monkeypatch):
    usados = []

    reutilizado = novo_state()
    tentar(token, retorno(reutilizado), SID_A)
    tentar(token, retorno(reutilizado), SID_A)
    usados.append(reutilizado)

    forjado = "forjado-" + "z" * 40
    tentar(token, retorno(forjado), SID_A)
    usados.append(forjado)

    alheio = novo_state()
    tentar(token, retorno(alheio), SID_B)
    usados.append(alheio)

    sem_sessao = novo_state()
    tentar(token, retorno(sem_sessao), None)
    usados.append(sem_sessao)

    outro_provedor = seguranca.criar_state_oauth("amazon", SID_A)
    tentar(token, retorno(outro_provedor), SID_A)
    usados.append(outro_provedor)

    vencido = novo_state()
    adiantar_relogio(monkeypatch, DEZ_MINUTOS + 1)
    tentar(token, retorno(vencido), SID_A)
    usados.append(vencido)

    recusas = recusas_registradas()
    assert len(recusas) == 6
    assert not any(state in mensagem for mensagem in recusas for state in usados)
    motivos = " | ".join(recusas)
    for motivo in ("desconhecido ou já usado", "de outra sessão", "de outro provedor", "vencido"):
        assert motivo in motivos


# ------------------------------------------------------- páginas de retorno

def test_pagina_de_retorno_escapa_html():
    pagina = modulo_app._pagina_retorno(False, "<script>x</script>")
    assert "&lt;script&gt;" in pagina
    assert "<script>" not in pagina


def test_retorno_da_shopee_com_erro_nao_ecoa_nada(sessao, monkeypatch):
    from conectores.shopee import ErroShopee

    def falha(code, shop_id):
        raise ErroShopee("<script>alert(1)</script> refresh_token=VAZADO")

    monkeypatch.setattr(configurar, "shopee_trocar_code", falha)
    cliente, _ = sessao
    r = cliente.get("/oauth/shopee/retorno", params={"code": "x", "shop_id": "<script>alert(2)</script>"})

    assert r.status_code == 200
    assert "<script>" not in r.text and "VAZADO" not in r.text
    with conectar() as conn:
        ultimo = conn.execute("SELECT mensagem FROM eventos WHERE origem = 'configuracao'"
                              " ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert ultimo == "Falha ao conectar Shopee: ErroShopee"


# ------------------------------------------------------------------- HTTP

def test_fluxo_http_com_duas_sessoes(token, abrir_sessao):
    sessao_a, csrf_a = abrir_sessao()

    r = sessao_a.get("/oauth/ml/retorno", params={"error": "<script>alert(1)</script>"})
    assert r.status_code == 200 and "<script>" not in r.text

    r = sessao_a.get("/oauth/ml/retorno", params={"code": "x", "state": "<script>alert(1)</script>"})
    assert "<script>" not in r.text and RECUSA in r.text

    r = sessao_a.get("/api/configuracao/url-autorizacao/mercadolivre")
    assert r.status_code == 200
    url_a = r.json()["url"]
    retorno_a = retorno(parametros(url_a)["state"][0])

    # Sem o token CSRF nem chega a olhar o state.
    assert sessao_a.post("/api/configuracao/concluir-ml", json={"retorno": retorno_a}).status_code == 403

    sessao_b, csrf_b = abrir_sessao()
    r = sessao_b.post("/api/configuracao/concluir-ml", json={"retorno": retorno_a},
                      headers=cabecalho(csrf_b))
    assert r.status_code == 200
    assert r.json()["ok"] is False and RECUSA in r.json()["detalhe"]

    # A sessão A continua aberta, mas o state já foi gasto pela tentativa alheia.
    assert sessao_a.get("/api/sessao").status_code == 200
    r = sessao_a.post("/api/configuracao/concluir-ml", json={"retorno": retorno_a},
                      headers=cabecalho(csrf_a))
    assert r.json()["ok"] is False and RECUSA in r.json()["detalhe"]

    assert token.chamadas == []


def test_fluxo_http_conclui_na_propria_sessao(token, sessao):
    cliente, csrf = sessao

    url = cliente.get("/api/configuracao/url-autorizacao/mercadolivre").json()["url"]
    r = cliente.post("/api/configuracao/concluir-ml",
                     json={"retorno": retorno(parametros(url)["state"][0])}, headers=cabecalho(csrf))
    assert r.json() == {"ok": True, "detalhe": "Conectado. Vendedor 4242."}

    # O retorno direto pela rota do painel segue a mesma regra.
    url = cliente.get("/api/configuracao/url-autorizacao/mercadolivre").json()["url"]
    r = cliente.get("/oauth/ml/retorno", params={"code": "C2", "state": parametros(url)["state"][0]})
    assert "Mercado Livre conectado. Vendedor 4242." in r.text
    assert len(token.chamadas) == 2
