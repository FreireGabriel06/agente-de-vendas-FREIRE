"""
Fila de aprovação.

Este módulo é o que separa "robô útil" de "robô que torra seu dinheiro às
3 da manhã". O worker faz todo o trabalho pesado sozinho — lê pedido,
calcula margem, monta a ordem de compra, redige a resposta ao cliente — e
para no último centímetro. As ações abaixo nunca saem sem seu OK:

  - compra_fornecedor : gasta dinheiro
  - resposta_cliente  : publica texto público no seu anúncio
  - ajuste_preco      : muda preço visível ao mercado
  - publicar_anuncio  : cria oferta pública

O resto o robô resolve sozinho. Você trabalha na fila, não no processo.

Regra que vale respeitar: ação irreversível não se automatiza. Nenhuma
margem de lucro paga uma conta suspensa ou uma compra errada em lote.
"""
import json
from dataclasses import dataclass
from typing import Callable

from db import conectar, agora, registrar_evento

TIPOS_SENSIVEIS = {"compra_fornecedor", "resposta_cliente", "ajuste_preco", "publicar_anuncio"}


@dataclass
class Aprovacao:
    id: int
    tipo: str
    pedido_id: int | None
    resumo: str
    payload: dict
    valor: float | None
    status: str


def enfileirar(tipo: str, resumo: str, payload: dict,
               pedido_id: int | None = None, valor: float | None = None) -> int:
    if tipo not in TIPOS_SENSIVEIS:
        raise ValueError(f"Tipo desconhecido: {tipo}")
    with conectar() as conn:
        cur = conn.execute(
            "INSERT INTO aprovacoes (tipo, pedido_id, resumo, payload_json, valor,"
            " status, criado_em) VALUES (?,?,?,?,?,'pendente',?)",
            (tipo, pedido_id, resumo, json.dumps(payload, ensure_ascii=False),
             valor, agora()),
        )
        return cur.lastrowid


def pendentes(tipo: str | None = None) -> list[Aprovacao]:
    sql = "SELECT * FROM aprovacoes WHERE status = 'pendente'"
    params: tuple = ()
    if tipo:
        sql += " AND tipo = ?"
        params = (tipo,)
    sql += " ORDER BY criado_em"
    with conectar() as conn:
        linhas = conn.execute(sql, params).fetchall()
    return [Aprovacao(
        id=l["id"], tipo=l["tipo"], pedido_id=l["pedido_id"], resumo=l["resumo"],
        payload=json.loads(l["payload_json"]), valor=l["valor"], status=l["status"],
    ) for l in linhas]


def _decidir(aprovacao_id: int, status: str, resultado: str = ""):
    with conectar() as conn:
        conn.execute(
            "UPDATE aprovacoes SET status = ?, resultado = ?, decidido_em = ?"
            " WHERE id = ? AND status IN ('pendente','aprovada')",
            (status, resultado, agora(), aprovacao_id),
        )


def recusar(aprovacao_id: int, motivo: str = ""):
    _decidir(aprovacao_id, "recusada", motivo)
    registrar_evento("info", "aprovacao", f"Ação {aprovacao_id} recusada: {motivo}")


def aprovar(aprovacao_id: int, executores: dict[str, Callable[[dict], str]]):
    """
    Aprova E executa em seguida, usando o executor registrado pro tipo.

    `executores` é um dict {tipo: função(payload) -> str}. Assim o módulo de
    aprovação não conhece nem o ML nem a Amazon — quem injeta é o worker.
    Facilita testar e evita import circular.
    """
    with conectar() as conn:
        linha = conn.execute(
            "SELECT * FROM aprovacoes WHERE id = ? AND status = 'pendente'",
            (aprovacao_id,),
        ).fetchone()
    if linha is None:
        raise ValueError(f"Aprovação {aprovacao_id} não existe ou já foi decidida")

    tipo = linha["tipo"]
    payload = json.loads(linha["payload_json"])
    executor = executores.get(tipo)
    if executor is None:
        raise ValueError(f"Sem executor registrado para '{tipo}'")

    _decidir(aprovacao_id, "aprovada")
    try:
        resultado = executor(payload)
        _decidir(aprovacao_id, "executada", resultado)
        registrar_evento("info", "aprovacao", f"Ação {aprovacao_id} ({tipo}) executada")
        return resultado
    except Exception as e:
        _decidir(aprovacao_id, "erro", str(e)[:500])
        registrar_evento("erro", "aprovacao", f"Ação {aprovacao_id} falhou: {e}")
        raise


def aprovar_em_lote(ids: list[int], executores: dict[str, Callable[[dict], str]]):
    """Aprova vários de uma vez. Erros não interrompem os demais."""
    sucesso, falhas = [], []
    for i in ids:
        try:
            aprovar(i, executores)
            sucesso.append(i)
        except Exception as e:
            falhas.append((i, str(e)))
    return sucesso, falhas
