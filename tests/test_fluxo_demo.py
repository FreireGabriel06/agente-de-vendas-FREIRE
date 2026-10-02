"""Fluxo do demo.py de ponta a ponta: margem -> fila -> aprovação.

Sem simulação, a compra aprovada vai para COMPRA_ENVIADA. Com MODO_SIMULACAO
ligado (o padrão), a ordem sai marcada como teste e o pedido continua em
AGUARDANDO_APROVACAO: nada foi comprado."""
import runpy
import sys
from pathlib import Path

import pytest

import worker
from apoio import USUARIO, cabecalho
from config import config
from core.estados import Estado, TransicaoInvalida, historico, transicionar
from db import conectar

RAIZ = Path(__file__).resolve().parent.parent


def rodar_demo(monkeypatch, capsys):
    """Roda o demo.py como script, no banco deste teste. O script mexe no
    sys.path; a cópia devolvida ao fim do teste desfaz isso."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    runpy.run_path(str(RAIZ / "demo.py"), run_name="__main__")
    return capsys.readouterr().out


def estados():
    with conectar() as conn:
        return {l["id_externo"]: l["estado"] for l in conn.execute(
            "SELECT id_externo, estado FROM pedidos ORDER BY id")}


@pytest.mark.parametrize("simulacao", [True, False])
def test_demo_py_de_ponta_a_ponta(monkeypatch, capsys, nota_fiscal_confirmada, simulacao):
    # Produtos do demo.py já dizem que não são de categoria regulada; o
    # vendedor confirmou a nota fiscal.
    monkeypatch.setattr(config, "modo_simulacao", simulacao)
    saida = rodar_demo(monkeypatch, capsys)

    aprovado = Estado.AGUARDANDO_APROVACAO if simulacao else Estado.COMPRA_ENVIADA
    assert estados() == {
        "2000000001": aprovado.value,                     # margem boa, aprovado
        "2000000002": Estado.RECUSADO_MARGEM.value,       # margem abaixo do mínimo
        "2000000003": Estado.AGUARDANDO_APROVACAO.value,  # na fila, sem decisão
        "2000000004": Estado.PROBLEMA.value,              # fornecedor lento demais
    }
    with conectar() as conn:
        margens = {l["id_externo"]: l["margem_prevista"] for l in conn.execute(
            "SELECT id_externo, margem_prevista FROM pedidos")}
        aprovacoes = [tuple(a) for a in conn.execute(
            "SELECT pedido_id, status FROM aprovacoes ORDER BY id")]
        pedido_id = conn.execute(
            "SELECT id FROM pedidos WHERE id_externo = '2000000001'").fetchone()["id"]
    minimo = config.negocio.margem_minima_pct
    assert margens["2000000002"] < minimo <= margens["2000000001"]
    assert aprovacoes == [(pedido_id, "executada"), (pedido_id + 2, "pendente")]

    ordem = worker.PASTA_ORDENS / f"oc_{pedido_id}.txt"
    texto = ordem.read_text(encoding="utf-8")
    assert "mercadolivre#2000000001" in texto
    assert texto.startswith(worker.AVISO_SIMULACAO_OC) is simulacao
    assert "Ordem de compra gravada" in saida
    assert ("Modo simulação ligado" in saida) is simulacao

    passos = [(h["de"], h["para"], h["automatico"]) for h in historico(pedido_id)]
    esperados = [
        ("NOVO", "ANALISADO", 1),
        ("ANALISADO", "AGUARDANDO_APROVACAO", 1),
    ]
    if not simulacao:
        esperados.append(("AGUARDANDO_APROVACAO", "COMPRA_ENVIADA", 0))  # só sai com uma pessoa
    assert passos == esperados


@pytest.mark.parametrize("simulacao", [True, False])
def test_item_da_fila_aprovado_pelo_painel(monkeypatch, capsys, sessao, nota_fiscal_confirmada,
                                           simulacao):
    monkeypatch.setattr(config, "modo_simulacao", simulacao)
    rodar_demo(monkeypatch, capsys)
    cliente, csrf = sessao

    fila = cliente.get("/api/pendencias").json()
    assert fila["total"] == 1 and fila["bloqueadas"] == 0
    item = fila["itens"][0]
    assert item["resumo"].startswith("[ROTINA] Comprar 1x LUM-114")

    r = cliente.post(f"/api/aprovar/{item['id']}", headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["simulado"] is simulacao

    # Em simulação nada foi comprado: o pedido continua esperando a compra.
    esperado = Estado.AGUARDANDO_APROVACAO if simulacao else Estado.COMPRA_ENVIADA
    assert estados()["2000000003"] == esperado.value
    assert cliente.get("/api/pendencias").json()["total"] == 0
    with conectar() as conn:
        pedido_id = conn.execute(
            "SELECT id FROM pedidos WHERE id_externo = '2000000003'").fetchone()["id"]
        liberada = conn.execute("SELECT 1 FROM eventos WHERE mensagem = ?",
                                (f"Ação {item['id']} liberada por {USUARIO}",)).fetchone()
    assert (worker.PASTA_ORDENS / f"oc_{pedido_id}.txt").is_file()
    assert liberada is not None

    # Aprovar de novo não compra duas vezes.
    assert cliente.post(f"/api/aprovar/{item['id']}", headers=cabecalho(csrf)).status_code == 404


def test_transicao_fora_da_tabela_e_recusada(monkeypatch, capsys):
    rodar_demo(monkeypatch, capsys)
    with conectar() as conn:
        recusado = conn.execute(
            "SELECT id FROM pedidos WHERE id_externo = '2000000002'").fetchone()["id"]

    with pytest.raises(TransicaoInvalida):
        transicionar(recusado, Estado.COMPRA_ENVIADA, "pular a fila")
    assert estados()["2000000002"] == Estado.RECUSADO_MARGEM.value
