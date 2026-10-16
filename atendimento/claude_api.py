"""
Acesso à Claude API pelo SDK oficial (pacote anthropic). Nenhuma chamada HTTP
feita à mão.

Regras desta integração, do guia de migração para o Claude Opus 5.5:

  - Modelo em MODELO_CLAUDE (padrão claude-opus-5-5), esforço em
    ESFORCO_CLAUDE (padrão low), os dois em config.claude. Trocar de modelo
    é decisão do dono, nunca do programa.
  - O raciocínio (thinking) fica sempre ligado no claude-opus-5-5. Não se
    envia thinking desligado, budget_tokens, temperature, top_p, top_k,
    prefill de assistente nem tool_choice forçado: tudo isso volta 400. O
    controle de custo e demora é output_config={"effort": ...}, sempre
    explícito.
  - max_tokens e tempo de espera crescem com o esforço (LIMITES_POR_ESFORCO):
    o raciocínio conta dentro do max_tokens e demora mais nos níveis altos.
    O texto curto vem do prompt de sistema, não de um limite apertado.
  - Fallback no servidor ligado: beta server-side-fallback-2026-07-01 com
    fallbacks="default". Se o modelo recusar por política, a API tenta o
    modelo que a Anthropic recomenda, na mesma chamada; quando isso acontece,
    um evento registra qual modelo respondeu (ele é cobrado pela tabela dele).
  - stop_reason conferido antes de ler o conteúdo: "refusal" (a cadeia toda
    recusou), "max_tokens" ou qualquer coisa que não seja "end_turn" não vira
    resposta — vira escalonamento para uma pessoa. Só blocos type == "text"
    são lidos.
  - A chave vem do cofre (core/cofre.segredo) e vai explícita para
    anthropic.Anthropic(api_key=...), com base_url fixa em URL_API: um
    ANTHROPIC_BASE_URL no ambiente não desvia a chave para outro endereço.
    Nem a chave nem o conteúdo do prompt (que traz a pergunta do comprador)
    vão para registro ou mensagem de erro.
  - Teto diário de gasto (lote E7; achado S-22 da auditoria): cada resposta
    soma os tokens de usage em uso_claude, com o custo estimado pela tabela
    de preço; antes de chamar, CLAUDE_TETO_DIARIO_USD e
    CLAUDE_CHAMADAS_POR_DIA são conferidos. Passou, a chamada não sai: a
    pergunta fica para o dia seguinte (erro temporário) e um evento avisa uma
    vez por dia.
"""
import re
from datetime import datetime, timezone
from decimal import Decimal

import anthropic

from config import config
from core import cofre
from db import agora, conectar, registrar_evento

BETA_FALLBACK = "server-side-fallback-2026-07-01"

# Endereço fixo da API. O SDK leria ANTHROPIC_BASE_URL do ambiente se a
# base_url não viesse explícita.
URL_API = "https://api.anthropic.com"

# (max_tokens, segundos de espera) por nível de esforço. O raciocínio do
# claude-opus-5-5 conta dentro do max_tokens e cresce com o esforço; nos
# níveis altos o turno também demora mais. O SDK ainda tenta de novo, sozinho,
# falha de conexão, tempo esgotado, 408, 409, 429 e 5xx (max_retries).
LIMITES_POR_ESFORCO = {
    "low": (4000, 60.0),
    "medium": (8000, 120.0),
    "high": (16000, 300.0),
    "xhigh": (16000, 300.0),
    "max": (16000, 300.0),
}
TEMPO_TESTE = 20.0
TENTATIVAS_SDK = 2

