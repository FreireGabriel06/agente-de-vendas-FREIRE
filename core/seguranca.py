"""
Autenticação do painel.

O painel aprova compra, revela dado de comprador e grava credencial de
marketplace. Sem sessão, quem alcança a porta faz tudo isso. Este módulo
resolve o mínimo necessário: um operador local, senha com hash forte, sessão
no servidor e token contra CSRF.

Decisões e fontes (consultadas em 16/09/2026):

  - Senha com scrypt da biblioteca padrão, N=2^17, r=8, p=1. É o parâmetro que
    o OWASP aceita para scrypt. Argon2id seria a primeira escolha, mas exige
    dependência nova; o hash guarda o nome do algoritmo para permitir a troca
    sem invalidar o que já está gravado. OWASP Password Storage Cheat Sheet.
  - Sessão com 256 bits de aleatoriedade — o mínimo recomendado é 64 —, cookie
    HttpOnly, identificador novo a cada login, 30 min de ociosidade e 8 h de
    duração máxima. OWASP Session Management Cheat Sheet.
  - SameSite=Lax, não Strict: o retorno do OAuth do marketplace é navegação
    vinda de outro site, e com Strict o cookie não seria enviado.
  - CSRF por token próprio em cabeçalho, adequado para API JSON de mesma
    origem. OWASP CSRF Prevention Cheat Sheet.
  - Retorno de OAuth só com state criado pela mesma sessão: aleatório, válido
    por 10 min, apagado no primeiro uso e preso ao hash da sessão. RFC 9700,
    seção 2.1; OWASP ASVS 5.0, requisitos 10.1.2 e 10.2.1.

Cada verificação de senha custa cerca de 134 MB de memória, por desenho do
scrypt: no máximo VERIFICACOES_SIMULTANEAS rodam ao mesmo tempo. Erros de
login contam por endereço (FALHAS_POR_IP em JANELA_IP_MIN minutos) e por
conta (TENTATIVAS_ATE_BLOQUEIO seguidos): um atacante sozinho não tranca o
operador legítimo. Lote S5; achados S-07, S-08 e S-79 da auditoria.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from db import agora, conectar, registrar_evento

SCHEMA_SEGURANCA = """
CREATE TABLE IF NOT EXISTS operadores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario       TEXT UNIQUE NOT NULL,
    senha_hash    TEXT NOT NULL,
    criado_em     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL,
    tentativas    INTEGER DEFAULT 0,
    bloqueado_ate TEXT
);

