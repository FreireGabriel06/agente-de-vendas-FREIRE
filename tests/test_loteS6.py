"""Lote S6 da auditoria de 06/10/2026: erro de marketplace sem o corpo da
resposta nem a URL com a query, em exceção, evento, fila e HTTP (S-06, A08),
e retorno da Shopee preso a um consentimento desta sessão (S-04)."""
import json

import pytest
import requests

from core import aprovacao, cofre, erros
from db import conectar

SEGREDO = "SEGREDO-DO-CORPO-777"


class Resp:
    def __init__(self, status, corpo=None, sem_json=False):
        self.status_code = status
        self._corpo = corpo if corpo is not None else {}
        self._sem_json = sem_json
        self.headers = {}
        self.text = "<html>" + SEGREDO if sem_json else json.dumps(self._corpo)

    def json(self):
        if self._sem_json:
            raise ValueError("Expecting value")
        return self._corpo


class Http:
    """Faz as vezes do módulo requests no conector."""

    def __init__(self, post=None, request=None):
        self._post, self._request = post, request

    def post(self, url, **kw):
        return self._post(url, **kw)

    def request(self, metodo, url, **kw):
        return self._request(metodo, url, **kw)


def rede_caiu(url_com_query):
    def falhar(*_, **__):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url_com_query}")
    return falhar


def responder(resposta):
    return lambda *_, **__: resposta


# ------------------------------------------------------------ Mercado Livre

def ml_com(monkeypatch, request=None, post=None):
    from conectores import mercadolivre
    token = responder(Resp(200, {"access_token": "at-ml", "refresh_token": "rt-ml",
                                 "expires_in": 21600}))
    monkeypatch.setattr(mercadolivre, "requests", Http(post or token, request))
    cofre.guardar("mercadolivre", "refresh_token", "rt-velho-ml")
    return mercadolivre.MercadoLivre()


def test_ml_erro_http_leva_so_metodo_caminho_status_e_codigo(monkeypatch):
    from conectores.mercadolivre import ErroMercadoLivre
    ml = ml_com(monkeypatch, request=responder(
        Resp(400, {"error": "bad_request", "message": SEGREDO, "cause": [SEGREDO]})))
    with pytest.raises(ErroMercadoLivre) as e:
        ml.pedidos_recentes()
    assert str(e.value) == "GET /orders/search -> HTTP 400 (bad_request)"


def test_ml_codigo_que_nao_parece_codigo_fica_de_fora(monkeypatch):
    from conectores.mercadolivre import ErroMercadoLivre
    ml = ml_com(monkeypatch, request=responder(Resp(500, {"error": "<b>" + SEGREDO + " x"})))
    with pytest.raises(ErroMercadoLivre) as e:
        ml.pedidos_recentes()
    assert str(e.value) == "GET /orders/search -> HTTP 500"


def test_ml_falha_de_rede_nao_leva_a_url(monkeypatch):
    from conectores.mercadolivre import ErroMercadoLivre
    ml = ml_com(monkeypatch, request=rede_caiu(f"/orders/search?seller=1&q={SEGREDO}"))
    with pytest.raises(ErroMercadoLivre) as e:
        ml.pedidos_recentes()
    assert str(e.value) == "GET /orders/search -> falha de rede (ConnectionError)"


def test_ml_renovacao_recusada_nao_leva_o_corpo(monkeypatch):
    from conectores.mercadolivre import ErroMercadoLivre
    ml = ml_com(monkeypatch, post=responder(
        Resp(400, {"error": "invalid_grant", "message": SEGREDO})))
    with pytest.raises(ErroMercadoLivre) as e:
        ml.token
    assert str(e.value) == "Falha ao renovar token: POST /oauth/token -> HTTP 400 (invalid_grant)"


def test_ml_renovacao_sem_rede(monkeypatch):
    from conectores.mercadolivre import ErroMercadoLivre
    ml = ml_com(monkeypatch, post=rede_caiu(f"/oauth/token?x={SEGREDO}"))
    with pytest.raises(ErroMercadoLivre, match="falha de rede") as e:
        ml.token
    assert SEGREDO not in str(e.value)


# ------------------------------------------------------------ Shopee

def shopee_com(monkeypatch, request=None, post=None):
    from conectores import shopee
    token = responder(Resp(200, {"access_token": "at-sp-novo", "refresh_token": "rt-sp-novo",
                                 "expire_in": 14400}))
    monkeypatch.setattr(shopee, "requests", Http(post or token, request))
    cofre.guardar("shopee", "refresh_token", "rt-velho-sp")
    return shopee.Shopee()


