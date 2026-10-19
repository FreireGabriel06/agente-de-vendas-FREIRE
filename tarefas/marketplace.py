"""
Etapas do ciclo que conversam com o Mercado Livre: ingestão de pedidos,
atendimento das perguntas (rascunhos para a fila) e rastreio. Recebem o
cliente MercadoLivre pronto do worker.py.
"""
import json
import time

from db import conectar, agora, registrar_evento
from core import dinheiro, privacidade
from core.estados import Estado, transicionar
from conectores.mercadolivre import MercadoLivre, ErroMercadoLivre
from atendimento import bot


# --------------------------------------------------------------- 1. Ingestão

def ingerir_mercadolivre(ml: MercadoLivre) -> int:
    novos = 0
    try:
        brutos = ml.pedidos_recentes(limit=50)
    except ErroMercadoLivre as e:
        registrar_evento("erro", "worker", f"Ingestão ML falhou: {e}")
        return 0

    for bruto in brutos:
        p = ml.normalizar_pedido(bruto)
        with conectar() as conn:
            existe = conn.execute(
                "SELECT 1 FROM pedidos WHERE marketplace = ? AND id_externo = ?",
                (p["marketplace"], p["id_externo"]),
            ).fetchone()
            if existe:
                continue

            produto = conn.execute(
                "SELECT id FROM produtos WHERE sku = ?", (p["sku"],)
            ).fetchone()

            # O valor vem do JSON do marketplace como float: entra em Decimal
            # pelo texto curto (dinheiro.do_real). A moeda é a que o
            # marketplace informou (currency_id); sem ela, fica NULL e a
            # margem diz por que não calcula. Nada presume BRL.
            valor = dinheiro.do_real(p["valor_bruto"])
            # LGPD art. 46: PII cifrada já na entrada. Em nenhum momento o
            # nome ou o identificador do comprador toca o disco em texto puro.
            conn.execute(
                "INSERT INTO pedidos (marketplace, id_externo, produto_id, quantidade,"
                " valor_bruto, valor_bruto_dec, valor_bruto_moeda, estado, comprador_nome,"
                " comprador_id, endereco_json, criado_em, atualizado_em)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["marketplace"], p["id_externo"], produto["id"] if produto else None,
                 p["quantidade"], p["valor_bruto"],
                 dinheiro.texto(valor) if valor is not None else None,
                 dinheiro.moeda_lida(p.get("moeda")), Estado.NOVO.value,
                 privacidade.cifrar(p["comprador_nome"]),
                 privacidade.cifrar(p["comprador_id"]),
                 privacidade.cifrar(json.dumps({"shipping_id": p["shipping_id"]})),
                 agora(), agora()),
            )
        novos += 1

    if novos:
        registrar_evento("info", "worker", f"{novos} pedido(s) novo(s) do Mercado Livre")
    return novos


# ------------------------------------------------------------ 4. Atendimento

def atender_perguntas(ml: MercadoLivre) -> int:
    """Redige (para a fila) as perguntas sem resposta que o bot ainda não
    tratou. A busca traz toda pergunta UNANSWERED a cada ciclo, inclusive as
    que já estão na fila, as escaladas e as aprovadas que não foram
    publicadas (em simulação, ou com a publicação falha): bot.ja_tratada pula
    essas antes de qualquer chamada, e só a falha temporária volta ao modelo
    no ciclo seguinte."""
    try:
        perguntas = ml.perguntas_sem_resposta(limit=30)
    except ErroMercadoLivre as e:
        registrar_evento("erro", "worker", f"Busca de perguntas falhou: {e}")
        return 0

    tratadas = 0
    for q in perguntas:
        if bot.ja_tratada(str(q.get("id"))):
            continue
        item_id = q.get("item_id", "")
        try:
            item = ml.anuncio(item_id)
            contexto = {
                "título": item.get("title", ""),
                "preço": item.get("price", ""),
                "estoque": item.get("available_quantity", ""),
                "condição": item.get("condition", ""),
                "atributos": ", ".join(
                    f"{a.get('name')}={a.get('value_name')}"
                    for a in item.get("attributes", [])[:12] if a.get("value_name")
                ),
            }
        except ErroMercadoLivre:
            contexto = {"título": "(não foi possível carregar a ficha)"}

        bot.processar_pergunta(str(q.get("id")), q.get("text", ""), contexto)
        tratadas += 1
        time.sleep(0.3)

    return tratadas


# --------------------------------------------------------------- 5. Rastreio

def atualizar_rastreio(ml: MercadoLivre) -> int:
    atualizados = 0
    with conectar() as conn:
        em_transito = conn.execute(
            "SELECT id, id_externo, endereco_json FROM pedidos WHERE estado IN (?,?)",
            (Estado.COMPRA_CONFIRMADA.value, Estado.EM_TRANSITO.value),
        ).fetchall()

    for ped in em_transito:
        # O endereço está cifrado desde a ingestão; a leitura passa pela
        # privacidade, que registra o acesso e não levanta com '{}' ou ilegível.
        shipping_id = privacidade.envio_para_rastreio(ped["id"], ped["endereco_json"])
        if not shipping_id:
            continue
        try:
            env = ml.envio(shipping_id)
        except ErroMercadoLivre:
            continue

        status = env.get("status", "")
        rastreio = env.get("tracking_number")
        if rastreio:
            with conectar() as conn:
                conn.execute("UPDATE pedidos SET codigo_rastreio = ? WHERE id = ?",
                             (rastreio, ped["id"]))
        try:
            if status == "delivered":
                transicionar(ped["id"], Estado.ENTREGUE, "Confirmado pelo ML")
                atualizados += 1
            elif status == "shipped":
                transicionar(ped["id"], Estado.EM_TRANSITO, f"Rastreio {rastreio}")
                atualizados += 1
        except Exception:
            pass  # transição inválida = já está no estado certo

    return atualizados