-- Senhas anteriores de cada operador, só o hash: nenhuma das últimas
-- SENHA_HISTORICO volta (DPP da Amazon, 1.4.1).
CREATE TABLE IF NOT EXISTS senhas_anteriores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    operador_id INTEGER NOT NULL REFERENCES operadores(id),
    senha_hash  TEXT NOT NULL,
    trocada_em  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessoes (
    id          TEXT PRIMARY KEY,
    operador_id INTEGER NOT NULL REFERENCES operadores(id),
    csrf        TEXT NOT NULL,
    criada_em   TEXT NOT NULL,
    ultimo_uso  TEXT NOT NULL,
    expira_em   TEXT NOT NULL
);
"""

SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 300 * 1024 * 1024   # 128 * N * r cabe aqui com folga

SENHA_MINIMA = 12
OCIOSIDADE_MIN = int(os.getenv("SESSAO_OCIOSA_MIN", "30"))
DURACAO_MAX_H = int(os.getenv("SESSAO_MAX_HORAS", "8"))
TENTATIVAS_ATE_BLOQUEIO = 10   # erros seguidos na conta, de qualquer endereço (DPP: até 10)
BLOQUEIO_MIN = 15
FALHAS_POR_IP = 5              # erros de um endereço dentro da janela abaixo
JANELA_IP_MIN = 15
IPS_VIGIADOS_MAX = 10_000      # teto da memória do limite por endereço
VERIFICACOES_SIMULTANEAS = 2   # scrypt ao mesmo tempo (~134 MB cada)

# Regra de senha do Data Protection Policy da Amazon (1.4.1), exigida no
# cadastro de desenvolvedor da SP-API: 12 caracteres, maiúscula, minúscula,
# número e símbolo, nada do nome do operador, nenhuma das últimas 10, troca
# no máximo uma vez por dia e validade de 365 dias. Lote S8.
SENHA_HISTORICO = 10
SENHA_IDADE_MINIMA_H = 24
SENHA_VALIDADE_DIAS = 365
VALORES_SIM = ("1", "true", "sim", "yes")
HOSTS_LOCAIS = ("127.0.0.1", "localhost", "::1")

COOKIE_SESSAO = "agente_sessao"


class FalhaLogin(Exception):
    """Credencial inválida, operador bloqueado ou senha fraca."""


class PrecisaCodigo(FalhaLogin):
    """Senha certa, falta o código do autenticador (MFA ligado)."""


class MuitasTentativas(FalhaLogin):
    """Erros demais a partir do mesmo endereço; espera_s diz quanto falta."""

    def __init__(self, espera_s: int):
        minutos = max(1, -(-espera_s // 60))
        super().__init__(f"Muitas tentativas a partir deste endereço. "
                         f"Tente de novo em {minutos} min.")
        self.espera_s = espera_s


# Colunas que entraram depois (lote S4). Banco antigo recebe cada uma, uma vez.
COLUNAS_OPERADORES = {
    "ativo": "INTEGER NOT NULL DEFAULT 1",
    "totp_ativo": "INTEGER NOT NULL DEFAULT 0",
    "totp_ultimo_passo": "INTEGER",
    "senha_trocada_em": "TEXT",
    "ultimo_login_em": "TEXT",
}


def inicializar_seguranca():
    with conectar() as conn:
        conn.executescript(SCHEMA_SEGURANCA)
        existentes = {l["name"] for l in conn.execute("PRAGMA table_info(operadores)")}
        for nome, tipo in COLUNAS_OPERADORES.items():
            if nome not in existentes:
                conn.execute(f"ALTER TABLE operadores ADD COLUMN {nome} {tipo}")
        # Sessões de antes do lote S5 guardavam o id em claro; nenhuma delas
        # confere com o hash, então só ocupariam espaço até vencer.
        conn.execute("DELETE FROM sessoes WHERE length(id) != 64")
        # Operador de antes do lote S8: a validade da senha conta da última
        # alteração conhecida.
        conn.execute("UPDATE operadores SET senha_trocada_em = COALESCE(atualizado_em, criado_em)"
                     " WHERE senha_trocada_em IS NULL")


class SenhaVencida(FalhaLogin):
    """Senha certa, mas com mais de SENHA_VALIDADE_DIAS dias."""


def painel_exposto() -> bool:
    """O painel escuta fora do endereço local (HOST_PAINEL)."""
    return os.getenv("HOST_PAINEL", "127.0.0.1").strip().lower() not in HOSTS_LOCAIS


def mfa_obrigatorio() -> bool:
    """MFA para todo operador (DPP 1.4.1): sempre que o painel está exposto,
    ou quando MFA_OBRIGATORIO=true também no endereço local."""
    return painel_exposto() or os.getenv("MFA_OBRIGATORIO", "").strip().lower() in VALORES_SIM


def operadores_sem_mfa() -> list[str]:
    with conectar() as conn:
        return [l["usuario"] for l in conn.execute(
            "SELECT usuario FROM operadores WHERE ativo = 1 AND totp_ativo = 0 ORDER BY usuario")]


def listar_operadores() -> list[dict]:
    """Para a revisão de acessos (DPP 1.2.2: trimestral). Sem hash nem segredo."""
    with conectar() as conn:
        return [dict(l) for l in conn.execute(
            "SELECT usuario, ativo, totp_ativo, ultimo_login_em, senha_trocada_em,"
            " bloqueado_ate, criado_em FROM operadores ORDER BY usuario")]


def problema_na_senha(usuario: str, senha: str) -> str | None:
    """O motivo de recusar a senha nova, ou None."""
    senha = senha or ""
    if len(senha) < SENHA_MINIMA:
        return f"A senha precisa de pelo menos {SENHA_MINIMA} caracteres."
    faltam = [nome for nome, tem in (
        ("letra maiúscula", any(c.isupper() for c in senha)),
        ("letra minúscula", any(c.islower() for c in senha)),
        ("número", any(c.isdigit() for c in senha)),
        ("símbolo", any(not c.isalnum() and not c.isspace() for c in senha)),
    ) if not tem]
    if faltam:
        return "A senha precisa de pelo menos: " + ", ".join(faltam) + "."
    partes = [p for p in re.split(r"[^0-9a-z]+", (usuario or "").lower()) if len(p) >= 3]
    if any(p in senha.lower() for p in partes):
        return "A senha não pode conter parte do nome do operador."
    return None


# --------------------------------------------------------------------- TOTP
#
# Segundo fator por app autenticador (TOTP, RFC 6238: HMAC-SHA1, 6 dígitos,
# passo de 30 s), com a biblioteca padrão. Aceita o passo atual e um vizinho
# de cada lado (relógio do celular um pouco fora), e nunca o mesmo passo duas
# vezes (o código não serve de novo). O segredo fica no cofre de credenciais,
# cifrado. Lote S4; achado S-17 da auditoria; OWASP ASVS 5.0, capítulo 6;
# NIST SP 800-63B-4 (autenticador OTP).

TOTP_PASSO_S = 30
TOTP_DIGITOS = 6
TOTP_JANELA = 1


def novo_segredo_totp() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _chave_totp(segredo: str) -> bytes:
    texto = segredo.strip().replace(" ", "").upper()
    return base64.b32decode(texto + "=" * (-len(texto) % 8))


def codigo_totp(segredo: str, passo: int) -> str:
    digest = hmac.new(_chave_totp(segredo), struct.pack(">Q", passo), hashlib.sha1).digest()
    deslocamento = digest[-1] & 0x0F
    numero = struct.unpack(">I", digest[deslocamento:deslocamento + 4])[0] & 0x7FFFFFFF
    return str(numero % (10 ** TOTP_DIGITOS)).zfill(TOTP_DIGITOS)


def passo_totp(instante: float | None = None) -> int:
    return int((time.time() if instante is None else instante) // TOTP_PASSO_S)


def conferir_totp(segredo: str, codigo: str, ultimo_passo: int | None,
                  instante: float | None = None) -> int | None:
    """O passo que o código prova, ou None. Recusa passo já usado."""
    codigo = (codigo or "").strip().replace(" ", "")
    if len(codigo) != TOTP_DIGITOS or not codigo.isdigit():
        return None
    atual = passo_totp(instante)
    for passo in range(atual - TOTP_JANELA, atual + TOTP_JANELA + 1):
        if ultimo_passo is not None and passo <= ultimo_passo:
            continue
        if hmac.compare_digest(codigo_totp(segredo, passo), codigo):
            return passo
    return None


def uri_totp(usuario: str, segredo: str, emissor: str = "Agente Comercial") -> str:
    """otpauth:// para o app autenticador (QR ou digitado)."""
    return (f"otpauth://totp/{quote(emissor)}:{quote(usuario)}?secret={segredo}"
            f"&issuer={quote(emissor)}&digits={TOTP_DIGITOS}&period={TOTP_PASSO_S}")