def test_shopee_falha_de_rede_nao_vaza_o_access_token_da_query(monkeypatch):
    from conectores.shopee import ErroShopee
    s = shopee_com(monkeypatch, request=rede_caiu(
        "/api/v2/order/get_order_list?partner_id=1&access_token=at-sp-novo&sign=abc"))
    with pytest.raises(ErroShopee) as e:
        s.pedidos_recentes()
    assert str(e.value) == "GET /api/v2/order/get_order_list -> falha de rede (ConnectionError)"
    assert "at-sp-novo" not in str(e.value)


def test_shopee_erro_leva_so_o_codigo(monkeypatch):
    from conectores.shopee import ErroShopee
    s = shopee_com(monkeypatch, request=responder(
        Resp(403, {"error": "error_auth", "message": SEGREDO})))
    with pytest.raises(ErroShopee) as e:
        s.pedidos_recentes()
    assert str(e.value) == "GET /api/v2/order/get_order_list -> HTTP 403 (error_auth)"


def test_shopee_resposta_sem_json(monkeypatch):
    from conectores.shopee import ErroShopee
    s = shopee_com(monkeypatch, request=responder(Resp(502, sem_json=True)))
    with pytest.raises(ErroShopee, match="resposta sem JSON") as e:
        s.pedidos_recentes()
    assert SEGREDO not in str(e.value)


def test_shopee_renovacao_recusada(monkeypatch):
    from conectores.shopee import ErroShopee
    s = shopee_com(monkeypatch, post=responder(
        Resp(200, {"error": "error_param", "message": SEGREDO})))
    with pytest.raises(ErroShopee) as e:
        s.token
    assert str(e.value) == ("Falha ao renovar: POST /api/v2/auth/access_token/get"
                            " -> HTTP 200 (error_param)")


# ------------------------------------------------------------ Amazon

def amazon_com(monkeypatch, request=None, post=None):
    from conectores import amazon
    token = responder(Resp(200, {"access_token": "at-amz", "expires_in": 3600}))
    monkeypatch.setattr(amazon, "requests", Http(post or token, request))
    cofre.guardar_segredo("AMZ_REFRESH_TOKEN", "rt-amz")
    return amazon.Amazon(dormir=lambda s: None)


def test_amazon_falha_de_rede_na_sp_api(monkeypatch):
    from conectores.amazon import ErroAmazon
    amz = amazon_com(monkeypatch, request=rede_caiu(f"/orders/v0/orders?NextToken={SEGREDO}"))
    with pytest.raises(ErroAmazon) as e:
        amz._req("GET", "/orders/v0/orders", "getOrders", params={"NextToken": SEGREDO})
    assert str(e.value) == "getOrders: GET /orders/v0/orders -> falha de rede (ConnectionError)"


def test_amazon_falha_de_rede_no_lwa(monkeypatch):
    from conectores.amazon import ErroAmazon
    amz = amazon_com(monkeypatch, post=rede_caiu(f"https://api.amazon.com/auth/o2/token?{SEGREDO}"))
    with pytest.raises(ErroAmazon) as e:
        amz.token
    assert str(e.value) == "Falha no LWA: POST /auth/o2/token -> falha de rede (ConnectionError)"


# ------------------------------------------------------------ fila, eventos e HTTP

def _item(qid="1"):
    return aprovacao.enfileirar("resposta_cliente", f"Responder pergunta {qid}",
                                {"question_id": qid, "pergunta": "p", "resposta": "r"})


def _resultado(aid):
    with conectar() as conn:
        return conn.execute("SELECT status, resultado FROM aprovacoes WHERE id = ?",
                            (aid,)).fetchone()


def _ultimo_evento(origem):
    with conectar() as conn:
        return conn.execute("SELECT mensagem, detalhe_json FROM eventos WHERE origem = ?"
                            " ORDER BY id DESC LIMIT 1", (origem,)).fetchone()


def explodir(payload):
    raise RuntimeError(f"valor decifrado {SEGREDO}")


def test_falha_inesperada_do_executor_fica_so_com_o_tipo():
    aid = _item("1")
    with pytest.raises(RuntimeError):
        aprovacao.aprovar(aid, {"resposta_cliente": explodir})

    assert tuple(_resultado(aid)) == ("erro", "falha inesperada (RuntimeError)")
    evento = _ultimo_evento("aprovacao")
    assert SEGREDO not in evento["mensagem"] and SEGREDO not in (evento["detalhe_json"] or "")
    assert "RuntimeError" in json.loads(evento["detalhe_json"])["traceback"]


