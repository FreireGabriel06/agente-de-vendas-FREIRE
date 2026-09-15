"""
Conector do Mercado Livre.

O token de acesso do ML vale 6 horas. O refresh token é de uso único: cada
refresh devolve um novo, e se você perder o novo, perdeu o acesso e precisa
refazer o fluxo OAuth no navegador. Por isso o refresh é persistido em disco
assim que chega — este é o erro que mais derruba integração de ML em
produção.
"""
import json
import time
from pathlib import Path

import requests

from config import config, BASE_DIR
from db import registrar_evento

ARQ_TOKEN = BASE_DIR / ".token_ml.json"


class ErroMercadoLivre(Exception):
    pass


class MercadoLivre:
    def __init__(self, cfg=None):
        self.cfg = cfg or config.ml
        self._access_token = None
        self._expira_em = 0
        self._carregar_token()

    # ------------------------------------------------------------- OAuth

    def _carregar_token(self):
        if ARQ_TOKEN.exists():
            dados = json.loads(ARQ_TOKEN.read_text())
            self._access_token = dados.get("access_token")
            self._expira_em = dados.get("expira_em", 0)
            if dados.get("refresh_token"):
                self.cfg.refresh_token = dados["refresh_token"]

    def _salvar_token(self, dados: dict):
        ARQ_TOKEN.write_text(json.dumps({
            "access_token": dados["access_token"],
            "refresh_token": dados["refresh_token"],
            "expira_em": int(time.time()) + dados.get("expires_in", 21600) - 300,
        }, indent=2))
        ARQ_TOKEN.chmod(0o600)

    def _renovar(self):
        if not self.cfg.configurado:
            raise ErroMercadoLivre(
                "Credenciais do ML ausentes. Preencha ML_CLIENT_ID, "
                "ML_CLIENT_SECRET e ML_REFRESH_TOKEN no .env"
            )
        r = requests.post(f"{self.cfg.base_url}/oauth/token", data={
            "grant_type": "refresh_token",
            "client_id": self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
            "refresh_token": self.cfg.refresh_token,
        }, timeout=20)
        if r.status_code != 200:
            raise ErroMercadoLivre(f"Falha ao renovar token: {r.status_code} {r.text[:200]}")
        dados = r.json()
        self._access_token = dados["access_token"]
        self.cfg.refresh_token = dados["refresh_token"]
        self._expira_em = int(time.time()) + dados.get("expires_in", 21600) - 300
        self._salvar_token(dados)
        registrar_evento("info", "mercadolivre", "Token renovado")

    @property
    def token(self) -> str:
        if not self._access_token or time.time() >= self._expira_em:
            self._renovar()
        return self._access_token

    # --------------------------------------------------------- HTTP base

    def _req(self, metodo: str, caminho: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        r = requests.request(metodo, f"{self.cfg.base_url}{caminho}",
                             headers=headers, timeout=25, **kwargs)
        if r.status_code == 401:
            self._renovar()
            headers["Authorization"] = f"Bearer {self._access_token}"
            r = requests.request(metodo, f"{self.cfg.base_url}{caminho}",
                                 headers=headers, timeout=25, **kwargs)
        if r.status_code >= 400:
            raise ErroMercadoLivre(f"{metodo} {caminho} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    # ------------------------------------------------------------ Pedidos

    def pedidos_recentes(self, offset: int = 0, limit: int = 50) -> list[dict]:
        """Pedidos pagos, mais recentes primeiro."""
        dados = self._req("GET", "/orders/search", params={
            "seller": self.cfg.seller_id,
            "order.status": "paid",
            "sort": "date_desc",
            "offset": offset,
            "limit": limit,
        })
        return dados.get("results", [])

    def pedido(self, order_id: str) -> dict:
        return self._req("GET", f"/orders/{order_id}")

    def envio(self, shipping_id: str) -> dict:
        return self._req("GET", f"/shipments/{shipping_id}")

    def normalizar_pedido(self, bruto: dict) -> dict:
        """Traduz o payload do ML pro formato interno do sistema."""
        item = (bruto.get("order_items") or [{}])[0]
        comprador = bruto.get("buyer", {})
        return {
            "marketplace": "mercadolivre",
            "id_externo": str(bruto.get("id")),
            "titulo": item.get("item", {}).get("title", ""),
            "sku": item.get("item", {}).get("seller_sku") or item.get("item", {}).get("id", ""),
            "quantidade": item.get("quantity", 1),
            "valor_bruto": float(bruto.get("total_amount", 0)),
            "comprador_nome": f"{comprador.get('first_name','')} {comprador.get('last_name','')}".strip(),
            "comprador_id": str(comprador.get("id", "")),
            "shipping_id": str((bruto.get("shipping") or {}).get("id", "")),
        }

    # -------------------------------------------------- Perguntas e mensagens

    def perguntas_sem_resposta(self, limit: int = 50) -> list[dict]:
        dados = self._req("GET", "/questions/search", params={
            "seller_id": self.cfg.seller_id,
            "status": "UNANSWERED",
            "limit": limit,
        })
        return dados.get("questions", [])

    def responder_pergunta(self, question_id: str, texto: str) -> dict:
        """
        AÇÃO IRREVERSÍVEL: publica resposta pública no anúncio.
        Nunca é chamada direto pelo worker — só depois de aprovação.
        """
        return self._req("POST", "/answers",
                         json={"question_id": question_id, "text": texto})

    def mensagens_pos_venda(self, order_id: str) -> list[dict]:
        dados = self._req("GET", f"/messages/packs/{order_id}/sellers/{self.cfg.seller_id}",
                          params={"tag": "post_sale"})
        return dados.get("messages", [])

    # ------------------------------------------------------------ Anúncios

    def anuncio(self, item_id: str) -> dict:
        return self._req("GET", f"/items/{item_id}")

    def atualizar_preco(self, item_id: str, preco: float) -> dict:
        """AÇÃO SENSÍVEL: muda o preço público. Passa por aprovação."""
        return self._req("PUT", f"/items/{item_id}", json={"price": round(preco, 2)})

    def atualizar_estoque(self, item_id: str, quantidade: int) -> dict:
        return self._req("PUT", f"/items/{item_id}", json={"available_quantity": quantidade})

    def comissao_categoria(self, category_id: str, preco: float,
                           tipo: str = "gold_special") -> dict:
        """
        Percentual REAL de comissão da categoria — use isto em vez das médias
        do módulo de precificação quando o produto já estiver cadastrado.
        gold_special = Clássico | gold_pro = Premium
        """
        return self._req("GET", f"/sites/{self.cfg.site_id}/listing_prices", params={
            "price": preco,
            "category_id": category_id,
            "listing_type_id": tipo,
        })