def _cofre():
    from core import cofre
    return cofre


def ativar_mfa(usuario: str, segredo: str, codigo: str) -> None:
    """Liga o MFA depois de conferir um código do app (prova que o operador
    cadastrou o segredo certo). Derruba as sessões abertas."""
    usuario = (usuario or "").strip().lower()
    with conectar() as conn:
        op = conn.execute("SELECT id FROM operadores WHERE usuario = ?", (usuario,)).fetchone()
    if op is None:
        raise FalhaLogin("Operador não encontrado.")
    passo = conferir_totp(segredo, codigo, None)
    if passo is None:
        raise FalhaLogin("Código do autenticador não confere. Confira a hora do celular e tente de novo.")
    _cofre().guardar("operador", f"totp:{usuario}", segredo)
    with conectar() as conn:
        conn.execute("UPDATE operadores SET totp_ativo = 1, totp_ultimo_passo = ?, atualizado_em = ?"
                     " WHERE id = ?", (passo, agora(), op["id"]))
        conn.execute("DELETE FROM sessoes WHERE operador_id = ?", (op["id"],))
    registrar_evento("info", "seguranca", f"MFA ligado: {usuario}")


def desativar_mfa(usuario: str) -> None:
    usuario = (usuario or "").strip().lower()
    with conectar() as conn:
        op = conn.execute("SELECT id FROM operadores WHERE usuario = ?", (usuario,)).fetchone()
        if op is None:
            raise FalhaLogin("Operador não encontrado.")
        conn.execute("UPDATE operadores SET totp_ativo = 0, totp_ultimo_passo = NULL,"
                     " atualizado_em = ? WHERE id = ?", (agora(), op["id"]))
        conn.execute("DELETE FROM sessoes WHERE operador_id = ?", (op["id"],))
    try:
        _cofre().apagar("operador", f"totp:{usuario}")
    except Exception:
        pass
    registrar_evento("atencao", "seguranca", f"MFA desligado: {usuario}")


