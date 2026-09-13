"""
Conector Amazon (Selling Partner API).

ANTES DE USAR, LEIA:

A Amazon proíbe dropshipping em que um terceiro apareça como remetente.
A política exige que você seja o vendedor registrado, que só o SEU nome
apareça em nota, embalagem e romaneio, e que você remova qualquer
identificação do fornecedor antes do envio. Violar isso é motivo de
suspensão de conta, sem aviso e com o saldo retido.

Consequência prática pro seu modelo: na Amazon, o fluxo precisa passar
fisicamente por você (ou por um centro de fulfillment seu). Não dá pra a
fábrica despachar direto pro comprador final.

O Mercado Livre é mais tolerante, mas cobra prazo de envio agressivo —
o que conflita com o prazo típico de fábrica. Por isso o campo
`prazo_fornecedor_dias` existe na config: o worker recusa pedidos cujo
prazo do fornecedor estoure a promessa do anúncio.

Autenticação: LWA (Login with Amazon) devolve um access token de 1 hora.
Desde 2023 a Amazon não exige mais assinatura AWS SigV4 na SP-API.
"""
import time

import requests

from config import config
from db import registrar_evento

URL_LWA = "https://api.amazon.com/auth/o2/token"


class ErroAmazon(Exception):
    pass


class Amazon:
    def __init__(self, cfg=None):
        self.cfg = cfg or config.amazon
        self._access_token = None
        self._expira_em = 0

    def _renovar(self):
        if not self.cfg.configurado:
            raise ErroAmazon(
                "Credenciais da Amazon ausentes. Você precisa de uma conta "
                "Seller Central aprovada e de um app registrado no Developer "
                "Central antes de preencher AMZ_* no .env"
            )
        r = requests.post(URL_LWA, data={
            "grant_type": "refresh_token",
            "refresh_token": self.cfg.refresh_token,
            "client_id": self.cfg.lwa_client_id,
            "client_secret": self.cfg.lwa_client_secret,
        }, timeout=20)
        if r.status_code != 200:
            raise ErroAmazon(f"Falha no LWA: {r.status_code} {r.text[:200]}")
        dados = r.json()
        self._access_token = dados["access_token"]
        self._expira_em = time.time() + dados.get("expires_in", 3600) - 120
        registrar_evento("info", "amazon", "Token LWA renovado")

    @property
    def token(self) -> str:
        if not self._access_token or time.time() >= self._expira_em:
            self._renovar()
        return self._access_token

    def _req(self, metodo: str, caminho: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["x-amz-access-token"] = self.token
        r = requests.request(metodo, f"{self.cfg.regiao_endpoint}{caminho}",
                             headers=headers, timeout=30, **kwargs)
        if r.status_code == 429:
            # SP-API usa token bucket; respeitar é obrigatório.
            time.sleep(2)
            r = requests.request(metodo, f"{self.cfg.regiao_endpoint}{caminho}",
                                 headers=headers, timeout=30, **kwargs)
        if r.status_code >= 400:
            raise ErroAmazon(f"{metodo} {caminho} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def pedidos_recentes(self, desde_iso: str) -> list[dict]:
        dados = self._req("GET", "/orders/v0/orders", params={
            "MarketplaceIds": self.cfg.marketplace_id,
            "CreatedAfter": desde_iso,
            "OrderStatuses": "Unshipped,PartiallyShipped",
        })
        return dados.get("payload", {}).get("Orders", [])

    def itens_do_pedido(self, order_id: str) -> list[dict]:
        dados = self._req("GET", f"/orders/v0/orders/{order_id}/orderItems")
        return dados.get("payload", {}).get("OrderItems", [])

    def normalizar_pedido(self, bruto: dict, itens: list[dict]) -> dict:
        item = itens[0] if itens else {}
        total = bruto.get("OrderTotal", {}).get("Amount", "0")
        return {
            "marketplace": "amazon",
            "id_externo": bruto.get("AmazonOrderId", ""),
            "titulo": item.get("Title", ""),
            "sku": item.get("SellerSKU", ""),
            "quantidade": int(item.get("QuantityOrdered", 1)),
            "valor_bruto": float(total),
            "comprador_nome": bruto.get("BuyerInfo", {}).get("BuyerName", ""),
            "comprador_id": bruto.get("BuyerInfo", {}).get("BuyerEmail", ""),
            "shipping_id": "",
        }
