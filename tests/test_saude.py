"""Lote de estrutura E3: checagem de saúde da instância (core/saude.py),
/saude sem login para monitor externo, /api/saude só para admin e
cli.py saude com código de saída."""
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from core import aprovacao, cofre, privacidade, saude
from db import agora, conectar


def _ha(**tempo) -> str:
    return (datetime.now(timezone.utc) - timedelta(**tempo)).isoformat(timespec="seconds")


def ciclo_terminado(quando=None, falhas=0):
    with conectar() as conn:
        conn.execute("INSERT INTO estado_sistema (chave, valor, atualizado_em) VALUES"
                     " ('ciclo_fim', ?, ?) ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor,"
                     " atualizado_em = excluded.atualizado_em", (str(falhas), quando or agora()))


def item(r, nome):
    return next(i for i in r["itens"] if i["nome"] == nome)


@pytest.fixture(autouse=True)
def worker_ligado(monkeypatch):
    monkeypatch.setenv("WORKER_ATIVO", "true")


def test_instancia_saudavel():
    ciclo_terminado()
    r = saude.verificar(completo=True)
    assert r["status"] == "ok", r
    assert {i["nome"] for i in r["itens"]} >= {"banco", "cofre", "chave_lgpd", "worker", "disco", "trilha"}


def test_ciclo_do_worker_registra_o_fim():
    import worker
    worker.ciclo()
    with conectar() as conn:
        linha = conn.execute("SELECT valor, atualizado_em FROM estado_sistema"
                             " WHERE chave = 'ciclo_fim'").fetchone()
    assert linha is not None and int(linha["valor"]) >= 0
    assert datetime.now(timezone.utc) - datetime.fromisoformat(linha["atualizado_em"]) < timedelta(minutes=1)
    assert item(saude.verificar(), "worker")["nivel"] != "falha"


def test_worker_parado_e_falha_e_sem_registro_e_atencao(monkeypatch):
    assert item(saude.verificar(), "worker")["nivel"] == "atencao"
    ciclo_terminado(_ha(hours=2))
    r = saude.verificar()
    assert item(r, "worker")["nivel"] == "falha" and r["status"] == "falha"
    ciclo_terminado(falhas=2)
    assert "2 etapa(s)" in item(saude.verificar(), "worker")["detalhe"]
    monkeypatch.setenv("WORKER_ATIVO", "false")
    assert item(saude.verificar(), "worker")["nivel"] == "ok"


def test_incertas_e_alertas_pedem_atencao():
    ciclo_terminado()
    aid = aprovacao.enfileirar("ajuste_preco", "p", {"item_id": "x", "preco": 1})
    with conectar() as conn:
        conn.execute("UPDATE aprovacoes SET status = 'incerta' WHERE id = ?", (aid,))
        conn.execute("INSERT INTO alertas (tipo, chave, mensagem, aberto_em, atualizado_em)"
                     " VALUES ('dado_velho', 'x', 'm', ?, ?)", (agora(), agora()))
    r = saude.verificar()
    assert item(r, "aprovacoes_incertas")["nivel"] == "atencao"
    assert item(r, "alertas")["nivel"] == "atencao" and r["status"] == "atencao"


def test_chave_lgpd_que_sumiu_com_dado_cifrado_e_falha(monkeypatch):
    with conectar() as conn:
        conn.execute("INSERT INTO pedidos (marketplace, id_externo, quantidade, valor_bruto, estado,"
                     " comprador_nome, criado_em, atualizado_em) VALUES ('mercadolivre', 'P', 1, 1,"
                     " 'NOVO', ?, ?, ?)", (privacidade.cifrar("Pessoa"), agora(), agora()))
    assert item(saude.verificar(), "chave_lgpd")["nivel"] == "ok"
    monkeypatch.delenv("CHAVE_LGPD")
    assert item(saude.verificar(), "chave_lgpd")["nivel"] == "falha"
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CHAVE_LGPD", Fernet.generate_key().decode())  # chave errada
    errada = item(saude.verificar(), "chave_lgpd")
    assert errada["nivel"] == "falha" and "não abre" in errada["detalhe"]


def test_cofre_com_chave_errada_e_falha(monkeypatch):
    from cryptography.fernet import Fernet
    cofre.guardar_segredo("ML_CLIENT_SECRET", "valor-unico-do-cofre-91")
    monkeypatch.setenv("CHAVE_COFRE", Fernet.generate_key().decode())
    r = saude.verificar()
    assert item(r, "cofre")["nivel"] == "falha" and "valor-unico-do-cofre-91" not in item(r, "cofre")["detalhe"]


def test_disco_quase_cheio(monkeypatch):
    Uso = namedtuple("Uso", "total used free")
    monkeypatch.setattr(saude.shutil, "disk_usage", lambda p: Uso(1, 1, 100 * 1024 * 1024))
    assert item(saude.verificar(), "disco")["nivel"] == "falha"
    monkeypatch.setattr(saude.shutil, "disk_usage", lambda p: Uso(1, 1, 500 * 1024 * 1024))
    assert item(saude.verificar(), "disco")["nivel"] == "atencao"


def test_verificacao_quebrada_nao_esconde_as_outras(monkeypatch):
    def quebrar():
        raise ValueError("segredo-interno")
    monkeypatch.setattr(saude, "_disco", quebrar)
    r = saude.verificar()
    assert item(r, "disco") == {"nome": "disco", "nivel": "falha",
                                "detalhe": "a verificação falhou (ValueError)"}
    assert item(r, "banco")["nivel"] == "ok"


def test_saude_publica_sem_login_so_com_o_status(cliente):
    ciclo_terminado()
    r = cliente.get("/saude")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    ciclo_terminado(_ha(hours=2))
    r = cliente.get("/saude")
    assert r.status_code == 503 and r.json() == {"status": "falha"}


def test_detalhes_so_para_admin(cliente, sessao, novo_cliente):
    from core import seguranca
    assert cliente.get("/api/saude").status_code == 401
    admin, _ = sessao
    assert admin.get("/api/saude").json()["status"] in saude.NIVEIS
    seguranca.definir_operador("leitor.beta", "Cavalo-Bateria-31!")
    leitor = novo_cliente()
    leitor.post("/api/login", json={"usuario": "leitor.beta", "senha": "Cavalo-Bateria-31!"})
    assert leitor.get("/api/saude").status_code == 403


def test_saude_publica_nao_grava_nada(cliente):
    ciclo_terminado()
    with conectar() as conn:
        antes = conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
    cliente.get("/saude")
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0] == antes


@pytest.mark.parametrize("quando, codigo", [(None, 0), ("velho", 2)])
def test_cli_saude_com_codigo_de_saida(monkeypatch, capsys, quando, codigo):
    import cli
    ciclo_terminado(_ha(hours=2) if quando else None)
    monkeypatch.setattr(sys, "argv", ["cli.py", "saude", "--completo"])
    with pytest.raises(SystemExit) as saida:
        cli.main()
    assert saida.value.code == codigo
    assert "Status:" in capsys.readouterr().out
