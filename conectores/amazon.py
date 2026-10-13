"""
Conector Amazon (Selling Partner API).

ANTES DE USAR, LEIA:

A Amazon proíbe dropshipping em que um terceiro apareça como remetente.
A política exige que você seja o vendedor registrado, que só o SEU nome
apareça em nota, embalagem e romaneio, e que você remova qualquer
identificação do fornecedor antes do envio. Violar isso é motivo de
suspensão de conta, sem aviso e com o saldo retido.

Consequência prática: pedidos da Amazon não entram no fluxo de compra com
fornecedor. Eles são lidos para monitoramento (tabelas amazon_pedidos e
amazon_itens, core/sincronizacao.py) e nada é escrito na Amazon.

Autenticação: LWA (Login with Amazon) devolve um access token de 1 hora.
Desde 2023 a Amazon não exige mais assinatura AWS SigV4 na SP-API.

Fontes (conferidas em 13/10/2026):
- Modelo oficial da Orders API v0 (github.com/amzn/selling-partner-api-models,
  models/orders-api-model/ordersV0.json): getOrders pagina com NextToken (até
  100 por página), getOrderItems também; limites padrão 0,0167 req/s (rajada
  20) e 0,5 req/s (rajada 30); o limite real vem em x-amzn-RateLimit-Limit;
  Money = {CurrencyCode, Amount} com Amount em texto.
- Website Authorization Workflow (developer-docs.amazon.com/sp-api): URI de
  consentimento /apps/authorize/consent com application_id e state; retorno
  com state, selling_partner_id e spapi_oauth_code, que vale 5 minutos; troca
  na LWA com grant_type=authorization_code, code, redirect_uri, client_id e
  client_secret; cabeçalho Referrer-Policy: no-referrer.
Nada disso foi validado contra uma conta real ainda.
"""
import os
import random
import time
from decimal import Decimal

import requests

from config import config
from core import cofre, dinheiro
from db import registrar_evento

URL_LWA = "https://api.amazon.com/auth/o2/token"

# Quantas vezes repetir uma chamada recusada por limite (429) ou por
# indisponibilidade temporária (500, 502, 503, 504), e o teto de cada espera.
TENTATIVAS = 5
ESPERA_MAXIMA = 60.0
ESPERA_BASE = 2.0

TAMANHO_PAGINA = 100  # máximo aceito por getOrders


class ErroAmazon(Exception):
    pass


def _codigo_do_erro(resposta) -> str:
    """Só o código de erro que a SP-API manda (errors[0].code), nunca o corpo:
    a mensagem pode trazer dado do vendedor ou ecoar o pedido."""
    try:
        corpo = resposta.json()
    except ValueError:
        return "sem_codigo"
    erros = corpo.get("errors") if isinstance(corpo, dict) else None
    if isinstance(erros, list) and erros and isinstance(erros[0], dict):
        codigo = str(erros[0].get("code", ""))
        if codigo.replace("_", "").isalnum() and len(codigo) <= 64:
            return codigo
    erro_lwa = corpo.get("error") if isinstance(corpo, dict) else None
    if isinstance(erro_lwa, str) and erro_lwa.replace("_", "").isalnum() and len(erro_lwa) <= 64:
        return erro_lwa
    return "sem_codigo"


def dinheiro_amazon(money) -> tuple[Decimal | None, str | None]:
    """Money da SP-API ({CurrencyCode, Amount}) em Decimal pelo texto, sem
    passar por float. Ausente ou ilegível vira (None, None)."""
    if not isinstance(money, dict):
        return None, None
    valor = dinheiro.do_texto(money.get("Amount"))
    return valor, dinheiro.moeda_lida(money.get("CurrencyCode"))


