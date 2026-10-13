"""Lote 8: anúncios e estoque FBA da Amazon, e alertas.

Listings Items API (searchListingsItems, pageToken, 20 por página), FBA
Inventory API (getInventorySummaries, nextToken) e os quatro alertas: estoque
baixo, sincronização com falha, conexão vencida e dado velho, cada um com o
horário do último dado válido. Servidor SP-API falso; nada fala com a Amazon.
"""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from apoio import cabecalho
from config import config
from core import alertas, cofre, sincronizacao
from db import conectar

VENDEDOR = "A1VENDEDORFALSO"


class Resp:
    def __init__(self, status, texto="", headers=None):
        self.status_code = status
        self.text = texto
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text) if self.text else {}


class SpApiFalsa:
    def __init__(self, anuncios=None, estoque=None):
        self.anuncios = anuncios or []
        self.estoque = estoque or []
        self.chamadas = []
        self.lwa_status = 200
        self.fba_status = 200

    def post(self, url, data=None, timeout=None, **_):
        if self.lwa_status != 200:
            return Resp(self.lwa_status, json.dumps({"error": "invalid_grant",
                                                     "error_description": "token revogado"}))
        return Resp(200, json.dumps({"access_token": "at", "expires_in": 3600}))

    def request(self, metodo, url, headers=None, params=None, timeout=None, **_):
        params = dict(params or {})
        caminho = urlparse(url).path
        self.chamadas.append({"caminho": caminho, "params": params})
        if caminho == f"/listings/2021-08-01/items/{VENDEDOR}":
            inicio = int(params.get("pageToken", "0") or 0)
            pagina = self.anuncios[inicio:inicio + 20]
            corpo = {"numberOfResults": len(self.anuncios), "items": pagina}
            if inicio + 20 < len(self.anuncios):
                corpo["pagination"] = {"nextToken": str(inicio + 20)}
            # Números como número no JSON, como a Listings API manda.
            return Resp(200, json.dumps(corpo).replace('"@@', "").replace('@@"', ""))
        if caminho == "/fba/inventory/v1/summaries":
            if self.fba_status != 200:
                return Resp(self.fba_status, json.dumps({"errors": [{"code": "Unauthorized"}]}))
            inicio = int(params.get("nextToken", "0") or 0)
            corpo = {"payload": {"inventorySummaries": self.estoque[inicio:inicio + 2]}}
            if inicio + 2 < len(self.estoque):
                corpo["pagination"] = {"nextToken": str(inicio + 2)}
            return Resp(200, json.dumps(corpo))
        if caminho == "/orders/v0/orders":
            return Resp(200, json.dumps({"payload": {"Orders": [],
                                                     "LastUpdatedBefore": "2026-10-13T00:00:00Z"}}))
        return Resp(404, json.dumps({"errors": [{"code": "NotFound"}]}))


def anuncio(n, quantidade=10, status=("BUYABLE", "DISCOVERABLE"), preco="@@19.99@@"):
    return {
        "sku": f"SKU-{n}",
        "summaries": [{"marketplaceId": "ATVPDKIKX0DER", "asin": f"B00000000{n}",
                       "itemName": f"Produto {n}", "status": list(status),
                       "lastUpdatedDate": "2026-10-12T10:00:00Z"}],
        "offers": [{"marketplaceId": "ATVPDKIKX0DER", "offerType": "B2C",
                    "price": {"currencyCode": "USD", "amount": preco}}],
        "fulfillmentAvailability": ([{"fulfillmentChannelCode": "DEFAULT", "quantity": quantidade}]
                                    if quantidade is not None else []),
    }


def fba(n, disponivel):
    return {"sellerSku": f"SKU-{n}", "asin": f"B00000000{n}", "totalQuantity": disponivel + 1,
            "inventoryDetails": {"fulfillableQuantity": disponivel}}


@pytest.fixture
def amazon(monkeypatch):
    from conectores import amazon as modulo

    def instalar(**kw):
        api = SpApiFalsa(**kw)
        monkeypatch.setattr(modulo, "requests", api)
        monkeypatch.setenv("AMZ_SELLING_PARTNER_ID", VENDEDOR)
        cofre.guardar_segredo("AMZ_REFRESH_TOKEN", "rt")
        return api

    return instalar


def amz():
    from conectores.amazon import Amazon
    return Amazon(dormir=lambda s: None)


def abertos():
    return {(a["tipo"], a["chave"]): a for a in alertas.listar()}


# ------------------------------------------------------------ anúncios e FBA

