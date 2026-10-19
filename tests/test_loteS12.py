"""Lote S12: ver o que sai antes de aprovar (auditoria, S-01) e escape do
painel também para aspas simples (S-15)."""
import re
from pathlib import Path

from core import aprovacao

HTML = (Path(__file__).resolve().parent.parent / "painel" / "painel.html").read_text(encoding="utf-8")
ESC = chr(27)


def _resposta(texto):
    return aprovacao.enfileirar("resposta_cliente", "Responder pergunta 55: tem azul?",
                                {"question_id": 55, "pergunta": "tem azul?", "resposta": texto})


def test_previa_da_resposta_e_o_texto_exato_que_sai():
    aid = _resposta("Olá! Temos azul, sim.\nEnviamos em 24 h.")
    a = aprovacao.obter_pendente(aid)
    assert aprovacao.previa(a) == "Olá! Temos azul, sim.\nEnviamos em 24 h."


def test_previa_tira_caracteres_de_controle_e_mantem_quebra_de_linha():
    a = aprovacao.obter_pendente(_resposta(f"linha 1{ESC}[2J\nlinha 2{chr(0)}"))
    texto = aprovacao.previa(a)
    assert ESC not in texto and chr(0) not in texto
    assert texto.splitlines()[0].startswith("linha 1") and "linha 2" in texto


def test_previa_do_ajuste_de_preco_e_vazia_para_compra():
    preco = aprovacao.enfileirar("ajuste_preco", "Preço MLB1", {"item_id": "MLB1", "preco": 89.9})
    assert aprovacao.previa(aprovacao.obter_pendente(preco)) == "Preço do anúncio MLB1 passa a 89.9"
    compra = aprovacao.enfileirar("compra_fornecedor", "OC 1", {"pedido_id": 1})
    assert aprovacao.previa(aprovacao.obter_pendente(compra)) is None
    vazia = _resposta("   ")
    assert aprovacao.previa(aprovacao.obter_pendente(vazia)) is None


def test_painel_recebe_a_previa_na_fila(sessao):
    cliente, _ = sessao
    _resposta("Temos azul, sim.")
    itens = cliente.get("/api/pendencias").json()["itens"]
    assert [i["previa"] for i in itens if i["tipo"] == "resposta_cliente"] == ["Temos azul, sim."]


def test_cli_mostra_o_texto_antes_da_decisao(capsys):
    import cli
    _resposta(f"Temos azul.{ESC}[31m\nAbraço.")
    cli.cmd_pendencias(None)
    saida = capsys.readouterr().out
    assert "Sai assim se aprovar:" in saida
    assert "| Temos azul." in saida and "| Abraço." in saida
    assert ESC not in saida


def test_painel_mostra_a_previa_escapada():
    assert "${esc(i.previa)}" in HTML
    assert "i.previa}" not in HTML.replace("esc(i.previa)}", "")


def test_esc_cobre_aspas_simples_e_nao_ha_texto_dentro_de_js_inline():
    linha = next(l for l in HTML.splitlines() if l.startswith("const esc = "))
    assert "\"'\":'&#39;'" in linha
    # Nenhum valor interpolado dentro de string JavaScript em atributo on*:
    # entidade HTML não protege esse contexto (o navegador decodifica antes).
    assert not re.search(r"on[a-z]+=\"[^\"]*'\$\{", HTML)
    assert 'data-copiar="${esc(c.redirect_uri)}"' in HTML
