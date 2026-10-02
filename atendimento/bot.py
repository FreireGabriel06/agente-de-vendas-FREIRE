"""
Bot de atendimento.

Fluxo: pergunta chega -> checagem determinística de escalonamento ->
modelo redige -> a resposta vai pra FILA, não pro ar.

A redação usa a Claude API pelo SDK oficial (atendimento/claude_api.py), com
o modelo de MODELO_CLAUDE. Recusa do modelo, resposta cortada, erro da API ou
falta de chave não viram resposta: viram escalonamento para você, o mesmo
caminho das perguntas que tocam em reembolso ou garantia.

Cada pergunta vai ao modelo uma vez. A tabela perguntas_tratadas guarda as
que já foram para a fila ou foram escaladas, e o worker pula essas: o
marketplace devolve a pergunta como sem resposta até ela ser publicada, e
cada envio ao modelo é uma chamada paga. Só a falha temporária (limite de
uso, API fora do ar, rede, chave ou cofre faltando) tenta de novo no ciclo
seguinte, sem repetir o aviso. A resposta aprovada que não foi publicada
(em simulação, ou porque a publicação falhou) volta para a fila com o mesmo
texto, sem nova chamada.

O registro de eventos não leva a pergunta do comprador nem o texto do modelo
(que pode repetir CPF ou endereço da pergunta): leva o id da pergunta e um
motivo fixo.

Depois de algumas semanas com a taxa de acerto medida, você pode liberar
auto-publicação só pras perguntas de baixo risco (as que casam com FAQ
fixo). Comece com tudo na fila. Bot solto em anúncio novo é a forma mais
rápida de perder reputação antes de ter feito a primeira venda.
"""
import json
from typing import NamedTuple

from atendimento import claude_api
from atendimento.persona import persona_padrao, precisa_escalar
from config import config
from core import aprovacao, cofre
from db import agora, conectar, registrar_evento

# Situações gravadas em perguntas_tratadas.
ENFILEIRADA = "enfileirada"
ESCALADA = "escalada"
TENTAR_DE_NOVO = "tentar_de_novo"

# Respostas fixas para as perguntas mais comuns. Resolvem a maior parte do
# volume sem custo de API e sem risco de improviso.
FAQ = {
    ("tem", "estoque"): "Sim, temos disponível para envio imediato.",
    ("nota", "fiscal"): "Sim, emitimos nota fiscal em todas as vendas.",
    ("original",): "Sim, produto original com garantia do fabricante.",
    ("cor",): None,   # depende do produto — deixa o modelo responder
}


class Rascunho(NamedTuple):
    texto: str              # a resposta, ou "ESCALAR: <motivo>"
    temporaria: bool = False
    do_modelo: bool = True  # False: motivo fixo do programa, que pode ir para o registro


def _responder_faq(pergunta: str) -> str | None:
    baixo = pergunta.lower()
    for chaves, resposta in FAQ.items():
        if resposta and all(c in baixo for c in chaves):
            return resposta
    return None


def _rascunho(pergunta: str, contexto_produto: dict, persona=None,
              referencia: str = "") -> Rascunho:
    persona = persona or persona_padrao
    try:
        chave = claude_api.chave_api()  # ambiente do sistema, depois o cofre
    except cofre.ErroCofre:
        return Rascunho("ESCALAR: cofre de credenciais indisponível (confira a chave do cofre)",
                        temporaria=True, do_modelo=False)
    if not chave:
        return Rascunho("ESCALAR: ANTHROPIC_API_KEY não configurada",
                        temporaria=True, do_modelo=False)

    ficha = "\n".join(f"{k}: {v}" for k, v in contexto_produto.items())
    try:
        texto = claude_api.redigir_texto(
            persona.system_prompt(),
            f"FICHA DO PRODUTO:\n{ficha}\n\nPERGUNTA DO COMPRADOR:\n{pergunta}",
            chave=chave,
            referencia=referencia,
        )
    except claude_api.ErroClaude as e:
        return Rascunho(f"ESCALAR: {e}", temporaria=e.temporaria, do_modelo=False)
    return Rascunho(texto)


def redigir(pergunta: str, contexto_produto: dict, persona=None) -> str:
    """
    Redige a resposta com a Claude API. Devolve texto começando com
    'ESCALAR:' quando falta informação ou quando não há rascunho confiável.
    """
    r = _rascunho(pergunta, contexto_produto, persona)
    if r.temporaria:
        return r.texto + "; tenta de novo no próximo ciclo"
    return r.texto


# ----------------------------------------------------- perguntas já tratadas

def _tratamento(question_id: str):
    with conectar() as conn:
        return conn.execute("SELECT * FROM perguntas_tratadas WHERE question_id = ?",
                            (question_id,)).fetchone()


