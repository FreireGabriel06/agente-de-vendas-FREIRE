"""
Assistente de configuração.

O que ele resolve: o vaivém de token do OAuth, que é onde a maioria das
integrações trava.

Você faz só duas coisas manuais, e nenhuma delas envolve me passar senha:

  1. Cria o app no portal de desenvolvedor do marketplace (login seu, no site
     deles) e copia duas strings públicas: client_id e client_secret.
  2. Cola essas duas aqui e clica em autorizar.

O resto — gerar a URL assinada, receber o code no retorno, trocar por
access/refresh token, guardar — é automático. Sua senha é digitada no site do
marketplace, nunca aqui.

Onde cada coisa fica: segredo (client secret, partner key, refresh token,
secret do LWA, chave da API) vai para o cofre cifrado, core/cofre.py; o resto
(IDs, URI de redirect, regras de negócio) continua no .env. Nenhum valor de
segredo volta para a tela nem para o registro de eventos.

Importante sobre o refresh token do ML: ele é de uso único. O conector grava
o novo no cofre a cada renovação, e o do cofre vence o do .env.
"""
import base64
import hashlib
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

from config import BASE_DIR, DATA_DIR, config, ler_arquivo_env
from core import cofre, seguranca

ARQ_ENV = DATA_DIR / ".env"

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


def gravar_env(chaves: dict[str, str]) -> dict:
    """
    Guarda as configurações. Segredo vai para o cofre cifrado e nunca é
    escrito no .env nem no ambiente do processo; o resto vai para o .env,
    preservando comentários e ordem (cria o arquivo se não existir).

    Uma linha antiga de segredo no .env não é apagada nem alterada: o valor do
    cofre passa a valer, e o retorno diz quais linhas podem sair à mão.

    Devolve só nomes, nunca valores: {"env", "cofre", "sobrepostos",
    "em_texto_no_env"}.
    """
    segredos = {k: v for k, v in chaves.items() if k in cofre.SEGREDOS}
    comuns = {k: v for k, v in chaves.items() if k not in cofre.SEGREDOS}

    for chave, valor in segredos.items():
        cofre.guardar_segredo(chave, valor)

    if comuns:
        if not ARQ_ENV.exists():
            modelo = BASE_DIR / ".env.example"
            ARQ_ENV.write_text(modelo.read_text(encoding="utf-8") if modelo.exists() else "",
                               encoding="utf-8")

        linhas = ARQ_ENV.read_text(encoding="utf-8").splitlines()
        restantes = dict(comuns)

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
        for chave, valor in comuns.items():
            os.environ[chave] = valor

    no_arquivo = ler_arquivo_env(ARQ_ENV) if segredos and ARQ_ENV.exists() else {}
    return {
        "env": sorted(comuns),
        "cofre": sorted(segredos),
        # Segredo fixo definido no ambiente do sistema vence o cofre.
        "sobrepostos": sorted(k for k in segredos
                              if k not in cofre.ROTATIVOS and cofre.definido_no_sistema(k)),
        "em_texto_no_env": sorted(k for k in segredos if no_arquivo.get(k, "").strip()),
    }


def _valor(nome: str) -> str:
    """Segredo pela regra do cofre; o resto, do ambiente."""
    if nome in cofre.SEGREDOS:
        return cofre.segredo(nome)
    return os.getenv(nome, "")


def status() -> dict:
    """O que já está configurado e o que falta. Alimenta a tela. Só diz se
    existe; nunca devolve o valor."""
    erro_cofre = []

    def preenchido(*nomes):
        try:
            return all(_valor(n) for n in nomes)
        except cofre.ErroCofre as e:
            erro_cofre.append(str(e))
            return False

    estado = {
        "mercadolivre": {
            "nome": "Mercado Livre",
            "app_criado": preenchido("ML_CLIENT_ID", "ML_CLIENT_SECRET"),
            "autorizado": preenchido("ML_REFRESH_TOKEN"),
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
            "autorizado": preenchido("SHOPEE_REFRESH_TOKEN"),
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
            "autorizado": preenchido("AMZ_REFRESH_TOKEN"),
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
            "app_criado": preenchido("ANTHROPIC_API_KEY"),
            "autorizado": preenchido("ANTHROPIC_API_KEY"),
            "portal": "https://console.anthropic.com",
            "passos": [
                "Crie uma chave de API no console",
                "Sem ela, o bot usa só as respostas de FAQ fixo",
            ],
        },
    }
    if erro_cofre:
        # Sem valor nenhum: a mensagem do cofre não traz segredo nem chave.
        for dados in estado.values():
            dados["aviso"] = " ".join(filter(None, [erro_cofre[0], dados.get("aviso")]))
    return estado


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
    # O código vale uma vez só: se o cofre não aceitaria o token, para aqui,
    # antes de gastá-lo.
    cofre.conferir()

    dados = {
        "grant_type": "authorization_code",
        "client_id": os.getenv("ML_CLIENT_ID"),
        "client_secret": cofre.segredo("ML_CLIENT_SECRET"),
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
    from conectores.mercadolivre import guardar_tokens
    guardar_tokens(d)  # access e refresh token no cofre, nunca no .env
    gravar_env({
        "ML_SELLER_ID": str(d.get("user_id", "")),
        "ML_REDIRECT_URI": redirect_uri,
    })
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
    cofre.conferir()  # antes de gastar o código de uso único
    s = Shopee()
    d = s.trocar_code(code)  # access e refresh token vão para o cofre
    gravar_env({"SHOPEE_SHOP_ID": str(shop_id or os.getenv("SHOPEE_SHOP_ID", ""))})
    return {"shop_id": shop_id, "expira_em_seg": d.get("expire_in")}


# ------------------------------------------------------------------ Teste

def testar(marketplace: str) -> dict:
    """Chama a API de verdade e confirma que a credencial funciona.

    Erro dos conectores e do cofre chega à tela com a mensagem deles. Qualquer
    outro chega só com o tipo: a mensagem de uma exceção inesperada pode
    repetir um valor decifrado (ex.: o int() de uma validade adulterada)."""
    from conectores.amazon import ErroAmazon
    from conectores.mercadolivre import ErroMercadoLivre
    from conectores.shopee import ErroShopee

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
            chave = cofre.segredo("ANTHROPIC_API_KEY")
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
    except (ErroMercadoLivre, ErroShopee, ErroAmazon, cofre.ErroCofre) as e:
        return {"ok": False, "detalhe": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "detalhe": f"Não foi possível testar a conexão ({type(e).__name__})."}
