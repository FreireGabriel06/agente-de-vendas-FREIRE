"""
Login, logout, sessão e as páginas (login e painel).
"""
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from db import registrar_evento
from core import seguranca
from fastapi import APIRouter

BASE = Path(__file__).resolve().parent

rotas = APIRouter()

# ---------------------------------------------------------------- Sessão

@rotas.get("/login", response_class=HTMLResponse)
def pagina_login():
    return _pagina_login()


def _texto_do_corpo(valor) -> str:
    return valor if isinstance(valor, str) else ""


@rotas.post("/api/login")
async def api_login(request: Request):
    # Só JSON: um formulário de outro site (text/plain ou urlencoded, que o
    # navegador manda sem perguntar antes) não faz login (achado S-78).
    tipo = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if tipo != "application/json":
        return JSONResponse({"detail": "Envie o login em JSON."}, status_code=415)
    try:
        corpo = await request.json()
    except ValueError:
        corpo = None
    if not isinstance(corpo, dict):
        return JSONResponse({"detail": "Usuário ou senha inválidos."}, status_code=401)
    codigo = corpo.get("codigo")
    if isinstance(codigo, int) and not isinstance(codigo, bool):
        codigo = str(codigo).zfill(seguranca.TOTP_DIGITOS)
    try:
        # O scrypt roda fora do laço de eventos: o painel continua atendendo
        # enquanto a senha é conferida (achado S-08).
        sid, csrf = await run_in_threadpool(
            seguranca.autenticar, _texto_do_corpo(corpo.get("usuario")),
            _texto_do_corpo(corpo.get("senha")), _texto_do_corpo(codigo) or None,
            request.client.host if request.client else None)
    except seguranca.MuitasTentativas as e:
        return JSONResponse({"detail": str(e)}, status_code=429,
                            headers={"Retry-After": str(e.espera_s)})
    except seguranca.PrecisaCodigo as e:
        return JSONResponse({"detail": str(e), "mfa": True}, status_code=401)
    except seguranca.FalhaLogin as e:
        return JSONResponse({"detail": str(e)}, status_code=401)
    resposta = JSONResponse({"ok": True, "csrf": csrf})
    resposta.set_cookie(
        seguranca.COOKIE_SESSAO, sid, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
        max_age=seguranca.DURACAO_MAX_H * 3600, path="/")
    return resposta


@rotas.post("/api/logout")
def api_logout(request: Request):
    seguranca.encerrar_sessao(request.cookies.get(seguranca.COOKIE_SESSAO))
    registrar_evento("info", "seguranca", f"Saída de {request.state.operador}")
    resposta = JSONResponse({"ok": True})
    resposta.delete_cookie(seguranca.COOKIE_SESSAO, path="/")
    return resposta


@rotas.get("/api/sessao")
def api_sessao(request: Request):
    """O painel busca aqui o token que assina as chamadas que mudam estado."""
    return {"usuario": request.state.operador, "csrf": request.state.csrf,
            "papel": request.state.papel}


def _pagina_login() -> str:
    return """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entrar — Agente Comercial</title>
<style>
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#E3E8E3;
      color:#16211D;font-family:"Public Sans","Segoe UI",system-ui,sans-serif}
 form{background:#EDF0EC;border:1px solid #C3CDC6;padding:1.6rem;width:min(22rem,90vw);
      display:grid;gap:.85rem}
 h1{margin:0;font-size:1.1rem;letter-spacing:-.01em}
 label{display:grid;gap:.25rem;font-size:.88rem;color:#59665F}
 input{font:inherit;padding:.5rem .6rem;border:1px solid #96A69C;background:#fff;color:#16211D}
 button{font:inherit;padding:.5rem;border:1px solid #14614A;background:#14614A;
        color:#EDF0EC;cursor:pointer;font-weight:600}
 .erro{margin:0;color:#8C2F1B;font-size:.88rem;min-height:1.2em}
 input:focus-visible,button:focus-visible{outline:2px solid #14614A;outline-offset:2px}
 [hidden]{display:none}
</style></head><body>
<form id="entrar">
  <h1>Agente Comercial</h1>
  <label>Operador
    <input id="usuario" autocomplete="username" autofocus required></label>
  <label>Senha
    <input id="senha" type="password" autocomplete="current-password" required></label>
  <label id="campo-codigo" hidden>Código do app autenticador
    <input id="codigo" inputmode="numeric" autocomplete="one-time-code" maxlength="6"></label>
  <button type="submit">Entrar</button>
  <p class="erro" id="erro" role="alert"></p>
</form>
<script>
document.getElementById('entrar').onsubmit = async ev => {
  ev.preventDefault();
  const r = await fetch('/api/login', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({usuario: document.getElementById('usuario').value,
                          senha: document.getElementById('senha').value,
                          codigo: document.getElementById('codigo').value || null})});
  if (r.ok) { window.location = '/'; return; }
  const d = await r.json().catch(() => ({detail:'Não deu para entrar.'}));
  if (d.mfa) { document.getElementById('campo-codigo').hidden = false;
               document.getElementById('codigo').focus(); }
  document.getElementById('erro').textContent = d.detail || 'Não deu para entrar.';
};
</script></body></html>"""


# ------------------------------------------------------------------ Tela

@rotas.get("/", response_class=HTMLResponse)
def painel():
    return (BASE / "painel.html").read_text(encoding="utf-8")