# Dólares por milhão de tokens (entrada, saída), da tabela da Anthropic
# consultada em 16/10/2026. A estimativa erra para cima: leitura de cache conta
# como entrada cheia, gravação de cache como 1,25 x, e modelo fora da tabela
# paga o preço do mais caro. O raciocínio vem dentro de output_tokens.
PRECO_POR_MILHAO = {
    "claude-fable-5-1": (Decimal("10"), Decimal("50")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
}
PRECO_DESCONHECIDO = max(PRECO_POR_MILHAO.values())


class ErroClaude(Exception):
    """Falha que não produz texto. A mensagem é fixa e não traz chave, prompt
    nem o corpo da resposta da API. temporaria=True: vale tentar de novo no
    próximo ciclo (limite de uso, API fora do ar, rede, tempo esgotado)."""

    def __init__(self, mensagem: str, temporaria: bool = False):
        super().__init__(mensagem)
        self.temporaria = temporaria


def chave_api() -> str:
    """ANTHROPIC_API_KEY pela regra do cofre: ambiente do sistema, cofre,
    valor antigo no .env. Levanta cofre.ErroCofre se o cofre não abre."""
    return cofre.segredo("ANTHROPIC_API_KEY")


def limites(esforco: str | None = None) -> tuple[int, float]:
    """(max_tokens, segundos de espera) para o esforço configurado."""
    return LIMITES_POR_ESFORCO.get(esforco or config.claude.esforco, LIMITES_POR_ESFORCO["low"])


def criar_cliente(chave: str, tempo: float) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=chave, base_url=URL_API, timeout=tempo,
                               max_retries=TENTATIVAS_SDK)


def _tipo_do_erro(erro: anthropic.APIStatusError) -> str:
    """O tipo do erro da API (invalid_request_error, billing_error...), que é
    um nome fixo e não traz o corpo da resposta."""
    tipo = getattr(erro, "type", None)
    return tipo if isinstance(tipo, str) and re.fullmatch(r"[a-z_]{1,40}", tipo) else "tipo não informado"


def _chamar(funcao, tempo: float):
    """Roda uma chamada ao SDK e converte o erro da API em ErroClaude, do mais
    específico ao mais geral. Só o tipo e o código HTTP entram na mensagem; o
    `from None` larga a exceção do SDK, que pode trazer o corpo da resposta."""
    modelo = config.claude.modelo
    try:
        return funcao()
    except anthropic.AuthenticationError:
        raise ErroClaude("chave da API recusada pela Anthropic (401); confira "
                         "ANTHROPIC_API_KEY no painel") from None
    except anthropic.PermissionDeniedError:
        raise ErroClaude(f"a chave não tem acesso ao modelo {modelo} (403)") from None
    except anthropic.NotFoundError:
        raise ErroClaude(f"modelo {modelo} não encontrado para esta chave (404); "
                         "confira MODELO_CLAUDE") from None
    except anthropic.BadRequestError as e:
        raise ErroClaude(f"a API recusou o pedido (HTTP 400, {_tipo_do_erro(e)}); confira se "
                         f"MODELO_CLAUDE ({modelo}) aceita esforço e fallback e se a "
                         f"organização tem o beta {BETA_FALLBACK}") from None
    except anthropic.RateLimitError:
        raise ErroClaude("limite de uso da API atingido (429)", temporaria=True) from None
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise ErroClaude(f"API da Anthropic indisponível (HTTP {e.status_code})",
                             temporaria=True) from None
        raise ErroClaude(f"a API recusou o pedido (HTTP {e.status_code}, "
                         f"{_tipo_do_erro(e)})") from None
    except anthropic.APITimeoutError:
        # Subclasse de APIConnectionError: precisa vir antes dela.
        raise ErroClaude(f"a API demorou mais que {tempo:.0f} s para responder (tempo "
                         "esgotado, já com as novas tentativas do SDK)", temporaria=True) from None
    except anthropic.APIConnectionError:
        raise ErroClaude("sem conexão com a API da Anthropic", temporaria=True) from None


def _hoje() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _inteiro_do_uso(uso, campo: str) -> int:
    valor = getattr(uso, campo, None) if uso is not None else None
    return valor if isinstance(valor, int) and valor >= 0 else 0