def test_anuncios_paginam_e_guardam_status_preco_decimal_e_quantidade(amazon):
    api = amazon(anuncios=[anuncio(i) for i in range(45)])

    resumo = sincronizacao.sincronizar_anuncios(amz())

    assert resumo == {"anuncios": 45}
    paginas = [c for c in api.chamadas if "listings" in c["caminho"]]
    assert len(paginas) == 3 and paginas[1]["params"]["pageToken"] == "20"
    assert paginas[0]["params"]["includedData"] == "summaries,offers,fulfillmentAvailability"
    with conectar() as conn:
        a = dict(conn.execute("SELECT * FROM amazon_anuncios WHERE sku = 'SKU-3'").fetchone())
    assert (a["preco_dec"], a["preco_moeda"], a["quantidade_propria"]) == ("19.99", "USD", 10)
    assert a["status"] == "BUYABLE,DISCOVERABLE" and a["asin"] == "B000000003"


def test_preco_numerico_grande_nao_perde_centavo(amazon):
    amazon(anuncios=[anuncio(1, preco="@@1234567.10@@")])
    sincronizacao.sincronizar_anuncios(amz())
    with conectar() as conn:
        assert conn.execute("SELECT preco_dec FROM amazon_anuncios").fetchone()[0] == "1234567.10"


def test_anuncios_sem_selling_partner_id_nao_chamam_a_amazon(amazon, monkeypatch):
    api = amazon(anuncios=[anuncio(1)])
    monkeypatch.delenv("AMZ_SELLING_PARTNER_ID")

    resumo = sincronizacao.sincronizar_anuncios(amz())

    assert resumo["resultado"] == "erro" and api.chamadas == []
    assert "autorize a Amazon" in sincronizacao.estado(sincronizacao.FONTE_ANUNCIOS)["detalhe"]


def test_estoque_fba_pagina_e_junta_no_mesmo_sku(amazon):
    api = amazon(anuncios=[anuncio(1, quantidade=None)], estoque=[fba(1, 7), fba(2, 0), fba(3, 4)])
    sincronizacao.sincronizar_anuncios(amz())

    resumo = sincronizacao.sincronizar_estoque_fba(amz())

    assert resumo == {"estoque_fba": 3}
    fba_chamadas = [c for c in api.chamadas if "fba" in c["caminho"]]
    assert len(fba_chamadas) == 2 and fba_chamadas[1]["params"]["nextToken"] == "2"
    assert fba_chamadas[0]["params"]["details"] == "true"
    with conectar() as conn:
        a = dict(conn.execute("SELECT * FROM amazon_anuncios WHERE sku = 'SKU-1'").fetchone())
    assert (a["titulo"], a["fba_disponivel"], a["fba_total"]) == ("Produto 1", 7, 8)


def test_conta_sem_fba_fica_indisponivel_e_sem_alerta(amazon):
    api = amazon()
    api.fba_status = 403

    resumo = sincronizacao.sincronizar_estoque_fba(amz())
    alertas.avaliar()

    assert resumo["resultado"] == "indisponivel"
    assert not any(t == "sincronizacao_falhou" for t, _ in abertos())


# ------------------------------------------------------------ alertas

def test_estoque_baixo_abre_e_fecha_sozinho(amazon, monkeypatch):
    monkeypatch.setattr(config, "estoque_minimo", 3)
    api = amazon(anuncios=[anuncio(1, quantidade=2), anuncio(2, quantidade=10)])
    sincronizacao.sincronizar_anuncios(amz())

    alertas.avaliar()
    a = abertos()
    assert set(a) == {("estoque_baixo", "SKU-1")}
    assert "2 disponível(is)" in a[("estoque_baixo", "SKU-1")]["mensagem"]
    assert a[("estoque_baixo", "SKU-1")]["ultimo_dado_valido"]

    api.anuncios = [anuncio(1, quantidade=9), anuncio(2, quantidade=10)]
    sincronizacao.sincronizar_anuncios(amz())
    resumo = alertas.avaliar()
    assert resumo["resolvidos"] == 1 and abertos() == {}


def test_estoque_soma_proprio_e_fba(amazon, monkeypatch):
    monkeypatch.setattr(config, "estoque_minimo", 3)
    amazon(anuncios=[anuncio(1, quantidade=1)], estoque=[fba(1, 5)])
    sincronizacao.sincronizar_anuncios(amz())
    sincronizacao.sincronizar_estoque_fba(amz())
    alertas.avaliar()
    assert abertos() == {}


@pytest.mark.parametrize("anuncio_teste", [
    anuncio(1, quantidade=0, status=()),     # inativo: não vende, não falta
    anuncio(1, quantidade=None),             # quantidade não informada não é zero
])
def test_sem_alerta_de_estoque_para_inativo_ou_sem_quantidade(amazon, anuncio_teste):
    amazon(anuncios=[anuncio_teste])
    sincronizacao.sincronizar_anuncios(amz())
    alertas.avaliar()
    assert abertos() == {}


