"""Lote S10: trilha de auditoria pelo Data Protection Policy da Amazon (2.6.1)
e achado S-21 da auditoria. Cada evento diz quem agiu, a tabela só aceita
inclusão e um encadeamento de hashes revela linha alterada ou apagada."""
import sqlite3
import sys
import threading

import pytest

from apoio import USUARIO, cabecalho
from config import config
from db import conectar, definir_ator, registrar_evento, verificar_trilha


def ultimo(origem=None):
    with conectar() as conn:
        sql = "SELECT * FROM eventos" + (" WHERE origem = ?" if origem else "") + " ORDER BY id DESC LIMIT 1"
        return conn.execute(sql, (origem,) if origem else ()).fetchone()


def test_sem_contexto_o_ator_e_o_sistema():
    registrar_evento("info", "teste", "algo")
    assert ultimo("teste")["ator"] == "sistema"


def test_painel_grava_o_operador_e_o_login_grava_o_endereco(sessao):
    cliente, csrf = sessao
    login = [l for l in _todos() if l["mensagem"] == f"Login de {USUARIO}"][-1]
    assert login["ator"] == "login de testclient"

    r = cliente.post("/api/configuracao/salvar", json={"MARGEM_MINIMA_PCT": "20"},
                     headers=cabecalho(csrf))
    assert r.status_code == 200
    assert ultimo("configuracao")["ator"] == USUARIO
    assert cliente.get("/api/eventos?n=5").json()["eventos"][0]["ator"] == USUARIO


def _todos():
    with conectar() as conn:
        return conn.execute("SELECT * FROM eventos ORDER BY id").fetchall()


def test_cli_grava_o_usuario_do_sistema(monkeypatch, capsys):
    import cli
    monkeypatch.setattr(cli, "_usuario_do_sistema", lambda: "conta-local")
    monkeypatch.setattr(sys, "argv", ["cli.py", "amazon", "apagar-dados", "--motivo", "teste", "--sim"])
    try:
        cli.main()
    finally:
        definir_ator(None)
    assert ultimo("amazon")["ator"] == "cli (conta-local)"


def test_tabela_so_aceita_inclusao():
    registrar_evento("info", "teste", "original")
    with pytest.raises(sqlite3.DatabaseError, match="só aceita inclusão"):
        with conectar() as conn:
            conn.execute("UPDATE eventos SET mensagem = 'trocada'")
    with pytest.raises(sqlite3.DatabaseError, match="só aceita inclusão"):
        with conectar() as conn:
            conn.execute("DELETE FROM eventos")
    assert ultimo("teste")["mensagem"] == "original"


def _sem_gatilhos():
    """Quem tem o arquivo do banco pode tirar os gatilhos; o hash ainda acusa."""
    with conectar() as conn:
        conn.execute("DROP TRIGGER eventos_sem_update")
        conn.execute("DROP TRIGGER eventos_sem_delete")


def test_trilha_confere_e_acusa_linha_alterada():
    for i in range(5):
        registrar_evento("info", "teste", f"evento {i}")
    v = verificar_trilha()
    assert v["quebra"] is None and len(v["ultimo_hash"]) == 64

    _sem_gatilhos()
    with conectar() as conn:
        alvo = conn.execute("SELECT id FROM eventos WHERE mensagem = 'evento 2'").fetchone()[0]
        conn.execute("UPDATE eventos SET mensagem = 'evento 2 adulterado' WHERE id = ?", (alvo,))
    assert verificar_trilha()["quebra"] == alvo


def test_trilha_acusa_linha_apagada_no_meio():
    for i in range(5):
        registrar_evento("info", "teste", f"evento {i}")
    _sem_gatilhos()
    with conectar() as conn:
        apagado = conn.execute("SELECT id FROM eventos WHERE mensagem = 'evento 2'").fetchone()[0]
        conn.execute("DELETE FROM eventos WHERE id = ?", (apagado,))
    assert verificar_trilha()["quebra"] == apagado + 1


def test_eventos_de_antes_da_trilha_contam_a_parte(isolamento, monkeypatch):
    banco = isolamento / "antigo.db"
    with sqlite3.connect(banco) as conn:
        conn.execute("CREATE TABLE eventos (id INTEGER PRIMARY KEY AUTOINCREMENT, nivel TEXT NOT NULL,"
                     " origem TEXT NOT NULL, mensagem TEXT NOT NULL, detalhe_json TEXT,"
                     " ocorrido_em TEXT NOT NULL)")
        conn.execute("INSERT INTO eventos (nivel, origem, mensagem, ocorrido_em)"
                     " VALUES ('info', 'velho', 'antes', '2026-01-01T00:00:00+00:00')")
    monkeypatch.setattr(config, "db_path", str(banco))
    registrar_evento("info", "velho", "sem migração ainda")  # banco antigo: grava como antes
    from db import inicializar
    inicializar()
    registrar_evento("info", "novo", "depois")
    v = verificar_trilha()
    assert (v["eventos"], v["sem_hash"], v["quebra"]) == (3, 2, None)


def test_eventos_em_paralelo_continuam_encadeados():
    def gravar(n):
        for i in range(25):
            registrar_evento("info", "paralelo", f"{n}-{i}")
    linhas = [threading.Thread(target=gravar, args=(n,)) for n in range(4)]
    for t in linhas:
        t.start()
    for t in linhas:
        t.join()
    v = verificar_trilha()
    assert v["quebra"] is None and v["eventos"] >= 100


def test_cli_verificar(monkeypatch, capsys):
    import cli
    registrar_evento("info", "teste", "um")
    monkeypatch.setattr(sys, "argv", ["cli.py", "eventos", "--verificar"])
    try:
        cli.main()
    finally:
        definir_ator(None)
    saida = capsys.readouterr().out
    assert "Trilha confere" in saida and "Último hash" in saida

    _sem_gatilhos()
    with conectar() as conn:
        conn.execute("UPDATE eventos SET mensagem = 'outro'")
    with pytest.raises(SystemExit):
        cli.main()
    assert "NÃO confere" in capsys.readouterr().out


def test_hash_apagado_no_meio_tambem_quebra():
    for i in range(4):
        registrar_evento("info", "teste", f"evento {i}")
    _sem_gatilhos()
    with conectar() as conn:
        alvo = conn.execute("SELECT id FROM eventos WHERE mensagem = 'evento 1'").fetchone()[0]
        conn.execute("UPDATE eventos SET hash = NULL WHERE id = ?", (alvo,))
    assert verificar_trilha()["quebra"] == alvo