def custo_estimado(modelos, entrada: int, cache_gravado: int, saida: int) -> Decimal:
    """Custo em dólar pelo preço do mais caro entre os modelos que podem ter
    respondido (o configurado e o de reserva)."""
    p_entrada, p_saida = max(PRECO_POR_MILHAO.get(m, PRECO_DESCONHECIDO) for m in modelos)
    return (entrada * p_entrada + cache_gravado * p_entrada * Decimal("1.25")
            + saida * p_saida) / Decimal(1_000_000)


def uso_de_hoje() -> dict:
    with conectar() as conn:
        linha = conn.execute("SELECT * FROM uso_claude WHERE dia = ?", (_hoje(),)).fetchone()
    if linha is None:
        return {"dia": _hoje(), "chamadas": 0, "tokens_entrada": 0, "tokens_saida": 0,
                "custo_estimado": Decimal("0")}
    return {**dict(linha), "custo_estimado": Decimal(linha["custo_estimado_dec"])}


def _registrar_uso(resposta, reserva: str | None) -> None:
    """Soma a resposta no uso do dia: toda resposta que chegou é cobrada,
    também a recusa e a cortada pelo limite."""
    uso = getattr(resposta, "usage", None)
    entrada = (_inteiro_do_uso(uso, "input_tokens")
               + _inteiro_do_uso(uso, "cache_read_input_tokens"))
    gravado = _inteiro_do_uso(uso, "cache_creation_input_tokens")
    saida = _inteiro_do_uso(uso, "output_tokens")
    custo = custo_estimado([config.claude.modelo] + ([reserva] if reserva else []),
                           entrada, gravado, saida)
    with conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")  # um somatório por vez, entre processos também
        linha = conn.execute("SELECT custo_estimado_dec FROM uso_claude WHERE dia = ?",
                             (_hoje(),)).fetchone()
        total = (Decimal(linha[0]) if linha else Decimal("0")) + custo
        conn.execute(
            "INSERT INTO uso_claude (dia, chamadas, tokens_entrada, tokens_saida, custo_estimado_dec)"
            " VALUES (?, 1, ?, ?, ?) ON CONFLICT(dia) DO UPDATE SET chamadas = chamadas + 1,"
            " tokens_entrada = tokens_entrada + excluded.tokens_entrada,"
            " tokens_saida = tokens_saida + excluded.tokens_saida,"
            " custo_estimado_dec = ?",
            (_hoje(), entrada + gravado, saida, str(total), str(total)))


def conferir_teto() -> None:
    """Levanta ErroClaude temporário quando o dia já passou do teto de gasto
    estimado ou de chamadas. O aviso vai para os eventos uma vez por dia."""
    uso = uso_de_hoje()
    teto = Decimal(str(config.claude.teto_diario_usd))
    limite = config.claude.chamadas_por_dia
    if uso["chamadas"] < limite and uso["custo_estimado"] < teto:
        return
    motivo = (f"teto diário da Claude API atingido ({uso['chamadas']} chamada(s), "
              f"US$ {uso['custo_estimado']:.2f} estimados; tetos {limite} e US$ {teto:.2f})")
    with conectar() as conn:
        avisado = conn.execute("SELECT valor FROM estado_sistema WHERE chave = 'claude_teto_avisado'"
                               ).fetchone()
        if avisado is None or avisado[0] != uso["dia"]:
            conn.execute("INSERT INTO estado_sistema (chave, valor, atualizado_em) VALUES"
                         " ('claude_teto_avisado', ?, ?) ON CONFLICT(chave) DO UPDATE SET"
                         " valor = excluded.valor, atualizado_em = excluded.atualizado_em",
                         (uso["dia"], agora()))
            novo_aviso = True
        else:
            novo_aviso = False
    if novo_aviso:
        registrar_evento("atencao", "atendimento",
                         f"{motivo[:1].upper()}{motivo[1:]}. As perguntas esperam o dia seguinte; "
                         "o teto fica em CLAUDE_TETO_DIARIO_USD e CLAUDE_CHAMADAS_POR_DIA.")
    raise ErroClaude(motivo, temporaria=True)


