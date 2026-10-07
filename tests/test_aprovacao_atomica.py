"""Lote S1 — aprovação atômica e estados terminais (achados N-01/S-02/C-04 e
N-02/S-03/C-05, auditoria de 05/10/2026).

Testes de aceite. Em main f393a21 os marcados [falha hoje] reprovam; depois
do lote S1 todos passam. Dados sintéticos, sem rede (conftest do projeto).

Regra que estes testes fixam:
  - pendente  -> aprovada  só por UMA aprovação (UPDATE condicional);
  - aprovada  -> executada | erro  só pelo próprio executor;
  - pendente  -> recusada  só a partir de 'pendente';
  - quem chega depois recebe aprovacao.JaDecidida (painel: HTTP 409).
"""
import threading

import pytest

from apoio import cabecalho
from core import aprovacao
from db import conectar


def _status(aid):
    with conectar() as c:
        return c.execute("SELECT status, resultado FROM aprovacoes WHERE id = ?", (aid,)).fetchone()


def _eventos(trecho):
    with conectar() as c:
        return [l[0] for l in c.execute("SELECT mensagem FROM eventos") if trecho in l[0]]


def _item(qid="1"):
    return aprovacao.enfileirar("resposta_cliente", f"Responder pergunta {qid}",
                                {"question_id": qid, "pergunta": "p", "resposta": "r"})


def _alinhar_leituras(monkeypatch, partes=2):
    """Faz as duas aprovações lerem 'pendente' antes de qualquer uma reservar
    o item: a janela de corrida fica determinística."""
    barreira = threading.Barrier(partes)
    original = aprovacao.checar

    def checar(item):
        barreira.wait(timeout=5)
        return original(item)

    monkeypatch.setattr(aprovacao, "checar", checar)


# [falha hoje] -------------------------------------------------------------
def test_duas_aprovacoes_simultaneas_executam_uma_vez(monkeypatch):
    executados, erros = [], []

    def executor(payload):
        executados.append(payload["question_id"])
        return "publicado"

    aid = _item()
    _alinhar_leituras(monkeypatch)

    def ir():
        try:
            aprovacao.aprovar(aid, {"resposta_cliente": executor})
        except aprovacao.JaDecidida as e:
            erros.append(e)

    ts = [threading.Thread(target=ir) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert executados == ["1"]
    assert len(erros) == 1
    assert _status(aid)["status"] == "executada"


# [falha hoje] -------------------------------------------------------------
def test_duas_aprovacoes_pelo_painel_executam_uma_vez(abrir_sessao, monkeypatch):
    import worker

    executados = []
    monkeypatch.setitem(worker.EXECUTORES, "resposta_cliente",
                        lambda p: executados.append(p["question_id"]) or "publicado")
    a, csrf_a = abrir_sessao()
    b, csrf_b = abrir_sessao()
    aid = _item("7")
    _alinhar_leituras(monkeypatch)
    codigos = []

    def ir(cliente, csrf):
        codigos.append(cliente.post(f"/api/aprovar/{aid}", headers=cabecalho(csrf)).status_code)

    ts = [threading.Thread(target=ir, args=(a, csrf_a)), threading.Thread(target=ir, args=(b, csrf_b))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert executados == ["7"]
    assert sorted(codigos) == [200, 409]


# [falha hoje] -------------------------------------------------------------
def test_recusa_durante_a_execucao_e_recusada_e_nao_apaga_a_execucao():
    comecou, liberar = threading.Event(), threading.Event()

    def executor(payload):
        comecou.set()
        liberar.wait(5)
        return "publicado"

    aid = _item("2")
    t = threading.Thread(target=aprovacao.aprovar, args=(aid, {"resposta_cliente": executor}))
    t.start()
    try:
        assert comecou.wait(5)
        with pytest.raises(aprovacao.JaDecidida):
            aprovacao.recusar(aid, "mudei de ideia")
    finally:
        liberar.set()
        t.join()
    assert tuple(_status(aid)) == ("executada", "publicado")
    assert not _eventos(f"Ação {aid} recusada")


# [falha hoje] -------------------------------------------------------------
def test_recusar_pelo_painel_item_ja_executado_nao_muda_nada(sessao):
    """Mesmo contrato que o aprovar já tem (test_fluxo_demo): item decidido
    responde 404. Hoje o recusar responde 200 e grava um evento falso."""
    cliente, csrf = sessao
    aid = _item("3")
    aprovacao.aprovar(aid, {"resposta_cliente": lambda p: "publicado"})
    r = cliente.post(f"/api/recusar/{aid}", json={"motivo": "tarde"}, headers=cabecalho(csrf))
    assert r.status_code == 404
    assert _status(aid)["status"] == "executada"
    assert not _eventos(f"Ação {aid} recusada")


# [falha hoje] -------------------------------------------------------------
def test_recusar_pelo_painel_id_inexistente_responde_404_sem_evento(sessao):
    cliente, csrf = sessao
    r = cliente.post("/api/recusar/987654", json={"motivo": "x"}, headers=cabecalho(csrf))
    assert r.status_code == 404
    assert not _eventos("Ação 987654 recusada")


# [falha hoje] -------------------------------------------------------------
def test_executor_nao_rebaixa_item_recusado_por_fora(monkeypatch):
    """Mesmo que alguém grave 'recusada' direto no banco no meio da execução,
    o fim da execução não some: o executor só conclui item 'aprovada', e a
    divergência fica registrada como evento de atenção."""
    def executor(payload):
        with conectar() as c:
            c.execute("UPDATE aprovacoes SET status = 'recusada' WHERE id = ?", (aid,))
        return "publicado"

    aid = _item("4")
    aprovacao.aprovar(aid, {"resposta_cliente": executor})
    assert _status(aid)["status"] == "recusada"
    assert _eventos(f"Ação {aid}") and any("diverg" in m for m in _eventos(f"Ação {aid}"))


# regressão ----------------------------------------------------------------
def test_recusar_pendente_continua_funcionando(sessao):
    cliente, csrf = sessao
    aid = _item("5")
    r = cliente.post(f"/api/recusar/{aid}", json={"motivo": "não"}, headers=cabecalho(csrf))
    assert r.status_code == 200
    assert _status(aid)["status"] == "recusada"


def test_aprovar_item_recusado_nao_executa():
    executados = []
    aid = _item("6")
    aprovacao.recusar(aid, "não")
    with pytest.raises(ValueError):
        aprovacao.aprovar(aid, {"resposta_cliente": lambda p: executados.append(1) or "x"})
    assert executados == []


def test_falha_do_executor_marca_erro():
    aid = _item("8")

    def executor(payload):
        raise RuntimeError("falha sintética")

    with pytest.raises(RuntimeError):
        aprovacao.aprovar(aid, {"resposta_cliente": executor})
    assert _status(aid)["status"] == "erro"


def test_ja_decidida_e_value_error_para_quem_ja_trata_value_error():
    assert issubclass(aprovacao.JaDecidida, ValueError)
