"""
Máquina de estados do pedido.

O ponto desse módulo é que um pedido NUNCA muda de estado por acidente.
Toda transição é validada contra a tabela abaixo e gravada em `transicoes`.
Quando algo der errado em produção, o histórico completo está no banco.
"""
from enum import Enum
from db import conectar, agora, registrar_evento


class Estado(str, Enum):
    NOVO = "NOVO"                              # chegou do marketplace
    ANALISADO = "ANALISADO"                    # margem calculada e aprovada
    RECUSADO_MARGEM = "RECUSADO_MARGEM"        # margem abaixo do mínimo
    AGUARDANDO_APROVACAO = "AGUARDANDO_APROVACAO"  # esperando seu OK
    COMPRA_ENVIADA = "COMPRA_ENVIADA"          # pedido feito ao fornecedor
    COMPRA_CONFIRMADA = "COMPRA_CONFIRMADA"    # fornecedor confirmou
    EM_TRANSITO = "EM_TRANSITO"                # rastreio ativo
    ENTREGUE = "ENTREGUE"
    CANCELADO = "CANCELADO"
    PROBLEMA = "PROBLEMA"                      # qualquer travada — te chama


# Transições permitidas. Qualquer coisa fora disso levanta exceção.
TRANSICOES = {
    Estado.NOVO: {Estado.ANALISADO, Estado.RECUSADO_MARGEM, Estado.CANCELADO, Estado.PROBLEMA},
    Estado.ANALISADO: {Estado.AGUARDANDO_APROVACAO, Estado.COMPRA_ENVIADA, Estado.CANCELADO, Estado.PROBLEMA},
    Estado.AGUARDANDO_APROVACAO: {Estado.COMPRA_ENVIADA, Estado.CANCELADO, Estado.PROBLEMA},
    Estado.COMPRA_ENVIADA: {Estado.COMPRA_CONFIRMADA, Estado.PROBLEMA, Estado.CANCELADO},
    Estado.COMPRA_CONFIRMADA: {Estado.EM_TRANSITO, Estado.PROBLEMA},
    Estado.EM_TRANSITO: {Estado.ENTREGUE, Estado.PROBLEMA},
    Estado.ENTREGUE: set(),
    Estado.RECUSADO_MARGEM: {Estado.ANALISADO},   # você pode forçar reanálise
    Estado.CANCELADO: set(),
    Estado.PROBLEMA: {Estado.ANALISADO, Estado.COMPRA_ENVIADA, Estado.EM_TRANSITO,
                      Estado.CANCELADO, Estado.ENTREGUE},  # saída manual
}

# Estados que exigem atenção humana — alimentam o painel de pendências.
ESTADOS_CRITICOS = {Estado.PROBLEMA, Estado.AGUARDANDO_APROVACAO, Estado.RECUSADO_MARGEM}


class TransicaoInvalida(Exception):
    pass


def transicionar(pedido_id: int, para: Estado, motivo: str = "", automatico: bool = True):
    """Move o pedido de estado, validando e gravando o histórico."""
    with conectar() as conn:
        row = conn.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
        if row is None:
            raise ValueError(f"Pedido {pedido_id} não existe")

        de = Estado(row["estado"])
        if para not in TRANSICOES[de]:
            raise TransicaoInvalida(f"Não é permitido ir de {de.value} para {para.value}")

        conn.execute(
            "UPDATE pedidos SET estado = ?, atualizado_em = ? WHERE id = ?",
            (para.value, agora(), pedido_id),
        )
        conn.execute(
            "INSERT INTO transicoes (pedido_id, de, para, motivo, automatico, ocorrido_em)"
            " VALUES (?,?,?,?,?,?)",
            (pedido_id, de.value, para.value, motivo, int(automatico), agora()),
        )

    if para in ESTADOS_CRITICOS:
        registrar_evento("atencao", "estados",
                         f"Pedido {pedido_id} entrou em {para.value}: {motivo}")
    return para


def historico(pedido_id: int):
    with conectar() as conn:
        return conn.execute(
            "SELECT de, para, motivo, automatico, ocorrido_em FROM transicoes"
            " WHERE pedido_id = ? ORDER BY id",
            (pedido_id,),
        ).fetchall()
