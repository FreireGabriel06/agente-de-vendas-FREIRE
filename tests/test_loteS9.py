"""Lote S9: exclusão dos dados da Amazon pelo Data Protection Policy (1.7).
Até 30 dias depois de o vendedor revogar a autorização, tudo o que veio da
Amazon sai: pedidos, itens, anúncios, sincronizações, alertas e o refresh
token. O arquivo do banco não guarda cópia apagada, e a restauração de um
backup mais antigo apaga de novo."""
import json
import sys
from pathlib import Path

import pytest

from config import config
from core import alertas, backup, cofre, privacidade, sincronizacao
from db import agora, conectar

SKU = "SKU-QUE-SAI-DPP-4242"
TITULO = "Titulo-da-Amazon-que-sai-7373"
SENHA_BACKUP = "senha-de-backup-longa-1"


@pytest.fixture(autouse=True)
def scrypt_rapido(monkeypatch):
    monkeypatch.setattr(backup, "SCRYPT_N", 2 ** 14)


def semear_amazon(pedido="111-0000000-0000001", sku=SKU):
    with conectar() as conn:
        conn.execute("INSERT INTO amazon_pedidos (amazon_order_id, status, sincronizado_em)"
                     " VALUES (?, 'Shipped', ?)", (pedido, agora()))
        conn.execute("INSERT INTO amazon_itens (amazon_order_id, order_item_id, sku, titulo)"
                     " VALUES (?, '1', ?, ?)", (pedido, sku, TITULO))
        conn.execute("INSERT INTO amazon_anuncios (sku, titulo, quantidade_propria, visto_em)"
                     " VALUES (?, ?, 1, ?)", (sku, TITULO, agora()))
        conn.execute("INSERT INTO sincronizacoes (fonte, resultado, ultimo_sucesso) VALUES"
                     " ('amazon_pedidos', 'ok', ?)", (agora(),))
        conn.execute("INSERT INTO alertas (tipo, chave, mensagem, aberto_em, atualizado_em)"
                     " VALUES ('estoque_baixo', ?, ?, ?, ?)", (sku, f"{TITULO} acabando", agora(), agora()))
    cofre.guardar_segredo("AMZ_REFRESH_TOKEN", "rt-amazon-que-sai")


