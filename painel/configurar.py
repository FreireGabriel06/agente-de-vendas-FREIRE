"""
Assistente de configuração.

O que ele resolve: o vaivém de token do OAuth, que é onde a maioria das
integrações trava.

Você faz só duas coisas manuais, e nenhuma delas envolve me passar senha:

  1. Cria o app no portal de desenvolvedor do marketplace (login seu, no site
     deles) e copia duas strings públicas: client_id e client_secret.
  2. Cola essas duas aqui e clica em autorizar.

O resto — gerar a URL assinada, receber o code no retorno, trocar por
access/refresh token, gravar no .env com permissão restrita — é automático.
Sua senha é digitada no site do marketplace, nunca aqui.

Importante sobre o refresh token do ML: ele é de uso único. Este módulo grava
o novo a cada renovação. Se você editar o .env na mão no meio de uma sessão
ativa, corre o risco de sobrescrever o válido por um já gastado.
"""
import base64
import hashlib
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

from config import BASE_DIR, config
from core import seguranca

ARQ_ENV = BASE_DIR / ".env"

# Porta padrão do painel. O ML exige HTTPS na URI de redirect, e o painel roda
# em HTTP local — então registramos https://localhost:PORTA/... e o navegador
# vai falhar ao carregar a página de retorno. Isso é esperado: o código vem na
# própria URL, e o painel tem um campo pra você colar essa URL inteira.
def redirect_padrao() -> str:
    porta = os.getenv("PORTA_PAINEL", "8777")
    return f"https://localhost:{porta}/oauth/ml/retorno"


# Só o código de erro do OAuth (ex.: invalid_grant) chega à tela. Texto livre
# vindo do marketplace fica de fora. Relatório, achado A08; OWASP ASVS 5.0, 16.5.1.
_CODIGO_OAUTH = re.compile(r"[a-z0-9_.\-]{1,60}")


def _codigo_erro_oauth(valor) -> str:
    valor = str(valor or "").strip().lower()
    return valor if _CODIGO_OAUTH.fullmatch(valor) else "sem_codigo"