def definir_ativo(usuario: str, ativo: bool) -> None:
    """Desativa (ou reativa) o operador. Desativar derruba as sessões na hora:
    validar_sessao também recusa sessão de operador inativo. Achado S-19."""
    usuario = (usuario or "").strip().lower()
    with conectar() as conn:
        op = conn.execute("SELECT id FROM operadores WHERE usuario = ?", (usuario,)).fetchone()
        if op is None:
            raise FalhaLogin("Operador não encontrado.")
        conn.execute("UPDATE operadores SET ativo = ?, atualizado_em = ? WHERE id = ?",
                     (1 if ativo else 0, agora(), op["id"]))
        if not ativo:
            conn.execute("DELETE FROM sessoes WHERE operador_id = ?", (op["id"],))
    registrar_evento("atencao" if not ativo else "info", "seguranca",
                     f"Operador {'desativado' if not ativo else 'reativado'}: {usuario}")


# -------------------------------------------------------------------- Senha

def gerar_hash(senha: str) -> str:
    if len(senha or "") < SENHA_MINIMA:
        raise FalhaLogin(f"A senha precisa de pelo menos {SENHA_MINIMA} caracteres.")
    sal = secrets.token_bytes(16)
    bruto = hashlib.scrypt(senha.encode(), salt=sal, n=SCRYPT_N, r=SCRYPT_R,
                           p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(sal).decode(), base64.b64encode(bruto).decode())


def conferir_hash(senha: str, guardado: str) -> bool:
    try:
        algoritmo, n, r, p, sal, esperado = guardado.split("$")
        if algoritmo != "scrypt":
            return False
        alvo = base64.b64decode(esperado)
        bruto = hashlib.scrypt(senha.encode(), salt=base64.b64decode(sal), n=int(n),
                               r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM, dklen=len(alvo))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(bruto, alvo)


# --------------------------------------------------------------- Operadores

