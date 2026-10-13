"""Lote 7: Amazon SP-API de ponta a ponta contra um servidor falso.

Autorização pelo site (Website Authorization Workflow), getOrders com
NextToken, todos os itens com getOrderItems, dinheiro em Decimal com a moeda
da Amazon, limites (429, Retry-After, x-amzn-RateLimit-Limit), erros sem o
corpo da resposta, sincronização incremental sem duplicar e nenhum dado de
comprador guardado. Nada aqui fala com a Amazon de verdade.
"""
import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

from apoio import cabecalho
from config import config
from core import cofre, sincronizacao
from db import conectar

SEGREDO_NO_CORPO = "SEGREDO-NO-CORPO-DO-ERRO-123"


class Resp:
    def __init__(self, status, corpo=None, headers=None):
        self.status_code = status
        self._corpo = corpo if corpo is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._corpo) if corpo is not None else ""

    def json(self):
        return self._corpo


class SpApiFalsa:
    """Faz as vezes do módulo requests no conector: LWA em post, SP-API em request."""

    def __init__(self, pedidos=None, itens=None, tamanho_pagina=100, itens_por_pagina=2):
        self.pedidos = pedidos or []
        self.itens = itens or {}
        self.tamanho_pagina = tamanho_pagina
        self.itens_por_pagina = itens_por_pagina
        self.posts, self.chamadas = [], []
        self.roteiro = []           # respostas forçadas, na ordem, para getOrders
        self.ultimo_ate = "2026-10-12T00:00:00Z"

    def post(self, url, data=None, timeout=None, **_):
        self.posts.append({"url": url, "data": dict(data or {})})
        if data and data.get("grant_type") == "authorization_code":
            return Resp(200, {"access_token": "at-novo", "refresh_token": "rt-amazon-novo",
                              "token_type": "bearer", "expires_in": 3600})
        return Resp(200, {"access_token": "at-falso", "expires_in": 3600})

    def request(self, metodo, url, headers=None, params=None, timeout=None, **_):
        params = dict(params or {})
        caminho = urlparse(url).path
        self.chamadas.append({"metodo": metodo, "caminho": caminho, "params": params,
                              "headers": dict(headers or {})})
        if caminho == "/orders/v0/orders":
            if self.roteiro:
                return self.roteiro.pop(0)
            inicio = int(params.get("NextToken", "0") or 0)
            pagina = self.pedidos[inicio:inicio + self.tamanho_pagina]
            carga = {"Orders": pagina, "LastUpdatedBefore": self.ultimo_ate}
            if inicio + self.tamanho_pagina < len(self.pedidos):
                carga["NextToken"] = str(inicio + self.tamanho_pagina)
            return Resp(200, {"payload": carga}, {"x-amzn-RateLimit-Limit": "0.0167"})
        if caminho.endswith("/orderItems"):
            order_id = caminho.split("/")[-2]
            todos = self.itens.get(order_id, [])
            inicio = int(params.get("NextToken", "0") or 0)
            carga = {"OrderItems": todos[inicio:inicio + self.itens_por_pagina],
                     "AmazonOrderId": order_id}
            if inicio + self.itens_por_pagina < len(todos):
                carga["NextToken"] = str(inicio + self.itens_por_pagina)
            return Resp(200, {"payload": carga})
        return Resp(404, {"errors": [{"code": "NotFound", "message": "?"}]})


def pedido(n, atualizado="2026-10-10T10:00:00Z", total="19.99", moeda="USD"):
    return {
        "AmazonOrderId": f"111-{n:07d}-0000000", "MarketplaceId": "ATVPDKIKX0DER",
        "OrderStatus": "Unshipped", "FulfillmentChannel": "MFN",
        "PurchaseDate": f"2026-10-0{1 + n % 9}T12:00:00Z", "LastUpdateDate": atualizado,
        "OrderTotal": {"CurrencyCode": moeda, "Amount": total},
        "NumberOfItemsUnshipped": 1,
        "BuyerInfo": {"BuyerEmail": "comprador-falso@marketplace.example", "BuyerName": "Nome Falso"},
        "ShippingAddress": {"AddressLine1": "Rua Falsa 123", "City": "Cidade Falsa"},
    }