def _marcar(question_id: str, situacao: str, aprovacao_id: int | None = None):
    with conectar() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO perguntas_tratadas (question_id, situacao, aprovacao_id,"
            " tratada_em) VALUES (?,?,?,?)", (question_id, situacao, aprovacao_id, agora()))


def _reabrir_resposta(question_id: str, a) -> None:
    """Põe de volta na fila, com o mesmo texto e sem nova chamada ao modelo, a
    resposta aprovada que não chegou ao marketplace:

      - aprovada em modo simulação (nada publicado), agora que MODO_SIMULACAO
        está desligado;
      - aprovada, mas a publicação falhou (Mercado Livre fora do ar, rede,
        renovação do token): a aprovação fica com status 'erro' e não pode
        ser aprovada de novo.

    Só chega aqui a pergunta que o marketplace ainda devolve como sem
    resposta. Resposta recusada na fila não volta."""
    if a["status"] == "executada" and aprovacao.foi_simulado(a["resultado"]):
        if config.modo_simulacao:
            return
        nivel, motivo = "info", "a aprovação anterior foi em modo simulação e nada foi publicado"
    elif a["status"] == "erro":
        nivel, motivo = "atencao", "a publicação anterior falhou e nada foi publicado"
    else:
        return
    novo = aprovacao.enfileirar("resposta_cliente", a["resumo"], json.loads(a["payload_json"]),
                                pedido_id=a["pedido_id"])
    _marcar(question_id, ENFILEIRADA, novo)
    registrar_evento(nivel, "atendimento",
                     f"Resposta à pergunta {question_id} voltou para a fila: {motivo}")


def ja_tratada(question_id: str) -> bool:
    """True quando a pergunta já foi para a fila ou foi escalada: não volta ao
    modelo. False para pergunta nova, cuja falha anterior foi temporária, ou
    cujo rascunho sumiu da fila (o demo.py apaga a fila).

    A resposta aprovada que não foi publicada (aprovada em simulação, com
    MODO_SIMULACAO desligado agora, ou com a publicação falha) volta para a
    fila aqui, com o mesmo texto."""
    linha = _tratamento(question_id)
    if linha is None or linha["situacao"] == TENTAR_DE_NOVO:
        return False
    if linha["situacao"] == ENFILEIRADA:
        with conectar() as conn:
            a = conn.execute("SELECT * FROM aprovacoes WHERE id = ?",
                             (linha["aprovacao_id"],)).fetchone()
        if a is None:
            return False
        _reabrir_resposta(question_id, a)
    return True


def processar_pergunta(question_id: str, pergunta: str, contexto_produto: dict,
                       pedido_id: int | None = None) -> dict:
    """
    Pipeline completo de uma pergunta. Devolve um dict com o que aconteceu.
    Nada é publicado aqui — só enfileirado. O resultado fica gravado em
    perguntas_tratadas.
    """
    escalar, motivo = precisa_escalar(pergunta)
    if escalar:
        # Só o id e o gatilho: a pergunta do comprador não vai para o log.
        registrar_evento("atencao", "atendimento", f"Pergunta {question_id} escalada ({motivo})")
        _marcar(question_id, ESCALADA)
        return {"acao": "escalada", "motivo": motivo, "pergunta": pergunta}

    faq = _responder_faq(pergunta)
    r = Rascunho(faq) if faq else _rascunho(pergunta, contexto_produto, referencia=question_id)

    if r.texto.startswith("ESCALAR:"):
        motivo = r.texto[8:].strip()
        if r.do_modelo:
            # O texto do modelo pode repetir dado da pergunta: fica só no
            # retorno, e o registro leva um motivo fixo.
            registro = (f"Pergunta {question_id} escalada: o modelo pediu revisão humana "
                        "(o texto dele não vai para o registro)")
        else:
            registro = f"Pergunta {question_id}: ESCALAR: {motivo}"
        if r.temporaria:
            motivo += "; tenta de novo no próximo ciclo"
            registro += "; tenta de novo no próximo ciclo"
        anterior = _tratamento(question_id)
        if not (r.temporaria and anterior is not None and anterior["situacao"] == TENTAR_DE_NOVO):
            registrar_evento("atencao", "atendimento", registro)
        _marcar(question_id, TENTAR_DE_NOVO if r.temporaria else ESCALADA)
        return {"acao": "escalada", "motivo": motivo, "pergunta": pergunta}

    aprov_id = aprovacao.enfileirar(
        tipo="resposta_cliente",
        resumo=f"Responder pergunta {question_id}: {pergunta[:70]}",
        payload={"question_id": question_id, "pergunta": pergunta, "resposta": r.texto},
        pedido_id=pedido_id,
    )
    _marcar(question_id, ENFILEIRADA, aprov_id)
    return {"acao": "enfileirada", "aprovacao_id": aprov_id, "resposta": r.texto}
