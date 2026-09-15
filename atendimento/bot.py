"""
Bot de atendimento.

Fluxo: pergunta chega -> checagem determinística de escalonamento ->
modelo redige -> a resposta vai pra FILA, não pro ar.

Depois de algumas semanas com a taxa de acerto medida, você pode liberar
auto-publicação só pras perguntas de baixo risco (as que casam com FAQ
fixo). Comece com tudo na fila. Bot solto em anúncio novo é a forma mais
rápida de perder reputação antes de ter feito a primeira venda.
"""
import os

import requests

from atendimento.persona import persona_padrao, precisa_escalar
from core import aprovacao
from db import registrar_evento

# Respostas fixas para as perguntas mais comuns. Resolvem a maior parte do
# volume sem custo de API e sem risco de improviso.
FAQ = {
    ("tem", "estoque"): "Sim, temos disponível para envio imediato.",
    ("nota", "fiscal"): "Sim, emitimos nota fiscal em todas as vendas.",
    ("original",): "Sim, produto original com garantia do fabricante.",
    ("cor",): None,   # depende do produto — deixa o modelo responder
}


def _responder_faq(pergunta: str) -> str | None:
    baixo = pergunta.lower()
    for chaves, resposta in FAQ.items():
        if resposta and all(c in baixo for c in chaves):
            return resposta
    return None


def redigir(pergunta: str, contexto_produto: dict, persona=None) -> str:
    """
    Redige a resposta com a Claude API. Devolve texto começando com
    'ESCALAR:' quando falta informação.
    """
    persona = persona or persona_padrao
    chave = os.getenv("ANTHROPIC_API_KEY", "")
    if not chave:
        return "ESCALAR: ANTHROPIC_API_KEY não configurada"

    ficha = "\n".join(f"{k}: {v}" for k, v in contexto_produto.items())
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": chave,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "system": persona.system_prompt(),
                "messages": [{
                    "role": "user",
                    "content": f"FICHA DO PRODUTO:\n{ficha}\n\nPERGUNTA DO COMPRADOR:\n{pergunta}",
                }],
            },
            timeout=30,
        )
        if r.status_code != 200:
            return f"ESCALAR: API retornou {r.status_code}"
        blocos = r.json().get("content", [])
        texto = "".join(b.get("text", "") for b in blocos if b.get("type") == "text")
        return texto.strip() or "ESCALAR: resposta vazia do modelo"
    except requests.RequestException as e:
        return f"ESCALAR: erro de rede ({e})"


def processar_pergunta(question_id: str, pergunta: str, contexto_produto: dict,
                       pedido_id: int | None = None) -> dict:
    """
    Pipeline completo de uma pergunta. Devolve um dict com o que aconteceu.
    Nada é publicado aqui — só enfileirado.
    """
    escalar, motivo = precisa_escalar(pergunta)
    if escalar:
        registrar_evento("atencao", "atendimento",
                         f"Pergunta {question_id} escalada ({motivo})",
                         {"pergunta": pergunta})
        return {"acao": "escalada", "motivo": motivo, "pergunta": pergunta}

    resposta = _responder_faq(pergunta) or redigir(pergunta, contexto_produto)

    if resposta.startswith("ESCALAR:"):
        registrar_evento("atencao", "atendimento",
                         f"Pergunta {question_id}: {resposta}", {"pergunta": pergunta})
        return {"acao": "escalada", "motivo": resposta[8:].strip(), "pergunta": pergunta}

    aprov_id = aprovacao.enfileirar(
        tipo="resposta_cliente",
        resumo=f"Responder pergunta {question_id}: {pergunta[:70]}",
        payload={"question_id": question_id, "pergunta": pergunta, "resposta": resposta},
        pedido_id=pedido_id,
    )
    return {"acao": "enfileirada", "aprovacao_id": aprov_id, "resposta": resposta}