def item(n, preco="19.99", moeda="USD"):
    return {"OrderItemId": f"OI-{n}", "ASIN": f"B0000000{n}", "SellerSKU": f"SKU-{n}",
            "Title": f"Produto {n}", "QuantityOrdered": 1, "QuantityShipped": 0,
            "ItemPrice": {"CurrencyCode": moeda, "Amount": preco},
            "BuyerInfo": {"GiftMessageText": "mensagem do comprador"}}


@pytest.fixture
def amazon_falsa(monkeypatch):
    from conectores import amazon

    def instalar(**kw):
        api = SpApiFalsa(**kw)
        monkeypatch.setattr(amazon, "requests", api)
        cofre.guardar_segredo("AMZ_REFRESH_TOKEN", "rt-guardado")
        return api

    return instalar


def conector(dormidas=None):
    from conectores import amazon
    return amazon.Amazon(dormir=(dormidas.append if dormidas is not None else (lambda s: None)))


def linhas(sql, *args):
    with conectar() as conn:
        return [dict(l) for l in conn.execute(sql, args)]


# ------------------------------------------------------------ leitura e paginação

def test_pagina_os_pedidos_ate_o_fim_e_guarda_todos(amazon_falsa):
    api = amazon_falsa(pedidos=[pedido(i) for i in range(250)],
                       itens={f"111-{i:07d}-0000000": [item(i)] for i in range(250)})

    resumo = sincronizacao.sincronizar_amazon(conector())

    assert resumo["pedidos"] == 250 and resumo["novos"] == 250
    assert linhas("SELECT COUNT(*) AS n FROM amazon_pedidos")[0]["n"] == 250
    paginas = [c for c in api.chamadas if c["caminho"] == "/orders/v0/orders"]
    assert len(paginas) == 3
    assert paginas[0]["params"]["MaxResultsPerPage"] == 100
    assert "LastUpdatedAfter" in paginas[0]["params"]
    # Com NextToken, só o marketplace e o token.
    assert set(paginas[1]["params"]) == {"MarketplaceIds", "NextToken"}


def test_guarda_todos_os_itens_do_pedido_seguindo_o_next_token(amazon_falsa):
    oid = "111-0000001-0000000"
    amazon_falsa(pedidos=[pedido(1)], itens={oid: [item(i) for i in range(5)]}, itens_por_pagina=2)

    sincronizacao.sincronizar_amazon(conector())

    itens = linhas("SELECT order_item_id FROM amazon_itens WHERE amazon_order_id = ?", oid)
    assert len(itens) == 5
    assert linhas("SELECT itens_lidos FROM amazon_pedidos")[0]["itens_lidos"] == 5


def test_dinheiro_em_decimal_com_a_moeda_da_amazon(amazon_falsa):
    oid = "111-0000001-0000000"
    amazon_falsa(pedidos=[pedido(1, total="1234567.10", moeda="USD")],
                 itens={oid: [item(1, preco="0.30", moeda="USD")]})

    sincronizacao.sincronizar_amazon(conector())

    p = linhas("SELECT total_dec, total_moeda FROM amazon_pedidos")[0]
    i = linhas("SELECT preco_dec, preco_moeda FROM amazon_itens")[0]
    assert (p["total_dec"], p["total_moeda"]) == ("1234567.10", "USD")
    assert (i["preco_dec"], i["preco_moeda"]) == ("0.30", "USD")


def test_nao_guarda_nenhum_dado_do_comprador(amazon_falsa, isolamento):
    oid = "111-0000001-0000000"
    amazon_falsa(pedidos=[pedido(1)], itens={oid: [item(1)]})

    sincronizacao.sincronizar_amazon(conector())

    with sqlite3.connect(config.db_path) as conn:
        despejo = "\n".join(conn.iterdump())
    for dado in ("comprador-falso", "Nome Falso", "Rua Falsa", "Cidade Falsa", "mensagem do comprador"):
        assert dado not in despejo


