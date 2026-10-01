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

Partner key e tokens ficam no cofre cifrado (core/cofre.py). O refresh token
também é de uso único: uma renovação de cada vez, com o cofre relido depois
da espera, e o par novo guardado na memória antes do banco (mesmas regras do
conector do ML). O antigo .token_shopee.json, em texto puro, só é lido uma
vez, para importar os tokens ao cofre; nunca é apagado nem alterado por aqui.
"""
import hashlib
import hmac
import time

import requests

from config import DATA_DIR
from core import cofre
import os

PROVEDOR = "shopee"

# Legado: só lido para importar ao cofre, nunca escrito.
ARQ_TOKEN = DATA_DIR / ".token_shopee.json"

HOST_PROD = "https://partner.shopeemobile.com"
HOST_TESTE = "https://partner.test-stable.shopeemobile.com"


class ErroShopee(Exception):
    pass


class Shopee:
    def __init__(self):
        self.partner_id = int(os.getenv("SHOPEE_PARTNER_ID", "0") or 0)
        self.shop_id = int(os.getenv("SHOPEE_SHOP_ID", "0") or 0)
        self.host = HOST_TESTE if os.getenv("SHOPEE_SANDBOX", "true").lower() == "true" else HOST_PROD
        self._access_token = ""
        self._expira_em = 0
        # Tokens vêm do cofre na primeira chamada, não aqui.
        self._carregado = False

    @property
    def partner_key(self) -> str:
        """Lida a cada uso: ambiente do sistema, depois o cofre."""
        return cofre.segredo("SHOPEE_PARTNER_KEY")

    @property
    def configurado(self) -> bool:
        return bool(self.partner_id and self.shop_id and self.partner_key)

    # ------------------------------------------------------------ Token

    def _carregar(self):
        if self._carregado:
            return
        cofre.importar_legado(PROVEDOR, ARQ_TOKEN)
        self._access_token = cofre.ler(PROVEDOR, "access_token") or ""
        self._expira_em = cofre.ler_validade(PROVEDOR)
        self._carregado = True

    def _salvar(self, d: dict):
        """Par que a Shopee acabou de emitir: memória do processo, depois o
        cofre (cofre.guardar_tokens), sem exceção antes de estar guardado."""
        # Token vale 4h; renovamos 10 min antes pra não pegar a borda.
        try:
            segundos = int(d.get("expire_in", 14400))
        except (TypeError, ValueError):
            segundos = 14400
        expira_em = int(time.time()) + segundos - 600
        novos = {n: str(d[n]) for n in ("access_token", "refresh_token") if d.get(n)}
        if "access_token" not in novos:
            expira_em = 0  # sem access token novo, a próxima chamada renova
        cofre.guardar_tokens(PROVEDOR, {**novos, "expira_em": str(expira_em)})
        if len(novos) < 2:
            raise ErroShopee("A resposta da Shopee veio sem access_token ou sem refresh_token.")
        self._access_token = novos["access_token"]
        self._expira_em = expira_em
        self._carregado = True

    def _pelo_cofre(self, funcao):
        """Problema no cofre chega a quem chama como ErroShopee, com a mensagem
        do cofre, que não traz valor nem chave."""
        try:
            return funcao()
        except cofre.ErroCofre as e:
            raise ErroShopee(str(e)) from None

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
        self._salvar(d)
        return d

    def _renovar(self):
        """Troca o access token que esta instância tem por um novo."""
        with cofre.trava_de_tokens(PROVEDOR):
            cofre.regravar_pendente(PROVEDOR)
            self._carregar()
            # Quem esperou a vez relê o cofre: se outra thread ou processo já
            # renovou, o refresh que esta instância conhecia foi gasto.
            recusado = self._access_token
            guardado = cofre.ler(PROVEDOR, "access_token") or ""
            validade = cofre.ler_validade(PROVEDOR)
            if guardado and guardado != recusado and time.time() < validade:
                self._access_token, self._expira_em = guardado, validade
                return
            if not self.configurado:
                raise ErroShopee("Credenciais da Shopee ausentes. Salve Partner ID, Partner Key "
                                 "e Shop ID no painel")
            refresh_token = cofre.segredo("SHOPEE_REFRESH_TOKEN")
            if not refresh_token:
                raise ErroShopee("Sem refresh token. Rode o fluxo de autorização primeiro.")
            # O refresh vale uma vez só: se o cofre não aceitaria o novo, para aqui.
            cofre.conferir()
            caminho = "/api/v2/auth/access_token/get"
            ts = int(time.time())
            r = requests.post(
                f"{self.host}{caminho}",
                params={"partner_id": self.partner_id, "timestamp": ts,
                        "sign": self._assinar(caminho, ts, com_loja=False)},
                json={"refresh_token": refresh_token,
                      "partner_id": self.partner_id, "shop_id": self.shop_id},
                timeout=25,
            )
            d = r.json()
            if d.get("error"):
                raise ErroShopee(f"Falha ao renovar: {d['error']} {d.get('message','')}")
            # O refresh antigo acabou de deixar de valer: o par novo fica na
            # memória do processo e vai para o cofre, sem exceção no caminho.
            self._salvar(d)
            cofre.avisar("info", "shopee", "Token renovado")

    def _garantir_token(self) -> str:
        cofre.regravar_pendente(PROVEDOR)
        self._carregar()
        if not self._access_token or time.time() >= self._expira_em:
            self._renovar()
        return self._access_token

    @property
    def token(self) -> str:
        return self._pelo_cofre(self._garantir_token)

    # ------------------------------------------------------------ Chamadas

    def _req(self, metodo: str, caminho: str, params: dict | None = None, corpo: dict | None = None):
        self.token  # garante renovação
        ts = int(time.time())
        base_params = {
            "partner_id": self.partner_id,
            "timestamp": ts,
            "access_token": self._access_token,
            "shop_id": self.shop_id,
            "sign": self._pelo_cofre(lambda: self._assinar(caminho, ts)),
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
