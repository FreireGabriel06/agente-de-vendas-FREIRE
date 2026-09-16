"""
Painel de operação.

Servidor local. Sobe junto com o worker no mesmo processo — você abre o
navegador e opera tudo dali.

Decisões de UX que valem explicar:

  - A tela abre no que importa: quanto dinheiro está esperando sua decisão.
    Não em gráfico, não em boas-vindas.
  - Triagem por teclado. j/k anda na lista, a aprova, r recusa, / filtra.
    Quem usa isso todo dia não quer mirar o mouse em 12 botões.
  - Compra tem confirmação; resposta a cliente tem desfazer de 6 segundos.
    Modal em tudo cansa; desfazer só funciona se o dano for reversível.
  - Item bloqueado pela conformidade não tem botão de aprovar. Não é aviso
    que você ignora clicando — o caminho não existe até a causa ser resolvida.
"""
import asyncio
import json
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from db import conectar, inicializar, registrar_evento
from core import aprovacao, conformidade, privacidade, seguranca
from core.estados import Estado, ESTADOS_CRITICOS, historico
from inteligencia import precificacao, tendencias

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Agente Comercial", docs_url=None, redoc_url=None)


# ------------------------------------------------------------- Autenticação
#
# Toda rota exige sessão, menos a tela de login e a chamada que a atende.
# O retorno do OAuth também exige sessão: o cookie é SameSite=Lax e viaja na
# navegação de volta do marketplace.

CAMINHOS_LIVRES = {"/login", "/api/login"}