# ------------------------------------------------------------ incremental, sem duplicar

def test_repetir_nao_duplica_e_nao_rele_itens_de_pedido_sem_mudanca(amazon_falsa):
    oid = "111-0000001-0000000"
    api = amazon_falsa(pedidos=[pedido(1)], itens={oid: [item(1), item(2)]})

    sincronizacao.sincronizar_amazon(conector())
    chamadas_itens = sum(c["caminho"].endswith("/orderItems") for c in api.chamadas)
    segunda = sincronizacao.sincronizar_amazon(conector())

    assert segunda["novos"] == 0 and segunda["atualizados"] == 0
    assert linhas("SELECT COUNT(*) AS n FROM amazon_pedidos")[0]["n"] == 1
    assert linhas("SELECT COUNT(*) AS n FROM amazon_itens")[0]["n"] == 2
    assert sum(c["caminho"].endswith("/orderItems") for c in api.chamadas) == chamadas_itens


def test_pedido_que_mudou_e_atualizado_com_os_itens_relidos(amazon_falsa):
    oid = "111-0000001-0000000"
    api = amazon_falsa(pedidos=[pedido(1)], itens={oid: [item(1)]})
    sincronizacao.sincronizar_amazon(conector())

    api.pedidos = [{**pedido(1, atualizado="2026-10-11T09:00:00Z"), "OrderStatus": "Shipped"}]
    api.itens = {oid: [{**item(1), "QuantityShipped": 1}]}
    resumo = sincronizacao.sincronizar_amazon(conector())

    assert resumo["atualizados"] == 1
    assert linhas("SELECT status FROM amazon_pedidos")[0]["status"] == "Shipped"
    assert linhas("SELECT quantidade_enviada FROM amazon_itens")[0]["quantidade_enviada"] == 1


def test_cursor_anda_so_com_sucesso_e_a_proxima_passada_parte_dele(amazon_falsa):
    api = amazon_falsa(pedidos=[pedido(1)], itens={"111-0000001-0000000": [item(1)]})
    api.ultimo_ate = "2026-10-12T08:00:00Z"

    sincronizacao.sincronizar_amazon(conector())
    estado = sincronizacao.estado()
    assert estado["cursor"] == "2026-10-12T08:00:00Z" and estado["resultado"] == "ok"
    assert estado["ultimo_sucesso"] and estado["ultima_tentativa"]

    sincronizacao.sincronizar_amazon(conector())
    ultima = [c for c in api.chamadas if c["caminho"] == "/orders/v0/orders"][-1]
    assert ultima["params"]["LastUpdatedAfter"] == "2026-10-12T08:00:00Z"


def test_falha_no_meio_nao_move_o_cursor_nem_apaga_o_ultimo_sucesso(amazon_falsa):
    api = amazon_falsa(pedidos=[pedido(1)], itens={"111-0000001-0000000": [item(1)]})
    sincronizacao.sincronizar_amazon(conector())
    antes = sincronizacao.estado()

    api.roteiro = [Resp(400, {"errors": [{"code": "InvalidInput", "message": SEGREDO_NO_CORPO}]})]
    resumo = sincronizacao.sincronizar_amazon(conector())

    depois = sincronizacao.estado()
    assert "erro" in resumo and depois["resultado"] == "erro"
    assert depois["cursor"] == antes["cursor"] and depois["ultimo_sucesso"] == antes["ultimo_sucesso"]
    assert "InvalidInput" in depois["detalhe"] and SEGREDO_NO_CORPO not in depois["detalhe"]
    eventos = " ".join(l["mensagem"] for l in linhas("SELECT mensagem FROM eventos"))
    assert SEGREDO_NO_CORPO not in eventos


# ------------------------------------------------------------ limites

def test_429_espera_o_retry_after_e_tenta_de_novo(amazon_falsa):
    api = amazon_falsa(pedidos=[pedido(1)], itens={"111-0000001-0000000": [item(1)]})
    api.roteiro = [Resp(429, {"errors": [{"code": "QuotaExceeded"}]}, {"Retry-After": "7"})]
    dormidas = []

    resumo = sincronizacao.sincronizar_amazon(conector(dormidas))

    assert resumo["pedidos"] == 1
    assert 7.0 in dormidas


