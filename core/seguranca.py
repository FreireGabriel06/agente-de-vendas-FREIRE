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

Cada verificação de senha custa cerca de 134 MB de memória, por desenho do
scrypt. O bloqueio por tentativas limita o abuso. Exposto à internet, isso
ainda pede limite de requisições na camada da frente.
"""
import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

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
TENTATIVAS_ATE_BLOQUEIO = 5
BLOQUEIO_MIN = 15

COOKIE_SESSAO = "agente_sessao"


class FalhaLogin(Exception):
    """Credencial inválida, operador bloqueado ou senha fraca."""


def inicializar_seguranca():
    with conectar() as conn:
        conn.executescript(SCHEMA_SEGURANCA)


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

def definir_operador(usuario: str, senha: str) -> str:
    """Cria o operador ou troca a senha dele. Chamado pela CLI, nunca pela web."""
    usuario = (usuario or "").strip().lower()
    if not usuario:
        raise FalhaLogin("Informe o nome do operador.")
    senha_hash = gerar_hash(senha)
    with conectar() as conn:
        atual = conn.execute("SELECT id FROM operadores WHERE usuario = ?", (usuario,)).fetchone()
        if atual:
            conn.execute(
                "UPDATE operadores SET senha_hash = ?, atualizado_em = ?, tentativas = 0,"
                " bloqueado_ate = NULL WHERE id = ?", (senha_hash, agora(), atual["id"]))
            conn.execute("DELETE FROM sessoes WHERE operador_id = ?", (atual["id"],))
            acao = "senha trocada"
        else:
            conn.execute(
                "INSERT INTO operadores (usuario, senha_hash, criado_em, atualizado_em)"
                " VALUES (?,?,?,?)", (usuario, senha_hash, agora(), agora()))
            acao = "operador criado"
    registrar_evento("info", "seguranca", f"{acao}: {usuario}")
    return acao


def existe_operador() -> bool:
    with conectar() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM operadores").fetchone()["c"] > 0


# ------------------------------------------------------------------ Sessões

def _quando(texto: str) -> datetime:
    return datetime.fromisoformat(texto)


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


def autenticar(usuario: str, senha: str) -> tuple[str, str]:
    """Devolve (id_da_sessao, token_csrf). Levanta FalhaLogin em qualquer recusa."""
    usuario = (usuario or "").strip().lower()
    with conectar() as conn:
        op = conn.execute("SELECT * FROM operadores WHERE usuario = ?", (usuario,)).fetchone()
    if op is None:
        raise FalhaLogin("Usuário ou senha inválidos.")

    momento = datetime.now(timezone.utc)
    if op["bloqueado_ate"] and _quando(op["bloqueado_ate"]) > momento:
        raise FalhaLogin("Operador bloqueado por tentativas. Espere alguns minutos.")

    if not conferir_hash(senha or "", op["senha_hash"]):
        _marcar_falha(op["id"], op["tentativas"] or 0)
        raise FalhaLogin("Usuário ou senha inválidos.")

    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    inicio = momento.isoformat(timespec="seconds")
    expira = (momento + timedelta(hours=DURACAO_MAX_H)).isoformat(timespec="seconds")
    with conectar() as conn:
        conn.execute("UPDATE operadores SET tentativas = 0, bloqueado_ate = NULL WHERE id = ?",
                     (op["id"],))
        conn.execute("DELETE FROM sessoes WHERE expira_em <= ?", (inicio,))
        conn.execute("INSERT INTO sessoes (id, operador_id, csrf, criada_em, ultimo_uso, expira_em)"
                     " VALUES (?,?,?,?,?,?)", (sid, op["id"], csrf, inicio, inicio, expira))
    registrar_evento("info", "seguranca", f"Login de {usuario}")
    return sid, csrf


def validar_sessao(sid: str | None) -> dict | None:
    """Devolve {usuario, operador_id, csrf} ou None. Renova a marca de uso."""
    if not sid:
        return None
    momento = datetime.now(timezone.utc)
    with conectar() as conn:
        s = conn.execute(
            "SELECT s.*, o.usuario FROM sessoes s JOIN operadores o ON o.id = s.operador_id"
            " WHERE s.id = ?", (sid,)).fetchone()
        if s is None:
            return None
        vencida = (_quando(s["expira_em"]) <= momento
                   or _quando(s["ultimo_uso"]) + timedelta(minutes=OCIOSIDADE_MIN) <= momento)
        if vencida:
            conn.execute("DELETE FROM sessoes WHERE id = ?", (sid,))
            return None
        conn.execute("UPDATE sessoes SET ultimo_uso = ? WHERE id = ?",
                     (momento.isoformat(timespec="seconds"), sid))
        return {"usuario": s["usuario"], "operador_id": s["operador_id"], "csrf": s["csrf"]}


def encerrar_sessao(sid: str | None):
    if not sid:
        return
    with conectar() as conn:
        conn.execute("DELETE FROM sessoes WHERE id = ?", (sid,))