def contar(tabela, banco=None):
    import sqlite3
    with sqlite3.connect(banco or config.db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


def test_apaga_tudo_o_que_veio_da_amazon_e_so_isso(isolamento):
    semear_amazon()
    with conectar() as conn:
        conn.execute("INSERT INTO sincronizacoes (fonte, resultado) VALUES ('outra_fonte', 'ok')")

    r = sincronizacao.apagar_dados_amazon("revogado")

    assert (r["amazon_pedidos"], r["amazon_itens"], r["amazon_anuncios"]) == (1, 1, 1)
    assert r["alertas"] == 1 and r["sincronizacoes"] == 1 and r["refresh_token"] == 1
    for tabela in sincronizacao.TABELAS_AMAZON + ("alertas",):
        assert contar(tabela) == 0
    assert contar("sincronizacoes") == 1  # a fonte que não é da Amazon fica
    assert cofre.ler("amazon", "refresh_token") is None
    with conectar() as conn:
        evento = conn.execute("SELECT nivel, mensagem FROM eventos WHERE origem = 'amazon'"
                              " ORDER BY id DESC LIMIT 1").fetchone()
    assert evento["nivel"] == "atencao" and "revogado" in evento["mensagem"]
    marca = json.loads(privacidade.ARQ_ELIMINACOES.read_text(encoding="utf-8").splitlines()[-1])
    assert marca["motivo"] == "revogado" and marca["amazon_apagado_em"]


def test_arquivo_do_banco_nao_guarda_copia_apagada(isolamento):
    semear_amazon()
    with conectar() as conn:
        token_cifrado = conn.execute("SELECT valor_cifrado FROM credenciais WHERE provedor = 'amazon'"
                                     " AND nome = 'refresh_token'").fetchone()[0].encode()
    antes = Path(config.db_path).read_bytes()
    assert SKU.encode() in antes and token_cifrado in antes
    sincronizacao.apagar_dados_amazon("fim da participação")
    bruto = Path(config.db_path).read_bytes()
    # O token sai por outra conexão, sem secure_delete: quem limpa é o VACUUM.
    assert SKU.encode() not in bruto and TITULO.encode() not in bruto
    assert token_cifrado not in bruto


def test_backup_anterior_restaurado_apaga_de_novo(isolamento):
    semear_amazon()
    arquivo = backup.criar(isolamento / "bk", SENHA_BACKUP)
    sincronizacao.apagar_dados_amazon("revogado")

    r = backup.restaurar(arquivo, SENHA_BACKUP, isolamento / "rest")

    banco = isolamento / "rest" / "agente.db"
    assert r["amazon_apagados"] >= 5
    for tabela in sincronizacao.TABELAS_AMAZON + ("alertas",):
        assert contar(tabela, banco) == 0
    assert SKU.encode() not in banco.read_bytes()


def test_backup_feito_depois_da_exclusao_fica_com_o_dado_novo(isolamento):
    semear_amazon()
    sincronizacao.apagar_dados_amazon("revogado")
    semear_amazon(pedido="111-0000000-0000002", sku="SKU-NOVO-DEPOIS")  # o vendedor voltou
    arquivo = backup.criar(isolamento / "bk", SENHA_BACKUP)

    r = backup.restaurar(arquivo, SENHA_BACKUP, isolamento / "rest")

    assert r["amazon_apagados"] == 0
    assert contar("amazon_pedidos", isolamento / "rest" / "agente.db") == 1


def test_cli_pede_confirmacao(isolamento, monkeypatch, capsys):
    import cli

    semear_amazon()
    monkeypatch.setattr("builtins.input", lambda *_: "não")
    monkeypatch.setattr(sys, "argv", ["cli.py", "amazon", "apagar-dados", "--motivo", "revogado"])
    with pytest.raises(SystemExit):
        cli.main()
    assert contar("amazon_pedidos") == 1 and "Nada foi apagado" in capsys.readouterr().out

    monkeypatch.setenv("AMZ_REFRESH_TOKEN", "ainda-no-ambiente")
    monkeypatch.setattr(sys, "argv", ["cli.py", "amazon", "apagar-dados", "--motivo", "revogado",
                                      "--sim"])
    cli.main()
    saida = capsys.readouterr().out
    assert contar("amazon_pedidos") == 0
    assert "1 pedido(s)" in saida and "AMZ_REFRESH_TOKEN ainda está" in saida
    assert "Backups feitos antes disto" in saida


def test_alerta_de_conexao_vencida_lembra_o_prazo(isolamento):
    with conectar() as conn:
        conn.execute("INSERT INTO sincronizacoes (fonte, resultado, ultimo_sucesso)"
                     " VALUES ('amazon_pedidos', 'conexao_vencida', ?)", (agora(),))
    mensagem = alertas.condicoes()[("conexao_vencida", "amazon")][0]
    assert "30 dias" in mensagem and "amazon apagar-dados" in mensagem


def test_endereco_no_limite_gera_um_alarme(novo_cliente, operador):
    from core import seguranca
    from apoio import USUARIO
    cliente = novo_cliente(client=("203.0.113.50", 50000))
    for _ in range(seguranca.FALHAS_POR_IP + 2):
        cliente.post("/api/login", json={"usuario": USUARIO, "senha": "Errada-Errada-1!"})
    with conectar() as conn:
        alarmes = conn.execute("SELECT nivel FROM eventos WHERE mensagem LIKE"
                               " 'Endereço 203.0.113.50 errou o login%'").fetchall()
    assert [a["nivel"] for a in alarmes] == ["atencao"]