def test_429_persistente_desiste_com_teto_de_espera_e_sem_corpo(amazon_falsa):
    from conectores import amazon

    api = amazon_falsa(pedidos=[pedido(1)])
    api.roteiro = [Resp(429, {"errors": [{"code": "QuotaExceeded", "message": SEGREDO_NO_CORPO}]})
                   for _ in range(amazon.TENTATIVAS)]
    dormidas = []

    resumo = sincronizacao.sincronizar_amazon(conector(dormidas))

    assert "erro" in resumo and SEGREDO_NO_CORPO not in resumo["erro"]
    assert len(dormidas) == amazon.TENTATIVAS - 1
    assert all(s <= amazon.ESPERA_MAXIMA for s in dormidas)


def test_respeita_o_limite_informado_entre_paginas(amazon_falsa):
    amazon_falsa(pedidos=[pedido(i) for i in range(150)],
                 itens={f"111-{i:07d}-0000000": [] for i in range(150)})
    dormidas = []

    sincronizacao.sincronizar_amazon(conector(dormidas))

    # x-amzn-RateLimit-Limit: 0.0167 req/s -> ~60 s entre páginas de getOrders (com teto).
    assert any(s > 30 for s in dormidas)


# ------------------------------------------------------------ worker

def test_worker_sem_credenciais_nao_chama_a_amazon(monkeypatch):
    import worker
    from conectores import amazon

    def recusar(*_, **__):
        raise AssertionError("não devia chamar a Amazon")
    monkeypatch.setattr(amazon, "requests", type("X", (), {"post": recusar, "request": recusar}))

    assert worker.sincronizar_amazon() == "não configurada"


def test_worker_com_credenciais_sincroniza(amazon_falsa, monkeypatch):
    import worker
    from conectores import amazon

    amazon_falsa(pedidos=[pedido(1)], itens={"111-0000001-0000000": [item(1)]})
    monkeypatch.setattr(amazon.Amazon.__init__, "__defaults__", (None, lambda s: None))

    resumo = worker.sincronizar_amazon()

    assert resumo["pedidos"]["pedidos"] == 1


# ------------------------------------------------------------ autorização pelo site

@pytest.fixture
def app_amazon(monkeypatch):
    monkeypatch.setenv("AMZ_APPLICATION_ID", "amzn1.sp.solution.0000-falso")
    monkeypatch.setenv("AMZ_REDIRECT_URI", "https://painel.example/oauth/amazon/retorno")


def test_url_de_consentimento_tem_app_state_e_beta_quando_pedido(sessao, app_amazon, monkeypatch):
    cliente, csrf = sessao
    monkeypatch.setenv("AMZ_APP_VERSAO", "beta")

    r = cliente.get("/api/configuracao/url-autorizacao/amazon")

    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert (url.scheme, url.hostname, url.path) == ("https", "sellercentral.amazon.com",
                                                     "/apps/authorize/consent")
    assert q["application_id"] == ["amzn1.sp.solution.0000-falso"] and q["state"][0]
    assert q["version"] == ["beta"]


def test_seller_central_que_nao_e_da_amazon_e_recusado(sessao, app_amazon, monkeypatch):
    cliente, _ = sessao
    monkeypatch.setenv("AMZ_SELLER_CENTRAL", "https://sellercentral.amazon.com.evil.example")
    r = cliente.get("/api/configuracao/url-autorizacao/amazon")
    assert r.status_code == 400


def abrir_consentimento(cliente):
    r = cliente.get("/api/configuracao/url-autorizacao/amazon")
    assert r.status_code == 200, r.text


