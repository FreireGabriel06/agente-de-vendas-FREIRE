"""Lote de estrutura E2: aprovação interrompida durante a execução (achado
S-83 da auditoria) e parada limpa do worker. Item preso em 'aprovada' vira
'incerta'; nada é repetido sozinho, salvo a resposta ao cliente que o Mercado
Livre ainda mostra sem resposta."""
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from core import aprovacao
from db import agora, conectar


def _ha(minutos) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat(timespec="seconds")


def item(tipo="resposta_cliente", status="aprovada", ha_minutos=11, pedido_id=None, qid="1"):
    payload = {"question_id": qid, "pergunta": "p", "resposta": "r"} if tipo == "resposta_cliente" \
        else {"pedido_id": pedido_id}
    aid = aprovacao.enfileirar(tipo, f"{tipo} {qid}", payload, pedido_id=pedido_id)
    with conectar() as conn:
        conn.execute("UPDATE aprovacoes SET status = ?, decidido_em = ? WHERE id = ?",
                     (status, _ha(ha_minutos), aid))
    return aid


def status(aid):
    with conectar() as conn:
        return tuple(conn.execute("SELECT status, resultado FROM aprovacoes WHERE id = ?",
                                  (aid,)).fetchone())


def test_aprovada_ha_muito_tempo_vira_incerta_e_avisa():
    velha = item(ha_minutos=aprovacao.EXECUCAO_MAXIMA_MIN + 1)
    recente = item(ha_minutos=1, qid="2")
    pendente = item(status="pendente", qid="3")

    assert aprovacao.recuperar_interrompidas() == [velha]

    assert status(velha)[0] == "incerta" and "confira no marketplace" in status(velha)[1]
    assert status(recente)[0] == "aprovada" and status(pendente)[0] == "pendente"
    with conectar() as conn:
        evento = conn.execute("SELECT nivel, mensagem FROM eventos WHERE origem = 'aprovacao'"
                              " ORDER BY id DESC LIMIT 1").fetchone()
    assert evento["nivel"] == "atencao" and f"resolver {velha}" in evento["mensagem"]
    assert aprovacao.recuperar_interrompidas() == []  # não marca de novo


def test_resolver_incerta():
    saiu, nao_saiu = item(status="incerta"), item(status="incerta", qid="2")
    aprovacao.resolver_incerta(saiu, saiu=True, ator="teste")
    aprovacao.resolver_incerta(nao_saiu, saiu=False, ator="teste")
    assert status(saiu) == ("executada", "Resolvida à mão por teste: a ação saiu.")
    assert status(nao_saiu)[0] == "erro"
    with pytest.raises(aprovacao.JaDecidida):
        aprovacao.resolver_incerta(saiu, saiu=True, ator="teste")
    assert [a.id for a in aprovacao.incertas()] == []


def test_resposta_incerta_volta_para_a_fila_se_a_pergunta_continua_sem_resposta():
    from atendimento import bot
    aid = item(status="incerta", qid="777")
    bot._marcar("777", bot.ENFILEIRADA, aid)

    assert bot.ja_tratada("777") is True  # o ML ainda lista a pergunta: reabre

    assert status(aid)[0] == "erro"
    novas = aprovacao.pendentes("resposta_cliente")
    assert len(novas) == 1 and novas[0].payload["question_id"] == "777"


def test_compra_incerta_impede_reanalisar_o_pedido():
    import worker
    with conectar() as conn:
        pid = conn.execute("INSERT INTO pedidos (marketplace, id_externo, quantidade, valor_bruto,"
                           " estado, criado_em, atualizado_em) VALUES ('mercadolivre', 'X1', 1, 10,"
                           " 'PROBLEMA', ?, ?)", (agora(), agora())).lastrowid
    item(tipo="compra_fornecedor", status="incerta", pedido_id=pid)
    with conectar() as conn:
        motivo = worker._compra_que_impede_reanalise(conn, pid)
    assert "ficou 'incerta'" in motivo


def test_ciclo_recupera_antes_das_outras_etapas(monkeypatch):
    import worker
    velha = item()
    resumo = worker.ciclo()
    assert resumo["interrompidas"] == [velha] and list(resumo)[0] == "interrompidas"


def test_cli_lista_e_resolve(monkeypatch, capsys):
    import cli
    aid = item(status="incerta")
    monkeypatch.setattr(sys, "argv", ["cli.py", "resolver"])
    cli.main()
    assert f"[{aid}] resposta_cliente" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["cli.py", "resolver", str(aid), "executada"])
    cli.main()
    assert status(aid)[0] == "executada"
    assert "resolvida como executada" in capsys.readouterr().out


def test_painel_para_o_worker_ao_sair(monkeypatch):
    import executar
    import uvicorn

    parada = threading.Event()
    terminou = threading.Event()

    def worker_falso():
        parada.wait(10)
        terminou.set()

    monkeypatch.setenv("WORKER_ATIVO", "true")
    monkeypatch.setattr(executar, "PARAR", parada)
    monkeypatch.setattr(executar, "_laco_worker", worker_falso)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    executar.main()
    assert parada.is_set() and terminou.is_set()


def test_falha_do_ciclo_nao_grava_a_mensagem(monkeypatch):
    import executar
    import worker

    class Parada:
        n = 0

        def wait(self, s):
            self.n += 1
            return self.n > 2

        def is_set(self):
            return self.n > 2

    segredo = "-".join(["valor", "decifrado", "secreto"])  # montado na execução, como um real

    def explodir():
        raise RuntimeError(segredo)
    monkeypatch.setattr(worker, "ciclo", explodir)
    executar._laco_worker(Parada())
    with conectar() as conn:
        evento = conn.execute("SELECT mensagem, detalhe_json FROM eventos WHERE origem = 'executar'"
                              " ORDER BY id DESC LIMIT 1").fetchone()
    assert evento["mensagem"] == "Ciclo falhou: falha inesperada (RuntimeError)"
    assert segredo not in (evento["detalhe_json"] or "")