def _modelo_de_reserva(resposta) -> str | None:
    """O modelo de reserva que serviu a resposta, ou None. O sinal é uma
    entrada fallback_message em usage.iterations."""
    iteracoes = getattr(getattr(resposta, "usage", None), "iterations", None) or []
    for it in iteracoes:
        if getattr(it, "type", None) == "fallback_message":
            return str(getattr(it, "model", None) or getattr(resposta, "model", "") or "?")
    return None


def redigir_texto(sistema: str, conteudo: str, *, chave: str, max_tokens: int | None = None,
                  referencia: str = "") -> str:
    """Uma mensagem ao modelo configurado; devolve só o texto final.
    max_tokens e tempo de espera vêm do esforço (LIMITES_POR_ESFORCO), salvo
    max_tokens explícito. referencia (o id da pergunta) só entra no evento do
    modelo de reserva.

    Levanta ErroClaude quando não há texto que possa ir para a fila: erro da
    API, recusa, resposta cortada pelo limite, resposta vazia.
    """
    conferir_teto()  # antes de gastar: passou do teto, a chamada não sai
    max_padrao, tempo = limites()
    cliente = criar_cliente(chave, tempo)
    resposta = _chamar(lambda: cliente.beta.messages.create(
        model=config.claude.modelo,
        max_tokens=max_tokens or max_padrao,
        system=sistema,
        messages=[{"role": "user", "content": conteudo}],
        output_config={"effort": config.claude.esforco},
        betas=[BETA_FALLBACK],
        fallbacks="default",
    ), tempo)

    # stop_reason antes de qualquer leitura do conteúdo.
    motivo = getattr(resposta, "stop_reason", None)
    reserva = _modelo_de_reserva(resposta)
    _registrar_uso(resposta, reserva)
    if motivo == "refusal":
        # stop_details é só informação (a categoria pode vir None).
        categoria = getattr(getattr(resposta, "stop_details", None), "category", None)
        detalhe = f", categoria {categoria}" if isinstance(categoria, str) and categoria else ""
        reserva_txt = f"; o modelo de reserva {reserva} também recusou" if reserva else ""
        raise ErroClaude(f"o modelo recusou redigir esta resposta (refusal{detalhe}){reserva_txt}")
    if motivo == "max_tokens":
        raise ErroClaude("o rascunho foi cortado pelo limite de tokens (max_tokens)")
    if motivo != "end_turn":
        raise ErroClaude(f"resposta incompleta do modelo (stop_reason={motivo})")

    texto = "".join(getattr(bloco, "text", None) or ""
                    for bloco in (resposta.content or [])
                    if getattr(bloco, "type", None) == "text").strip()
    if not texto:
        raise ErroClaude("resposta vazia do modelo")
    if reserva:
        alvo = f" da pergunta {referencia}" if referencia else ""
        registrar_evento("info", "atendimento",
                         f"Rascunho{alvo} redigido pelo modelo de reserva {reserva}: o "
                         f"{config.claude.modelo} recusou e a API usou o fallback. A chamada é "
                         "cobrada pela tabela do modelo de reserva.")
    return texto


def _frase(texto: str) -> str:
    return texto[:1].upper() + texto[1:] + "."


def testar_conexao() -> dict:
    """Teste do painel: confere a chave e o acesso ao modelo configurado com
    models.retrieve, que não gasta token. Não manda mensagem nenhuma, então
    não confere cobrança nem o acesso ao beta de fallback."""
    chave = chave_api()
    if not chave:
        return {"ok": False, "detalhe": "Chave não preenchida."}
    modelo = config.claude.modelo
    try:
        info = _chamar(lambda: criar_cliente(chave, TEMPO_TESTE).models.retrieve(modelo),
                       TEMPO_TESTE)
    except ErroClaude as e:
        detalhe = _frase(str(e)) + (" Tente de novo em instantes." if e.temporaria else "")
        return {"ok": False, "detalhe": detalhe}
    return {"ok": True,
            "detalhe": f"Chave válida e modelo {info.id} disponível "
                       f"(esforço {config.claude.esforco}). O teste não gasta tokens; "
                       "cobrança e acesso ao beta de fallback só aparecem ao redigir."}