def test_login_devolve_o_vendedor_a_amazon_com_state_da_sessao(sessao, app_amazon):
    cliente, _ = sessao
    abrir_consentimento(cliente)
    r = cliente.get("/oauth/amazon/login", params={
        "amazon_callback_uri": "https://sellercentral.amazon.com/apps/authorize/confirm/amzn1.sp.solution.x",
        "amazon_state": "estado-da-amazon", "selling_partner_id": "A1B2C3D4E5F6G7"},
        follow_redirects=False)

    assert r.status_code == 303 and r.headers["referrer-policy"] == "no-referrer"
    destino = urlparse(r.headers["location"])
    q = parse_qs(destino.query)
    assert destino.hostname == "sellercentral.amazon.com"
    assert q["amazon_state"] == ["estado-da-amazon"] and q["state"][0]
    assert q["redirect_uri"] == ["https://painel.example/oauth/amazon/retorno"]


@pytest.mark.parametrize("callback", [
    "https://evil.example/apps/authorize/confirm/x",
    "https://sellercentral.amazon.com.evil.example/apps/authorize/confirm/x",
    "http://sellercentral.amazon.com/apps/authorize/confirm/x",
    "https://sellercentral.amazon.com/outra/rota",
    "javascript:alert(1)",
])
def test_login_nao_redireciona_para_fora_da_amazon(sessao, app_amazon, callback):
    cliente, _ = sessao
    abrir_consentimento(cliente)
    r = cliente.get("/oauth/amazon/login", params={"amazon_callback_uri": callback,
                                                  "amazon_state": "x"}, follow_redirects=False)
    assert r.status_code == 200 and "location" not in r.headers
    assert "não é da Amazon" in r.text


def test_login_sem_sessao_nao_vai_para_a_amazon(cliente, app_amazon):
    r = cliente.get("/oauth/amazon/login", params={
        "amazon_callback_uri": "https://sellercentral.amazon.com/apps/authorize/confirm/x",
        "amazon_state": "x"}, follow_redirects=False)
    assert "sellercentral" not in r.headers.get("location", "")


def _state_de_login(cliente):
    abrir_consentimento(cliente)
    r = cliente.get("/oauth/amazon/login", params={
        "amazon_callback_uri": "https://sellercentral.amazon.com/apps/authorize/confirm/x",
        "amazon_state": "a"}, follow_redirects=False)
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_retorno_troca_o_codigo_e_guarda_o_refresh_token_no_cofre(sessao, app_amazon, amazon_falsa):
    cliente, _ = sessao
    api = amazon_falsa()
    state = _state_de_login(cliente)

    r = cliente.get("/oauth/amazon/retorno", params={
        "state": state, "selling_partner_id": "A1B2C3D4E5F6G7", "spapi_oauth_code": "codigo-1"})

    assert "Amazon conectada" in r.text and r.headers["referrer-policy"] == "no-referrer"
    troca = api.posts[-1]["data"]
    assert troca["grant_type"] == "authorization_code" and troca["code"] == "codigo-1"
    assert troca["redirect_uri"] == "https://painel.example/oauth/amazon/retorno"
    assert cofre.ler("amazon", "refresh_token") == "rt-amazon-novo"
    assert "rt-amazon-novo" not in r.text


def test_retorno_com_state_forjado_ou_repetido_nao_chama_a_lwa(sessao, app_amazon, amazon_falsa):
    cliente, _ = sessao
    api = amazon_falsa()
    forjado = cliente.get("/oauth/amazon/retorno", params={
        "state": "inventado", "spapi_oauth_code": "c"})
    assert "Não deu certo" in forjado.text and api.posts == []

    state = _state_de_login(cliente)
    cliente.get("/oauth/amazon/retorno", params={"state": state, "spapi_oauth_code": "c1"})
    trocas = len(api.posts)
    repetido = cliente.get("/oauth/amazon/retorno", params={"state": state, "spapi_oauth_code": "c2"})
    assert "Não deu certo" in repetido.text and len(api.posts) == trocas


def test_retorno_com_state_de_outra_sessao_e_recusado(abrir_sessao, app_amazon, amazon_falsa):
    api = amazon_falsa()
    a, _ = abrir_sessao()
    b, _ = abrir_sessao()
    state = _state_de_login(a)
    r = b.get("/oauth/amazon/retorno", params={"state": state, "spapi_oauth_code": "c"})
    assert "Não deu certo" in r.text and api.posts == []