def _pkce() -> tuple[str, str]:
    """Gera o par verifier/challenge do PKCE (S256)."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def extrair_code(texto: str) -> tuple[str, str]:
    """
    Aceita a URL inteira colada da barra de endereço, ou só o código — que a
    troca recusa, porque vem sem state. Devolve (code, state).

    Existe porque o retorno do ML cai num endereço HTTPS que o painel local
    não serve. A página não carrega, mas o código está lá na URL.
    """
    texto = texto.strip()
    if not texto:
        raise ValueError("Cole a URL de retorno ou o código.")

    if "?" in texto or texto.startswith("http"):
        q = parse_qs(urlparse(texto).query)
        if "error" in q:
            raise ValueError("O Mercado Livre recusou a autorização "
                             f"({_codigo_erro_oauth(q['error'][0])}).")
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        if not code:
            raise ValueError("Não encontrei 'code=' nessa URL. Confira se copiou inteira.")
        return code, state

    # Colou só o código.
    return texto, ""


def gravar_env(chaves: dict[str, str]) -> None:
    """
    Atualiza o .env preservando comentários e ordem. Cria se não existir.
    Permissão 600: o arquivo passa a conter segredo de verdade.
    """
    if not ARQ_ENV.exists():
        modelo = BASE_DIR / ".env.example"
        ARQ_ENV.write_text(modelo.read_text(encoding="utf-8") if modelo.exists() else "",
                           encoding="utf-8")

    linhas = ARQ_ENV.read_text(encoding="utf-8").splitlines()
    restantes = dict(chaves)

    for i, linha in enumerate(linhas):
        m = re.match(r"^(\s*)([A-Z0-9_]+)\s*=", linha)
        if m and m.group(2) in restantes:
            chave = m.group(2)
            linhas[i] = f"{chave}={restantes.pop(chave)}"

    for chave, valor in restantes.items():
        linhas.append(f"{chave}={valor}")

    ARQ_ENV.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    ARQ_ENV.chmod(0o600)

    # Reflete na sessão atual sem precisar reiniciar.
    for chave, valor in chaves.items():
        os.environ[chave] = valor


def status() -> dict:
    """O que já está configurado e o que falta. Alimenta a tela."""
    def preenchido(*nomes):
        return all(os.getenv(n) for n in nomes)

    return {
        "mercadolivre": {
            "nome": "Mercado Livre",
            "app_criado": preenchido("ML_CLIENT_ID", "ML_CLIENT_SECRET"),
            "autorizado": bool(os.getenv("ML_REFRESH_TOKEN")),
            "portal": "https://developers.mercadolivre.com.br/devcenter",
            "redirect_uri": os.getenv("ML_REDIRECT_URI") or redirect_padrao(),
            "passos": [
                "No DevCenter, clique em Criar aplicação",
                "Nome e nome curto: qualquer coisa única (ex.: agente-vendas-teste)",
                "Em 'URI de redirect', cole exatamente: {redirect}",
                "Marque os escopos read, write e offline_access — sem o offline_access "
                "você não recebe refresh token e a conexão morre em 6 horas",
                "Em Tópicos, marque orders_v2 e questions",
                "Salve, depois abra os três pontinhos → Editar pra ver o App ID e a Secret Key",
            ],
            "aviso": "O ML só aceita HTTPS na URI de redirect, e o painel roda em HTTP local. "
                     "Depois de autorizar, o navegador vai dar erro de conexão — é esperado. "
                     "Copie a URL inteira da barra de endereço e cole no campo abaixo.",
        },
        "shopee": {
            "nome": "Shopee",
            "app_criado": preenchido("SHOPEE_PARTNER_ID", "SHOPEE_PARTNER_KEY"),
            "autorizado": bool(os.getenv("SHOPEE_REFRESH_TOKEN")),
            "portal": "https://open.shopee.com",
            "passos": [
                "Registre-se no Open Platform (comece pelo ambiente de teste)",
                "App Management → App List → crie um app",
                "Copie o Partner ID e o Partner Key",
                "Em 'Redirect URL', cole: {redirect}",
            ],
        },
        "amazon": {
            "nome": "Amazon",
            "app_criado": preenchido("AMZ_LWA_CLIENT_ID", "AMZ_LWA_CLIENT_SECRET"),
            "autorizado": bool(os.getenv("AMZ_REFRESH_TOKEN")),
            "portal": "https://sellercentral.amazon.com.br",
            "passos": [
                "Exige conta Professional Seller aprovada — pode levar dias",
                "Seller Central → Apps e Serviços → Developer Central",
                "Registre um app e peça as permissões de Orders e Listings",
                "O fluxo de autorização da Amazon é mais longo; siga o guia deles",
            ],
        },
        "claude": {
            "nome": "Redação das respostas",
            "app_criado": bool(os.getenv("ANTHROPIC_API_KEY")),
            "autorizado": bool(os.getenv("ANTHROPIC_API_KEY")),
            "portal": "https://console.anthropic.com",
            "passos": [
                "Crie uma chave de API no console",
                "Sem ela, o bot usa só as respostas de FAQ fixo",
            ],
        },
    }


# --------------------------------------------------------- Mercado Livre

def ml_url_autorizacao(sid: str | None, redirect_uri: str | None = None) -> str:
    if not os.getenv("ML_CLIENT_ID"):
        raise ValueError("Salve o App ID e a Secret Key do Mercado Livre primeiro.")
    redirect_uri = redirect_uri or os.getenv("ML_REDIRECT_URI") or redirect_padrao()
    verifier, challenge = _pkce()
    # O state prende o retorno a esta sessão; o verifier do PKCE viaja com ele.
    estado = seguranca.criar_state_oauth("mercadolivre", sid, {"verifier": verifier})
    return "https://auth.mercadolivre.com.br/authorization?" + urlencode({
        "response_type": "code",
        "client_id": os.getenv("ML_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "state": estado,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def ml_trocar_code(code_ou_url: str, sid: str | None, redirect_uri: str | None = None) -> dict:
    code, estado = extrair_code(code_ou_url)
    if not estado:
        raise ValueError("Cole a URL inteira da barra de endereço, não só o código: "
                         "é ela que prova que a autorização saiu deste painel.")
    # Antes de qualquer chamada ao marketplace: recusa o state que esta sessão
    # não criou, que venceu ou que já foi usado.
    verifier = seguranca.consumir_state_oauth(estado, "mercadolivre", sid)["verifier"]
    redirect_uri = redirect_uri or os.getenv("ML_REDIRECT_URI") or redirect_padrao()

    dados = {
        "grant_type": "authorization_code",
        "client_id": os.getenv("ML_CLIENT_ID"),
        "client_secret": os.getenv("ML_CLIENT_SECRET"),
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }

    r = requests.post("https://api.mercadolibre.com/oauth/token", data=dados, timeout=25)

    if r.status_code != 200:
        try:
            corpo = r.json()
        except ValueError:
            corpo = {}
        codigo = _codigo_erro_oauth(corpo.get("error") if isinstance(corpo, dict) else "")
        dica = ""
        if "redirect_uri" in r.text:
            dica += ("  →  A URI aqui e a cadastrada no DevCenter precisam ser "
                     "idênticas, caractere por caractere.")
        if codigo == "invalid_grant":
            dica += ("  →  O código só vale uma vez e expira em minutos. "
                     "Clique em autorizar de novo e cole a URL nova.")
        raise ValueError(f"O Mercado Livre recusou a troca ({r.status_code}, {codigo}).{dica}")

    d = r.json()
    gravar_env({
        "ML_REFRESH_TOKEN": d["refresh_token"],
        "ML_SELLER_ID": str(d.get("user_id", "")),
        "ML_REDIRECT_URI": redirect_uri,
    })
    config.ml.refresh_token = d["refresh_token"]
    config.ml.seller_id = str(d.get("user_id", ""))
    return {"seller_id": d.get("user_id"), "expira_em_seg": d.get("expires_in")}


# ---------------------------------------------------------------- Shopee

def shopee_url_autorizacao(redirect_uri: str) -> str:
    from conectores.shopee import Shopee
    s = Shopee()
    if not s.configurado:
        raise ValueError("Salve o Partner ID, o Partner Key e o Shop ID da Shopee primeiro.")
    # O link da Shopee expira em 5 minutos — por isso ele é gerado no clique,
    # e não guardado na página.
    return s.url_autorizacao(redirect_uri)


def shopee_trocar_code(code: str, shop_id: str) -> dict:
    from conectores.shopee import Shopee
    if shop_id:
        os.environ["SHOPEE_SHOP_ID"] = str(shop_id)
    s = Shopee()
    d = s.trocar_code(code)
    gravar_env({
        "SHOPEE_REFRESH_TOKEN": d["refresh_token"],
        "SHOPEE_SHOP_ID": str(shop_id or os.getenv("SHOPEE_SHOP_ID", "")),
    })
    return {"shop_id": shop_id, "expira_em_seg": d.get("expire_in")}


# ------------------------------------------------------------------ Teste

def testar(marketplace: str) -> dict:
    """Chama a API de verdade e confirma que a credencial funciona."""
    try:
        if marketplace == "mercadolivre":
            from conectores.mercadolivre import MercadoLivre
            ml = MercadoLivre()
            pedidos = ml.pedidos_recentes(limit=1)
            return {"ok": True,
                    "detalhe": f"Conectado. Vendedor {ml.cfg.seller_id}. "
                               f"{len(pedidos)} pedido(s) pago(s) na primeira página."}

        if marketplace == "shopee":
            from conectores.shopee import Shopee
            s = Shopee()
            pedidos = s.pedidos_recentes(janela_horas=24)
            return {"ok": True, "detalhe": f"Conectado. {len(pedidos)} pedido(s) nas últimas 24h."}

        if marketplace == "amazon":
            from conectores.amazon import Amazon
            a = Amazon()
            a.token
            return {"ok": True, "detalhe": "Token LWA obtido com sucesso."}

        if marketplace == "claude":
            chave = os.getenv("ANTHROPIC_API_KEY", "")
            if not chave:
                return {"ok": False, "detalhe": "Chave não preenchida."}
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": chave, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 10,
                      "messages": [{"role": "user", "content": "ok"}]},
                timeout=20)
            return {"ok": r.status_code == 200,
                    "detalhe": "Chave válida." if r.status_code == 200
                               else f"Recusada ({r.status_code})."}

        return {"ok": False, "detalhe": "Marketplace desconhecido."}
    except Exception as e:
        return {"ok": False, "detalhe": str(e)[:300]}
