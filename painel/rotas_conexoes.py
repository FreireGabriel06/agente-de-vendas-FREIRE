"""
Rotas de configuração e conexão com os marketplaces (OAuth do Mercado
Livre, da Amazon e da Shopee), gravação das chaves e teste de conexão.
"""
import html

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from db import registrar_evento
from core import cofre, seguranca
from fastapi import APIRouter

rotas = APIRouter()

# ------------------------------------------------------------ Configuração

def _redirect_uri(request: Request, provedor: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/oauth/{provedor}/retorno"


@rotas.get("/api/configuracao")
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


@rotas.get("/api/configuracao/url-autorizacao/{provedor}")
def api_url_autorizacao(provedor: str, request: Request):
    """Devolve o link de autorização em vez de redirecionar — assim o painel
    abre em aba nova e você não perde a tela de configuração."""
    from painel import configurar
    try:
        if provedor == "mercadolivre":
            return {"url": configurar.ml_url_autorizacao(request.state.sid)}
        if provedor == "shopee":
            return {"url": configurar.shopee_url_autorizacao(_redirect_uri(request, "shopee"),
                                                            request.state.sid)}
        if provedor == "amazon":
            return {"url": configurar.amazon_url_autorizacao(request.state.sid)}
        raise HTTPException(400, "Provedor desconhecido.")
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except cofre.ErroCofre as e:
        raise HTTPException(503, str(e)) from None


@rotas.post("/api/configuracao/concluir-ml")
async def api_concluir_ml(request: Request):
    """Recebe a URL de retorno colada e finaliza a conexão."""
    from painel import configurar
    corpo = await request.json()
    try:
        d = configurar.ml_trocar_code(corpo.get("retorno", ""), request.state.sid)
        registrar_evento("info", "configuracao", f"Mercado Livre conectado (vendedor {d['seller_id']})")
        return {"ok": True, "detalhe": f"Conectado. Vendedor {d['seller_id']}."}
    except (ValueError, cofre.ErroCofre) as e:
        return {"ok": False, "detalhe": str(e)}
    except Exception as e:
        return {"ok": False, "detalhe": _falha_generica("Mercado Livre", e)}


@rotas.get("/oauth/ml/retorno", response_class=HTMLResponse)
def oauth_ml_retorno(request: Request, code: str = "", error: str = ""):
    """Só funciona se você conseguir servir HTTPS. Caso contrário, use o
    campo de colar a URL no painel — é o caminho normal."""
    from painel import configurar
    if error or not code:
        return _pagina_retorno(False, "O Mercado Livre não concluiu a autorização. "
                                      "Volte ao painel e tente de novo.")
    try:
        d = configurar.ml_trocar_code(str(request.url), request.state.sid)
        return _pagina_retorno(True, f"Mercado Livre conectado. Vendedor {d['seller_id']}.")
    except (ValueError, cofre.ErroCofre) as e:
        return _pagina_retorno(False, str(e))
    except Exception as e:
        return _pagina_retorno(False, _falha_generica("Mercado Livre", e))


# Amazon: Website Authorization Workflow (ver painel/configurar.py). As três
# respostas levam Referrer-Policy: no-referrer, como a documentação pede: o
# state e o código não vazam no Referer para outro site.
_SEM_REFERER = {"Referrer-Policy": "no-referrer"}


@rotas.get("/oauth/amazon/login")
def oauth_amazon_login(request: Request, amazon_callback_uri: str = "", amazon_state: str = "",
                       selling_partner_id: str = ""):
    """URI de log-in do app: a Amazon traz o vendedor aqui depois do consentimento.
    O middleware já exigiu a sessão do painel; aqui o vendedor volta à Amazon
    com um state novo, preso a esta sessão."""
    from painel import configurar
    try:
        destino = configurar.amazon_callback(amazon_callback_uri, amazon_state, request.state.sid)
    except (ValueError, seguranca.StateInvalido) as e:
        return HTMLResponse(_pagina_retorno(False, str(e)), headers=_SEM_REFERER)
    return RedirectResponse(destino, status_code=303, headers=_SEM_REFERER)


@rotas.get("/oauth/amazon/retorno", response_class=HTMLResponse)
def oauth_amazon_retorno(request: Request):
    """Redirect URI do app. Só funciona servindo HTTPS no endereço cadastrado;
    sem isso, cole a URL de retorno no painel (concluir-amazon)."""
    from painel import configurar
    try:
        d = configurar.amazon_concluir(dict(request.query_params), request.state.sid)
    except (ValueError, seguranca.StateInvalido, cofre.ErroCofre) as e:
        return HTMLResponse(_pagina_retorno(False, str(e)), headers=_SEM_REFERER)
    except Exception as e:
        return HTMLResponse(_pagina_retorno(False, _falha_generica("Amazon", e)), headers=_SEM_REFERER)
    registrar_evento("info", "configuracao",
                     f"Amazon conectada (vendedor {d['selling_partner_id'] or 'sem id'}) "
                     f"por {request.state.operador}")
    return HTMLResponse(_pagina_retorno(True, "Amazon conectada."), headers=_SEM_REFERER)


@rotas.post("/api/configuracao/concluir-amazon")
async def api_concluir_amazon(request: Request):
    """Recebe a URL de retorno da Amazon colada e conclui a conexão."""
    from painel import configurar
    corpo = await request.json()
    try:
        d = configurar.amazon_concluir(configurar.parametros_da_url(corpo.get("retorno", "")),
                                       request.state.sid)
    except (ValueError, seguranca.StateInvalido, cofre.ErroCofre) as e:
        return {"ok": False, "detalhe": str(e)}
    except Exception as e:
        return {"ok": False, "detalhe": _falha_generica("Amazon", e)}
    registrar_evento("info", "configuracao",
                     f"Amazon conectada (vendedor {d['selling_partner_id'] or 'sem id'}) "
                     f"por {request.state.operador}")
    return {"ok": True, "detalhe": "Amazon conectada."}


@rotas.post("/api/configuracao/salvar")
async def api_salvar_config(request: Request):
    """Grava as chaves de aplicação — nunca senha. Segredo vai para o cofre
    cifrado, o resto para o .env. A resposta e o evento levam só os nomes."""
    from painel import configurar
    corpo = await request.json()
    permitidas = {
        "ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_SELLER_ID",
        "SHOPEE_PARTNER_ID", "SHOPEE_PARTNER_KEY", "SHOPEE_SHOP_ID", "SHOPEE_SANDBOX",
        "AMZ_LWA_CLIENT_ID", "AMZ_LWA_CLIENT_SECRET", "AMZ_REFRESH_TOKEN",
        "AMZ_APPLICATION_ID", "AMZ_REDIRECT_URI",
        "ANTHROPIC_API_KEY", "MARGEM_MINIMA_PCT", "TETO_COMPRA_AUTOMATICA",
        "ALIQUOTA_IMPOSTO_PCT", "PRAZO_FORNECEDOR_DIAS",
    }
    chaves = {k: str(v).strip() for k, v in corpo.items() if k in permitidas and str(v).strip()}
    if not chaves:
        raise HTTPException(400, "Nada para salvar.")
    for chave, valor in chaves.items():
        problema = configurar.problema_na_chave(chave, valor)
        if problema:
            raise HTTPException(400, problema)
    try:
        destino = configurar.gravar_env(chaves)
    except cofre.ErroCofre as e:
        raise HTTPException(503, str(e)) from None
    registrar_evento("info", "configuracao", f"Chaves atualizadas: {', '.join(sorted(chaves))}")
    avisos = []
    if destino["sobrepostos"]:
        avisos.append(f"{', '.join(destino['sobrepostos'])} também está no ambiente do "
                      "sistema, que vale antes do cofre.")
    if destino["em_texto_no_env"]:
        avisos.append(f"O .env ainda tem {', '.join(destino['em_texto_no_env'])} em texto puro. "
                      "O cofre já vale; apague essas linhas do .env à mão.")
    return {"ok": True, "salvas": sorted(chaves), "no_cofre": destino["cofre"],
            "aviso": " ".join(avisos)}


@rotas.get("/oauth/shopee/iniciar")
def oauth_shopee_iniciar(request: Request):
    from fastapi.responses import RedirectResponse
    from painel import configurar
    try:
        return RedirectResponse(configurar.shopee_url_autorizacao(_redirect_uri(request, "shopee"),
                                                                  request.state.sid))
    except (ValueError, cofre.ErroCofre) as e:
        return HTMLResponse(_pagina_retorno(False, str(e)), status_code=400)


@rotas.get("/oauth/shopee/retorno", response_class=HTMLResponse)
def oauth_shopee_retorno(request: Request, code: str = "", shop_id: str = ""):
    from painel import configurar
    if not code:
        return _pagina_retorno(False, "A Shopee não devolveu o código. "
                                      "O link de autorização expira em 5 minutos — tente de novo.")
    try:
        d = configurar.shopee_concluir(code, shop_id, request.state.sid)
        return _pagina_retorno(True, f"Shopee conectada. Loja {d['shop_id']}.")
    except (seguranca.StateInvalido, configurar.RetornoRecusado, cofre.ErroCofre) as e:
        return _pagina_retorno(False, str(e))
    except Exception as e:
        return _pagina_retorno(False, _falha_generica("Shopee", e))


@rotas.post("/api/configuracao/testar/{marketplace}")
def api_testar(marketplace: str):
    from painel import configurar
    return configurar.testar(marketplace)


def _falha_generica(provedor: str, erro: Exception) -> str:
    """Erro inesperado ao conectar: a tela recebe texto fixo e o registro guarda
    só o tipo do erro, nunca a resposta do marketplace, que pode trazer dado ou
    token. Relatório, achado A08; OWASP ASVS 5.0, 16.5.1 e 16.2.5."""
    registrar_evento("erro", "configuracao", f"Falha ao conectar {provedor}: {type(erro).__name__}")
    return f"Não foi possível concluir a conexão com {provedor}. Tente de novo."


def _pagina_retorno(ok: bool, mensagem: str) -> str:
    # Escape de saída: a mensagem pode trazer texto da URL ou do marketplace.
    # Relatório, achado A06; OWASP ASVS 5.0, 1.2.1.
    mensagem = html.escape(mensagem)
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