@app.middleware("http")
async def exigir_sessao(request: Request, call_next):
    caminho = request.url.path
    if caminho in CAMINHOS_LIVRES:
        return await call_next(request)

    sessao = seguranca.validar_sessao(request.cookies.get(seguranca.COOKIE_SESSAO))
    if sessao is None:
        if caminho.startswith("/api/"):
            return JSONResponse({"detail": "Sessão necessária."}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        enviado = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(enviado, sessao["csrf"]):
            return JSONResponse({"detail": "Token CSRF inválido."}, status_code=403)

    request.state.operador = sessao["usuario"]
    request.state.csrf = sessao["csrf"]
    return await call_next(request)



# ------------------------------------------------------------------ Dados

def _contexto_conformidade(pendencia) -> dict:
    p = pendencia.payload
    return {
        "marketplace": p.get("marketplace"),
        "envio_direto_fornecedor": p.get("envio_direto_fornecedor", False),
        "reembalagem_confirmada": p.get("reembalagem_confirmada", True),
        "prazo_fornecedor_dias": p.get("prazo_fornecedor_dias"),
        "prazo_anuncio_dias": p.get("prazo_anuncio_dias"),
        "margem_pct": p.get("margem_prevista"),
        "categoria_regulada": p.get("categoria_regulada"),
        "habilitacao_confirmada": p.get("habilitacao_confirmada", True),
        "emite_nota": p.get("emite_nota", True),
    }


def _pendencias_com_conformidade() -> list[dict]:
    itens = []
    for a in aprovacao.pendentes():
        res = conformidade.verificar(_contexto_conformidade(a))
        itens.append({
            "id": a.id,
            "tipo": a.tipo,
            "resumo": a.resumo,
            "valor": a.valor,
            "margem": a.payload.get("margem_prevista"),
            "marketplace": a.payload.get("marketplace", ""),
            "bloqueado": res.bloqueado,
            "violacoes": [
                {"regra": v.regra, "severidade": v.severidade.value,
                 "mensagem": v.mensagem, "saida": v.saida, "fonte": v.fonte}
                for v in res.violacoes
            ],
            "reversivel": a.tipo != "compra_fornecedor",
        })
    return itens


@app.get("/api/pendencias")
def api_pendencias():
    itens = _pendencias_com_conformidade()
    liberadas = [i for i in itens if not i["bloqueado"]]
    return {
        "itens": itens,
        "total": len(itens),
        "bloqueadas": len(itens) - len(liberadas),
        "exposicao": round(sum(i["valor"] or 0 for i in liberadas), 2),
    }


@app.get("/api/pedidos")
def api_pedidos(estado: str | None = None):
    sql = ("SELECT id, marketplace, id_externo, valor_bruto, margem_prevista,"
           " estado, codigo_rastreio, criado_em FROM pedidos")
    params = ()
    if estado:
        sql += " WHERE estado = ?"
        params = (estado,)
    sql += " ORDER BY id DESC LIMIT 200"
    with conectar() as conn:
        linhas = conn.execute(sql, params).fetchall()
    return {"pedidos": [dict(l) for l in linhas],
            "criticos": [e.value for e in ESTADOS_CRITICOS]}


@app.get("/api/pedido/{pedido_id}")
def api_pedido(pedido_id: int):
    with conectar() as conn:
        p = conn.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
    if not p:
        raise HTTPException(404, "Pedido não encontrado")
    d = dict(p)
    # PII nunca vai pra tela inteira sem motivo. Mascarado por padrão.
    d["comprador_nome"] = privacidade.mascarar(
        privacidade.decifrar(p["comprador_nome"]), visivel=4)
    d.pop("comprador_id", None)
    d.pop("endereco_json", None)
    d["historico"] = [dict(h) for h in historico(pedido_id)]
    return d


@app.get("/api/pedido/{pedido_id}/comprador")
def api_comprador(pedido_id: int, request: Request):
    """Revela o PII sob demanda, e registra o acesso. LGPD art. 6º, X.

    O registro guarda o operador que pediu, não o nome do sistema: sem isso
    a trilha não responde quem viu o dado do comprador.
    """
    return privacidade.ler_comprador(pedido_id, ator=request.state.operador,
                                     finalidade="conferência de entrega pelo operador")


@app.post("/api/aprovar/{aprovacao_id}")
def api_aprovar(aprovacao_id: int, request: Request):
    from worker import EXECUTORES
    itens = {i["id"]: i for i in _pendencias_com_conformidade()}
    alvo = itens.get(aprovacao_id)
    if alvo is None:
        raise HTTPException(404, "Pendência não encontrada")
    if alvo["bloqueado"]:
        raise HTTPException(409, "Bloqueado pela conformidade: " +
                            "; ".join(v["mensagem"] for v in alvo["violacoes"]))
    try:
        resultado = aprovacao.aprovar(aprovacao_id, EXECUTORES)
        registrar_evento("info", "aprovacao",
                         f"Ação {aprovacao_id} liberada por {request.state.operador}")
        return {"ok": True, "resultado": resultado}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/recusar/{aprovacao_id}")
async def api_recusar(aprovacao_id: int, request: Request):
    corpo = await request.json() if await request.body() else {}
    motivo = corpo.get("motivo", "recusado no painel")
    aprovacao.recusar(aprovacao_id, f"{motivo} (por {request.state.operador})")
    return {"ok": True}


@app.post("/api/nichos")
async def api_nichos(request: Request):
    corpo = await request.json()
    termos = [t.strip() for t in corpo.get("termos", []) if t.strip()]
    if not termos:
        raise HTTPException(400, "Informe pelo menos um termo")
    loop = asyncio.get_event_loop()
    ops = await loop.run_in_executor(None, lambda: tendencias.analisar(termos))
    return {"oportunidades": [
        {"termo": o.termo, "demanda": o.demanda, "tendencia": o.tendencia_pct,
         "concorrencia": o.concorrencia, "preco": o.preco_mediano, "score": o.score}
        for o in ops]}


@app.post("/api/preco")
async def api_preco(request: Request):
    c = await request.json()
    r = precificacao.sugerir_preco(
        custo_produto=float(c["custo"]),
        margem_alvo_pct=float(c.get("margem", 25)),
        peso_kg=float(c.get("peso", 0.3)),
        tipo_anuncio=c.get("tipo", "classico"),
    )
    return r.como_dict()


@app.get("/api/lgpd")
def api_lgpd():
    with conectar() as conn:
        acessos = conn.execute(
            "SELECT operacao, ator, finalidade, ocorrido_em FROM acessos_pii"
            " ORDER BY id DESC LIMIT 50").fetchall()
        com_pii = conn.execute(
            "SELECT COUNT(*) c FROM pedidos WHERE comprador_nome IS NOT NULL").fetchone()["c"]
    return {
        "registros_com_pii": com_pii,
        "retencao_dias": privacidade.RETENCAO_DIAS,
        "base_legal": "Execução de contrato — LGPD art. 7º, V",
        "solicitacoes_vencendo": [dict(s) for s in privacidade.solicitacoes_vencendo()],
        "acessos": [dict(a) for a in acessos],
    }


@app.post("/api/lgpd/expurgar")
def api_expurgar():
    return {"expurgados": privacidade.expurgar()}


@app.get("/api/eventos")
def api_eventos(n: int = 40):
    with conectar() as conn:
        linhas = conn.execute(
            "SELECT nivel, origem, mensagem, ocorrido_em FROM eventos"
            " ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return {"eventos": [dict(l) for l in linhas]}


@app.post("/api/ciclo")
async def api_ciclo():
    from worker import ciclo
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, ciclo)


# ------------------------------------------------------------ Configuração

def _redirect_uri(request: Request, provedor: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/oauth/{provedor}/retorno"


@app.get("/api/configuracao")
def api_configuracao(request: Request):
    from painel import configurar
    st = configurar.status()
    for prov, dados in st.items():
        uri = dados.get("redirect_uri")
        if prov == "shopee":
            uri = _redirect_uri(request, "shopee")
            dados["redirect_uri"] = uri
        if uri:
            dados["passos"] = [p.replace("{redirect}", uri) for p in dados["passos"]]
    return st


@app.get("/api/configuracao/url-autorizacao/{provedor}")
def api_url_autorizacao(provedor: str, request: Request):
    """Devolve o link de autorização em vez de redirecionar — assim o painel
    abre em aba nova e você não perde a tela de configuração."""
    from painel import configurar
    try:
        if provedor == "mercadolivre":
            return {"url": configurar.ml_url_autorizacao()}
        if provedor == "shopee":
            return {"url": configurar.shopee_url_autorizacao(_redirect_uri(request, "shopee"))}
        raise HTTPException(400, "Provedor desconhecido.")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/configuracao/concluir-ml")
async def api_concluir_ml(request: Request):
    """Recebe a URL de retorno colada e finaliza a conexão."""
    from painel import configurar
    corpo = await request.json()
    try:
        d = configurar.ml_trocar_code(corpo.get("retorno", ""))
        registrar_evento("info", "configuracao", f"Mercado Livre conectado (vendedor {d['seller_id']})")
        return {"ok": True, "detalhe": f"Conectado. Vendedor {d['seller_id']}."}
    except ValueError as e:
        return {"ok": False, "detalhe": str(e)}
    except Exception as e:
        return {"ok": False, "detalhe": str(e)[:300]}


@app.get("/oauth/ml/retorno", response_class=HTMLResponse)
def oauth_ml_retorno(request: Request, code: str = "", state: str = "", error: str = ""):
    """Só funciona se você conseguir servir HTTPS. Caso contrário, use o
    campo de colar a URL no painel — é o caminho normal."""
    from painel import configurar
    if error or not code:
        return _pagina_retorno(False, error or "O Mercado Livre não devolveu o código.")
    try:
        d = configurar.ml_trocar_code(str(request.url))
        return _pagina_retorno(True, f"Mercado Livre conectado. Vendedor {d['seller_id']}.")
    except Exception as e:
        return _pagina_retorno(False, str(e))


@app.post("/api/configuracao/salvar")
async def api_salvar_config(request: Request):
    """Grava as chaves no .env. Só strings de aplicação — nunca senha."""
    from painel import configurar
    corpo = await request.json()
    permitidas = {
        "ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_SELLER_ID",
        "SHOPEE_PARTNER_ID", "SHOPEE_PARTNER_KEY", "SHOPEE_SHOP_ID", "SHOPEE_SANDBOX",
        "AMZ_LWA_CLIENT_ID", "AMZ_LWA_CLIENT_SECRET", "AMZ_REFRESH_TOKEN",
        "ANTHROPIC_API_KEY", "MARGEM_MINIMA_PCT", "TETO_COMPRA_AUTOMATICA",
        "ALIQUOTA_IMPOSTO_PCT", "PRAZO_FORNECEDOR_DIAS",
    }
    chaves = {k: str(v) for k, v in corpo.items() if k in permitidas and str(v).strip()}
    if not chaves:
        raise HTTPException(400, "Nada para salvar.")
    configurar.gravar_env(chaves)
    registrar_evento("info", "configuracao", f"Chaves atualizadas: {', '.join(chaves)}")
    return {"ok": True, "salvas": sorted(chaves)}


@app.get("/oauth/ml/retorno", response_class=HTMLResponse)
def oauth_ml_retorno(request: Request, code: str = "", state: str = "", error: str = ""):
    from painel import configurar
    if error or not code:
        return _pagina_retorno(False, error or "O Mercado Livre não devolveu o código.")
    try:
        d = configurar.ml_trocar_code(code, state, _redirect_uri(request, "ml"))
        return _pagina_retorno(True, f"Mercado Livre conectado. Vendedor {d['seller_id']}.")
    except Exception as e:
        return _pagina_retorno(False, str(e))


@app.get("/oauth/shopee/iniciar")
def oauth_shopee_iniciar(request: Request):
    from fastapi.responses import RedirectResponse
    from painel import configurar
    try:
        return RedirectResponse(configurar.shopee_url_autorizacao(_redirect_uri(request, "shopee")))
    except ValueError as e:
        return HTMLResponse(_pagina_retorno(False, str(e)), status_code=400)


@app.get("/oauth/shopee/retorno", response_class=HTMLResponse)
def oauth_shopee_retorno(code: str = "", shop_id: str = ""):
    from painel import configurar
    if not code:
        return _pagina_retorno(False, "A Shopee não devolveu o código. "
                                      "O link de autorização expira em 5 minutos — tente de novo.")
    try:
        d = configurar.shopee_trocar_code(code, shop_id)
        return _pagina_retorno(True, f"Shopee conectada. Loja {d['shop_id']}.")
    except Exception as e:
        return _pagina_retorno(False, str(e))


@app.post("/api/configuracao/testar/{marketplace}")
def api_testar(marketplace: str):
    from painel import configurar
    return configurar.testar(marketplace)


def _pagina_retorno(ok: bool, mensagem: str) -> str:
    cor = "#14614A" if ok else "#8C2F1B"
    titulo = "Pronto" if ok else "Não deu certo"
    return f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<title>{titulo}</title></head>
<body style="font-family:system-ui,sans-serif;background:#E3E8E3;color:#16211D;
             display:grid;place-items:center;height:100vh;margin:0">
  <div style="background:#EDF0EC;padding:2rem 2.5rem;border-left:4px solid {cor};max-width:34rem">
    <h1 style="margin:0 0 .6rem;font-size:1.4rem;color:{cor}">{titulo}</h1>
    <p style="margin:0 0 1.4rem;line-height:1.5">{mensagem}</p>
    <a href="/" style="color:{cor};font-weight:600">Voltar ao painel</a>
  </div>
</body></html>"""


# ---------------------------------------------------------------- Sessão

@app.get("/login", response_class=HTMLResponse)
def pagina_login():
    return _pagina_login()


@app.post("/api/login")
async def api_login(request: Request):
    corpo = await request.json()
    try:
        sid, csrf = seguranca.autenticar(corpo.get("usuario", ""), corpo.get("senha", ""))
    except seguranca.FalhaLogin as e:
        return JSONResponse({"detail": str(e)}, status_code=401)
    resposta = JSONResponse({"ok": True, "csrf": csrf})
    resposta.set_cookie(
        seguranca.COOKIE_SESSAO, sid, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
        max_age=seguranca.DURACAO_MAX_H * 3600, path="/")
    return resposta


@app.post("/api/logout")
def api_logout(request: Request):
    seguranca.encerrar_sessao(request.cookies.get(seguranca.COOKIE_SESSAO))
    registrar_evento("info", "seguranca", f"Saída de {request.state.operador}")
    resposta = JSONResponse({"ok": True})
    resposta.delete_cookie(seguranca.COOKIE_SESSAO, path="/")
    return resposta


@app.get("/api/sessao")
def api_sessao(request: Request):
    """O painel busca aqui o token que assina as chamadas que mudam estado."""
    return {"usuario": request.state.operador, "csrf": request.state.csrf}


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
</style></head><body>
<form id="entrar">
  <h1>Agente Comercial</h1>
  <label>Operador
    <input id="usuario" autocomplete="username" autofocus required></label>
  <label>Senha
    <input id="senha" type="password" autocomplete="current-password" required></label>
  <button type="submit">Entrar</button>
  <p class="erro" id="erro" role="alert"></p>
</form>
<script>
document.getElementById('entrar').onsubmit = async ev => {
  ev.preventDefault();
  const r = await fetch('/api/login', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({usuario: document.getElementById('usuario').value,
                          senha: document.getElementById('senha').value})});
  if (r.ok) { window.location = '/'; return; }
  const d = await r.json().catch(() => ({detail:'Não deu para entrar.'}));
  document.getElementById('erro').textContent = d.detail || 'Não deu para entrar.';
};
</script></body></html>"""


# ------------------------------------------------------------------ Tela

@app.get("/", response_class=HTMLResponse)
def painel():
    return (BASE / "painel.html").read_text(encoding="utf-8")


def preparar():
    inicializar()
    privacidade.inicializar_lgpd()
    seguranca.inicializar_seguranca()
