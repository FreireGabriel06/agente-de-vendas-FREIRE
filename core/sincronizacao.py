"""
Sincronização da Amazon (lotes 7 e 8). Só leitura.

Fontes: pedidos (incremental, por cursor), anúncios (Listings Items API) e
estoque FBA (FBA Inventory API). Para cada fonte há uma linha em
`sincronizacoes` com o cursor, a última tentativa, o último sucesso e o
resultado. O cursor só anda quando a passada inteira termina bem: uma falha
no meio deixa o cursor onde estava, e a próxima passada relê o mesmo trecho.
Como a gravação é por chave (pedido, item, SKU), reler não duplica nada.

Resultados: 'ok'; 'erro' (falha da API ou da rede); 'conexao_vencida' (a LWA
recusou o refresh token: só autorizar de novo resolve); 'indisponivel' (a
conta não tem acesso àquela API, como uma conta sem FBA).
"""
import os
from datetime import datetime, timedelta, timezone

from conectores import amazon as conector
from core.erros import mensagem_segura
from db import agora, conectar, registrar_evento

FONTE = "amazon_pedidos"
FONTE_ANUNCIOS = "amazon_anuncios"
FONTE_ESTOQUE_FBA = "amazon_estoque_fba"
FONTES = (FONTE, FONTE_ANUNCIOS, FONTE_ESTOQUE_FBA)
# Primeira passada de pedidos: quantos dias para trás buscar.
JANELA_INICIAL_DIAS = 30


