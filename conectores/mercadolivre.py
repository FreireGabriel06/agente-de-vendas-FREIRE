"""
Conector do Mercado Livre.

O token de acesso do ML vale 6 horas. O refresh token é de uso único: cada
refresh devolve um novo, e se você perder o novo, perdeu o acesso e precisa
refazer o fluxo OAuth no navegador. Por isso:

  - antes de gastar o refresh, o cofre confere se aceitaria o novo;
  - uma renovação de cada vez (cofre.trava_de_tokens), e quem esperou a vez
    relê o cofre antes de mandar o refresh, que pode já ter sido gasto;
  - o par novo vai para a memória do processo e depois para o cofre cifrado
    (cofre.guardar_tokens); se o banco falhar, o token segue valendo da
    memória. Este é o erro que mais derruba integração de ML em produção.

O antigo .token_ml.json, em texto puro, só é lido uma vez, para importar os
tokens ao cofre. Ele nunca é apagado nem alterado por aqui.
"""
import time

import requests

from config import config, DATA_DIR
from core import cofre

PROVEDOR = "mercadolivre"

# Legado: só lido para importar ao cofre, nunca escrito.
ARQ_TOKEN = DATA_DIR / ".token_ml.json"


class ErroMercadoLivre(Exception):
    pass


def guardar_tokens(dados: dict) -> int:
    """Guarda o par que o ML acabou de emitir, sem exceção antes de ele estar
    guardado: memória do processo, depois o cofre, numa transação. Devolve a
    validade (epoch). Usado pela renovação e pela troca do código de
    autorização."""
    try:
        segundos = int(dados.get("expires_in", 21600))
    except (TypeError, ValueError):
        segundos = 21600
    expira_em = int(time.time()) + segundos - 300
    novos = {n: str(dados[n]) for n in ("access_token", "refresh_token") if dados.get(n)}
    if "access_token" not in novos:
        expira_em = 0  # sem access token novo, a próxima chamada renova
    cofre.guardar_tokens(PROVEDOR, {**novos, "expira_em": str(expira_em)})
    if len(novos) < 2:
        raise ErroMercadoLivre("A resposta do Mercado Livre veio sem access_token "
                               "ou sem refresh_token.")
    return expira_em


class MercadoLivre:
    def __init__(self, cfg=None):
        self.cfg = cfg or config.ml
        self._access_token = None
        self._expira_em = 0
        # Nada é lido do cofre aqui: um cofre com problema vira ErroMercadoLivre
        # na primeira chamada, que o worker registra, em vez de derrubar o ciclo.
        self._carregado = False

    # ------------------------------------------------------------- OAuth

    def _carregar_token(self):
        if self._carregado:
            return
        cofre.importar_legado(PROVEDOR, ARQ_TOKEN)
        self._access_token = cofre.ler(PROVEDOR, "access_token")
        self._expira_em = cofre.ler_validade(PROVEDOR)
        self._carregado = True

    def _renovar(self):
        """Troca o access token que esta instância tem por um novo."""
        with cofre.trava_de_tokens(PROVEDOR):
            cofre.regravar_pendente(PROVEDOR)
            self._carregar_token()
            # Quem esperou a vez relê o cofre: se outra thread ou processo já
            # renovou, o refresh que esta instância conhecia foi gasto.
            recusado = self._access_token
            guardado = cofre.ler(PROVEDOR, "access_token")
            validade = cofre.ler_validade(PROVEDOR)
            if guardado and guardado != recusado and time.time() < validade:
                self._access_token, self._expira_em = guardado, validade
                return
            if not self.cfg.configurado:
                raise ErroMercadoLivre(
                    "Credenciais do ML ausentes. Salve o App ID e a Secret Key no painel "
                    "e autorize a conta (ou defina ML_CLIENT_ID, ML_CLIENT_SECRET e "
                    "ML_REFRESH_TOKEN no ambiente)"
                )
            # O refresh vale uma vez só: se o cofre não aceitaria o novo, para aqui.
            cofre.conferir()
            r = requests.post(f"{self.cfg.base_url}/oauth/token", data={
                "grant_type": "refresh_token",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "refresh_token": self.cfg.refresh_token,
            }, timeout=20)
            if r.status_code != 200:
                raise ErroMercadoLivre(f"Falha ao renovar token: {r.status_code} {r.text[:200]}")
            dados = r.json()
            # O refresh antigo acabou de deixar de valer: o par novo fica na
            # memória do processo e vai para o cofre, sem exceção no caminho.
            self._expira_em = guardar_tokens(dados)
            self._access_token = dados["access_token"]
            cofre.avisar("info", "mercadolivre", "Token renovado")

    def _garantir_token(self) -> str:
        cofre.regravar_pendente(PROVEDOR)
        self._carregar_token()
        if not self._access_token or time.time() >= self._expira_em:
            self._renovar()
        return self._access_token

    def _pelo_cofre(self, funcao):
        """Problema no cofre chega a quem chama como erro do conector, com a
        mensagem do cofre, que não traz valor nem chave."""
        try:
            return funcao()
        except cofre.ErroCofre as e:
            raise ErroMercadoLivre(str(e)) from None

    @property
    def token(self) -> str:
        return self._pelo_cofre(self._garantir_token)

    # --------------------------------------------------------- HTTP base

    def _req(self, metodo: str, caminho: str, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        r = requests.request(metodo, f"{self.cfg.base_url}{caminho}",
                             headers=headers, timeout=25, **kwargs)
        if r.status_code == 401:
            self._pelo_cofre(self._renovar)
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
