"""
LGPD (Lei 13.709/2018) aplicada ao banco do agente.

O sistema guarda nome, endereço e identificador de comprador. Isso é dado
pessoal, e hoje estava em texto puro no SQLite. Este módulo resolve quatro
obrigações concretas:

  1. MINIMIZAÇÃO (art. 6º, III) — só grava o que é necessário pra entregar o
     pedido. Nada de e-mail, telefone ou CPF do comprador se o fluxo não usa.

  2. SEGURANÇA (art. 46) — PII criptografada em repouso com Fernet. Se o
     arquivo .db vazar, o conteúdo não é legível sem a chave, que fica fora
     do banco.

  3. RETENÇÃO (art. 15/16) — dado pessoal tem prazo. Passado o período fiscal
     necessário, o expurgo apaga o PII e mantém só o registro contábil
     anonimizado, que é o que você precisa guardar.

  4. DIREITOS DO TITULAR (art. 18) — se um comprador pedir seus dados ou a
     exclusão, há uma função pra cada coisa, com log.

Base legal do tratamento: execução de contrato (art. 7º, V). Você não precisa
de consentimento pra processar o endereço de quem comprou de você — precisa
dele pra entregar. O que você NÃO pode é usar esse mesmo dado pra mandar
marketing depois; isso exige base legal própria.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import BASE_DIR
from db import conectar, agora, registrar_evento

ARQ_CHAVE = BASE_DIR / ".chave_lgpd"

# Prazo de guarda do dado pessoal, em dias. 5 anos cobre prescrição do CDC
# (art. 27) e o prazo fiscal. Depois disso o PII vira lixo de risco.
RETENCAO_DIAS = int(os.getenv("RETENCAO_PII_DIAS", "1825"))

SCHEMA_LGPD = """
-- Log de acesso a dado pessoal. Accountability (art. 6º, X): você precisa
-- conseguir demonstrar quem viu o quê e quando.
CREATE TABLE IF NOT EXISTS acessos_pii (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id   INTEGER,
    operacao    TEXT NOT NULL,      -- leitura | exportacao | eliminacao | expurgo
    ator        TEXT NOT NULL,      -- worker | painel | cli
    finalidade  TEXT NOT NULL,
    ocorrido_em TEXT NOT NULL
);