def test_concluir_colando_a_url_de_retorno(sessao, app_amazon, amazon_falsa):
    cliente, csrf = sessao
    amazon_falsa()
    state = _state_de_login(cliente)
    url = ("https://painel.example/oauth/amazon/retorno?state=" + state
           + "&selling_partner_id=A1B2C3D4E5F6G7&spapi_oauth_code=codigo-colado")

    r = cliente.post("/api/configuracao/concluir-amazon", json={"retorno": url},
                     headers=cabecalho(csrf))

    assert r.json() == {"ok": True, "detalhe": "Amazon conectada."}
    assert cofre.ler("amazon", "refresh_token") == "rt-amazon-novo"


@pytest.mark.parametrize("chave,valor", [
    ("AMZ_REDIRECT_URI", "http://painel.example/retorno"),
    ("AMZ_REDIRECT_URI", "https://painel.example/retorno?x=1"),
    ("AMZ_APPLICATION_ID", "app-qualquer"),
])
def test_salvar_recusa_redirect_sem_https_e_id_de_app_invalido(sessao, chave, valor):
    cliente, csrf = sessao
    r = cliente.post("/api/configuracao/salvar", json={chave: valor}, headers=cabecalho(csrf))
    assert r.status_code == 400 and chave in r.json()["detail"]


# ------------------------------------------------------------ leitura pelo painel

def test_pedidos_da_amazon_pedem_sessao(cliente):
    assert cliente.get("/api/amazon/pedidos").status_code == 401
    assert cliente.get("/api/amazon/sincronizacao").status_code == 401


def test_painel_mostra_pedidos_com_itens_e_sem_comprador(sessao, amazon_falsa):
    cliente, _ = sessao
    oid = "111-0000001-0000000"
    amazon_falsa(pedidos=[pedido(1)], itens={oid: [item(1), item(2)]})
    sincronizacao.sincronizar_amazon(conector())

    r = cliente.get("/api/amazon/pedidos")

    p = r.json()["pedidos"][0]
    assert p["amazon_order_id"] == oid and p["total_dec"] == "19.99" and len(p["itens"]) == 2
    assert "comprador" not in r.text.lower() and "Nome Falso" not in r.text
    fontes = {f["fonte"]: f for f in cliente.get("/api/amazon/sincronizacao").json()["fontes"]}
    assert fontes["amazon_pedidos"]["resultado"] == "ok"


# ------------------------------------------------------------ CSRF de associação de conta

LOGIN = {"amazon_callback_uri": "https://sellercentral.amazon.com/apps/authorize/confirm/x",
         "amazon_state": "estado-do-atacante", "selling_partner_id": "ATACANTE00001"}


def test_login_sem_consentimento_aberto_por_esta_sessao_e_recusado(sessao, app_amazon):
    """Link da URI de log-in mandado por outra pessoa: o painel não continua."""
    cliente, _ = sessao
    r = cliente.get("/oauth/amazon/login", params=LOGIN, follow_redirects=False)
    assert r.status_code == 200 and "location" not in r.headers
    assert "Nenhuma autorização foi aberta por esta sessão" in r.text


def test_consentimento_de_outra_sessao_nao_serve(abrir_sessao, app_amazon):
    a, _ = abrir_sessao()
    b, _ = abrir_sessao()
    abrir_consentimento(a)
    r = b.get("/oauth/amazon/login", params=LOGIN, follow_redirects=False)
    assert "location" not in r.headers


def test_consentimento_vale_um_login_so(sessao, app_amazon):
    cliente, _ = sessao
    abrir_consentimento(cliente)
    primeiro = cliente.get("/oauth/amazon/login", params=LOGIN, follow_redirects=False)
    segundo = cliente.get("/oauth/amazon/login", params=LOGIN, follow_redirects=False)
    assert primeiro.status_code == 303 and "location" not in segundo.headers