def trocar_codigo(code: str, redirect_uri: str) -> dict:
    """Troca o spapi_oauth_code por tokens na LWA (passo 12 do fluxo). Devolve
    o JSON da LWA; quem chama guarda o refresh token no cofre e não o mostra."""
    cfg = config.amazon
    client_id = (os.getenv("AMZ_LWA_CLIENT_ID") or cfg.lwa_client_id or "").strip()
    if not (client_id and cfg.lwa_client_secret):
        raise ErroAmazon("Salve o client ID e o client secret LWA da Amazon primeiro.")
    r = requests.post(URL_LWA, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": cfg.lwa_client_secret,
    }, timeout=20)
    if r.status_code != 200:
        raise ErroAmazon(f"A Amazon recusou a troca do código ({r.status_code}, "
                         f"{_codigo_do_erro(r)}). O código vale 5 minutos e uma vez só: "
                         "comece a autorização de novo.")
    dados = r.json()
    if not dados.get("refresh_token"):
        raise ErroAmazon("A Amazon não devolveu o refresh token. Comece a autorização de novo.")
    return dados


class Amazon:
    def __init__(self, cfg=None, dormir=time.sleep):
        self.cfg = cfg or config.amazon
        self._access_token = None
        self._expira_em = 0
        self._dormir = dormir
        # Intervalo mínimo entre chamadas de cada operação, aprendido do
        # cabeçalho x-amzn-RateLimit-Limit (requisições por segundo).
        self._intervalo: dict[str, float] = {}
        self._ultima: dict[str, float] = {}

    def _renovar(self):
        if not self.cfg.configurado:
            raise ErroAmazon(
                "Credenciais da Amazon ausentes. Você precisa de uma conta "
                "Seller Central aprovada e de um app registrado no Developer "
                "Central antes de salvar as credenciais AMZ_* no painel"
            )
        r = requests.post(URL_LWA, data={
            "grant_type": "refresh_token",
            "refresh_token": self.cfg.refresh_token,
            "client_id": self.cfg.lwa_client_id,
            "client_secret": self.cfg.lwa_client_secret,
        }, timeout=20)
        if r.status_code != 200:
            raise ErroAmazon(f"Falha no LWA ({r.status_code}, {_codigo_do_erro(r)}). "
                             "Confira as credenciais ou autorize o app de novo.")
        dados = r.json()
        self._access_token = dados["access_token"]
        self._expira_em = time.time() + dados.get("expires_in", 3600) - 120
        registrar_evento("info", "amazon", "Token LWA renovado")

    @property
    def token(self) -> str:
        # Secret e refresh token vêm do cofre na hora do uso (config.amazon).
        try:
            if not self._access_token or time.time() >= self._expira_em:
                self._renovar()
        except cofre.ErroCofre as e:
            raise ErroAmazon(str(e)) from None
        return self._access_token

    def _esperar_vez(self, operacao: str):
        intervalo = self._intervalo.get(operacao)
        anterior = self._ultima.get(operacao)
        if intervalo and anterior is not None:
            falta = anterior + intervalo - time.monotonic()
            if falta > 0:
                self._dormir(min(falta, ESPERA_MAXIMA))

    def _aprender_limite(self, operacao: str, resposta):
        bruto = resposta.headers.get("x-amzn-RateLimit-Limit") if resposta.headers else None
        try:
            taxa = float(bruto) if bruto is not None else None
        except ValueError:
            taxa = None
        if taxa and taxa > 0:
            self._intervalo[operacao] = min(1.0 / taxa, ESPERA_MAXIMA)

    def _espera_para(self, resposta, tentativa: int) -> float:
        retry = resposta.headers.get("Retry-After") if resposta.headers else None
        try:
            pedido = float(retry) if retry is not None else None
        except ValueError:
            pedido = None
        if pedido is not None and pedido >= 0:
            return min(pedido, ESPERA_MAXIMA)
        # Espera exponencial com jitter, com teto.
        return min(ESPERA_BASE * (2 ** tentativa) * (0.5 + random.random()), ESPERA_MAXIMA)

    def _req(self, metodo: str, caminho: str, operacao: str, **kwargs):
        for tentativa in range(TENTATIVAS):
            self._esperar_vez(operacao)
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["x-amz-access-token"] = self.token
            r = requests.request(metodo, f"{self.cfg.regiao_endpoint}{caminho}",
                                 headers=headers, timeout=30, **kwargs)
            self._ultima[operacao] = time.monotonic()
            self._aprender_limite(operacao, r)
            if r.status_code in (429, 500, 502, 503, 504) and tentativa < TENTATIVAS - 1:
                espera = self._espera_para(r, tentativa)
                registrar_evento("atencao", "amazon",
                                 f"{operacao}: HTTP {r.status_code}; nova tentativa em {espera:.1f} s")
                self._dormir(espera)
                kwargs["headers"] = {k: v for k, v in headers.items() if k != "x-amz-access-token"}
                continue
            if r.status_code == 403 and tentativa == 0:
                # Token vencido antes da hora: renova uma vez e repete.
                self._access_token = None
                kwargs["headers"] = {k: v for k, v in headers.items() if k != "x-amz-access-token"}
                continue
            if r.status_code >= 400:
                raise ErroAmazon(f"{operacao} -> HTTP {r.status_code} ({_codigo_do_erro(r)})")
            return r.json() if r.text else {}
        raise ErroAmazon(f"{operacao}: sem resposta válida depois de {TENTATIVAS} tentativas")

    def paginas_de_pedidos(self, atualizados_desde: str):
        """Gera (pedidos, last_updated_before) por página de getOrders, com
        NextToken até acabar. Filtra por LastUpdatedAfter, que pega pedidos
        novos e mudanças de status (ordersV0: "an update is defined as any
        change in order status, including the creation of a new order")."""
        params = {
            "MarketplaceIds": self.cfg.marketplace_id,
            "LastUpdatedAfter": atualizados_desde,
            "MaxResultsPerPage": TAMANHO_PAGINA,
        }
        while True:
            dados = self._req("GET", "/orders/v0/orders", "getOrders", params=params)
            carga = dados.get("payload") or {}
            yield carga.get("Orders") or [], carga.get("LastUpdatedBefore")
            proximo = carga.get("NextToken")
            if not proximo:
                return
            params = {"MarketplaceIds": self.cfg.marketplace_id, "NextToken": proximo}

    def itens_do_pedido(self, order_id: str) -> list[dict]:
        """Todos os itens do pedido, seguindo o NextToken de getOrderItems."""
        itens, params = [], {}
        while True:
            dados = self._req("GET", f"/orders/v0/orders/{order_id}/orderItems",
                              "getOrderItems", params=params)
            carga = dados.get("payload") or {}
            itens.extend(carga.get("OrderItems") or [])
            proximo = carga.get("NextToken")
            if not proximo:
                return itens
            params = {"NextToken": proximo}