def definir_operador(usuario: str, senha: str, troca_imediata: bool = False) -> str:
    """Cria o operador ou troca a senha dele. Chamado pela CLI, nunca pela web.
    troca_imediata dispensa a idade mínima de 24 h: é para senha que vazou."""
    usuario = (usuario or "").strip().lower()
    if not usuario:
        raise FalhaLogin("Informe o nome do operador.")
    problema = problema_na_senha(usuario, senha)
    if problema:
        raise FalhaLogin(problema)
    with conectar() as conn:
        atual = conn.execute("SELECT id, senha_hash, senha_trocada_em FROM operadores"
                             " WHERE usuario = ?", (usuario,)).fetchone()
        anteriores = [] if atual is None else [atual["senha_hash"]] + [
            l["senha_hash"] for l in conn.execute(
                "SELECT senha_hash FROM senhas_anteriores WHERE operador_id = ?"
                " ORDER BY id DESC LIMIT ?", (atual["id"], SENHA_HISTORICO - 1))]
    if atual is not None and not troca_imediata and atual["senha_trocada_em"]:
        if _quando(atual["senha_trocada_em"]) + timedelta(hours=SENHA_IDADE_MINIMA_H) \
                > datetime.now(timezone.utc):
            raise FalhaLogin(f"A senha foi trocada há menos de {SENHA_IDADE_MINIMA_H} horas. Se ela "
                             "vazou, troque com --suspeita-de-vazamento.")
    if any(conferir_hash(senha, h) for h in anteriores):
        raise FalhaLogin(f"Esta senha já foi usada. Escolha uma diferente das últimas "
                         f"{SENHA_HISTORICO}.")
    senha_hash = gerar_hash(senha)
    with conectar() as conn:
        if atual:
            conn.execute("INSERT INTO senhas_anteriores (operador_id, senha_hash, trocada_em)"
                         " VALUES (?,?,?)", (atual["id"], atual["senha_hash"], agora()))
            conn.execute("DELETE FROM senhas_anteriores WHERE operador_id = ? AND id NOT IN"
                         " (SELECT id FROM senhas_anteriores WHERE operador_id = ?"
                         " ORDER BY id DESC LIMIT ?)",
                         (atual["id"], atual["id"], SENHA_HISTORICO - 1))
            conn.execute(
                "UPDATE operadores SET senha_hash = ?, atualizado_em = ?, senha_trocada_em = ?,"
                " tentativas = 0, bloqueado_ate = NULL WHERE id = ?",
                (senha_hash, agora(), agora(), atual["id"]))
            conn.execute("DELETE FROM sessoes WHERE operador_id = ?", (atual["id"],))
            acao = "senha trocada" + (" (suspeita de vazamento)" if troca_imediata else "")
        else:
            conn.execute(
                "INSERT INTO operadores (usuario, senha_hash, criado_em, atualizado_em,"
                " senha_trocada_em) VALUES (?,?,?,?,?)",
                (usuario, senha_hash, agora(), agora(), agora()))
            acao = "operador criado"
    registrar_evento("atencao" if troca_imediata else "info", "seguranca", f"{acao}: {usuario}")
    return acao


def existe_operador() -> bool:
    with conectar() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM operadores").fetchone()["c"] > 0


# ------------------------------------------------------- Limite por endereço
#
# FALHAS_POR_IP erros de login a partir do mesmo endereço em JANELA_IP_MIN
# minutos fazem esse endereço esperar, qualquer que seja o operador tentado.
# Assim um atacante sozinho não tranca a conta do operador legítimo (achado
# S-08): o bloqueio da conta só vem com TENTATIVAS_ATE_BLOQUEIO erros
# seguidos, que pedem vários endereços. Em memória, como o state do OAuth: o
# painel é um processo só, e um reinício só zera a contagem. Atrás de proxy,
# o endereço vem de X-Forwarded-For, aceito só de PROXY_IPS (executar.py).

_falhas_por_ip: dict[str, list[float]] = {}
_trava_ips = threading.Lock()


def _falhas_recentes(ip: str, momento: float) -> list[float]:
    """As falhas do endereço dentro da janela. Chamar com _trava_ips."""
    limite = momento - JANELA_IP_MIN * 60
    falhas = [t for t in _falhas_por_ip.get(ip, ()) if t > limite]
    if falhas:
        _falhas_por_ip[ip] = falhas
    else:
        _falhas_por_ip.pop(ip, None)
    return falhas


def espera_do_ip(ip: str | None) -> int:
    """Segundos que o endereço ainda espera para tentar de novo; 0 se pode."""
    if not ip:
        return 0
    momento = time.monotonic()
    with _trava_ips:
        falhas = _falhas_recentes(ip, momento)
    if len(falhas) < FALHAS_POR_IP:
        return 0
    return max(1, int(falhas[-FALHAS_POR_IP] + JANELA_IP_MIN * 60 - momento) + 1)


def _anotar_falha_do_ip(ip: str | None) -> None:
    if not ip:
        return
    momento = time.monotonic()
    with _trava_ips:
        if ip not in _falhas_por_ip and len(_falhas_por_ip) >= IPS_VIGIADOS_MAX:
            for outro in list(_falhas_por_ip):
                _falhas_recentes(outro, momento)
            if len(_falhas_por_ip) >= IPS_VIGIADOS_MAX:
                _falhas_por_ip.pop(next(iter(_falhas_por_ip)))
        _falhas_por_ip.setdefault(ip, []).append(momento)


