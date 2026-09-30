"""Fluxo do demo.py de ponta a ponta: margem -> fila -> aprovação -> COMPRA_ENVIADA."""
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


def test_demo_py_de_ponta_a_ponta(monkeypatch, capsys):
    saida = rodar_demo(monkeypatch, capsys)

    assert estados() == {
        "2000000001": Estado.COMPRA_ENVIADA.value,        # margem boa, aprovado
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
    assert ordem.is_file()
    assert "mercadolivre#2000000001" in ordem.read_text(encoding="utf-8")
    assert "Ordem de compra gravada" in saida

    passos = [(h["de"], h["para"], h["automatico"]) for h in historico(pedido_id)]
    assert passos == [
        ("NOVO", "ANALISADO", 1),
        ("ANALISADO", "AGUARDANDO_APROVACAO", 1),
        ("AGUARDANDO_APROVACAO", "COMPRA_ENVIADA", 0),  # só sai com uma pessoa
    ]


def test_item_da_fila_aprovado_pelo_painel_vira_compra_enviada(monkeypatch, capsys, sessao):
    rodar_demo(monkeypatch, capsys)
    cliente, csrf = sessao

    fila = cliente.get("/api/pendencias").json()
    assert fila["total"] == 1 and fila["bloqueadas"] == 0
    item = fila["itens"][0]
    assert item["resumo"].startswith("[ROTINA] Comprar 1x LUM-114")

    r = cliente.post(f"/api/aprovar/{item['id']}", headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["ok"] is True

    assert estados()["2000000003"] == Estado.COMPRA_ENVIADA.value
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
