"""Lote S3 da auditoria de 06/10/2026: backup cifrado do banco com as chaves,
restauração testada (S-28), chave LGPD que não renasce em silêncio (S-11) e
restauração que reaplica expurgo e eliminações (S-84)."""
import sqlite3
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from core import backup, cofre, privacidade
from db import agora, conectar

SENHA = "senha-de-backup-longa-1"


@pytest.fixture(autouse=True)
def scrypt_rapido(monkeypatch):
    monkeypatch.setattr(backup, "SCRYPT_N", 2 ** 14)


def criar_pedido(id_externo, comprador_id, nome, criado_em=None):
    with conectar() as conn:
        conn.execute(
            "INSERT INTO pedidos (marketplace, id_externo, quantidade, valor_bruto, estado,"
            " comprador_nome, comprador_id, endereco_json, criado_em, atualizado_em)"
            " VALUES ('mercadolivre', ?, 1, 10, 'NOVO', ?, ?, ?, ?, ?)",
            (id_externo, privacidade.cifrar(nome), privacidade.cifrar(comprador_id),
             privacidade.cifrar('{"shipping_id": "1"}'), criado_em or agora(), agora()))


def compradores(banco: Path, chave: bytes) -> dict:
    f = Fernet(chave)
    with sqlite3.connect(banco) as conn:
        linhas = conn.execute("SELECT id_externo, comprador_nome FROM pedidos ORDER BY id").fetchall()
    return {i: (f.decrypt(n.encode()).decode() if n else None) for i, n in linhas}


# ------------------------------------------------------------ backup e restauração

def test_backup_cifrado_restaura_banco_e_chaves_numa_pasta_nova(isolamento):
    criar_pedido("P1", "comprador-1", "Pessoa Um")
    cofre.guardar_segredo("ML_CLIENT_SECRET", "segredo-do-cofre")
    arquivo = backup.criar(isolamento / "backups", SENHA)

    destino = isolamento / "restaurado"
    r = backup.restaurar(arquivo, SENHA, destino)

    assert set(r["partes"]) == {"agente.db", ".chave_lgpd", ".chave_cofre"}
    chave = (destino / ".chave_lgpd").read_bytes()
    assert compradores(destino / "agente.db", chave) == {"P1": "Pessoa Um"}
    # A chave do cofre restaurada abre a credencial do banco restaurado.
    from cryptography.fernet import MultiFernet
    with sqlite3.connect(destino / "agente.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM credenciais").fetchone()[0] == 1
    MultiFernet([Fernet((destino / ".chave_cofre").read_bytes().split(b",")[0].strip())])


def test_arquivo_de_backup_nao_tem_chave_nem_dado_em_texto(isolamento):
    criar_pedido("P1", "comprador-1", "Pessoa Um")
    arquivo = backup.criar(isolamento / "backups", SENHA)
    bruto = arquivo.read_bytes()
    chave_lgpd = privacidade._obter_chave()
    assert chave_lgpd not in bruto and b"Pessoa Um" not in bruto and b"SQLite format" not in bruto
    assert bruto.startswith(backup.CABECALHO)


def test_verificar_confere_sem_gravar(isolamento):
    criar_pedido("P1", "c1", "Pessoa Um")
    criar_pedido("P2", "c2", "Pessoa Dois")
    arquivo = backup.criar(isolamento / "backups", SENHA)
    m = backup.verificar(arquivo, SENHA)
    assert m["pedidos"] == 2 and "agente.db" in m["partes"]


@pytest.mark.parametrize("estrago", ["senha", "byte", "cabecalho"])
def test_senha_errada_ou_arquivo_alterado_nao_abrem(isolamento, estrago):
    criar_pedido("P1", "c1", "Pessoa Um")
    arquivo = backup.criar(isolamento / "backups", SENHA)
    senha = SENHA
    if estrago == "senha":
        senha = "outra-senha-qualquer"
    else:
        bruto = bytearray(arquivo.read_bytes())
        bruto[-10 if estrago == "byte" else 0] ^= 1
        arquivo.write_bytes(bytes(bruto))
    with pytest.raises(backup.ErroBackup):
        backup.verificar(arquivo, senha)
    with pytest.raises(backup.ErroBackup):
        backup.restaurar(arquivo, senha, isolamento / "nunca")
    assert not (isolamento / "nunca").exists()


