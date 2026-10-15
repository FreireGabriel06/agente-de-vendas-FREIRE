"""
Camada de persistência. SQLite por padrão — troque a connection string por
Postgres quando o volume justificar; o schema é compatível.

Dinheiro (core/dinheiro.py): cada valor tem a coluna REAL antiga, o texto
decimal canônico (<nome>_dec) e a moeda ISO 4217 (<nome>_moeda). O código
novo lê o texto decimal; a REAL fica como espelho para bancos e código antigos.
Datas são gravadas por agora(): UTC, ISO 8601 com o fuso (+00:00).
"""
import contextvars
import hashlib
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
    status        TEXT DEFAULT 'pendente', -- pendente | aprovada | recusada | executada | erro | incerta
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

-- Amazon (lote 7): só leitura, para monitoramento. Pedidos da Amazon não entram
-- no fluxo de compra com fornecedor: a Amazon proíbe que um terceiro apareça
-- como remetente (ver conectores/amazon.py). Nada de comprador é guardado:
-- nem nome, nem e-mail, nem endereço (BuyerInfo e ShippingAddress ficam de fora).
CREATE TABLE IF NOT EXISTS amazon_pedidos (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    amazon_order_id    TEXT NOT NULL UNIQUE,
    marketplace_id     TEXT,
    status             TEXT,
    canal              TEXT,             -- FulfillmentChannel: AFN (FBA) ou MFN
    comprado_em        TEXT,             -- PurchaseDate (UTC, ISO 8601)
    atualizado_na_amazon TEXT,           -- LastUpdateDate
    total_dec          TEXT,             -- OrderTotal.Amount, texto decimal
    total_moeda        TEXT,             -- OrderTotal.CurrencyCode
    itens_pendentes    INTEGER,          -- NumberOfItemsUnshipped
    itens_lidos        INTEGER NOT NULL DEFAULT 0,
    sincronizado_em    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS amazon_itens (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    amazon_order_id    TEXT NOT NULL REFERENCES amazon_pedidos(amazon_order_id),
    order_item_id      TEXT NOT NULL,
    asin               TEXT,
    sku                TEXT,
    titulo             TEXT,
    quantidade         INTEGER,
    quantidade_enviada INTEGER,
    preco_dec          TEXT,             -- ItemPrice.Amount (total da linha)
    preco_moeda        TEXT,
    UNIQUE(amazon_order_id, order_item_id)
);

-- Anúncios da Amazon (lote 8), um por SKU no marketplace configurado. A
-- quantidade própria vem da Listings API (canal DEFAULT, estoque do vendedor);
-- a da FBA, da FBA Inventory API. NULL é "não informado", nunca zero.
CREATE TABLE IF NOT EXISTS amazon_anuncios (
    sku                  TEXT PRIMARY KEY,
    asin                 TEXT,
    titulo               TEXT,
    status               TEXT,           -- BUYABLE, DISCOVERABLE (separados por vírgula)
    quantidade_propria   INTEGER,
    fba_disponivel       INTEGER,
    fba_total            INTEGER,
    preco_dec            TEXT,
    preco_moeda          TEXT,
    atualizado_na_amazon TEXT,
    visto_em             TEXT,           -- última passada que trouxe o SKU
    estoque_fba_em       TEXT            -- última passada da FBA que trouxe o SKU
);

-- Alertas (lote 8): um aberto por (tipo, chave). Cada um diz o horário do
-- último dado válido. Fecha sozinho quando a condição some.
CREATE TABLE IF NOT EXISTS alertas (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo               TEXT NOT NULL,    -- estoque_baixo | sincronizacao_falhou | conexao_vencida | dado_velho
    chave              TEXT NOT NULL,    -- SKU ou fonte
    mensagem           TEXT NOT NULL,
    ultimo_dado_valido TEXT,
    aberto_em          TEXT NOT NULL,
    atualizado_em      TEXT NOT NULL,
    resolvido_em       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerta_aberto ON alertas(tipo, chave) WHERE resolvido_em IS NULL;

-- Uma linha por fonte de dados: cursor, última tentativa, último sucesso e
-- o resultado. Último sucesso e última tentativa são separados: uma falha não
-- apaga a data do último dado válido.
-- Estado do sistema em chave e valor (lote de estrutura E3): o fim do
-- último ciclo do worker, lido pela checagem de saúde (core/saude.py).
CREATE TABLE IF NOT EXISTS estado_sistema (
    chave          TEXT PRIMARY KEY,
    valor          TEXT,
    atualizado_em  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sincronizacoes (
    fonte              TEXT PRIMARY KEY,
    cursor             TEXT,
    ultima_tentativa   TEXT,
    ultimo_sucesso     TEXT,
    resultado          TEXT,
    detalhe            TEXT
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
    # Trilha de auditoria (lote S10): quem agiu e o encadeamento.
    "eventos": {
        "ator": "TEXT",
        "hash": "TEXT",
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
        _trilha_so_de_inclusao(conn)


# ------------------------------------------------------------ Trilha de eventos
#
# Data Protection Policy da Amazon, 2.6.1: registro com data, resultado e
# autor, protegido contra alteração, guardado por pelo menos 12 meses. Aqui:
#   - cada evento diz quem agiu (ator): o operador da sessão do painel, o
#     endereço de quem tenta o login, "cli (usuário do sistema)" ou "sistema"
#     (o worker), por uma variável de contexto, sem mexer em quem registra;
#   - gatilhos recusam UPDATE e DELETE na tabela (nada no código apaga
#     evento);
#   - cada evento guarda o SHA-256 do anterior com o próprio conteúdo:
#     verificar_trilha() acha a primeira linha alterada ou apagada no meio. O
#     último hash, anotado fora da máquina na revisão quinzenal, protege
#     também o fim da trilha.

_ator = contextvars.ContextVar("ator", default=None)


def definir_ator(nome: str | None):
    """Quem age daqui em diante neste contexto (requisição, comando, thread)."""
    return _ator.set(nome)


def ator_atual() -> str:
    return _ator.get() or "sistema"


def _trilha_so_de_inclusao(conn):
    for acao in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS eventos_sem_{acao.lower()} BEFORE {acao}"
                     " ON eventos BEGIN SELECT RAISE(ABORT, 'A trilha de eventos só aceita"
                     " inclusão.'); END")


def _hash_do_evento(anterior, nivel, origem, mensagem, detalhe_json, ocorrido_em, ator) -> str:
    conteudo = json.dumps([anterior or "", nivel, origem, mensagem, detalhe_json, ocorrido_em, ator],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(conteudo.encode()).hexdigest()


def registrar_evento(nivel: str, origem: str, mensagem: str, detalhe=None):
    detalhe_json = json.dumps(detalhe, ensure_ascii=False) if detalhe else None
    momento, ator = agora(), ator_atual()
    with conectar() as conn:
        # Um evento por vez, entre processos também: o hash do anterior não
        # muda entre a leitura e a gravação.
        conn.execute("BEGIN IMMEDIATE")
        try:
            anterior = conn.execute("SELECT hash FROM eventos WHERE hash IS NOT NULL"
                                    " ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            # Banco de antes da trilha, ainda sem inicializar(): grava como antes.
            conn.execute("INSERT INTO eventos (nivel, origem, mensagem, detalhe_json, ocorrido_em)"
                         " VALUES (?,?,?,?,?)", (nivel, origem, mensagem, detalhe_json, momento))
            return
        conn.execute(
            "INSERT INTO eventos (nivel, origem, mensagem, detalhe_json, ocorrido_em, ator, hash)"
            " VALUES (?,?,?,?,?,?,?)",
            (nivel, origem, mensagem, detalhe_json, momento, ator,
             _hash_do_evento(anterior["hash"] if anterior else None, nivel, origem, mensagem,
                             detalhe_json, momento, ator)))


def verificar_trilha() -> dict:
    """Refaz o encadeamento. Devolve o total, quantos eventos são de antes da
    trilha (sem hash, no começo), o id da primeira linha que não confere
    (ou None) e o último hash."""
    anterior, sem_hash, total, quebra = None, 0, 0, None
    with conectar() as conn:
        for l in conn.execute("SELECT * FROM eventos ORDER BY id"):
            total += 1
            if l["hash"] is None:
                if anterior is None:
                    sem_hash += 1
                    continue
                quebra = l["id"]
                break
            esperado = _hash_do_evento(anterior, l["nivel"], l["origem"], l["mensagem"],
                                       l["detalhe_json"], l["ocorrido_em"], l["ator"])
            if esperado != l["hash"]:
                quebra = l["id"]
                break
            anterior = l["hash"]
    return {"eventos": total, "sem_hash": sem_hash, "quebra": quebra, "ultimo_hash": anterior}