def test_avaliar_de_novo_nao_duplica_alerta(amazon):
    amazon(anuncios=[anuncio(1, quantidade=0)])
    sincronizacao.sincronizar_anuncios(amz())
    alertas.avaliar()
    alertas.avaliar()
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alertas").fetchone()[0] == 1


def test_falha_de_sincronizacao_vira_alerta_com_ultimo_dado_valido(amazon):
    api = amazon(anuncios=[anuncio(1)])
    sincronizacao.sincronizar_anuncios(amz())
    ultimo = sincronizacao.estado(sincronizacao.FONTE_ANUNCIOS)["ultimo_sucesso"]

    api.anuncios = None  # a próxima resposta quebra a sincronização
    api.request = lambda *a, **k: Resp(500, json.dumps({"errors": [{"code": "InternalFailure"}]}))
    sincronizacao.sincronizar_anuncios(amz())
    alertas.avaliar()

    a = abertos()[("sincronizacao_falhou", "amazon_anuncios")]
    assert a["ultimo_dado_valido"] == ultimo and ultimo in a["mensagem"]
    assert "InternalFailure" in a["mensagem"]


def test_autorizacao_recusada_vira_conexao_vencida_e_fecha_ao_reautorizar(amazon):
    api = amazon(anuncios=[anuncio(1)])
    api.lwa_status = 400

    resumo = sincronizacao.sincronizar_amazon(amz())
    alertas.avaliar()

    assert resumo["resultado"] == "conexao_vencida"
    a = abertos()[("conexao_vencida", "amazon")]
    assert "Autorize o app de novo" in a["mensagem"] and "token revogado" not in a["mensagem"]

    api.lwa_status = 200
    sincronizacao.sincronizar_amazon(amz())
    alertas.avaliar()
    assert ("conexao_vencida", "amazon") not in abertos()


def test_dado_velho_abre_quando_o_ultimo_sucesso_passa_do_limite(amazon, monkeypatch):
    monkeypatch.setattr(config, "alerta_dado_velho_horas", 6)
    amazon(anuncios=[anuncio(1)])
    sincronizacao.sincronizar_anuncios(amz())
    velho = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds")
    with conectar() as conn:
        conn.execute("UPDATE sincronizacoes SET ultimo_sucesso = ? WHERE fonte = ?",
                     (velho, sincronizacao.FONTE_ANUNCIOS))

    alertas.avaliar()
    a = abertos()[("dado_velho", "amazon_anuncios")]
    assert a["ultimo_dado_valido"] == velho and "mais de 6 h" in a["mensagem"]

    sincronizacao.sincronizar_anuncios(amz())
    alertas.avaliar()
    assert ("dado_velho", "amazon_anuncios") not in abertos()


# ------------------------------------------------------------ worker e painel

def test_worker_avalia_alertas_mesmo_sem_amazon_configurada():
    import worker
    assert worker.avaliar_alertas() == {"abertos": 0, "resolvidos": 0, "ativos": 0}


def test_worker_le_pedidos_anuncios_e_estoque_com_um_conector(amazon, monkeypatch):
    import worker
    from conectores import amazon as modulo

    amazon(anuncios=[anuncio(1)], estoque=[fba(1, 2)])
    monkeypatch.setattr(modulo.Amazon.__init__, "__defaults__", (None, lambda s: None))

    resumo = worker.sincronizar_amazon()

    assert resumo["anuncios"] == {"anuncios": 1} and resumo["estoque_fba"] == {"estoque_fba": 1}
    assert resumo["pedidos"]["pedidos"] == 0


def test_rotas_de_alertas_e_anuncios_pedem_sessao(cliente):
    assert cliente.get("/api/alertas").status_code == 401
    assert cliente.get("/api/amazon/anuncios").status_code == 401


def test_painel_lista_alertas_abertos_e_resolvidos(sessao, amazon):
    cliente, _ = sessao
    api = amazon(anuncios=[anuncio(1, quantidade=0)])
    sincronizacao.sincronizar_anuncios(amz())
    alertas.avaliar()

    abertos_api = cliente.get("/api/alertas").json()["alertas"]
    assert [a["tipo"] for a in abertos_api] == ["estoque_baixo"]
    assert abertos_api[0]["ultimo_dado_valido"]

    api.anuncios = [anuncio(1, quantidade=50)]
    sincronizacao.sincronizar_anuncios(amz())
    alertas.avaliar()
    assert cliente.get("/api/alertas").json()["alertas"] == []
    todos = cliente.get("/api/alertas", params={"todos": "true"}).json()["alertas"]
    assert todos[0]["resolvido_em"]

    anuncios = cliente.get("/api/amazon/anuncios").json()["anuncios"]
    assert anuncios[0]["sku"] == "SKU-1" and anuncios[0]["quantidade_propria"] == 50