-- Registro das solicitações de titular, com prazo de resposta.
CREATE TABLE IF NOT EXISTS solicitacoes_titular (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    identificador TEXT NOT NULL,    -- id do comprador no marketplace
    tipo          TEXT NOT NULL,    -- acesso | eliminacao | correcao | portabilidade
    status        TEXT DEFAULT 'aberta',
    recebida_em   TEXT NOT NULL,
    prazo_em      TEXT NOT NULL,
    respondida_em TEXT,
    observacao    TEXT
);
"""


# ------------------------------------------------------------- Criptografia

def _obter_chave() -> bytes:
    """
    Chave de criptografia. Em produção, prefira variável de ambiente ou um
    cofre (Vault, AWS Secrets Manager) — arquivo no disco protege contra
    vazamento do .db, não contra acesso ao servidor.
    """
    env = os.getenv("CHAVE_LGPD")
    if env:
        return env.encode()
    if ARQ_CHAVE.exists():
        return ARQ_CHAVE.read_bytes()

    from cryptography.fernet import Fernet
    chave = Fernet.generate_key()
    ARQ_CHAVE.write_bytes(chave)
    ARQ_CHAVE.chmod(0o600)
    registrar_evento("info", "lgpd",
                     "Chave de criptografia gerada. Faça backup: sem ela os "
                     "dados de comprador ficam ilegíveis.")
    return chave


def cifrar(texto: str | None) -> str | None:
    if not texto:
        return texto
    from cryptography.fernet import Fernet
    return Fernet(_obter_chave()).encrypt(texto.encode()).decode()


def decifrar(cifrado: str | None) -> str | None:
    if not cifrado:
        return cifrado
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(_obter_chave()).decrypt(cifrado.encode()).decode()
    except (InvalidToken, ValueError):
        return "(não foi possível decifrar)"


def pseudonimizar(identificador: str) -> str:
    """
    Hash estável do id do comprador. Permite reconhecer cliente recorrente e
    cruzar pedidos sem guardar o identificador real em consulta comum.
    """
    salgado = (identificador + _obter_chave().decode()[:16]).encode()
    return hashlib.sha256(salgado).hexdigest()[:24]


def mascarar(texto: str | None, visivel: int = 3) -> str:
    """Pra exibir em tela e em log sem expor o dado inteiro."""
    if not texto:
        return "—"
    if len(texto) <= visivel:
        return "•" * len(texto)
    return texto[:visivel] + "•" * min(len(texto) - visivel, 8)


# ------------------------------------------------------------------- Acesso

def registrar_acesso(pedido_id: int | None, operacao: str, ator: str, finalidade: str):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO acessos_pii (pedido_id, operacao, ator, finalidade, ocorrido_em)"
            " VALUES (?,?,?,?,?)",
            (pedido_id, operacao, ator, finalidade, agora()),
        )


def ler_comprador(pedido_id: int, ator: str = "painel",
                  finalidade: str = "conferência de entrega") -> dict:
    """Única porta de leitura de PII. Sempre deixa rastro."""
    registrar_acesso(pedido_id, "leitura", ator, finalidade)
    with conectar() as conn:
        p = conn.execute(
            "SELECT comprador_nome, comprador_id, endereco_json FROM pedidos WHERE id = ?",
            (pedido_id,),
        ).fetchone()
    if not p:
        return {}
    return {
        "nome": decifrar(p["comprador_nome"]),
        "identificador": decifrar(p["comprador_id"]),
        "endereco": json.loads(decifrar(p["endereco_json"]) or "{}"),
    }


# ---------------------------------------------------------------- Retenção

def expurgar(dias: int = RETENCAO_DIAS) -> int:
    """
    Apaga o PII de pedidos antigos, mantendo o registro contábil.

    Rode isso por cron. Dado pessoal guardado além da necessidade não é
    arquivo — é passivo. Se vazar, você responde por ele.
    """
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat(timespec="seconds")
    with conectar() as conn:
        alvos = conn.execute(
            "SELECT id FROM pedidos WHERE criado_em < ? AND comprador_nome IS NOT NULL",
            (corte,),
        ).fetchall()
        for a in alvos:
            conn.execute(
                "UPDATE pedidos SET comprador_nome = NULL, comprador_id = NULL,"
                " endereco_json = '{}' WHERE id = ?", (a["id"],),
            )
    for a in alvos:
        registrar_acesso(a["id"], "expurgo", "cron", f"retenção de {dias} dias vencida")
    if alvos:
        registrar_evento("info", "lgpd", f"{len(alvos)} pedido(s) tiveram PII expurgado")
    return len(alvos)


# ------------------------------------------------- Direitos do titular (art. 18)

def abrir_solicitacao(identificador: str, tipo: str, observacao: str = "") -> int:
    """
    A LGPD dá 15 dias pra resposta em pedido de acesso (art. 19, II).
    O prazo é gravado pra você não perder.
    """
    prazo = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat(timespec="seconds")
    with conectar() as conn:
        cur = conn.execute(
            "INSERT INTO solicitacoes_titular (identificador, tipo, recebida_em,"
            " prazo_em, observacao) VALUES (?,?,?,?,?)",
            (identificador, tipo, agora(), prazo, observacao),
        )
        return cur.lastrowid


def exportar_dados(identificador: str) -> dict:
    """Atende pedido de acesso e portabilidade. Formato legível e estruturado."""
    # A comparação precisa decifrar cada registro. Em base grande, indexe pelo
    # pseudônimo (coluna dedicada) em vez de varrer a tabela inteira.
    encontrados = []
    with conectar() as conn:
        linhas = conn.execute("SELECT * FROM pedidos WHERE comprador_id IS NOT NULL").fetchall()
    for l in linhas:
        if decifrar(l["comprador_id"]) != identificador:
            continue
        registrar_acesso(l["id"], "exportacao", "painel", "direito de acesso art. 18")
        encontrados.append({
            "pedido": l["id_externo"],
            "marketplace": l["marketplace"],
            "valor": l["valor_bruto"],
            "estado": l["estado"],
            "nome": decifrar(l["comprador_nome"]),
            "endereco": json.loads(decifrar(l["endereco_json"]) or "{}"),
            "rastreio": l["codigo_rastreio"],
            "data": l["criado_em"],
        })

    return {
        "titular": identificador,
        "base_legal": "Execução de contrato — LGPD art. 7º, V",
        "finalidade": "Processamento e entrega de pedido de compra",
        "prazo_de_guarda": f"{RETENCAO_DIAS} dias a contar da compra",
        "compartilhamento": ["Transportadora, para entrega",
                             "Marketplace de origem, que já é controlador dos mesmos dados"],
        "registros": encontrados,
        "gerado_em": agora(),
    }


def eliminar_dados(identificador: str) -> int:
    """
    Atende pedido de eliminação (art. 18, VI).

    Atenção: a eliminação não é absoluta. O art. 16, I permite guardar o que
    for necessário ao cumprimento de obrigação legal — no seu caso, o registro
    fiscal da venda. Então o PII sai e o registro contábil fica, anonimizado.
    """
    removidos = 0
    with conectar() as conn:
        linhas = conn.execute("SELECT id, comprador_id FROM pedidos WHERE comprador_id IS NOT NULL").fetchall()
    for l in linhas:
        if decifrar(l["comprador_id"]) != identificador:
            continue
        with conectar() as conn:
            conn.execute(
                "UPDATE pedidos SET comprador_nome = NULL, comprador_id = NULL,"
                " endereco_json = '{}' WHERE id = ?", (l["id"],),
            )
        registrar_acesso(l["id"], "eliminacao", "painel", "direito de eliminação art. 18, VI")
        removidos += 1

    registrar_evento("info", "lgpd",
                     f"Eliminação atendida: {removidos} registro(s). "
                     f"Dados fiscais mantidos por obrigação legal (art. 16, I).")
    return removidos


def solicitacoes_vencendo(dias: int = 5) -> list:
    limite = (datetime.now(timezone.utc) + timedelta(days=dias)).isoformat(timespec="seconds")
    with conectar() as conn:
        return conn.execute(
            "SELECT * FROM solicitacoes_titular WHERE status = 'aberta' AND prazo_em <= ?"
            " ORDER BY prazo_em", (limite,),
        ).fetchall()


def inicializar_lgpd():
    with conectar() as conn:
        conn.executescript(SCHEMA_LGPD)
    _obter_chave()