def test_erro_do_conector_fica_com_a_mensagem_montada_por_ele():
    from conectores.mercadolivre import ErroMercadoLivre

    def falhar(payload):
        raise ErroMercadoLivre("POST /answers -> HTTP 503")

    aid = _item("2")
    with pytest.raises(ErroMercadoLivre):
        aprovacao.aprovar(aid, {"resposta_cliente": falhar})
    assert tuple(_resultado(aid)) == ("erro", "POST /answers -> HTTP 503")


def test_aprovar_em_lote_tambem_saneia():
    aid = _item("3")
    sucesso, falhas = aprovacao.aprovar_em_lote([aid], {"resposta_cliente": explodir})
    assert sucesso == [] and falhas == [(aid, "falha inesperada (RuntimeError)")]


def test_http_500_da_aprovacao_nao_mostra_a_mensagem(sessao, monkeypatch):
    import worker
    monkeypatch.setitem(worker.EXECUTORES, "resposta_cliente", explodir)
    cliente, csrf = sessao
    aid = _item("4")
    r = cliente.post(f"/api/aprovar/{aid}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 500
    assert r.json()["detail"] == "falha inesperada (RuntimeError)" and SEGREDO not in r.text


def test_mensagem_segura():
    assert erros.mensagem_segura(cofre.ErroCofre("cofre trancado")) == "cofre trancado"
    assert erros.mensagem_segura(ValueError(SEGREDO)) == "falha inesperada (ValueError)"
    assert erros.mensagem_segura(aprovacao.JaDecidida("já foi")) == "já foi"
    assert len(erros.mensagem_segura(erros.MensagemSegura("x" * 900))) == 500
    assert erros.descrever("GET", "https://h.example/a/b?token=1", 404, "not_found") == \
        "GET /a/b -> HTTP 404 (not_found)"


# ------------------------------------------------------------ retorno da Shopee

@pytest.fixture
def troca_falsa(monkeypatch):
    from painel import configurar
    chamadas = []
    monkeypatch.setattr(configurar, "shopee_trocar_code",
                        lambda code, shop_id: chamadas.append((code, shop_id))
                        or {"shop_id": shop_id, "expira_em_seg": 14400})
    return chamadas


def abrir_autorizacao(cliente):
    assert cliente.get("/api/configuracao/url-autorizacao/shopee").status_code == 200


def retorno(cliente, shop_id="2000002", code="CODIGO"):
    return cliente.get("/oauth/shopee/retorno", params={"code": code, "shop_id": shop_id})


def test_retorno_sem_autorizacao_aberta_nao_gasta_o_codigo(sessao, troca_falsa):
    cliente, _ = sessao
    r = retorno(cliente)
    assert "Nenhuma autorização foi aberta por esta sessão" in r.text
    assert troca_falsa == []


def test_retorno_de_outra_sessao_nao_vale(abrir_sessao, troca_falsa):
    dono, _ = abrir_sessao()
    outro, _ = abrir_sessao()
    abrir_autorizacao(dono)
    assert "Nenhuma autorização" in retorno(outro).text and troca_falsa == []
    assert "Shopee conectada" in retorno(dono).text
    assert troca_falsa == [("CODIGO", "2000002")]


def test_consentimento_vale_uma_vez(sessao, troca_falsa):
    cliente, _ = sessao
    abrir_autorizacao(cliente)
    assert "Shopee conectada" in retorno(cliente).text
    assert "Nenhuma autorização" in retorno(cliente, code="OUTRO").text
    assert troca_falsa == [("CODIGO", "2000002")]


@pytest.mark.parametrize("shop_id", ["<script>x</script>", "12a", "", "1" * 21])
def test_shop_id_invalido_e_recusado_sem_eco(sessao, troca_falsa, shop_id):
    cliente, _ = sessao
    abrir_autorizacao(cliente)
    r = retorno(cliente, shop_id=shop_id)
    assert "shop_id inválido" in r.text and "<script>" not in r.text
    assert troca_falsa == []


def test_iniciar_pela_rota_antiga_tambem_abre_o_consentimento(sessao, troca_falsa):
    cliente, _ = sessao
    assert cliente.get("/oauth/shopee/iniciar", follow_redirects=False).status_code == 307
    assert "Shopee conectada" in retorno(cliente).text
