"""
Conector Shopee (Open Platform v2).

Diferente do ML e da Amazon, a Shopee não usa Bearer token. Toda chamada
carrega uma assinatura HMAC-SHA256 na query string, montada assim:

    base   = partner_id + caminho + timestamp            (APIs públicas)
    base   = partner_id + caminho + timestamp + token + shop_id   (APIs de loja)
    sign   = hex(HMAC_SHA256(base, partner_key))

Dois detalhes que quebram integração e não estão óbvios na documentação:

  - O access_token vale 4 HORAS. Muito mais curto que os outros. Se o worker
    roda de 5 em 5 minutos, ele vai renovar várias vezes por dia; o refresh
    precisa ser à prova de falha.
  - O link de autorização expira em 5 minutos. Se você gerar e demorar pra
    clicar, dá "Invalid timestamp" e você acha que errou a chave.

URLs de imagem da Shopee expiram — guarde o ID da imagem, nunca a URL.
"""
import hashlib
import hmac
import json
import time
from pathlib import Path

import requests

from config import BASE_DIR
from db import registrar_evento
import os

ARQ_TOKEN = BASE_DIR / ".token_shopee.json"

HOST_PROD = "https://partner.shopeemobile.com"
HOST_TESTE = "https://partner.test-stable.shopeemobile.com"


class ErroShopee(Exception):
    pass