def test_senha_curta_e_recusada(isolamento):
    criar_pedido("P1", "c1", "Pessoa Um")
    with pytest.raises(backup.ErroBackup, match="pelo menos"):
        backup.criar(isolamento / "backups", "curta")


def test_restaurar_nunca_escreve_por_cima(isolamento):
    criar_pedido("P1", "c1", "Pessoa Um")
    arquivo = backup.criar(isolamento / "backups", SENHA)
    ocupada = isolamento / "ocupada"
    ocupada.mkdir()
    (ocupada / "agente.db").write_bytes(b"dado que existe")

    with pytest.raises(backup.ErroBackup, match="pasta nova ou vazia"):
        backup.restaurar(arquivo, SENHA, ocupada)
    assert (ocupada / "agente.db").read_bytes() == b"dado que existe"


def test_restaurar_reaplica_a_retencao(isolamento, monkeypatch):
    criar_pedido("VELHO", "c-velho", "Pessoa Antiga", criado_em="2015-01-01T00:00:00+00:00")
    criar_pedido("NOVO", "c-novo", "Pessoa Recente")
    arquivo = backup.criar(isolamento / "backups", SENHA)

    r = backup.restaurar(arquivo, SENHA, isolamento / "rest")

    assert r["expurgados"] == 1
    chave = (isolamento / "rest" / ".chave_lgpd").read_bytes()
    assert compradores(isolamento / "rest" / "agente.db", chave) == {"VELHO": None, "NOVO": "Pessoa Recente"}


def test_restaurar_reaplica_eliminacao_feita_depois_do_backup(isolamento):
    criar_pedido("P1", "comprador-que-pediu", "Pessoa Que Pediu")
    criar_pedido("P2", "outro-comprador", "Outra Pessoa")
    arquivo = backup.criar(isolamento / "backups", SENHA)

    assert privacidade.eliminar_dados("comprador-que-pediu") == 1
    registro = privacidade.ARQ_ELIMINACOES.read_text(encoding="utf-8")
    assert "comprador-que-pediu" not in registro  # só o pseudônimo

    r = backup.restaurar(arquivo, SENHA, isolamento / "rest")

    assert r["eliminados"] == 1
    chave = (isolamento / "rest" / ".chave_lgpd").read_bytes()
    assert compradores(isolamento / "rest" / "agente.db", chave) == {"P1": None, "P2": "Outra Pessoa"}


# ------------------------------------------------------------ chave LGPD (S-11)

def test_chave_lgpd_nao_renasce_com_dado_cifrado_no_banco(isolamento, monkeypatch):
    criar_pedido("P1", "c1", "Pessoa Um")
    monkeypatch.delenv("CHAVE_LGPD")
    assert not privacidade.ARQ_CHAVE.exists()

    with pytest.raises(privacidade.ErroChaveLGPD, match="nenhuma é gerada"):
        privacidade.cifrar("qualquer coisa")
    assert not privacidade.ARQ_CHAVE.exists()


def test_chave_lgpd_nasce_num_banco_sem_dado_cifrado(isolamento, monkeypatch):
    monkeypatch.delenv("CHAVE_LGPD")
    assert privacidade.cifrar("primeiro dado")
    assert privacidade.ARQ_CHAVE.exists()


# ------------------------------------------------------------ CLI

def test_cli_cria_verifica_e_restaura_com_senha_do_ambiente(isolamento, monkeypatch, capsys):
    import cli

    criar_pedido("P1", "c1", "Pessoa Um")
    monkeypatch.setenv("BACKUP_SENHA", SENHA)
    pasta = isolamento / "bk"

    monkeypatch.setattr(sys, "argv", ["cli.py", "backup", "criar", "--destino", str(pasta)])
    cli.main()
    arquivo = next(pasta.glob("*.agvbkp"))

    monkeypatch.setattr(sys, "argv", ["cli.py", "backup", "verificar", str(arquivo)])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["cli.py", "backup", "restaurar", str(arquivo),
                                      "--pasta", str(isolamento / "rest")])
    cli.main()

    saida = capsys.readouterr().out
    assert "Backup cifrado gravado" in saida and "confere: 1 pedido(s)" in saida
    assert "Restaurado em" in saida and SENHA not in saida
    assert (isolamento / "rest" / "agente.db").exists()
