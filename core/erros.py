"""
Mensagens de erro que podem sair do processo: para eventos, para a fila de
aprovação e para a tela.

O corpo da resposta de um marketplace pode trazer dado de comprador ou ecoar
o que foi enviado, e a URL da Shopee leva o access_token na query. A mensagem
de uma exceção qualquer pode repetir um valor decifrado do cofre. Por isso:

  - os conectores montam a mensagem com método, caminho sem query, status HTTP
    e o código de erro do marketplace, quando ele tem cara de código; nunca o
    corpo, nunca a URL inteira (falha de rede vira só o tipo da exceção);
  - quem grava ou mostra um erro usa mensagem_segura(): o texto de exceções
    marcadas com MensagemSegura (escritas pelo projeto) passa; de qualquer
    outra, só o tipo.

Lote S6; achado S-06 da auditoria de 06/10/2026 (A08 do relatório).
"""
import re
import traceback
from urllib.parse import urlsplit

_CODIGO = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


class MensagemSegura(Exception):
    """Exceção cujo texto foi escrito pelo projeto, sem dado de fora."""


def mensagem_segura(erro: BaseException, limite: int = 500) -> str:
    if isinstance(erro, MensagemSegura):
        return str(erro)[:limite]
    return f"falha inesperada ({type(erro).__name__})"


def pilha_sem_mensagem(erro: BaseException) -> str:
    """Arquivo, linha e código de cada quadro, sem a mensagem da exceção."""
    quadros = traceback.format_list(traceback.extract_tb(erro.__traceback__))
    return f"{type(erro).__name__}\n{''.join(quadros)}"[-2000:]


# ------------------------------------------------------------------ HTTP

def codigo_seguro(valor) -> str | None:
    """O valor, se for um código curto (letras, dígitos, _ . : -); senão None."""
    return valor if isinstance(valor, str) and _CODIGO.fullmatch(valor) else None


def caminho_sem_query(caminho: str) -> str:
    return urlsplit(caminho).path or caminho.split("?", 1)[0]


def codigo_do_corpo(resposta) -> str | None:
    """O campo "error" (ou "code") do JSON de erro, se for um código. Nunca
    "message": é texto livre do marketplace."""
    try:
        corpo = resposta.json()
    except ValueError:
        return None
    if isinstance(corpo, dict):
        for chave in ("error", "code"):
            codigo = codigo_seguro(corpo.get(chave))
            if codigo:
                return codigo
    return None


def descrever(metodo: str, caminho: str, status: int | None = None,
              codigo: str | None = None) -> str:
    """'GET /orders/search -> HTTP 404 (not_found)'."""
    texto = f"{metodo} {caminho_sem_query(caminho)}"
    if status is not None:
        texto += f" -> HTTP {status}"
    codigo = codigo_seguro(codigo)
    if codigo:
        texto += f" ({codigo})"
    return texto


def falha_de_rede(metodo: str, caminho: str, erro: BaseException) -> str:
    """Só o tipo: a mensagem do requests traz a URL com a query."""
    return f"{metodo} {caminho_sem_query(caminho)} -> falha de rede ({type(erro).__name__})"