def _limpar_ip(ip: str | None) -> None:
    if ip:
        with _trava_ips:
            _falhas_por_ip.pop(ip, None)


# --------------------------------------------------------- Conferir a senha

_verificacoes = threading.BoundedSemaphore(VERIFICACOES_SIMULTANEAS)
_hash_ficticio: dict[int, str] = {}


def _conferir_senha(senha: str, guardado: str) -> bool:
    with _verificacoes:
        return conferir_hash(senha, guardado)


def _gastar_o_tempo_de_uma_senha(senha: str) -> None:
    """Operador que não existe custa o mesmo scrypt que senha errada: o tempo
    da resposta não diz quais operadores existem (achado S-07)."""
    if SCRYPT_N not in _hash_ficticio:
        _hash_ficticio[SCRYPT_N] = gerar_hash(secrets.token_urlsafe(16))
    _conferir_senha(senha, _hash_ficticio[SCRYPT_N])


# ------------------------------------------------------------------ Sessões

def _quando(texto: str) -> datetime:
    return datetime.fromisoformat(texto)


def hash_da_sessao(sid: str) -> str:
    """O banco guarda só o SHA-256 do id da sessão: uma cópia do banco não
    abre sessão nenhuma (achado S-79). O id tem 256 bits aleatórios, então o
    hash dispensa sal e custo."""
    return hashlib.sha256(sid.encode()).hexdigest()


def _marcar_falha(operador_id: int, tentativas: int):
    tentativas += 1
    bloqueio = None
    if tentativas >= TENTATIVAS_ATE_BLOQUEIO:
        bloqueio = (datetime.now(timezone.utc)
                    + timedelta(minutes=BLOQUEIO_MIN)).isoformat(timespec="seconds")
    with conectar() as conn:
        conn.execute("UPDATE operadores SET tentativas = ?, bloqueado_ate = ? WHERE id = ?",
                     (tentativas, bloqueio, operador_id))
    if bloqueio:
        registrar_evento("atencao", "seguranca",
                         f"Operador {operador_id} bloqueado por {BLOQUEIO_MIN} min "
                         f"depois de {tentativas} tentativas")