class Shopee:
    def __init__(self):
        self.partner_id = int(os.getenv("SHOPEE_PARTNER_ID", "0") or 0)
        self.partner_key = os.getenv("SHOPEE_PARTNER_KEY", "")
        self.shop_id = int(os.getenv("SHOPEE_SHOP_ID", "0") or 0)
        self.host = HOST_TESTE if os.getenv("SHOPEE_SANDBOX", "true").lower() == "true" else HOST_PROD
        self._access_token = ""
        self._refresh_token = os.getenv("SHOPEE_REFRESH_TOKEN", "")
        self._expira_em = 0
        self._carregar()

    @property
    def configurado(self) -> bool:
        return bool(self.partner_id and self.partner_key and self.shop_id)

    # ------------------------------------------------------------ Token

    def _carregar(self):
        if ARQ_TOKEN.exists():
            d = json.loads(ARQ_TOKEN.read_text())
            self._access_token = d.get("access_token", "")
            self._refresh_token = d.get("refresh_token", self._refresh_token)
            self._expira_em = d.get("expira_em", 0)

    def _salvar(self, d: dict):
        ARQ_TOKEN.write_text(json.dumps({
            "access_token": d["access_token"],
            "refresh_token": d["refresh_token"],
            # Token vale 4h; renovamos 10 min antes pra não pegar a borda.
            "expira_em": int(time.time()) + d.get("expire_in", 14400) - 600,
        }, indent=2))
        ARQ_TOKEN.chmod(0o600)

    def _assinar(self, caminho: str, timestamp: int, com_loja: bool = True) -> str:
        base = f"{self.partner_id}{caminho}{timestamp}"
        if com_loja:
            base += f"{self._access_token}{self.shop_id}"
        return hmac.new(self.partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()

    def url_autorizacao(self, redirect: str) -> str:
        """
        Gera o link pro lojista autorizar o app. Vale 5 minutos — abra logo.
        Depois de autorizar, a Shopee redireciona com ?code=...&shop_id=...
        """
        caminho = "/api/v2/shop/auth_partner"
        ts = int(time.time())
        sign = self._assinar(caminho, ts, com_loja=False)
        return (f"{self.host}{caminho}?partner_id={self.partner_id}"
                f"&timestamp={ts}&sign={sign}&redirect={redirect}")

    def trocar_code(self, code: str) -> dict:
        """Troca o code da autorização pelo par access/refresh token."""
        caminho = "/api/v2/auth/token/get"
        ts = int(time.time())
        r = requests.post(
            f"{self.host}{caminho}",
            params={"partner_id": self.partner_id, "timestamp": ts,
                    "sign": self._assinar(caminho, ts, com_loja=False)},
            json={"code": code, "partner_id": self.partner_id, "shop_id": self.shop_id},
            timeout=25,
        )
        d = r.json()
        if d.get("error"):
            raise ErroShopee(f"{d['error']}: {d.get('message','')}")
        self._access_token = d["access_token"]
        self._refresh_token = d["refresh_token"]
        self._salvar(d)
        return d

    def _renovar(self):
        if not self.configurado:
            raise ErroShopee("Credenciais da Shopee ausentes. Preencha SHOPEE_* no .env")
        if not self._refresh_token:
            raise ErroShopee("Sem refresh token. Rode o fluxo de autorização primeiro.")
        caminho = "/api/v2/auth/access_token/get"
        ts = int(time.time())
        r = requests.post(
            f"{self.host}{caminho}",
            params={"partner_id": self.partner_id, "timestamp": ts,
                    "sign": self._assinar(caminho, ts, com_loja=False)},
            json={"refresh_token": self._refresh_token,
                  "partner_id": self.partner_id, "shop_id": self.shop_id},
            timeout=25,
        )
        d = r.json()
        if d.get("error"):
            raise ErroShopee(f"Falha ao renovar: {d['error']} {d.get('message','')}")
        self._access_token = d["access_token"]
        self._refresh_token = d["refresh_token"]
        self._expira_em = int(time.time()) + d.get("expire_in", 14400) - 600
        self._salvar(d)
        registrar_evento("info", "shopee", "Token renovado")

    @property
    def token(self) -> str:
        if not self._access_token or time.time() >= self._expira_em:
            self._renovar()
        return self._access_token

    # ------------------------------------------------------------ Chamadas

    def _req(self, metodo: str, caminho: str, params: dict | None = None, corpo: dict | None = None):
        self.token  # garante renovação
        ts = int(time.time())
        base_params = {
            "partner_id": self.partner_id,
            "timestamp": ts,
            "access_token": self._access_token,
            "shop_id": self.shop_id,
            "sign": self._assinar(caminho, ts),
        }
        base_params.update(params or {})
        r = requests.request(metodo, f"{self.host}{caminho}",
                             params=base_params, json=corpo, timeout=30)
        d = r.json()
        if d.get("error"):
            raise ErroShopee(f"{caminho} -> {d['error']}: {d.get('message','')}")
        return d.get("response", d)

    def pedidos_recentes(self, janela_horas: int = 24) -> list[dict]:
        fim = int(time.time())
        # A Shopee limita a janela de consulta a 15 dias por chamada.
        inicio = fim - min(janela_horas, 360) * 3600
        d = self._req("GET", "/api/v2/order/get_order_list", params={
            "time_range_field": "create_time",
            "time_from": inicio,
            "time_to": fim,
            "page_size": 50,
            "order_status": "READY_TO_SHIP",
        })
        return d.get("order_list", [])

    def detalhe_pedidos(self, order_sns: list[str]) -> list[dict]:
        if not order_sns:
            return []
        d = self._req("GET", "/api/v2/order/get_order_detail", params={
            "order_sn_list": ",".join(order_sns[:50]),
            "response_optional_fields": "item_list,recipient_address,total_amount,buyer_username",
        })
        return d.get("order_list", [])

    def normalizar_pedido(self, bruto: dict) -> dict:
        item = (bruto.get("item_list") or [{}])[0]
        end = bruto.get("recipient_address", {}) or {}
        return {
            "marketplace": "shopee",
            "id_externo": bruto.get("order_sn", ""),
            "titulo": item.get("item_name", ""),
            "sku": item.get("model_sku") or item.get("item_sku") or "",
            "quantidade": item.get("model_quantity_purchased", 1),
            "valor_bruto": float(bruto.get("total_amount", 0)),
            "comprador_nome": end.get("name") or bruto.get("buyer_username", ""),
            "comprador_id": str(bruto.get("buyer_user_id", "")),
            "endereco": {
                "cidade": end.get("city", ""),
                "estado": end.get("state", ""),
                "cep": end.get("zipcode", ""),
                "completo": end.get("full_address", ""),
            },
        }

    def rastreio(self, order_sn: str) -> str:
        d = self._req("GET", "/api/v2/logistics/get_tracking_number",
                      params={"order_sn": order_sn})
        return d.get("tracking_number", "")
