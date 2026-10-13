"""
Sincronização incremental de pedidos da Amazon (lote 7). Só leitura.

Para cada fonte há uma linha em `sincronizacoes` com o cursor, a última
tentativa, o último sucesso e o resultado. O cursor só anda quando a passada
inteira termina bem: uma falha no meio deixa o cursor onde estava, e a próxima
passada relê o mesmo trecho. Como a gravação é por chave (amazon_order_id, e
order_item_id dentro dele), reler não duplica nada.
"""
from datetime import datetime, timedelta, timezone

from conectores import amazon as conector
from db import agora, conectar, registrar_evento

FONTE = "amazon_pedidos"
# Primeira passada: quantos dias para trás buscar.
JANELA_INICIAL_DIAS = 30


def _iso_utc(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def estado() -> dict | None:
    with conectar() as conn:
        linha = conn.execute("SELECT * FROM sincronizacoes WHERE fonte = ?", (FONTE,)).fetchone()
    return dict(linha) if linha else None


def _marcar(conn, **campos):
    conn.execute("INSERT INTO sincronizacoes (fonte) VALUES (?) ON CONFLICT(fonte) DO NOTHING",
                 (FONTE,))
    colunas = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE sincronizacoes SET {colunas} WHERE fonte = ?", (*campos.values(), FONTE))


def _gravar_pedido(conn, p: dict) -> tuple[bool, bool]:
    """(novo, mudou) para o pedido. Grava por amazon_order_id."""
    anterior = conn.execute(
        "SELECT atualizado_na_amazon FROM amazon_pedidos WHERE amazon_order_id = ?",
        (p["amazon_order_id"],)).fetchone()
    conn.execute(
        "INSERT INTO amazon_pedidos (amazon_order_id, marketplace_id, status, canal, comprado_em,"
        " atualizado_na_amazon, total_dec, total_moeda, itens_pendentes, sincronizado_em)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(amazon_order_id) DO UPDATE SET marketplace_id = excluded.marketplace_id,"
        " status = excluded.status, canal = excluded.canal, comprado_em = excluded.comprado_em,"
        " atualizado_na_amazon = excluded.atualizado_na_amazon, total_dec = excluded.total_dec,"
        " total_moeda = excluded.total_moeda, itens_pendentes = excluded.itens_pendentes,"
        " sincronizado_em = excluded.sincronizado_em",
        (p["amazon_order_id"], p["marketplace_id"], p["status"], p["canal"], p["comprado_em"],
         p["atualizado_na_amazon"], p["total_dec"], p["total_moeda"], p["itens_pendentes"],
         agora()))
    if anterior is None:
        return True, True
    return False, anterior["atualizado_na_amazon"] != p["atualizado_na_amazon"]


def _gravar_itens(conn, order_id: str, itens: list[dict]) -> None:
    for i in itens:
        conn.execute(
            "INSERT INTO amazon_itens (amazon_order_id, order_item_id, asin, sku, titulo,"
            " quantidade, quantidade_enviada, preco_dec, preco_moeda)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(amazon_order_id, order_item_id) DO UPDATE SET asin = excluded.asin,"
            " sku = excluded.sku, titulo = excluded.titulo, quantidade = excluded.quantidade,"
            " quantidade_enviada = excluded.quantidade_enviada, preco_dec = excluded.preco_dec,"
            " preco_moeda = excluded.preco_moeda",
            (order_id, i["order_item_id"], i["asin"], i["sku"], i["titulo"], i["quantidade"],
             i["quantidade_enviada"], i["preco_dec"], i["preco_moeda"]))
    conn.execute("UPDATE amazon_pedidos SET itens_lidos = ? WHERE amazon_order_id = ?",
                 (len(itens), order_id))


def sincronizar_amazon(amz: conector.Amazon | None = None, agora_utc=None) -> dict:
    """Uma passada. Devolve as contagens; levanta nada: a falha vira o
    resultado da linha em `sincronizacoes` e um evento sem dado do vendedor."""
    agora_utc = agora_utc or datetime.now(timezone.utc)
    atual = estado() or {}
    desde = atual.get("cursor") or _iso_utc(agora_utc - timedelta(days=JANELA_INICIAL_DIAS))
    with conectar() as conn:
        _marcar(conn, ultima_tentativa=agora())

    resumo = {"pedidos": 0, "novos": 0, "atualizados": 0, "itens": 0}
    novo_cursor = None
    maior_atualizacao = None
    try:
        amz = amz or conector.Amazon()
        for pedidos, ate in amz.paginas_de_pedidos(desde):
            for bruto in pedidos:
                p = conector.normalizar_pedido(bruto)
                if not p["amazon_order_id"]:
                    continue
                with conectar() as conn:
                    novo, mudou = _gravar_pedido(conn, p)
                if novo or mudou:
                    itens = [conector.normalizar_item(i)
                             for i in amz.itens_do_pedido(p["amazon_order_id"])]
                    with conectar() as conn:
                        _gravar_itens(conn, p["amazon_order_id"], itens)
                    resumo["itens"] += len(itens)
                resumo["pedidos"] += 1
                resumo["novos"] += int(novo)
                resumo["atualizados"] += int(mudou and not novo)
                if p["atualizado_na_amazon"] and (maior_atualizacao is None
                                                  or p["atualizado_na_amazon"] > maior_atualizacao):
                    maior_atualizacao = p["atualizado_na_amazon"]
            novo_cursor = ate or novo_cursor
    except conector.ErroAmazon as e:
        with conectar() as conn:
            _marcar(conn, resultado="erro", detalhe=str(e)[:300])
        registrar_evento("erro", "amazon", f"Sincronização de pedidos falhou: {e}")
        return {**resumo, "erro": str(e)}

    cursor = novo_cursor or maior_atualizacao or desde
    detalhe = (f"{resumo['pedidos']} pedido(s) lido(s): {resumo['novos']} novo(s), "
               f"{resumo['atualizados']} atualizado(s), {resumo['itens']} item(ns)")
    with conectar() as conn:
        _marcar(conn, cursor=cursor, ultimo_sucesso=agora(), resultado="ok", detalhe=detalhe)
    if resumo["pedidos"]:
        registrar_evento("info", "amazon", f"Sincronização: {detalhe}")
    return resumo