def autenticar(usuario: str, senha: str, codigo: str | None = None,
               ip: str | None = None) -> tuple[str, str]:
    """Devolve (id_da_sessao, token_csrf). Levanta FalhaLogin em qualquer recusa;
    PrecisaCodigo quando a senha confere e o MFA pede o código; MuitasTentativas
    quando o endereço errou demais na janela."""
    usuario = (usuario or "").strip().lower()
    espera = espera_do_ip(ip)
    if espera:
        raise MuitasTentativas(espera)
    with conectar() as conn:
        op = conn.execute("SELECT * FROM operadores WHERE usuario = ?", (usuario,)).fetchone()
    if op is None:
        _gastar_o_tempo_de_uma_senha(senha or "")
        _anotar_falha_do_ip(ip)
        raise FalhaLogin("Usuário ou senha inválidos.")

    momento = datetime.now(timezone.utc)
    if op["bloqueado_ate"] and _quando(op["bloqueado_ate"]) > momento:
        raise FalhaLogin("Operador bloqueado por tentativas. Espere alguns minutos.")

    if not _conferir_senha(senha or "", op["senha_hash"]):
        _marcar_falha(op["id"], op["tentativas"] or 0)
        _anotar_falha_do_ip(ip)
        raise FalhaLogin("Usuário ou senha inválidos.")

    # Operador desativado: a mesma resposta de credencial errada, sem dizer
    # que a conta existe; o evento registra o motivo.
    if not op["ativo"]:
        registrar_evento("atencao", "seguranca", f"Login recusado: operador desativado ({usuario})")
        _anotar_falha_do_ip(ip)
        raise FalhaLogin("Usuário ou senha inválidos.")

    # Senha certa, conta ativa: as duas recusas abaixo não contam como erro.
    if op["senha_trocada_em"] and _quando(op["senha_trocada_em"]) \
            + timedelta(days=SENHA_VALIDADE_DIAS) <= momento:
        registrar_evento("atencao", "seguranca", f"Login recusado: senha vencida ({usuario})")
        raise SenhaVencida(f"Senha vencida (mais de {SENHA_VALIDADE_DIAS} dias). Troque com: "
                           f"python cli.py operador --usuario {usuario}")
    if not op["totp_ativo"] and mfa_obrigatorio():
        registrar_evento("atencao", "seguranca", f"Login recusado: operador sem MFA ({usuario})")
        raise FalhaLogin("Este painel exige o código do app autenticador. Ative com: "
                         f"python cli.py operador --usuario {usuario} --mfa-ativar")

    passo = None
    if op["totp_ativo"]:
        if not (codigo or "").strip():
            raise PrecisaCodigo("Digite o código do app autenticador.")
        segredo = _cofre().ler("operador", f"totp:{usuario}")
        passo = conferir_totp(segredo, codigo, op["totp_ultimo_passo"]) if segredo else None
        if passo is None:
            _marcar_falha(op["id"], op["tentativas"] or 0)
            _anotar_falha_do_ip(ip)
            raise FalhaLogin("Código do autenticador inválido ou já usado.")

    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    inicio = momento.isoformat(timespec="seconds")
    expira = (momento + timedelta(hours=DURACAO_MAX_H)).isoformat(timespec="seconds")
    with conectar() as conn:
        if passo is not None:
            # Grava o passo só se nenhum login em paralelo usou este código
            # depois da leitura lá em cima. Sem linha gravada, o erro desfaz
            # a transação e nenhuma sessão nasce.
            gravado = conn.execute(
                "UPDATE operadores SET totp_ultimo_passo = ? WHERE id = ? AND totp_ativo = 1"
                " AND (totp_ultimo_passo IS NULL OR totp_ultimo_passo < ?)",
                (passo, op["id"], passo)).rowcount
            if not gravado:
                raise FalhaLogin("Código do autenticador inválido ou já usado.")
        conn.execute("UPDATE operadores SET tentativas = 0, bloqueado_ate = NULL,"
                     " ultimo_login_em = ? WHERE id = ?", (inicio, op["id"]))
        conn.execute("DELETE FROM sessoes WHERE expira_em <= ?", (inicio,))
        conn.execute("INSERT INTO sessoes (id, operador_id, csrf, criada_em, ultimo_uso, expira_em)"
                     " VALUES (?,?,?,?,?,?)",
                     (hash_da_sessao(sid), op["id"], csrf, inicio, inicio, expira))
    _limpar_ip(ip)
    registrar_evento("info", "seguranca", f"Login de {usuario}")
    return sid, csrf


def validar_sessao(sid: str | None) -> dict | None:
    """Devolve {usuario, operador_id, csrf} ou None. Renova a marca de uso."""
    if not sid:
        return None
    momento = datetime.now(timezone.utc)
    chave = hash_da_sessao(sid)
    with conectar() as conn:
        s = conn.execute(
            "SELECT s.*, o.usuario FROM sessoes s JOIN operadores o ON o.id = s.operador_id"
            " WHERE s.id = ? AND o.ativo = 1", (chave,)).fetchone()
        if s is None:
            return None
        vencida = (_quando(s["expira_em"]) <= momento
                   or _quando(s["ultimo_uso"]) + timedelta(minutes=OCIOSIDADE_MIN) <= momento)
        if vencida:
            conn.execute("DELETE FROM sessoes WHERE id = ?", (chave,))
            return None
        conn.execute("UPDATE sessoes SET ultimo_uso = ? WHERE id = ?",
                     (momento.isoformat(timespec="seconds"), chave))
        return {"usuario": s["usuario"], "operador_id": s["operador_id"], "csrf": s["csrf"]}


def encerrar_sessao(sid: str | None):
    if not sid:
        return
    with conectar() as conn:
        conn.execute("DELETE FROM sessoes WHERE id = ?", (hash_da_sessao(sid),))