def _iso_utc(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def estado(fonte: str = FONTE) -> dict | None:
    with conectar() as conn:
        linha = conn.execute("SELECT * FROM sincronizacoes WHERE fonte = ?", (fonte,)).fetchone()
    return dict(linha) if linha else None


def estados() -> list[dict]:
    with conectar() as conn:
        return [dict(l) for l in conn.execute("SELECT * FROM sincronizacoes ORDER BY fonte")]


def _marcar(conn, fonte: str = FONTE, **campos):
    conn.execute("INSERT INTO sincronizacoes (fonte) VALUES (?) ON CONFLICT(fonte) DO NOTHING",
                 (fonte,))
    colunas = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE sincronizacoes SET {colunas} WHERE fonte = ?", (*campos.values(), fonte))


def _falhou(fonte: str, erro: conector.ErroAmazon, rotulo: str) -> dict:
    """Grava o resultado da falha; o cursor e o último sucesso ficam."""
    if isinstance(erro, conector.ErroAutorizacaoAmazon):
        resultado = "conexao_vencida"
    elif getattr(erro, "status", None) == 403 and fonte == FONTE_ESTOQUE_FBA:
        resultado = "indisponivel"
    else:
        resultado = "erro"
    with conectar() as conn:
        _marcar(conn, fonte, resultado=resultado, detalhe=mensagem_segura(erro, 300))
    nivel = "info" if resultado == "indisponivel" else "erro"
    registrar_evento(nivel, "amazon", f"Sincronização de {rotulo}: {resultado}. {erro}")
    return {"erro": str(erro), "resultado": resultado}


def _sucesso(fonte: str, detalhe: str, cursor: str | None = None) -> None:
    campos = {"ultimo_sucesso": agora(), "resultado": "ok", "detalhe": detalhe}
    if cursor is not None:
        campos["cursor"] = cursor
    with conectar() as conn:
        _marcar(conn, fonte, **campos)


# ---------------------------------------------------------------- pedidos

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
    """Uma passada de pedidos. Não levanta: a falha vira o resultado da linha
    em `sincronizacoes` e um evento sem dado do vendedor."""
    agora_utc = agora_utc or datetime.now(timezone.utc)
    atual = estado() or {}
    desde = atual.get("cursor") or _iso_utc(agora_utc - timedelta(days=JANELA_INICIAL_DIAS))
    with conectar() as conn:
        _marcar(conn, FONTE, ultima_tentativa=agora())

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
        return {**resumo, **_falhou(FONTE, e, "pedidos")}

    detalhe = (f"{resumo['pedidos']} pedido(s) lido(s): {resumo['novos']} novo(s), "
               f"{resumo['atualizados']} atualizado(s), {resumo['itens']} item(ns)")
    _sucesso(FONTE, detalhe, cursor=novo_cursor or maior_atualizacao or desde)
    if resumo["pedidos"]:
        registrar_evento("info", "amazon", f"Sincronização: {detalhe}")
    return resumo


# ---------------------------------------------------------------- anúncios

def vendedor_amazon() -> str:
    """O selling_partner_id gravado na autorização (lido na hora)."""
    return (os.getenv("AMZ_SELLING_PARTNER_ID") or "").strip()


def sincronizar_anuncios(amz: conector.Amazon | None = None) -> dict:
    """Todos os anúncios do vendedor no marketplace: status, preço e a
    quantidade própria. Grava por SKU; SKU que não veio fica como estava."""
    with conectar() as conn:
        _marcar(conn, FONTE_ANUNCIOS, ultima_tentativa=agora())
    vendedor = vendedor_amazon()
    if not vendedor:
        return _falhou(FONTE_ANUNCIOS, conector.ErroAmazon(
            "Falta o selling_partner_id: autorize a Amazon pelo painel."), "anúncios")
    marketplace = (amz.cfg if amz else conector.config.amazon).marketplace_id
    lidos = 0
    try:
        amz = amz or conector.Amazon()
        for pagina in amz.paginas_de_anuncios(vendedor):
            for bruto in pagina:
                a = conector.normalizar_anuncio(bruto, marketplace)
                if not a["sku"]:
                    continue
                with conectar() as conn:
                    conn.execute(
                        "INSERT INTO amazon_anuncios (sku, asin, titulo, status, quantidade_propria,"
                        " preco_dec, preco_moeda, atualizado_na_amazon, visto_em)"
                        " VALUES (?,?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(sku) DO UPDATE SET asin = excluded.asin,"
                        " titulo = excluded.titulo, status = excluded.status,"
                        " quantidade_propria = excluded.quantidade_propria,"
                        " preco_dec = excluded.preco_dec, preco_moeda = excluded.preco_moeda,"
                        " atualizado_na_amazon = excluded.atualizado_na_amazon,"
                        " visto_em = excluded.visto_em",
                        (a["sku"], a["asin"], a["titulo"], a["status"], a["quantidade_propria"],
                         a["preco_dec"], a["preco_moeda"], a["atualizado_na_amazon"], agora()))
                lidos += 1
    except conector.ErroAmazon as e:
        return {"anuncios": lidos, **_falhou(FONTE_ANUNCIOS, e, "anúncios")}
    _sucesso(FONTE_ANUNCIOS, f"{lidos} anúncio(s) lido(s)")
    return {"anuncios": lidos}


def sincronizar_estoque_fba(amz: conector.Amazon | None = None) -> dict:
    """Estoque FBA por SKU. Conta sem FBA (HTTP 403) fica 'indisponivel', sem alerta."""
    with conectar() as conn:
        _marcar(conn, FONTE_ESTOQUE_FBA, ultima_tentativa=agora())
    lidos = 0
    try:
        amz = amz or conector.Amazon()
        for pagina in amz.paginas_de_estoque_fba():
            for bruto in pagina:
                e = conector.normalizar_estoque_fba(bruto)
                if not e["sku"]:
                    continue
                with conectar() as conn:
                    conn.execute(
                        "INSERT INTO amazon_anuncios (sku, fba_disponivel, fba_total, estoque_fba_em)"
                        " VALUES (?,?,?,?) ON CONFLICT(sku) DO UPDATE SET"
                        " fba_disponivel = excluded.fba_disponivel, fba_total = excluded.fba_total,"
                        " estoque_fba_em = excluded.estoque_fba_em",
                        (e["sku"], e["fba_disponivel"], e["fba_total"], agora()))
                lidos += 1
    except conector.ErroAmazon as erro:
        return {"estoque_fba": lidos, **_falhou(FONTE_ESTOQUE_FBA, erro, "estoque FBA")}
    _sucesso(FONTE_ESTOQUE_FBA, f"{lidos} SKU(s) com estoque FBA")
    return {"estoque_fba": lidos}
