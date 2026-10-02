"""
Fila de aprovação.

Este módulo é o que separa "robô útil" de "robô que torra seu dinheiro às
3 da manhã". O worker faz todo o trabalho pesado sozinho — lê pedido,
calcula margem, monta a ordem de compra, redige a resposta ao cliente — e
para no último centímetro. As ações abaixo nunca saem sem seu OK:

  - compra_fornecedor : grava a ordem de compra; o envio ao fornecedor, e o
                        pagamento, ficam com você
  - resposta_cliente  : publica texto público no seu anúncio
  - ajuste_preco      : muda preço visível ao mercado
  - publicar_anuncio  : cria oferta pública (sem executor por enquanto)

Antes de executar, a aprovação roda a conformidade de novo
(core/conformidade.verificar_pendencia). Item bloqueado não executa, venha o
OK do painel ou do cli.py, e a recusa traz o mesmo motivo nos dois.

Com MODO_SIMULACAO ligado, o executor só registra o que faria: o resultado
começa com PREFIXO_SIMULACAO.

Regra que vale respeitar: ação irreversível não se automatiza. Nenhuma
margem de lucro paga uma conta suspensa ou uma compra errada em lote.
"""
import json
from dataclasses import dataclass
from typing import Callable

from core import conformidade
from db import conectar, agora, registrar_evento

TIPOS_SENSIVEIS = {"compra_fornecedor", "resposta_cliente", "ajuste_preco", "publicar_anuncio"}

# Marca o resultado de um executor que rodou em modo simulação. Fica gravado
# no banco junto com o resultado, então a marca não se perde depois.
PREFIXO_SIMULACAO = "[SIMULAÇÃO] "


def simular(texto: str) -> str:
    """Resultado de executor em modo simulação: nada saiu do sistema."""
    return PREFIXO_SIMULACAO + texto


def foi_simulado(resultado) -> bool:
    return isinstance(resultado, str) and resultado.startswith(PREFIXO_SIMULACAO)


class BloqueadoPelaConformidade(Exception):
    """A ação está bloqueada pela conformidade e não foi executada. A
    mensagem é a mesma que o painel devolve com HTTP 409."""

    def __init__(self, aprovacao_id: int, resultado: conformidade.Resultado):
        self.aprovacao_id = aprovacao_id
        self.resultado = resultado
        super().__init__(conformidade.motivo_do_bloqueio(resultado))


@dataclass
class Aprovacao:
    id: int
    tipo: str
    pedido_id: int | None
    resumo: str
    payload: dict
    valor: float | None
    status: str


def _da_linha(l) -> Aprovacao:
    return Aprovacao(
        id=l["id"], tipo=l["tipo"], pedido_id=l["pedido_id"], resumo=l["resumo"],
        payload=json.loads(l["payload_json"]), valor=l["valor"], status=l["status"],
    )


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
    return [_da_linha(l) for l in linhas]


def obter_pendente(aprovacao_id: int) -> Aprovacao | None:
    with conectar() as conn:
        linha = conn.execute(
            "SELECT * FROM aprovacoes WHERE id = ? AND status = 'pendente'",
            (aprovacao_id,),
        ).fetchone()
    return _da_linha(linha) if linha else None


def checar(aprovacao: Aprovacao) -> conformidade.Resultado:
    """A conformidade de uma pendência, a mesma para painel, cli.py e aprovar."""
    return conformidade.verificar_pendencia(aprovacao.tipo, aprovacao.payload)


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
    Confere a conformidade, aprova E executa em seguida, usando o executor
    registrado pro tipo.

    `executores` é um dict {tipo: função(payload) -> str}. Assim o módulo de
    aprovação não conhece nem o ML nem a Amazon — quem injeta é o worker.
    Facilita testar e evita import circular.

    Item bloqueado levanta BloqueadoPelaConformidade e continua pendente.
    """
    item = obter_pendente(aprovacao_id)
    if item is None:
        raise ValueError(f"Aprovação {aprovacao_id} não existe ou já foi decidida")

    executor = executores.get(item.tipo)
    if executor is None:
        raise ValueError(f"Sem executor registrado para '{item.tipo}'")

    checagem = checar(item)
    if checagem.bloqueado:
        erro = BloqueadoPelaConformidade(aprovacao_id, checagem)
        registrar_evento("atencao", "aprovacao", f"Ação {aprovacao_id} não executada. {erro}")
        raise erro

    _decidir(aprovacao_id, "aprovada")
    try:
        resultado = executor(item.payload)
        _decidir(aprovacao_id, "executada", resultado)
        if foi_simulado(resultado):
            registrar_evento("info", "aprovacao",
                             f"Ação {aprovacao_id} ({item.tipo}) simulada: nada saiu do sistema")
        else:
            registrar_evento("info", "aprovacao", f"Ação {aprovacao_id} ({item.tipo}) executada")
        return resultado
    except Exception as e:
        _decidir(aprovacao_id, "erro", str(e)[:500])
        registrar_evento("erro", "aprovacao", f"Ação {aprovacao_id} falhou: {e}")
        raise


def aprovar_em_lote(ids: list[int], executores: dict[str, Callable[[dict], str]]):
    """Aprova vários de uma vez. Erros, inclusive bloqueio da conformidade,
    não interrompem os demais."""
    sucesso, falhas = [], []
    for i in ids:
        try:
            aprovar(i, executores)
            sucesso.append(i)
        except Exception as e:
            falhas.append((i, str(e)))
    return sucesso, falhas