# ------------------------------------------------------------ State do OAuth
#
# O retorno de um marketplace só vale se a autorização saiu desta mesma
# sessão. O state é aleatório (256 bits), vale 10 minutos, sai da memória no
# primeiro uso — certo ou errado — e fica preso ao hash da sessão, nunca ao
# identificador em claro. Não depende de PKCE, que nem todo marketplace
# oferece. RFC 9700, seção 2.1; OWASP ASVS 5.0, requisitos 10.1.2 e 10.2.1.
#
# Fica em memória porque o painel roda num processo só (executar.py). Um
# reinício no meio da autorização só pede um clique novo em "autorizar".

STATE_VALIDADE_S = 10 * 60
STATE_PENDENTES_MAX = 50

_states: dict[str, dict] = {}
_trava_states = threading.Lock()


class StateInvalido(ValueError):
    """Retorno de OAuth que esta sessão não iniciou, vencido ou repetido."""


def _vinculo(sid: str) -> str:
    return hash_da_sessao(sid)


def criar_state_oauth(provedor: str, sid: str | None, dados: dict | None = None) -> str:
    """Abre uma autorização de `provedor` presa à sessão `sid`. `dados` guarda o
    que o retorno vai precisar, como o verifier do PKCE."""
    if not sid:
        raise StateInvalido("Entre no painel antes de autorizar.")
    momento = time.monotonic()
    state = secrets.token_urlsafe(32)
    with _trava_states:
        for chave in [k for k, v in _states.items() if momento - v["criado"] > STATE_VALIDADE_S]:
            del _states[chave]
        while len(_states) >= STATE_PENDENTES_MAX:
            del _states[min(_states, key=lambda k: _states[k]["criado"])]
        _states[state] = {"provedor": provedor, "vinculo": _vinculo(sid),
                          "criado": momento, "dados": dict(dados or {})}
    return state


def consumir_consentimento_pendente(provedor: str, sid: str | None) -> None:
    """Consome a autorização mais recente de `provedor` aberta por esta sessão
    e ainda válida, ou levanta StateInvalido. Serve a um passo em que o
    provedor não devolve o nosso state (a URI de log-in da Amazon): sem um
    consentimento que esta sessão começou, o passo para. Assim um link da URI
    de log-in mandado por outra pessoa não liga a conta dela a este painel."""
    if not sid:
        raise StateInvalido("Entre no painel antes de autorizar.")
    momento = time.monotonic()
    vinculo = _vinculo(sid)
    with _trava_states:
        candidatos = [k for k, v in _states.items()
                      if v["provedor"] == provedor and v["vinculo"] == vinculo
                      and momento - v["criado"] <= STATE_VALIDADE_S]
        if not candidatos:
            raise StateInvalido("Nenhuma autorização foi aberta por esta sessão nos últimos "
                                "10 minutos. Comece pelo botão de autorizar no painel.")
        del _states[max(candidatos, key=lambda k: _states[k]["criado"])]


def consumir_state_oauth(state: str | None, provedor: str, sid: str | None) -> dict:
    """Devolve os `dados` da autorização ou levanta StateInvalido. O state sai
    da memória antes de qualquer conferência: não serve para outra tentativa."""
    with _trava_states:
        registro = _states.pop(state or "", None)
    if registro is None:
        motivo = "desconhecido ou já usado"
    elif time.monotonic() - registro["criado"] > STATE_VALIDADE_S:
        motivo = "vencido"
    elif registro["provedor"] != provedor:
        motivo = "de outro provedor"
    elif not sid or not hmac.compare_digest(registro["vinculo"], _vinculo(sid)):
        motivo = "de outra sessão"
    else:
        return registro["dados"]
    # A recusa vai para o registro sem o valor do state. ASVS 5.0, 16.3.2 e 16.2.5.
    registrar_evento("aviso", "seguranca", f"Retorno de autorização recusado ({provedor}): state {motivo}")
    raise StateInvalido("Esta autorização não saiu desta sessão, já foi usada ou venceu. "
                        "Clique em autorizar de novo.")
