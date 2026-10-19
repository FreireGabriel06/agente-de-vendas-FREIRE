"""Versão do esquema (db.py): PRAGMA user_version, migrações numa transação
só e recusa de banco migrado por uma versão mais nova do agente."""
import sqlite3

import pytest

import db
from config import config
from core import saude


def _versao():
    with db.conectar() as conn:
        return db.versao_esquema(conn)


def _tabelas():
    with db.conectar() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_banco_novo_nasce_na_versao_atual():
    db.inicializar()
    assert _versao() == db.VERSAO_ESQUEMA


def test_banco_antigo_sem_versao_ganha_a_versao_atual():
    db.inicializar()
    with db.conectar() as conn:
        conn.execute("PRAGMA user_version = 0")
    db.inicializar()
    assert _versao() == db.VERSAO_ESQUEMA


def test_banco_mais_novo_e_recusado_antes_de_qualquer_escrita(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", str(tmp_path / "migrado.db"))
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("CREATE TABLE so_esta (x)")
        conn.execute(f"PRAGMA user_version = {db.VERSAO_ESQUEMA + 1}")
    with pytest.raises(db.EsquemaMaisNovo, match="Atualize o agente"):
        db.inicializar()
    assert _tabelas() == {"so_esta"}
    assert _versao() == db.VERSAO_ESQUEMA + 1


def test_migracoes_pendentes_rodam_em_ordem_uma_vez():
    db.inicializar()
    chamadas = []

    def v2(conn):
        chamadas.append(2)
        conn.execute("CREATE TABLE nova (id INTEGER)")

    def v3(conn):
        chamadas.append(3)
        conn.execute("ALTER TABLE nova ADD COLUMN nome TEXT")

    with db.conectar() as conn:
        db._migrar(conn, {2: v2, 3: v3}, alvo=3)
        db._migrar(conn, {2: v2, 3: v3}, alvo=3)
    assert chamadas == [2, 3]
    assert _versao() == 3
    with db.conectar() as conn:
        assert [c["name"] for c in conn.execute("PRAGMA table_info(nova)")] == ["id", "nome"]


def test_migracao_que_falha_desfaz_tudo():
    db.inicializar()

    def v2(conn):
        conn.execute("CREATE TABLE meio_caminho (id INTEGER)")

    def v3(conn):
        raise RuntimeError("quebrou no meio")

    with db.conectar() as conn, pytest.raises(RuntimeError, match="quebrou no meio"):
        db._migrar(conn, {2: v2, 3: v3}, alvo=3)
    assert _versao() == db.VERSAO_ESQUEMA
    assert "meio_caminho" not in _tabelas()


def test_migrar_relê_a_versao_sob_a_trava():
    """Outro processo migrou para além do alvo enquanto este esperava a trava."""
    db.inicializar()

    class ConexaoQueMuda:
        def __init__(self, conn):
            self.conn, self.leituras = conn, 0

        def execute(self, sql, *a):
            if sql == "PRAGMA user_version":
                self.leituras += 1
                if self.leituras == 2:
                    self.conn.execute("PRAGMA user_version = 9")
            return self.conn.execute(sql, *a)

        def __getattr__(self, nome):
            return getattr(self.conn, nome)

    with db.conectar() as conn, pytest.raises(db.EsquemaMaisNovo):
        db._migrar(ConexaoQueMuda(conn), {2: lambda c: None}, alvo=2)


@pytest.mark.parametrize("versao, nivel", [(None, "ok"), (0, "atencao"), (99, "falha")])
def test_saude_mostra_a_versao_do_esquema(versao, nivel):
    db.inicializar()
    if versao is not None:
        with db.conectar() as conn:
            conn.execute(f"PRAGMA user_version = {versao}")
    item = next(i for i in saude.verificar()["itens"] if i["nome"] == "esquema")
    assert item["nivel"] == nivel, item
