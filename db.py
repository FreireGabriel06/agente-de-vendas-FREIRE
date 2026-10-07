"""
Camada de persistência. SQLite por padrão — troque a connection string por
Postgres quando o volume justificar; o schema é compatível.

Dinheiro (core/dinheiro.py): cada valor tem a coluna REAL antiga, o texto
decimal canônico (<nome>_dec) e a moeda ISO 4217 (<nome>_moeda). O código
novo lê o texto decimal; a REAL fica como espelho para bancos e código antigos.
Datas são gravadas por agora(): UTC, ISO 8601 com o fuso (+00:00).
"""
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from config import config
from core import dinheiro

SCHEMA = """
CREATE TABLE IF NOT EXISTS produtos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sku               TEXT UNIQUE NOT NULL,
    titulo            TEXT NOT NULL,
    categoria_ml      TEXT,
    custo_fornecedor  REAL NOT NULL,     -- espelho antigo de custo_fornecedor_dec
    custo_fornecedor_dec   TEXT,         -- valor decimal canônico, ex. '18.50'
    custo_fornecedor_moeda TEXT,         -- ISO 4217; NULL = moeda não informada
    peso_kg           REAL DEFAULT 0.3,
    fornecedor_id     INTEGER,
    ativo             INTEGER DEFAULT 1, -- desativar em vez de apagar
    criado_em         TEXT NOT NULL,
    atualizado_em     TEXT,
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
    pedido_minimo REAL DEFAULT 0,       -- espelho antigo de pedido_minimo_dec
    pedido_minimo_dec   TEXT,
    pedido_minimo_moeda TEXT,
    observacoes   TEXT,
    ativo         INTEGER DEFAULT 1,    -- desativar em vez de apagar
    criado_em     TEXT,
    atualizado_em TEXT
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
    valor_bruto_dec     TEXT,
    valor_bruto_moeda   TEXT,           -- a moeda que o marketplace informou
    custo_previsto      REAL,
    custo_previsto_dec  TEXT,
    custo_previsto_moeda TEXT,
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
    """Data e hora para gravar: UTC, ISO 8601 com o fuso, ex. 2026-10-02T14:05:09+00:00."""
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
# cada coluna aqui, uma vez, sem perder dado. Coluna nova entra vazia (NULL);
# ativo entra com o padrão 1, porque o que já existia continua em uso.
COLUNAS_ACRESCENTADAS = {
    "produtos": {
        "categoria_regulada": "TEXT",
        "habilitacao_confirmada": "INTEGER",
        "reembalagem_confirmada": "INTEGER",
        "custo_fornecedor_dec": "TEXT",
        "custo_fornecedor_moeda": "TEXT",
        "atualizado_em": "TEXT",
    },
    "fornecedores": {
        "pedido_minimo_dec": "TEXT",
        "pedido_minimo_moeda": "TEXT",
        "ativo": "INTEGER DEFAULT 1",
        "criado_em": "TEXT",
        "atualizado_em": "TEXT",
    },
    "pedidos": {
        "valor_bruto_dec": "TEXT",
        "valor_bruto_moeda": "TEXT",
        "custo_previsto_dec": "TEXT",
        "custo_previsto_moeda": "TEXT",
    },
}

# Valores em dinheiro: a coluna REAL antiga ganha <nome>_dec e <nome>_moeda.
VALORES_EM_DINHEIRO = {
    "produtos": ("custo_fornecedor",),
    "fornecedores": ("pedido_minimo",),
    "pedidos": ("valor_bruto", "custo_previsto"),
}


def _copiar_da_coluna_real(conn, tabela: str, real: str):
    """Quando <real>_dec acaba de entrar num banco antigo: copia o valor da
    coluna REAL só onde ele existe. A moeda fica NULL ("moeda não informada"):
    o banco antigo não diz a moeda, e nada aqui presume BRL ou USD."""
    for linha in conn.execute(f"SELECT id, {real} FROM {tabela} WHERE {real} IS NOT NULL").fetchall():
        valor = dinheiro.do_real(linha[real])
        if valor is not None:
            conn.execute(f"UPDATE {tabela} SET {real}_dec = ? WHERE id = ?",
                         (dinheiro.texto(valor), linha["id"]))


def _gatilhos_da_coluna_real(conn):
    """SQL à mão que muda só a coluna REAL deixaria o texto decimal velho, e o
    código novo leria o valor antigo. Quando a REAL muda, o decimal não muda e
    os dois deixam de bater, o gatilho apaga o decimal e a moeda dessa linha:
    o valor passa a vir da REAL, com a moeda não informada, até alguém
    cadastrar de novo pela API ou pelo cli.py. Quem grava pelo código grava os
    dois juntos (a REAL é CAST do mesmo texto), e o gatilho não dispara."""
    for tabela, reais in VALORES_EM_DINHEIRO.items():
        for real in reais:
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {tabela}_{real}_real_divergiu"
                f" AFTER UPDATE OF {real} ON {tabela}"
                f" WHEN NEW.{real} IS NOT OLD.{real} AND NEW.{real}_dec IS OLD.{real}_dec"
                f" AND NEW.{real}_dec IS NOT NULL"
                f" AND CAST(NEW.{real}_dec AS REAL) IS NOT NEW.{real}"
                f" BEGIN UPDATE {tabela} SET {real}_dec = NULL, {real}_moeda = NULL"
                f" WHERE id = NEW.id; END")


def inicializar():
    with conectar() as conn:
        conn.executescript(SCHEMA)
        for tabela, colunas in COLUNAS_ACRESCENTADAS.items():
            existentes = {linha["name"] for linha in conn.execute(f"PRAGMA table_info({tabela})")}
            for nome, tipo in colunas.items():
                if nome not in existentes:
                    conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
                    real = nome.removesuffix("_dec")
                    if nome.endswith("_dec") and real in VALORES_EM_DINHEIRO.get(tabela, ()):
                        _copiar_da_coluna_real(conn, tabela, real)
        _gatilhos_da_coluna_real(conn)


def registrar_evento(nivel: str, origem: str, mensagem: str, detalhe=None):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO eventos (nivel, origem, mensagem, detalhe_json, ocorrido_em)"
            " VALUES (?,?,?,?,?)",
            (nivel, origem, mensagem,
             json.dumps(detalhe, ensure_ascii=False) if detalhe else None, agora()),
        )
