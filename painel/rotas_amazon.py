"""
Rotas de leitura dos dados da Amazon e dos alertas.
"""


from db import conectar
from fastapi import APIRouter

rotas = APIRouter()

@rotas.get("/api/amazon/anuncios")
def api_amazon_anuncios():
    """Anúncios da Amazon com status, preço e as quantidades conhecidas."""
    with conectar() as conn:
        return {"anuncios": [dict(l) for l in conn.execute(
            "SELECT sku, asin, titulo, status, quantidade_propria, fba_disponivel, fba_total,"
            " preco_dec, preco_moeda, atualizado_na_amazon, visto_em, estoque_fba_em"
            " FROM amazon_anuncios ORDER BY sku")]}


@rotas.get("/api/amazon/pedidos")
def api_amazon_pedidos(limite: int = 100):
    """Pedidos lidos da Amazon (monitoramento). Sem dado de comprador."""
    limite = max(1, min(limite, 500))
    with conectar() as conn:
        pedidos = [dict(l) for l in conn.execute(
            "SELECT amazon_order_id, marketplace_id, status, canal, comprado_em,"
            " atualizado_na_amazon, total_dec, total_moeda, itens_pendentes, itens_lidos"
            " FROM amazon_pedidos ORDER BY comprado_em DESC LIMIT ?", (limite,))]
        for p in pedidos:
            p["itens"] = [dict(i) for i in conn.execute(
                "SELECT order_item_id, asin, sku, titulo, quantidade, quantidade_enviada,"
                " preco_dec, preco_moeda FROM amazon_itens WHERE amazon_order_id = ?"
                " ORDER BY order_item_id", (p["amazon_order_id"],))]
    return {"pedidos": pedidos}


@rotas.get("/api/amazon/sincronizacao")
def api_amazon_sincronizacao():
    """Cursor, última tentativa, último sucesso e resultado da sincronização."""
    from core import sincronizacao
    return {"fontes": sincronizacao.estados()}


@rotas.get("/api/alertas")
def api_alertas(todos: bool = False):
    """Alertas abertos (ou os últimos 200, com os resolvidos, se todos=true).
    Cada um traz o horário do último dado válido."""
    from core import alertas
    return {"alertas": alertas.listar(so_abertos=not todos)}
