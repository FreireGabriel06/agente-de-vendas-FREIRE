"""
Proteção de toda requisição do painel: sessão, CSRF, papel, Host e
cabeçalhos de segurança (lotes 1, S4, S5, S11). instalar(app) registra os
middlewares na ordem certa.
"""
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from db import definir_ator, registrar_evento
from core import seguranca
from core.erros import pilha_sem_mensagem
# ------------------------------------------------------------- Autenticação
#
# Toda rota exige sessão, menos a tela de login e a chamada que a atende.
# O retorno do OAuth também exige sessão: o cookie é SameSite=Lax e viaja na
# navegação de volta do marketplace.

CAMINHOS_LIVRES = {"/login", "/api/login", "/saude"}

# O que o papel "leitura" pode fazer; o resto é só do "admin" (DPP da Amazon,
# 1.3: negar por padrão). Fica de fora: aprovar e recusar, revelar comprador,
# configuração e conexões, eventos de segurança, LGPD, cadastro e ciclo.
LEITURA_GET_EXATO = {"/", "/api/sessao", "/api/pendencias", "/api/pedidos", "/api/alertas",
                     "/api/produtos", "/api/fornecedores"}
LEITURA_GET_PREFIXO = ("/api/pedido/", "/api/produtos/", "/api/fornecedores/", "/api/amazon/")
LEITURA_POST = {"/api/logout", "/api/preco"}


def leitura_pode(metodo: str, caminho: str) -> bool:
    if metodo in ("GET", "HEAD"):
        if caminho.endswith("/comprador"):
            return False
        return caminho in LEITURA_GET_EXATO or caminho.startswith(LEITURA_GET_PREFIXO)
    return metodo == "POST" and caminho in LEITURA_POST


async def exigir_sessao(request: Request, call_next):
    caminho = request.url.path
    if caminho in CAMINHOS_LIVRES:
        # Na trilha, o login fica com o endereço de quem tentou (DPP 2.6.1).
        definir_ator(f"login de {request.client.host if request.client else 'desconhecido'}")
        return await call_next(request)

    sid = request.cookies.get(seguranca.COOKIE_SESSAO)
    sessao = seguranca.validar_sessao(sid)
    if sessao is None:
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Sessão necessária."}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        enviado = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(enviado, sessao["csrf"]):
            return JSONResponse({"detail": "Token CSRF inválido."}, status_code=403)

    if sessao["papel"] != "admin" and not leitura_pode(request.method, caminho):
        return JSONResponse({"detail": "Seu acesso é só de leitura."}, status_code=403)

    request.state.operador = sessao["usuario"]
    request.state.papel = sessao["papel"]
    definir_ator(sessao["usuario"])
    request.state.csrf = sessao["csrf"]
    request.state.sid = sid  # prende o state do OAuth a esta sessão
    return await call_next(request)


# Nomes pelos quais o painel aceita ser chamado (cabeçalho Host). Um Host
# qualquer viraria o endereço de retorno do OAuth da Shopee (achado S-14) e
# deixaria o painel local aberto a DNS rebinding. Fora do loopback, o nome
# público vem de HOSTS_PAINEL, separado por vírgula; executar.py recusa
# iniciar sem ele.
HOSTS_LOCAIS = frozenset({"127.0.0.1", "localhost", "::1"})


def hosts_permitidos() -> set[str]:
    extras = os.getenv("HOSTS_PAINEL", "")
    return set(HOSTS_LOCAIS) | {h.strip().lower().rstrip(".") for h in extras.split(",")
                                if h.strip()}


def nome_do_host(valor: str) -> str:
    """'painel.exemplo:8777' -> 'painel.exemplo'; '[::1]:8777' -> '::1'."""
    valor = (valor or "").strip().lower()
    if valor.startswith("["):
        return valor[1:valor.find("]")] if "]" in valor else ""
    return valor.partition(":")[0].rstrip(".")


async def host_permitido(request: Request, call_next):
    if nome_do_host(request.headers.get("host", "")) not in hosts_permitidos():
        return JSONResponse({"detail": "Host não permitido."}, status_code=400)
    return await call_next(request)


# Script e estilo embutidos continuam valendo (painel.html e o login usam
# <script> e onclick); a política ainda impede carregar script, imagem ou
# fetch de outro site, abrir o painel numa moldura, trocar a <base> e mandar
# formulário para fora. Achado S-09.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")


# Declarado depois de exigir_sessao e host_permitido para ficar por fora
# deles (o último middleware declarado roda primeiro): assim o 400, o 401 e o
# redirecionamento para o login também levam os cabeçalhos.
async def cabecalhos_de_seguranca(request: Request, call_next):
    """Cabeçalhos em toda resposta (lotes S4 e S5): CSP, sem farejar tipo,
    sem moldura de outro site, Referer só para a mesma origem (rotas da
    Amazon pedem no-referrer e já o definem), nada em cache (a resposta pode
    ter dado de comprador) e, sob HTTPS, HSTS."""
    resposta = await call_next(request)
    resposta.headers.setdefault("Content-Security-Policy", CSP)
    resposta.headers.setdefault("Cache-Control", "no-store")
    resposta.headers.setdefault("X-Content-Type-Options", "nosniff")
    resposta.headers.setdefault("X-Frame-Options", "DENY")
    resposta.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.scheme == "https":
        resposta.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resposta


async def identificar_requisicao(request: Request, call_next):
    """Um código curto por requisição (achado S-21): vai no cabeçalho
    X-Request-ID e, numa falha inesperada, na tela e no evento, para achar o
    evento a partir do que o operador vê. A exceção não sobe ao servidor: o
    uvicorn imprimiria a mensagem dela no log do container, e a mensagem de
    uma exceção qualquer pode repetir um valor decifrado do cofre (S-06). O
    evento guarda só o tipo e a pilha, sem a mensagem."""
    codigo = secrets.token_hex(6)
    request.state.codigo_requisicao = codigo
    try:
        resposta = await call_next(request)
    except Exception as e:
        try:
            registrar_evento("erro", "painel",
                             f"Falha inesperada em {request.method} {request.url.path}"
                             f" (código {codigo}): {type(e).__name__}",
                             {"codigo": codigo, "traceback": pilha_sem_mensagem(e)})
        except Exception:
            pass  # sem banco, a resposta ainda sai com o código
        resposta = JSONResponse(
            {"detail": f"Falha inesperada ({type(e).__name__}). Código para o suporte: {codigo}"},
            status_code=500)
    resposta.headers["X-Request-ID"] = codigo
    return resposta


def instalar(app) -> None:
    """O último middleware registrado roda primeiro: cabecalhos_de_seguranca
    fica por fora, para o 400, o 401, o redirecionamento e o 500 também
    levarem os cabeçalhos; identificar_requisicao dá o código a tudo, inclusive
    ao que host_permitido recusa; host_permitido vem antes da sessão."""
    app.middleware("http")(exigir_sessao)
    app.middleware("http")(host_permitido)
    app.middleware("http")(identificar_requisicao)
    app.middleware("http")(cabecalhos_de_seguranca)
