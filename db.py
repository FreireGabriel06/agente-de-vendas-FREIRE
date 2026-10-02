"""
Camada de persistência. SQLite por padrão — troque a connection string por
Postgres quando o volume justificar; o schema é compatível.
"""
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS produtos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sku               TEXT UNIQUE NOT NULL,
    titulo            TEXT NOT NULL,
    categoria_ml      TEXT,
    custo_fornecedor  REAL NOT NULL,
    peso_kg           REAL DEFAULT 0.3,
    fornecedor_id     INTEGER,
    ativo             INTEGER DEFAULT 1,
    criado_em         TEXT NOT NULL,
    -- Confirmações que a conformidade exige (core/conformidade.py). NULL quer
    -- dizer "ainda não confirmado" e bloqueia a compra até alguém preencher.
    categoria_regulada     TEXT,     -- 'nenhuma' ou suplemento, cosmetico, brinquedo...
    habilitacao_confirmada INTEGER,  -- 1 = registro/licença da categoria em dia
    reembalagem_confirmada INTEGER   -- 1 = sai sem o nome do fornecedor (Amazon)
);

CREATE TABLE IF NOT EXISTS fornecedores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nome          TEXT NOT NULL,
    canal         TEXT NOT NULL,          -- email | whatsapp | api | portal
    contato       TEXT NOT NULL,
    prazo_dias    INTEGER DEFAULT 5,
    pedido_minimo REAL DEFAULT 0,
    observacoes   TEXT
);

CREATE TABLE IF NOT EXISTS anuncios (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    produto_id     INTEGER NOT NULL REFERENCES produtos(id),
    marketplace    TEXT NOT NULL,         -- mercadolivre | amazon
    id_externo     TEXT NOT NULL,
    tipo_anuncio   TEXT DEFAULT 'classico',
    preco_venda    REAL NOT NULL,
    estoque        INTEGER DEFAULT 0,
    atualizado_em  TEXT,
    UNIQUE(marketplace, id_externo)
);

CREATE TABLE IF NOT EXISTS pedidos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace         TEXT NOT NULL,
    id_externo          TEXT NOT NULL,
    produto_id          INTEGER REFERENCES produtos(id),
    quantidade          INTEGER NOT NULL DEFAULT 1,
    valor_bruto         REAL NOT NULL,
    custo_previsto      REAL,
    margem_prevista     REAL,
    estado              TEXT NOT NULL,
    comprador_nome      TEXT,
    comprador_id        TEXT,
    endereco_json       TEXT,
    codigo_rastreio     TEXT,
    criado_em           TEXT NOT NULL,
    atualizado_em       TEXT NOT NULL,
    UNIQUE(marketplace, id_externo)
);

CREATE TABLE IF NOT EXISTS transicoes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id   INTEGER NOT NULL REFERENCES pedidos(id),
    de          TEXT,
    para        TEXT NOT NULL,
    motivo      TEXT,
    automatico  INTEGER DEFAULT 1,
    ocorrido_em TEXT NOT NULL
);

-- Fila de ações que o robô preparou mas NÃO executou sozinho.
-- É aqui que você entra: aprova ou recusa.
CREATE TABLE IF NOT EXISTS aprovacoes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo          TEXT NOT NULL,          -- compra_fornecedor | resposta_cliente | ajuste_preco | publicar_anuncio
    pedido_id     INTEGER REFERENCES pedidos(id),
    resumo        TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    valor         REAL,
    status        TEXT DEFAULT 'pendente', -- pendente | aprovada | recusada | executada | erro
    resultado     TEXT,
    criado_em     TEXT NOT NULL,
    decidido_em   TEXT
);

-- Perguntas de comprador que o bot já tratou (atendimento/bot.py). O worker
-- não manda de novo ao modelo uma pergunta que já foi para a fila ou foi
-- escalada: cada envio é uma chamada paga à Claude API.
CREATE TABLE IF NOT EXISTS perguntas_tratadas (
    question_id   TEXT PRIMARY KEY,
    situacao      TEXT NOT NULL,          -- enfileirada | escalada | tentar_de_novo
    aprovacao_id  INTEGER,                -- a resposta na fila (sem FK: o demo.py apaga a fila)
    tratada_em    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oportunidades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    termo             TEXT NOT NULL,
    nicho             TEXT,
    score             REAL NOT NULL,
    demanda           REAL,
    concorrencia      REAL,
    preco_mediano     REAL,
    tendencia_pct     REAL,
    fonte             TEXT,
    coletado_em       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eventos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nivel        TEXT NOT NULL,
    origem       TEXT NOT NULL,
    mensagem     TEXT NOT NULL,
    detalhe_json TEXT,
    ocorrido_em  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pedidos_estado ON pedidos(estado);
CREATE INDEX IF NOT EXISTS idx_aprov_status  ON aprovacoes(status);
CREATE INDEX IF NOT EXISTS idx_oport_score   ON oportunidades(score DESC);
"""


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conectar(espera: float = 5.0):
    """espera: segundos aguardando um banco ocupado por outro processo antes
    de desistir com 'database is locked' (padrão do sqlite3: 5)."""
    conn = sqlite3.connect(config.db_path, timeout=espera)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Colunas que entraram depois da primeira versão do banco. O CREATE TABLE IF
# NOT EXISTS não mexe numa tabela que já existe, então um banco antigo recebe
# cada coluna aqui, uma vez, sem perder dado. Coluna nova entra vazia (NULL).
COLUNAS_ACRESCENTADAS = {
    "produtos": {
        "categoria_regulada": "TEXT",
        "habilitacao_confirmada": "INTEGER",
        "reembalagem_confirmada": "INTEGER",
    },
}


def inicializar():
    with conectar() as conn:
        conn.executescript(SCHEMA)
        for tabela, colunas in COLUNAS_ACRESCENTADAS.items():
            existentes = {linha["name"] for linha in conn.execute(f"PRAGMA table_info({tabela})")}
            for nome, tipo in colunas.items():
                if nome not in existentes:
                    conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")


def registrar_evento(nivel: str, origem: str, mensagem: str, detalhe=None):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO eventos (nivel, origem, mensagem, detalhe_json, ocorrido_em)"
            " VALUES (?,?,?,?,?)",
            (nivel, origem, mensagem,
             json.dumps(detalhe, ensure_ascii=False) if detalhe else None, agora()),
        )
