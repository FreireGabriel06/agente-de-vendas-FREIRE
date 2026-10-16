"""Lote de estrutura E8: o expurgo por retenção roda sozinho, uma vez por dia,
e alcança as perguntas de compradores na fila (achado S-16 da auditoria);
DB_PATH fora da pasta de dados aparece na checagem de saúde (S-35)."""
import json
from datetime import datetime, timedelta, timezone

from core import aprovacao, privacidade, saude
from db import agora, conectar

VELHO = (datetime.now(timezone.utc) - timedelta(days=privacidade.RETENCAO_DIAS + 1)).isoformat(
    timespec="seconds")


def resposta(qid, status, criado_em=VELHO):
    aid = aprovacao.enfileirar("resposta_cliente", f"Responder pergunta {qid}: Meu CPF é 1",
                               {"question_id": qid, "pergunta": "Meu CPF é 1", "resposta": "Sim."})
    with conectar() as conn:
        conn.execute("UPDATE aprovacoes SET status = ?, criado_em = ? WHERE id = ?",
                     (status, criado_em, aid))
    return aid


def linha(aid):
    with conectar() as conn:
        l = conn.execute("SELECT resumo, payload_json FROM aprovacoes WHERE id = ?", (aid,)).fetchone()
    return l["resumo"], json.loads(l["payload_json"])


def test_pergunta_decidida_e_velha_sai_da_fila():
    decidida = resposta("Q1", "executada")
    pendente = resposta("Q2", "pendente")
    recente = resposta("Q3", "recusada", criado_em=agora())

    privacidade.expurgar()

    resumo, payload = linha(decidida)
    assert payload == {"question_id": "Q1", "pergunta": "", "resposta": "Sim."}
    assert resumo == "Responder pergunta Q1 (pergunta expurgada)" and "CPF" not in resumo
    assert linha(pendente)[1]["pergunta"] == "Meu CPF é 1"
    assert linha(recente)[1]["pergunta"] == "Meu CPF é 1"
    privacidade.expurgar()  # de novo: nada muda
    assert linha(decidida)[0] == "Responder pergunta Q1 (pergunta expurgada)"


def test_ciclo_roda_o_expurgo_uma_vez_por_dia(monkeypatch):
    import worker
    chamadas = []
    original = privacidade.expurgar
    monkeypatch.setattr(privacidade, "expurgar", lambda: chamadas.append(1) or original())
    decidida = resposta("Q9", "erro")

    primeiro = worker.ciclo()
    segundo = worker.ciclo()

    assert len(chamadas) == 1 and primeiro["expurgo"] == 0 and segundo["expurgo"] == "já rodou hoje"
    assert linha(decidida)[1]["pergunta"] == ""


def test_db_path_fora_da_pasta_de_dados_pede_atencao(monkeypatch, tmp_path):
    def nivel():
        return next(i for i in saude.verificar()["itens"] if i["nome"] == "banco_na_pasta")["nivel"]
    assert nivel() == "ok"
    monkeypatch.setenv("DB_PATH", "agente.db")  # relativo conta a partir da pasta de dados
    assert nivel() == "ok"
    monkeypatch.setenv("DB_PATH", str(tmp_path / "outro-cliente" / "agente.db"))
    assert nivel() == "atencao"