def normalizar_pedido(bruto: dict) -> dict:
    """Só o que o monitoramento usa. BuyerInfo, ShippingAddress e qualquer
    outro dado do comprador ficam de fora de propósito."""
    total, moeda = dinheiro_amazon(bruto.get("OrderTotal"))
    return {
        "amazon_order_id": str(bruto.get("AmazonOrderId") or ""),
        "marketplace_id": bruto.get("MarketplaceId"),
        "status": bruto.get("OrderStatus"),
        "canal": bruto.get("FulfillmentChannel"),
        "comprado_em": bruto.get("PurchaseDate"),
        "atualizado_na_amazon": bruto.get("LastUpdateDate"),
        "total_dec": dinheiro.texto(total) if total is not None else None,
        "total_moeda": moeda,
        "itens_pendentes": bruto.get("NumberOfItemsUnshipped"),
    }


def normalizar_item(bruto: dict) -> dict:
    preco, moeda = dinheiro_amazon(bruto.get("ItemPrice"))
    return {
        "order_item_id": str(bruto.get("OrderItemId") or ""),
        "asin": bruto.get("ASIN"),
        "sku": bruto.get("SellerSKU"),
        "titulo": bruto.get("Title"),
        "quantidade": bruto.get("QuantityOrdered"),
        "quantidade_enviada": bruto.get("QuantityShipped"),
        "preco_dec": dinheiro.texto(preco) if preco is not None else None,
        "preco_moeda": moeda,
    }
