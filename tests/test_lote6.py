"""Lote 6: cadastro de produtos e fornecedores sem SQL, dinheiro em Decimal.

1. Rotas do cadastro atrás do login e do CSRF (401 sem sessão, 403 sem CSRF).
2. Cada regra de validação: um caso que passa e um que é recusado (422, em
   português, campo a campo); SKU repetido 409; campo desconhecido recusado.
3. Dinheiro: Decimal do começo ao fim, moeda ISO 4217 obrigatória no dado
   novo, banco antigo migrado com a moeda NULL, margem recusada entre moedas.
4. Datas gravadas com o fuso (UTC, +00:00).
5. cli.py produto/fornecedor com a mesma validação da API.
6. Desativar em vez de apagar: as referências continuam válidas; produto
   desativado não é comprado.
7. Ordem refeita (troca de fornecedor, custo alterado, volta da simulação)
   com a margem refeita pelo custo de agora; pedido em PROBLEMA de volta à
   análise com pedido reanalisar; a moeda na API de pedidos e da fila.

Tudo com dados sintéticos, no banco temporário de cada teste.
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import config
import worker
from apoio import USUARIO, cabecalho
from core import aprovacao, cadastro, conformidade, dinheiro
from core.estados import Estado, historico, transicionar
from db import agora, conectar
from inteligencia import precificacao


# ------------------------------------------------------------------ apoio

def fornecedor_valido(**extra) -> dict:
    dados = {"nome": "Fornecedor Teste", "canal": "email",
             "contato": "pedidos@fornecedor.example", "prazo_dias": 4}
    dados.update(extra)
    return dados


def produto_valido(**extra) -> dict:
    dados = {"sku": "ORG-001", "titulo": "Organizador de gaveta",
             "custo_fornecedor": {"valor": "18.50", "moeda": "BRL"}, "peso_kg": "0.4"}
    dados.update(extra)
    return dados


def criar_fornecedor(cliente, csrf, **extra) -> dict:
    r = cliente.post("/api/fornecedores", json=fornecedor_valido(**extra), headers=cabecalho(csrf))
    assert r.status_code == 201, r.text
    return r.json()


def criar_produto(cliente, csrf, **extra) -> dict:
    r = cliente.post("/api/produtos", json=produto_valido(**extra), headers=cabecalho(csrf))
    assert r.status_code == 201, r.text
    return r.json()


def erros(resposta) -> dict:
    """{campo: mensagem} de uma resposta 422."""
    assert resposta.status_code == 422, resposta.text
    corpo = resposta.json()
    assert "Traceback" not in resposta.text
    return {e["campo"]: e["mensagem"] for e in corpo["erros"]}


def linha(sql: str, *parametros):
    with conectar() as conn:
        return conn.execute(sql, parametros).fetchone()


def contar(tabela: str) -> int:
    return linha(f"SELECT COUNT(*) FROM {tabela}")[0]


def inserir_pedido(produto_id, valor="54.90", moeda="BRL", id_externo="6000000001", **extra):
    colunas = {"marketplace": "mercadolivre", "id_externo": id_externo, "produto_id": produto_id,
               "quantidade": 1, "valor_bruto": float(valor), "valor_bruto_dec": valor,
               "valor_bruto_moeda": moeda, "estado": Estado.NOVO.value,
               "criado_em": agora(), "atualizado_em": agora(), **extra}
    with conectar() as conn:
        return conn.execute(f"INSERT INTO pedidos ({', '.join(colunas)}) VALUES "
                            f"({', '.join('?' * len(colunas))})",
                            tuple(colunas.values())).lastrowid


def rodar_cli(monkeypatch, capsys, *argumentos) -> tuple[int, str, str]:
    """cli.main() no banco deste teste: (código de saída, saída, erro)."""
    import cli

    monkeypatch.setattr(sys, "argv", ["cli.py", *argumentos])
    try:
        cli.main()
        codigo = 0
    except SystemExit as e:
        codigo = e.code if isinstance(e.code, int) else 1
    saida = capsys.readouterr()
    return codigo, saida.out, saida.err


# ========================================== 1. login e CSRF em toda rota nova

ROTAS_DE_LEITURA = [
    ("GET", "/api/produtos"), ("GET", "/api/produtos/1"),
    ("GET", "/api/fornecedores"), ("GET", "/api/fornecedores/1"),
]
ROTAS_DE_ESCRITA = [
    ("POST", "/api/produtos", produto_valido()),
    ("PATCH", "/api/produtos/1", {"titulo": "Outro"}),
    ("POST", "/api/produtos/1/desativar", None),
    ("POST", "/api/fornecedores", fornecedor_valido()),
    ("PATCH", "/api/fornecedores/1", {"prazo_dias": 6}),
    ("POST", "/api/fornecedores/1/desativar", None),
]


@pytest.mark.parametrize("metodo, caminho, corpo",
                         [(m, c, None) for m, c in ROTAS_DE_LEITURA] + ROTAS_DE_ESCRITA)
def test_rota_do_cadastro_sem_sessao_responde_401(cliente, metodo, caminho, corpo):
    r = cliente.request(metodo, caminho, json=corpo)
    assert r.status_code == 401
    assert r.json() == {"detail": "Sessão necessária."}
    assert contar("produtos") == 0 and contar("fornecedores") == 0


@pytest.mark.parametrize("metodo, caminho, corpo", ROTAS_DE_ESCRITA)
def test_escrita_do_cadastro_sem_csrf_responde_403_e_nao_grava(sessao, metodo, caminho, corpo):
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto = criar_produto(cliente, csrf, sku="BASE-1", fornecedor_id=fornecedor["id"])
    antes = (linha("SELECT * FROM produtos WHERE id = 1"),
             linha("SELECT * FROM fornecedores WHERE id = 1"), contar("produtos"))
    assert produto["id"] == 1 and fornecedor["id"] == 1

    for cabecalhos in ({}, cabecalho("token-errado"), cabecalho("")):
        r = cliente.request(metodo, caminho, json=corpo, headers=cabecalhos)
        assert r.status_code == 403, cabecalhos
        assert r.json() == {"detail": "Token CSRF inválido."}

    depois = (linha("SELECT * FROM produtos WHERE id = 1"),
              linha("SELECT * FROM fornecedores WHERE id = 1"), contar("produtos"))
    assert tuple(map(tuple, depois[:2])) == tuple(map(tuple, antes[:2]))
    assert depois[2] == antes[2]


@pytest.mark.parametrize("metodo, caminho", ROTAS_DE_LEITURA)
def test_leitura_do_cadastro_com_sessao_nao_pede_csrf(sessao, metodo, caminho):
    cliente, csrf = sessao
    criar_produto(cliente, csrf, fornecedor_id=criar_fornecedor(cliente, csrf)["id"])
    assert cliente.request(metodo, caminho).status_code == 200


def test_nao_existe_rota_de_apagar(sessao):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf)
    fornecedor = criar_fornecedor(cliente, csrf)
    for caminho in (f"/api/produtos/{produto['id']}", f"/api/fornecedores/{fornecedor['id']}"):
        assert cliente.delete(caminho, headers=cabecalho(csrf)).status_code == 405
    assert contar("produtos") == 1 and contar("fornecedores") == 1


# ============================================ 2. criar, ler, listar, editar

def test_fornecedor_criado_lido_listado_e_editado(sessao):
    cliente, csrf = sessao
    criado = criar_fornecedor(cliente, csrf, pedido_minimo={"valor": "100", "moeda": "BRL"},
                              observacoes="Entrega em caixa fechada.")
    assert criado["nome"] == "Fornecedor Teste" and criado["canal"] == "email"
    assert criado["pedido_minimo"] == {"valor": "100.00", "moeda": "BRL"}
    assert criado["ativo"] is True

    assert cliente.get(f"/api/fornecedores/{criado['id']}").json() == criado
    lista = cliente.get("/api/fornecedores").json()
    assert lista["total"] == 1 and lista["fornecedores"] == [criado]

    r = cliente.patch(f"/api/fornecedores/{criado['id']}", json={"prazo_dias": 6, "canal": "portal"},
                      headers=cabecalho(csrf))
    assert r.status_code == 200, r.text
    editado = r.json()
    assert (editado["prazo_dias"], editado["canal"]) == (6, "portal")
    assert editado["contato"] == criado["contato"]  # o que não foi enviado não muda
    assert editado["pedido_minimo"] == criado["pedido_minimo"]

    # null limpa um campo opcional
    r = cliente.patch(f"/api/fornecedores/{criado['id']}",
                      json={"observacoes": None, "pedido_minimo": None}, headers=cabecalho(csrf))
    assert (r.json()["observacoes"], r.json()["pedido_minimo"]) == (None, None)


def test_produto_criado_lido_listado_e_editado(sessao):
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    criado = criar_produto(cliente, csrf, fornecedor_id=fornecedor["id"], categoria_ml="MLB1234")
    assert criado["sku"] == "ORG-001" and criado["fornecedor_id"] == fornecedor["id"]
    assert criado["custo_fornecedor"] == {"valor": "18.50", "moeda": "BRL"}
    assert criado["peso_kg"] == "0.4" and criado["ativo"] is True
    assert (criado["categoria_regulada"], criado["habilitacao_confirmada"],
            criado["reembalagem_confirmada"]) == (None, None, None)

    assert cliente.get(f"/api/produtos/{criado['id']}").json() == criado
    assert cliente.get("/api/produtos").json() == {"produtos": [criado], "total": 1}

    r = cliente.patch(f"/api/produtos/{criado['id']}",
                      json={"titulo": "Organizador 8 divisórias",
                            "custo_fornecedor": {"valor": "19.9", "moeda": "BRL"}},
                      headers=cabecalho(csrf))
    assert r.status_code == 200, r.text
    assert r.json()["titulo"] == "Organizador 8 divisórias"
    assert r.json()["custo_fornecedor"] == {"valor": "19.90", "moeda": "BRL"}
    assert r.json()["sku"] == "ORG-001"


@pytest.mark.parametrize("caminho", ["/api/produtos/999", "/api/fornecedores/999",
                                     "/api/produtos/abc", "/api/fornecedores/-1",
                                     "/api/produtos/0", "/api/produtos/1e3"])
def test_id_que_nao_existe_responde_404_em_portugues(sessao, caminho):
    cliente, csrf = sessao
    r = cliente.get(caminho)
    assert r.status_code == 404
    assert r.json()["detail"].endswith("não encontrado.")
    assert cliente.patch(caminho, json={"titulo": "x"} if "produtos" in caminho
                         else {"nome": "x"}, headers=cabecalho(csrf)).status_code == 404
    assert cliente.post(caminho + "/desativar", headers=cabecalho(csrf)).status_code == 404


# ========================================================= 3. validação

def test_sku_e_aparado(sessao):
    cliente, csrf = sessao
    assert criar_produto(cliente, csrf, sku="  ORG-002 \t")["sku"] == "ORG-002"
    assert linha("SELECT sku FROM produtos")[0] == "ORG-002"


@pytest.mark.parametrize("sku, mensagem", [
    ("", "não pode ficar vazio."),
    ("    ", "não pode ficar vazio."),
    ("A" * 65, "use no máximo 64 caracteres (veio com 65)."),
    (123, "use texto."),
    (None, "não pode ser null."),
    ("ORG\n001", "tem caractere de controle (quebra de linha, tabulação); tire-o."),
])
def test_sku_vazio_longo_ou_que_nao_e_texto_e_recusado(sessao, sku, mensagem):
    cliente, csrf = sessao
    r = cliente.post("/api/produtos", json=produto_valido(sku=sku), headers=cabecalho(csrf))
    assert erros(r) == {"sku": mensagem}
    assert r.json()["detail"] == "Dados inválidos: confira os campos."
    assert contar("produtos") == 0


def test_sku_no_limite_passa(sessao):
    cliente, csrf = sessao
    assert criar_produto(cliente, csrf, sku="A" * 64)["sku"] == "A" * 64


CONTROLE = "tem caractere de controle (quebra de linha, tabulação); tire-o."


@pytest.mark.parametrize("invisivel", ["\x85", " ", " ", "‮", "‏", "\x9b",
                                       "\x00", "\x7f"],
                         ids=["NEL", "LS", "PS", "RLO", "RLM", "CSI", "NUL", "DEL"])
def test_texto_recusa_controle_c1_separador_e_marca_de_direcao(sessao, invisivel):
    """U+0085 vira quebra de linha no splitlines(); U+202E inverte o resto da
    linha da fila na tela. Nenhum campo de texto aceita, nem observacoes."""
    cliente, csrf = sessao
    texto = f"AB{invisivel}CD"
    r = cliente.post("/api/produtos", json=produto_valido(sku=texto, titulo=texto),
                     headers=cabecalho(csrf))
    assert erros(r) == {"sku": CONTROLE, "titulo": CONTROLE}
    r = cliente.post("/api/fornecedores", json=fornecedor_valido(
        nome=texto, contato=texto, observacoes=f"linha 1\n{texto}"), headers=cabecalho(csrf))
    assert erros(r) == {"nome": CONTROLE, "contato": CONTROLE, "observacoes": CONTROLE}
    assert contar("produtos") == 0 and contar("fornecedores") == 0


def test_texto_aceita_acento_espaco_rigido_e_quebra_em_observacoes(sessao):
    cliente, csrf = sessao
    f = criar_fornecedor(cliente, csrf, nome="Fábrica São João Ltda",
                         observacoes="Entrega:\r\n\tcaixa fechada")
    assert f["nome"] == "Fábrica São João Ltda"
    assert f["observacoes"] == "Entrega:\r\n\tcaixa fechada"


@pytest.mark.parametrize("campo, maximo", [("nome", 120), ("observacoes", 1000)])
def test_texto_do_fornecedor_no_limite_passa_e_acima_e_recusado(sessao, campo, maximo):
    cliente, csrf = sessao
    assert len(criar_fornecedor(cliente, csrf, **{campo: "n" * maximo})[campo]) == maximo
    r = cliente.post("/api/fornecedores", json=fornecedor_valido(**{campo: "n" * (maximo + 1)}),
                     headers=cabecalho(csrf))
    assert erros(r) == {campo: f"use no máximo {maximo} caracteres (veio com {maximo + 1})."}
    assert contar("fornecedores") == 1


def test_categoria_ml_no_limite_passa_e_acima_e_recusada(sessao):
    cliente, csrf = sessao
    assert criar_produto(cliente, csrf, categoria_ml="M" * 40)["categoria_ml"] == "M" * 40
    r = cliente.post("/api/produtos", json=produto_valido(sku="ORG-002", categoria_ml="M" * 41),
                     headers=cabecalho(csrf))
    assert erros(r) == {"categoria_ml": "use no máximo 40 caracteres (veio com 41)."}


def test_pedido_minimo_negativo_e_recusado(sessao):
    cliente, csrf = sessao
    r = cliente.post("/api/fornecedores", json=fornecedor_valido(
        pedido_minimo={"valor": "-0.01", "moeda": "BRL"}), headers=cabecalho(csrf))
    assert erros(r) == {"pedido_minimo.valor": "não pode ser negativo."}


@pytest.mark.parametrize("fornecedor_id", [2 ** 63, 0, -1])
def test_fornecedor_id_fora_do_inteiro_do_sqlite_e_recusado_sem_erro_500(sessao, fornecedor_id):
    cliente, csrf = sessao
    r = cliente.post("/api/produtos", json=produto_valido(fornecedor_id=fornecedor_id),
                     headers=cabecalho(csrf))
    assert erros(r) == {"fornecedor_id": "use o id de um fornecedor (inteiro positivo)."}


def test_ativo_null_de_banco_antigo_conta_como_ativo(sessao):
    cliente, csrf = sessao
    with conectar() as conn:
        conn.execute("INSERT INTO fornecedores (nome, canal, contato, prazo_dias, ativo)"
                     " VALUES ('Antigo', 'email', 'a@b.example', 4, NULL)")
        conn.execute("INSERT INTO produtos (sku, titulo, custo_fornecedor, ativo, criado_em)"
                     " VALUES ('VELHO-1', 'Antigo', 18.5, NULL, ?)", (agora(),))
    assert cliente.get("/api/fornecedores/1").json()["ativo"] is True
    assert cliente.get("/api/produtos/1").json()["ativo"] is True
    assert [f["id"] for f in cliente.get("/api/fornecedores?ativo=true").json()["fornecedores"]] == [1]
    assert [p["id"] for p in cliente.get("/api/produtos?ativo=true").json()["produtos"]] == [1]
    assert cliente.get("/api/produtos?ativo=false").json()["produtos"] == []


def test_sku_repetido_que_escapa_da_checagem_vira_409_pelo_indice_unico(monkeypatch, sessao):
    """Dois cadastros ao mesmo tempo passam os dois pela checagem antes de
    gravar: o índice único do banco segura, e a resposta é 409, não 500."""
    cliente, csrf = sessao
    with conectar() as conn:
        unicos = [l["name"] for l in conn.execute("PRAGMA index_list(produtos)") if l["unique"]]
        colunas = {nome: [c["name"] for c in conn.execute(f"PRAGMA index_info('{nome}')")]
                   for nome in unicos}
    assert ["sku"] in colunas.values()

    criar_produto(cliente, csrf, sku="ORG-001")
    outro = criar_produto(cliente, csrf, sku="ORG-002")
    monkeypatch.setattr(cadastro, "_checar_sku", lambda *args, **kwargs: None)

    r = cliente.post("/api/produtos", json=produto_valido(sku="ORG-001"), headers=cabecalho(csrf))
    assert r.status_code == 409
    assert r.json()["erros"] == [{"campo": "sku", "mensagem": r.json()["detail"]}]
    r = cliente.patch(f"/api/produtos/{outro['id']}", json={"sku": "ORG-001"},
                      headers=cabecalho(csrf))
    assert r.status_code == 409 and r.json()["erros"][0]["campo"] == "sku"
    with conectar() as conn:
        assert [l[0] for l in conn.execute("SELECT sku FROM produtos ORDER BY id")] == [
            "ORG-001", "ORG-002"]


@pytest.mark.parametrize("sku", ["X;calc", "X$(calc)", 'a"b c', "A&B", "-X", "Q 1|calc",
                                 "x`calc`", "AÇO-1"])
def test_saida_para_colar_no_terminal_nao_leva_sku_que_o_shell_interpreta(sku):
    saida = conformidade._no_produto({"sku": sku}, "reembalagem_confirmada", True)
    assert saida.startswith("python cli.py produto editar SKU --reembalagem sim (troque SKU "
                            "pelo SKU do produto")
    assert f"WHERE sku = '{sku}';" in saida  # o SKU de verdade fica no SQL, entre aspas de SQL
    for seguro in ("ORG-001", "lum_114.b"):
        assert conformidade._no_produto({"sku": seguro}, "reembalagem_confirmada", True) \
            .startswith(f"python cli.py produto editar {seguro} --reembalagem sim (ou PATCH")


def test_saida_do_worker_sem_moeda_nao_leva_sku_que_o_shell_interpreta():
    with conectar() as conn:
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, peso_kg, criado_em)"
            " VALUES ('Q 1;calc', 'Perigoso', 18.5, 0.4, ?)", (agora(),)).lastrowid
    pedido = inserir_pedido(produto)
    assert worker.analisar_novos() == 0
    motivo = historico(pedido)[-1]["motivo"]
    assert "python cli.py produto editar SKU --custo VALOR --moeda BRL (troque SKU pelo SKU " \
           f"do produto {produto}" in motivo
    assert "Q 1;calc" not in motivo


def test_sku_repetido_responde_409_na_criacao_e_na_edicao(sessao):
    cliente, csrf = sessao
    primeiro = criar_produto(cliente, csrf, sku="ORG-001")
    segundo = criar_produto(cliente, csrf, sku="ORG-002")

    r = cliente.post("/api/produtos", json=produto_valido(sku=" ORG-001 "), headers=cabecalho(csrf))
    assert r.status_code == 409
    assert r.json()["erros"] == [{"campo": "sku", "mensagem": r.json()["detail"]}]
    assert "ORG-001" in r.json()["detail"] and f"id {primeiro['id']}" in r.json()["detail"]

    r = cliente.patch(f"/api/produtos/{segundo['id']}", json={"sku": "ORG-001"},
                      headers=cabecalho(csrf))
    assert r.status_code == 409
    assert r.json()["detail"] == f"Já existe um produto com o SKU 'ORG-001' (id {primeiro['id']})."
    assert linha("SELECT sku FROM produtos WHERE id = ?", segundo["id"])[0] == "ORG-002"
    # Manter o próprio SKU não é conflito.
    r = cliente.patch(f"/api/produtos/{segundo['id']}", json={"sku": "ORG-002"},
                      headers=cabecalho(csrf))
    assert r.status_code == 200
    assert contar("produtos") == 2


def test_sku_diferencia_maiusculas_de_minusculas_como_o_worker(sessao):
    """A regra é a do worker, que acha o produto pelo seller_sku exato do
    pedido: ORG-001 e org-001 são dois produtos, e cada pedido vai para o seu.
    Mudar isso aqui sem mudar a busca do worker ligaria pedido a produto errado."""
    from apoio import COMPRADOR, MercadoLivreFalso

    cliente, csrf = sessao
    maiusculo = criar_produto(cliente, csrf, sku="ORG-001")
    minusculo = criar_produto(cliente, csrf, sku="org-001")
    assert minusculo["id"] != maiusculo["id"] and contar("produtos") == 2
    terceiro = criar_produto(cliente, csrf, sku="ORG-002")
    r = cliente.patch(f"/api/produtos/{terceiro['id']}", json={"sku": "Org-001"},
                      headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["sku"] == "Org-001"

    brutos = [{"id": 990001 + i, "total_amount": 54.9, "currency_id": "BRL",
               "order_items": [{"item": {"seller_sku": sku}, "quantity": 1}],
               "buyer": dict(COMPRADOR), "shipping": {"id": 1}}
              for i, sku in enumerate(("org-001", "ORG-001", "Org-001"))]
    assert worker.ingerir_mercadolivre(MercadoLivreFalso(brutos)) == 3
    with conectar() as conn:
        ligados = [l[0] for l in conn.execute("SELECT produto_id FROM pedidos ORDER BY id")]
    assert ligados == [minusculo["id"], maiusculo["id"], terceiro["id"]]


def test_titulo_no_limite_passa_e_acima_e_recusado(sessao):
    cliente, csrf = sessao
    assert len(criar_produto(cliente, csrf, titulo="T" * 200)["titulo"]) == 200
    r = cliente.post("/api/produtos", json=produto_valido(sku="ORG-002", titulo="T" * 201),
                     headers=cabecalho(csrf))
    assert erros(r) == {"titulo": "use no máximo 200 caracteres (veio com 201)."}


@pytest.mark.parametrize("canal", cadastro.CANAIS)
def test_canal_do_conjunto_passa(sessao, canal):
    cliente, csrf = sessao
    assert criar_fornecedor(cliente, csrf, canal=canal)["canal"] == canal


@pytest.mark.parametrize("canal", ["telegram", "EMAIL", "", None, 1])
def test_canal_fora_do_conjunto_e_recusado(sessao, canal):
    cliente, csrf = sessao
    r = cliente.post("/api/fornecedores", json=fornecedor_valido(canal=canal),
                     headers=cabecalho(csrf))
    assert erros(r) == {"canal": "use um destes: email, whatsapp, api, portal."}


def test_contato_no_limite_passa_e_acima_e_recusado(sessao):
    cliente, csrf = sessao
    assert len(criar_fornecedor(cliente, csrf, contato="c" * 200)["contato"]) == 200
    r = cliente.post("/api/fornecedores", json=fornecedor_valido(contato="c" * 201),
                     headers=cabecalho(csrf))
    assert erros(r) == {"contato": "use no máximo 200 caracteres (veio com 201)."}


@pytest.mark.parametrize("prazo", [1, 365])
def test_prazo_na_faixa_passa(sessao, prazo):
    cliente, csrf = sessao
    assert criar_fornecedor(cliente, csrf, prazo_dias=prazo)["prazo_dias"] == prazo


@pytest.mark.parametrize("prazo, mensagem", [
    (0, "use um número inteiro de 1 a 365."),
    (366, "use um número inteiro de 1 a 365."),
    (-3, "use um número inteiro de 1 a 365."),
    ("5", "use um número inteiro."),
    (5.5, "use um número inteiro."),
    (True, "use um número inteiro."),
    (None, "use um número inteiro."),
])
def test_prazo_fora_da_faixa_ou_que_nao_e_inteiro_e_recusado(sessao, prazo, mensagem):
    cliente, csrf = sessao
    r = cliente.post("/api/fornecedores", json=fornecedor_valido(prazo_dias=prazo),
                     headers=cabecalho(csrf))
    assert erros(r) == {"prazo_dias": mensagem}


@pytest.mark.parametrize("peso, gravado", [("0.4", "0.4"), ("0.001", "0.001"), (2, "2"),
                                           ("1000", "1000")])
def test_peso_maior_que_zero_passa(sessao, peso, gravado):
    cliente, csrf = sessao
    assert criar_produto(cliente, csrf, peso_kg=peso)["peso_kg"] == gravado


# NaN e infinito em texto: o Decimal() aceitaria, a validação não.
NAO_FINITOS = ("NaN", "nan", "-NaN", "sNaN", "Infinity", "-Infinity", "inf", "+Inf")


@pytest.mark.parametrize("peso, mensagem", [
    ("0", "precisa ser maior que zero."),
    (0, "precisa ser maior que zero."),
    ("-1", "precisa ser maior que zero."),
    ("0.0001", "use no máximo 3 casas decimais."),
    ("1000.5", "use no máximo 1000."),
    ("0,4", 'use ponto como separador decimal, por exemplo "18.50".'),
    ("leve", 'use um número decimal, por exemplo "18.50".'),
    (None, "não pode ser null."),
    *[(texto, 'use um número decimal, por exemplo "18.50".') for texto in NAO_FINITOS],
])
def test_peso_zero_negativo_ou_invalido_e_recusado(sessao, peso, mensagem):
    cliente, csrf = sessao
    r = cliente.post("/api/produtos", json=produto_valido(peso_kg=peso), headers=cabecalho(csrf))
    assert erros(r) == {"peso_kg": mensagem}


def test_fornecedor_do_produto_precisa_existir_e_estar_ativo(sessao):
    cliente, csrf = sessao
    ativo = criar_fornecedor(cliente, csrf)
    desativado = criar_fornecedor(cliente, csrf, nome="Outro")
    cliente.post(f"/api/fornecedores/{desativado['id']}/desativar", headers=cabecalho(csrf))

    assert criar_produto(cliente, csrf, fornecedor_id=ativo["id"])["fornecedor_id"] == ativo["id"]

    r = cliente.post("/api/produtos", json=produto_valido(sku="ORG-002", fornecedor_id=999),
                     headers=cabecalho(csrf))
    assert erros(r) == {"fornecedor_id": "o fornecedor 999 não existe."}

    r = cliente.post("/api/produtos", json=produto_valido(sku="ORG-002",
                                                          fornecedor_id=desativado["id"]),
                     headers=cabecalho(csrf))
    assert erros(r) == {"fornecedor_id": f"o fornecedor {desativado['id']} está desativado; "
                                         "reative-o ou escolha outro."}

    produto = linha("SELECT id FROM produtos")[0]
    r = cliente.patch(f"/api/produtos/{produto}", json={"fornecedor_id": desativado["id"]},
                      headers=cabecalho(csrf))
    assert r.status_code == 422
    assert linha("SELECT fornecedor_id FROM produtos")[0] == ativo["id"]

    r = cliente.post("/api/produtos", json=produto_valido(sku="ORG-003", fornecedor_id="1"),
                     headers=cabecalho(csrf))
    assert erros(r) == {"fornecedor_id": "use um número inteiro."}


@pytest.mark.parametrize("campo", ["habilitacao_confirmada", "reembalagem_confirmada"])
def test_confirmacao_aceita_so_true_false_ou_null(sessao, campo):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf)
    for valor, gravado in ((True, 1), (False, 0), (None, None)):
        r = cliente.patch(f"/api/produtos/{produto['id']}", json={campo: valor},
                          headers=cabecalho(csrf))
        assert r.status_code == 200 and r.json()[campo] is valor
        assert linha(f"SELECT {campo} FROM produtos")[0] == gravado

    for invalido in (1, 0, "sim", "true", "1", ""):
        r = cliente.patch(f"/api/produtos/{produto['id']}", json={campo: invalido},
                          headers=cabecalho(csrf))
        assert erros(r) == {campo: 'use true, false ou null, sem aspas '
                                   '(1, 0 e "sim" não valem aqui).'}


def test_categoria_regulada_aceita_nenhuma_ou_uma_da_lista(sessao):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf)
    caminho = f"/api/produtos/{produto['id']}"
    for valor in ("nenhuma", " Suplemento ", None):
        r = cliente.patch(caminho, json={"categoria_regulada": valor}, headers=cabecalho(csrf))
        esperado = valor.strip().lower() if valor else None
        assert r.status_code == 200 and r.json()["categoria_regulada"] == esperado
    r = cliente.patch(caminho, json={"categoria_regulada": "eletronico"}, headers=cabecalho(csrf))
    assert list(erros(r)) == ["categoria_regulada"]
    assert "nenhuma" in erros(r)["categoria_regulada"]


CAMPO_DESCONHECIDO = "campo desconhecido; confira o nome (campo extra não é aceito)."
# Chave que, se virasse nome de coluna no SQL montado pelo cadastro, mudaria
# outra coluna: "UPDATE produtos SET ativo = 0, titulo = ?".
CHAVE_COM_SQL = "ativo = 0, titulo"
DINHEIRO_COM_EXTRA = {"valor": "18.50", "moeda": "BRL", "cambio": "5.1"}


def _retrato() -> tuple:
    with conectar() as conn:
        return tuple(tuple(tuple(l) for l in conn.execute(f"SELECT * FROM {t} ORDER BY id"))
                     for t in ("produtos", "fornecedores"))


@pytest.mark.parametrize("metodo, caminho, corpo, campo", [
    ("POST", "/api/produtos", produto_valido(sku="NOVO-1", preco=99), "preco"),
    ("POST", "/api/produtos", produto_valido(sku="NOVO-1", ativo=False), "ativo"),
    ("POST", "/api/produtos", produto_valido(sku="NOVO-1", **{CHAVE_COM_SQL: "x"}), CHAVE_COM_SQL),
    ("POST", "/api/produtos", produto_valido(sku="NOVO-1", custo_fornecedor=DINHEIRO_COM_EXTRA),
     "custo_fornecedor.cambio"),
    ("PATCH", "/api/produtos/1", {"titulo": "Outro", "preco": 99}, "preco"),
    ("PATCH", "/api/produtos/1", {"titulo": "Outro", CHAVE_COM_SQL: "x"}, CHAVE_COM_SQL),
    ("PATCH", "/api/produtos/1", {"custo_fornecedor": DINHEIRO_COM_EXTRA},
     "custo_fornecedor.cambio"),
    ("POST", "/api/fornecedores", fornecedor_valido(email="x@y.example"), "email"),
    ("POST", "/api/fornecedores", fornecedor_valido(**{CHAVE_COM_SQL: "x"}), CHAVE_COM_SQL),
    ("POST", "/api/fornecedores", fornecedor_valido(pedido_minimo=DINHEIRO_COM_EXTRA),
     "pedido_minimo.cambio"),
    ("PATCH", "/api/fornecedores/1", {"nome": "Outro", "email": "x@y.example"}, "email"),
    ("PATCH", "/api/fornecedores/1", {"nome": "Outro", CHAVE_COM_SQL: "x"}, CHAVE_COM_SQL),
    ("PATCH", "/api/fornecedores/1", {"pedido_minimo": DINHEIRO_COM_EXTRA},
     "pedido_minimo.cambio"),
])
def test_campo_desconhecido_e_recusado_em_toda_rota_de_escrita(sessao, metodo, caminho, corpo,
                                                              campo):
    """Nenhuma rota ignora nem aceita campo extra, também dentro do dinheiro:
    o nome da coluna no SQL vem do modelo, nunca do corpo."""
    cliente, csrf = sessao
    criar_produto(cliente, csrf, fornecedor_id=criar_fornecedor(cliente, csrf)["id"])
    antes = _retrato()

    r = cliente.request(metodo, caminho, json=corpo, headers=cabecalho(csrf))
    assert erros(r) == {campo: CAMPO_DESCONHECIDO}
    assert _retrato() == antes


@pytest.mark.parametrize("funcao, dados", [
    (cadastro.editar_produto, {CHAVE_COM_SQL: "x"}),
    (cadastro.editar_fornecedor, {CHAVE_COM_SQL: "x"}),
    (cadastro.criar_produto, produto_valido(sku="NOVO-1", **{CHAVE_COM_SQL: "x"})),
    (cadastro.criar_fornecedor, fornecedor_valido(**{CHAVE_COM_SQL: "x"})),
])
def test_campo_desconhecido_e_recusado_tambem_por_quem_chama_em_python(sessao, funcao, dados):
    cliente, csrf = sessao
    criar_produto(cliente, csrf, fornecedor_id=criar_fornecedor(cliente, csrf)["id"])
    antes = _retrato()
    argumentos = (1, dados) if funcao.__name__.startswith("editar") else (dados,)
    with pytest.raises(cadastro.ErroValidacao) as erro:
        funcao(*argumentos, ator="teste")
    assert erro.value.erros == [{"campo": CHAVE_COM_SQL, "mensagem": CAMPO_DESCONHECIDO}]
    assert _retrato() == antes


def test_varios_erros_voltam_juntos_campo_a_campo(sessao):
    cliente, csrf = sessao
    r = cliente.post("/api/fornecedores", json={"canal": "fax", "prazo_dias": 0},
                     headers=cabecalho(csrf))
    assert erros(r) == {"nome": "obrigatório.", "contato": "obrigatório.",
                        "canal": "use um destes: email, whatsapp, api, portal.",
                        "prazo_dias": "use um número inteiro de 1 a 365."}


@pytest.mark.parametrize("corpo, mensagem", [
    (b"{nao e json", "JSON inválido; envie um objeto JSON."),
    (b'{"sku": NaN}', "JSON inválido; envie um objeto JSON."),
    (b"\xff\xfe\x00", "JSON inválido; envie um objeto JSON."),
    (b"[" * 100000, "JSON inválido; envie um objeto JSON."),
    (b"", "envie um objeto JSON com os campos."),
    (b'["ORG-001"]', "envie um objeto JSON com os campos."),
], ids=["quebrado", "nan", "utf8-invalido", "aninhado-demais", "vazio", "lista"])
def test_corpo_que_nao_e_objeto_json_e_recusado_sem_traceback(sessao, corpo, mensagem):
    cliente, csrf = sessao
    r = cliente.post("/api/produtos", content=corpo,
                     headers={**cabecalho(csrf), "Content-Type": "application/json"})
    assert erros(r) == {"corpo": mensagem}


def test_edicao_sem_campo_e_recusada(sessao):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf)
    r = cliente.patch(f"/api/produtos/{produto['id']}", json={}, headers=cabecalho(csrf))
    assert erros(r) == {"corpo": "informe pelo menos um campo para alterar."}


def test_campo_obrigatorio_nao_aceita_null_na_edicao(sessao):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf)
    r = cliente.patch(f"/api/produtos/{produto['id']}",
                      json={"titulo": None, "custo_fornecedor": None}, headers=cabecalho(csrf))
    assert erros(r) == {"titulo": "não pode ser null.",
                        "custo_fornecedor": 'use um objeto com valor e moeda, por exemplo '
                                            '{"valor": "18.50", "moeda": "BRL"}.'}


def test_filtro_de_ativos_na_listagem(sessao):
    cliente, csrf = sessao
    um = criar_produto(cliente, csrf, sku="A-1")
    dois = criar_produto(cliente, csrf, sku="A-2")
    cliente.post(f"/api/produtos/{dois['id']}/desativar", headers=cabecalho(csrf))
    assert [p["sku"] for p in cliente.get("/api/produtos?ativo=true").json()["produtos"]] == ["A-1"]
    assert [p["sku"] for p in cliente.get("/api/produtos?ativo=false").json()["produtos"]] == ["A-2"]
    assert cliente.get("/api/produtos").json()["total"] == 2
    assert erros(cliente.get("/api/produtos?ativo=talvez")) == {"ativo": "use true ou false."}
    assert um["ativo"] is True


# ============================================================ 4. dinheiro

def test_valor_do_json_entra_em_decimal_sem_passar_por_float(sessao):
    """O corpo é lido com parse_float=Decimal: 0.1 chega como Decimal('0.1'),
    e 0.1 + 0.2 dá 0.3 exato, sem o 0.30000000000000004 do float."""
    cliente, csrf = sessao
    for sku, valor in (("D-1", "0.1"), ("D-2", "0.2")):
        corpo = ('{"sku": "%s", "titulo": "Decimal", "peso_kg": 0.4,'
                 ' "custo_fornecedor": {"valor": %s, "moeda": "USD"}}' % (sku, valor))
        r = cliente.post("/api/produtos", content=corpo.encode(),
                         headers={**cabecalho(csrf), "Content-Type": "application/json"})
        assert r.status_code == 201, r.text

    produtos = cliente.get("/api/produtos").json()["produtos"]
    valores = [Decimal(p["custo_fornecedor"]["valor"]) for p in produtos]
    assert valores == [Decimal("0.10"), Decimal("0.20")]
    assert sum(valores) == Decimal("0.3")
    assert 0.1 + 0.2 != 0.3  # o problema que o Decimal evita
    assert all(isinstance(p["custo_fornecedor"]["valor"], str) for p in produtos)
    with conectar() as conn:
        assert [l[0] for l in conn.execute(
            "SELECT custo_fornecedor_dec FROM produtos ORDER BY id")] == ["0.10", "0.20"]


def test_valor_com_mais_casas_que_o_aceito_e_recusado_nao_arredondado(sessao):
    cliente, csrf = sessao
    corpo = (b'{"sku": "D-1", "titulo": "Decimal", "peso_kg": "0.4",'
             b' "custo_fornecedor": {"valor": 0.30000000000000004, "moeda": "BRL"}}')
    r = cliente.post("/api/produtos", content=corpo,
                     headers={**cabecalho(csrf), "Content-Type": "application/json"})
    assert erros(r) == {"custo_fornecedor.valor": "use no máximo 4 casas decimais."}
    assert criar_produto(cliente, csrf, custo_fornecedor={"valor": "0.0125", "moeda": "BRL"}
                         )["custo_fornecedor"]["valor"] == "0.0125"


def test_gravacao_guarda_texto_canonico_moeda_e_espelho_real(sessao):
    cliente, csrf = sessao
    criar_produto(cliente, csrf, custo_fornecedor={"valor": "18.5", "moeda": "BRL"})
    gravado = linha("SELECT custo_fornecedor, custo_fornecedor_dec, custo_fornecedor_moeda,"
                    " typeof(custo_fornecedor) FROM produtos")
    assert tuple(gravado) == (18.5, "18.50", "BRL", "real")


def test_moeda_e_obrigatoria_em_valor_novo(sessao):
    cliente, csrf = sessao
    obrigatoria = ("obrigatória: todo valor leva a moeda (código ISO 4217), "
                   "por exemplo BRL ou USD.")
    r = cliente.post("/api/produtos", json=produto_valido(custo_fornecedor={"valor": "18.50"}),
                     headers=cabecalho(csrf))
    assert erros(r) == {"custo_fornecedor.moeda": obrigatoria}

    objeto = 'use um objeto com valor e moeda, por exemplo {"valor": "18.50", "moeda": "BRL"}.'
    for sem_objeto in ("18.50", 18.5, None):
        r = cliente.post("/api/produtos", json=produto_valido(custo_fornecedor=sem_objeto),
                         headers=cabecalho(csrf))
        assert erros(r) == {"custo_fornecedor": objeto}

    r = cliente.post("/api/fornecedores",
                     json=fornecedor_valido(pedido_minimo={"valor": "100"}), headers=cabecalho(csrf))
    assert erros(r) == {"pedido_minimo.moeda": obrigatoria}
    assert contar("produtos") == 0 and contar("fornecedores") == 0


@pytest.mark.parametrize("moeda", sorted(dinheiro.MOEDAS_ACEITAS))
def test_moeda_da_lista_passa(sessao, moeda):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf, custo_fornecedor={"valor": "9.90", "moeda": moeda})
    assert produto["custo_fornecedor"] == {"valor": "9.90", "moeda": moeda}


@pytest.mark.parametrize("moeda, mensagem", [
    ("brl", "use 3 letras maiúsculas (ISO 4217), por exemplo BRL ou USD."),
    ("R$", "use 3 letras maiúsculas (ISO 4217), por exemplo BRL ou USD."),
    ("BRLX", "use 3 letras maiúsculas (ISO 4217), por exemplo BRL ou USD."),
    ("JPY", "a moeda JPY não está na lista aceita: BRL, CAD, EUR, GBP, MXN, USD."),
    (986, 'use o código ISO 4217 da moeda em texto, por exemplo "BRL".'),
])
def test_moeda_fora_do_formato_ou_da_lista_e_recusada(sessao, moeda, mensagem):
    cliente, csrf = sessao
    r = cliente.post("/api/produtos", json=produto_valido(
        custo_fornecedor={"valor": "9.90", "moeda": moeda}), headers=cabecalho(csrf))
    assert erros(r) == {"custo_fornecedor.moeda": mensagem}


@pytest.mark.parametrize("valor, mensagem", [
    ("0", "precisa ser maior que zero."),
    ("-1.00", "precisa ser maior que zero."),
    ("1000000000", "use um valor menor que 1000000000."),
    ("1E+9999999", 'use um número decimal, por exemplo "18.50".'),
    ("١٢", 'use um número decimal, por exemplo "18.50".'),
    (True, 'use um número decimal, por exemplo "18.50".'),
    *[(texto, 'use um número decimal, por exemplo "18.50".') for texto in NAO_FINITOS],
])
def test_custo_zero_negativo_ou_absurdo_e_recusado(sessao, valor, mensagem):
    cliente, csrf = sessao
    r = cliente.post("/api/produtos", json=produto_valido(
        custo_fornecedor={"valor": valor, "moeda": "BRL"}), headers=cabecalho(csrf))
    assert erros(r) == {"custo_fornecedor.valor": mensagem}


@pytest.mark.parametrize("valor", [Decimal("NaN"), Decimal("-NaN"), Decimal("sNaN"),
                                   Decimal("Infinity"), Decimal("-Infinity")])
def test_validar_valor_recusa_decimal_que_nao_e_finito(valor):
    """Quem chama em Python pode mandar o Decimal pronto, sem passar pelo
    texto: a recusa é ValorInvalido, nunca decimal.InvalidOperation (que não
    é ValueError e viraria HTTP 500 ou traceback)."""
    with pytest.raises(dinheiro.ValorInvalido, match="^use um número finito.$"):
        dinheiro.validar_valor(valor)
    with pytest.raises(cadastro.ErroValidacao) as erro:
        cadastro.criar_produto(produto_valido(custo_fornecedor={"valor": valor, "moeda": "BRL"}),
                               ator="teste")
    assert erro.value.erros == [{"campo": "custo_fornecedor.valor",
                                 "mensagem": "use um número finito."}]


def test_validar_valor_recusa_float_de_quem_chama_em_python():
    """O JSON das rotas nunca entrega float (parse_float=Decimal); quem chama
    em Python pode, e 0.1 em float não é 0.1."""
    float_perde = 'use texto decimal, por exemplo "18.50": float perde precisão.'
    with pytest.raises(dinheiro.ValorInvalido, match=float_perde):
        dinheiro.validar_valor(0.1)
    with pytest.raises(cadastro.ErroValidacao) as erro:
        cadastro.criar_produto(produto_valido(custo_fornecedor={"valor": 0.1, "moeda": "BRL"}),
                               ator="teste")
    assert erro.value.erros == [{"campo": "custo_fornecedor.valor", "mensagem": float_perde}]
    assert contar("produtos") == 0


def test_numero_json_com_expoente_enorme_e_recusado_sem_erro_500(sessao):
    """1e9999999 vira Decimal; normalize()/abs() levantariam decimal.Overflow."""
    cliente, csrf = sessao
    for literal, mensagem in ((b"1e9999999", "use um valor menor que 1000000000."),
                              (b"1e-9999999", "use no máximo 4 casas decimais.")):
        corpo = (b'{"sku": "D-1", "titulo": "Decimal", "peso_kg": "0.4",'
                 b' "custo_fornecedor": {"valor": ' + literal + b', "moeda": "BRL"}}')
        r = cliente.post("/api/produtos", content=corpo,
                         headers={**cabecalho(csrf), "Content-Type": "application/json"})
        assert erros(r) == {"custo_fornecedor.valor": mensagem}


@pytest.mark.parametrize("literal", [b"1e9999999999999999999", b"-1e-99999999999999999999"])
def test_expoente_que_nem_o_decimal_representa_e_json_invalido_sem_erro_500(sessao, literal):
    """Além do expoente máximo do Decimal, parse_float levanta
    decimal.InvalidOperation, que não é ValueError: era HTTP 500 em toda rota
    de escrita, também em campo que não é dinheiro."""
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf, fornecedor_id=criar_fornecedor(cliente, csrf)["id"])
    antes = _retrato()
    json_ = {**cabecalho(csrf), "Content-Type": "application/json"}
    corpos = [
        ("POST", "/api/produtos", b'{"sku": "D-1", "titulo": "Decimal", "peso_kg": "0.4",'
                                  b' "custo_fornecedor": {"valor": ' + literal + b', "moeda": "BRL"}}'),
        ("PATCH", f"/api/produtos/{produto['id']}", b'{"peso_kg": ' + literal + b"}"),
        ("POST", "/api/fornecedores", b'{"nome": "F", "canal": "email", "contato": "c",'
                                      b' "prazo_dias": ' + literal + b"}"),
        ("PATCH", "/api/fornecedores/1", b'{"pedido_minimo": {"valor": ' + literal
                                         + b', "moeda": "BRL"}}'),
    ]
    for metodo, caminho, corpo in corpos:
        r = cliente.request(metodo, caminho, content=corpo, headers=json_)
        assert erros(r) == {"corpo": "JSON inválido; envie um objeto JSON."}, caminho
    assert _retrato() == antes


def test_pedido_minimo_aceita_zero(sessao):
    cliente, csrf = sessao
    f = criar_fornecedor(cliente, csrf, pedido_minimo={"valor": "0", "moeda": "EUR"})
    assert f["pedido_minimo"] == {"valor": "0.00", "moeda": "EUR"}


def test_dinheiro_texto_canonico_e_exato():
    assert [dinheiro.texto(Decimal(v)) for v in ("18.5", "0.0125", "1E+2", "-3.1", "-0", "5E-7")] \
        == ["18.50", "0.0125", "100.00", "-3.10", "0.00", "0.0000005"]
    assert dinheiro.do_real(0.1 + 0.2) == Decimal("0.3")
    assert dinheiro.ler("18.50", 99.0) == Decimal("18.50")  # o decimal vale antes da REAL
    assert dinheiro.ler(None, 18.5) == Decimal("18.5")       # linha antiga: a REAL
    assert dinheiro.ler("lixo", 18.5) is None                # ilegível não cai na REAL
    assert dinheiro.do_texto("1E+9999999") is None and dinheiro.do_real(1e300) is None
    assert dinheiro.formatar(Decimal("62"), "BRL") == "R$ 62.00"
    assert dinheiro.formatar(Decimal("9.9"), "USD") == "USD 9.90"
    assert dinheiro.formatar(Decimal("18.5"), None) == "18.50 (moeda não informada)"
    assert dinheiro.moeda_lida(" BRL ") == "BRL" and dinheiro.moeda_lida("brl") is None


def test_banco_antigo_migra_valores_da_coluna_real_com_moeda_nula(monkeypatch, tmp_path):
    """Banco da versão anterior (só as colunas REAL): as colunas novas entram,
    o valor é copiado só onde existe, e a moeda fica NULL — nada inventa BRL."""
    import db

    antigo = tmp_path / "antigo.db"
    with sqlite3.connect(antigo) as conn:
        conn.executescript("""
            CREATE TABLE produtos (id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT UNIQUE NOT NULL, titulo TEXT NOT NULL, categoria_ml TEXT,
                custo_fornecedor REAL NOT NULL, peso_kg REAL DEFAULT 0.3,
                fornecedor_id INTEGER, ativo INTEGER DEFAULT 1, criado_em TEXT NOT NULL,
                categoria_regulada TEXT, habilitacao_confirmada INTEGER,
                reembalagem_confirmada INTEGER);
            CREATE TABLE fornecedores (id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL, canal TEXT NOT NULL, contato TEXT NOT NULL,
                prazo_dias INTEGER DEFAULT 5, pedido_minimo REAL DEFAULT 0, observacoes TEXT);
            CREATE TABLE pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL, id_externo TEXT NOT NULL,
                produto_id INTEGER REFERENCES produtos(id),
                quantidade INTEGER NOT NULL DEFAULT 1, valor_bruto REAL NOT NULL,
                custo_previsto REAL, margem_prevista REAL, estado TEXT NOT NULL,
                comprador_nome TEXT, comprador_id TEXT, endereco_json TEXT,
                codigo_rastreio TEXT, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL,
                UNIQUE(marketplace, id_externo));
        """)
        conn.execute("INSERT INTO fornecedores (nome, canal, contato, prazo_dias, pedido_minimo)"
                     " VALUES ('Antigo', 'email', 'a@b.example', 4, 150)")
        conn.execute("INSERT INTO fornecedores (nome, canal, contato, pedido_minimo)"
                     " VALUES ('Sem mínimo', 'email', 'c@d.example', NULL)")
        conn.execute("INSERT INTO produtos (sku, titulo, custo_fornecedor, fornecedor_id,"
                     " criado_em) VALUES ('VELHO-1', 'Antigo', ?, 1, 'ontem')", (0.1 + 0.2,))
        conn.execute("INSERT INTO produtos (sku, titulo, custo_fornecedor, criado_em)"
                     " VALUES ('VELHO-2', 'Digitado errado', '18,50', 'ontem')")
        conn.execute("INSERT INTO pedidos (marketplace, id_externo, produto_id, valor_bruto,"
                     " custo_previsto, estado, criado_em, atualizado_em)"
                     " VALUES ('mercadolivre', '1', 1, 54.9, NULL, 'NOVO', 'ontem', 'ontem')")
    monkeypatch.setattr(config.config, "db_path", str(antigo))

    db.inicializar()
    db.inicializar()  # de novo: nada é acrescentado nem copiado duas vezes

    with conectar() as conn:
        produtos = [tuple(l) for l in conn.execute(
            "SELECT sku, custo_fornecedor, custo_fornecedor_dec, custo_fornecedor_moeda,"
            " atualizado_em FROM produtos ORDER BY id")]
        fornecedores = [tuple(l) for l in conn.execute(
            "SELECT nome, pedido_minimo_dec, pedido_minimo_moeda, ativo FROM fornecedores"
            " ORDER BY id")]
        pedido = tuple(conn.execute(
            "SELECT valor_bruto_dec, valor_bruto_moeda, custo_previsto_dec,"
            " custo_previsto_moeda FROM pedidos").fetchone())

    assert produtos == [("VELHO-1", 0.1 + 0.2, "0.30", None, None),
                        ("VELHO-2", "18,50", None, None, None)]  # ilegível não é copiado
    assert fornecedores == [("Antigo", "150.00", None, 1), ("Sem mínimo", None, None, 1)]
    assert pedido == ("54.90", None, None, None)  # custo_previsto NULL continua NULL

    # A API mostra o valor antigo com a moeda não informada.
    velho = cadastro.obter_produto(1)
    assert velho["custo_fornecedor"] == {"valor": "0.30", "moeda": None}
    assert cadastro.obter_produto(2)["custo_fornecedor"] == {"valor": None, "moeda": None}
    assert cadastro.obter_fornecedor(1)["ativo"] is True


def test_sql_a_mao_na_coluna_real_apaga_decimal_e_moeda_que_nao_batem(sessao):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf, custo_fornecedor={"valor": "18.50", "moeda": "USD"})
    with conectar() as conn:
        conn.execute("UPDATE produtos SET custo_fornecedor = 18.5 WHERE id = ?", (produto["id"],))
    # O mesmo valor: o decimal e a moeda ficam.
    assert cliente.get(f"/api/produtos/{produto['id']}").json()["custo_fornecedor"] == {
        "valor": "18.50", "moeda": "USD"}
    with conectar() as conn:
        conn.execute("UPDATE produtos SET custo_fornecedor = 20 WHERE id = ?", (produto["id"],))
    # Outro valor: o decimal velho não pode valer, e a moeda do valor novo
    # não foi dita.
    assert cliente.get(f"/api/produtos/{produto['id']}").json()["custo_fornecedor"] == {
        "valor": "20.00", "moeda": None}
    # Cadastrar de novo pela API devolve a moeda.
    r = cliente.patch(f"/api/produtos/{produto['id']}",
                      json={"custo_fornecedor": {"valor": "20", "moeda": "USD"}},
                      headers=cabecalho(csrf))
    assert r.json()["custo_fornecedor"] == {"valor": "20.00", "moeda": "USD"}


def test_gravar_pela_api_numa_linha_dessincronizada_nao_perde_a_moeda(sessao):
    """Linha inserida à mão com a REAL diferente do decimal: a API grava os
    dois juntos, a REAL muda e o decimal não; o gatilho não pode apagar a
    moeda que acabou de ser informada."""
    cliente, csrf = sessao
    with conectar() as conn:
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec,"
            " custo_fornecedor_moeda, peso_kg, criado_em)"
            " VALUES ('DESSINC', 'Dessincronizado', 99, '18.50', 'GBP', 0.4, ?)",
            (agora(),)).lastrowid
    r = cliente.patch(f"/api/produtos/{produto}",
                      json={"custo_fornecedor": {"valor": "18.50", "moeda": "USD"}},
                      headers=cabecalho(csrf))
    assert r.json()["custo_fornecedor"] == {"valor": "18.50", "moeda": "USD"}
    assert tuple(linha("SELECT custo_fornecedor, custo_fornecedor_dec, custo_fornecedor_moeda"
                       " FROM produtos")) == (18.5, "18.50", "USD")


@pytest.mark.parametrize("custo, venda, trecho", [
    ("USD", "BRL", "o custo está em USD e a venda em BRL; o programa não converte moedas"),
    ("BRL", "EUR", "o custo está em BRL e a venda em EUR; o programa não converte moedas"),
    (None, "BRL", "a moeda do custo do produto não foi informada (moeda não informada)"),
    ("BRL", None, "a moeda da venda não foi informada pelo marketplace (moeda não informada)"),
    ("USD", "USD", "o modelo de taxas (Mercado Livre Brasil) é em BRL; não há modelo para USD"),
])
def test_margem_recusada_entre_moedas_diferentes_ou_desconhecidas(custo, venda, trecho):
    with conectar() as conn:
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec,"
            " custo_fornecedor_moeda, peso_kg, criado_em) VALUES ('M-1', 'Moeda', 18.5,"
            " '18.50', ?, 0.4, ?)", (custo, agora())).lastrowid
    pedido = inserir_pedido(produto, valor="54.90", moeda=venda)

    assert worker.analisar_novos() == 0
    estado, margem, custo_previsto = linha(
        "SELECT estado, margem_prevista, custo_previsto_dec FROM pedidos WHERE id = ?", pedido)
    assert estado == Estado.PROBLEMA.value
    assert margem is None and custo_previsto is None  # nada calculado misturando moedas
    motivo = historico(pedido)[-1]["motivo"]
    assert motivo.startswith("Margem não calculada: ") and trecho in motivo
    if custo is None:
        assert "python cli.py produto editar M-1 --custo VALOR --moeda BRL" in motivo
    assert precificacao.motivo_sem_margem(custo, venda) in motivo


def test_margem_calculada_em_decimal_com_a_mesma_moeda(nota_fiscal_confirmada):
    with conectar() as conn:
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec,"
            " custo_fornecedor_moeda, peso_kg, criado_em) VALUES ('M-1', 'Moeda', 18.5,"
            " '18.50', 'BRL', 0.4, ?)", (agora(),)).lastrowid
    pedido = inserir_pedido(produto, valor="120.00", moeda="BRL", quantidade=2)

    assert worker.analisar_novos() == 1
    gravado = linha("SELECT estado, custo_previsto, custo_previsto_dec, custo_previsto_moeda,"
                    " margem_prevista FROM pedidos WHERE id = ?", pedido)
    assert gravado["estado"] == Estado.ANALISADO.value
    assert (gravado["custo_previsto_dec"], gravado["custo_previsto_moeda"]) == ("37.00", "BRL")
    assert gravado["custo_previsto"] == 37.0
    esperado = precificacao.calcular(Decimal("120.00"), Decimal("37.00"), Decimal("0.4"),
                                     aliquota_imposto_pct=config.config.negocio.aliquota_imposto_pct,
                                     margem_minima_pct=config.config.negocio.margem_minima_pct)
    assert isinstance(esperado.margem_pct, Decimal)
    assert Decimal(str(gravado["margem_prevista"])) == esperado.margem_pct


def test_margem_do_worker_recebe_decimal_e_nao_perde_centavo(monkeypatch, nota_fiscal_confirmada):
    """Custo e venda chegam à conta da margem em Decimal, sem passar por
    float: o custo total de 100 x 999999999.9999 sai exato."""
    recebidos = []
    original = precificacao.calcular

    def espiar(*args, **kwargs):
        recebidos.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(precificacao, "calcular", espiar)
    with conectar() as conn:
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec,"
            " custo_fornecedor_moeda, peso_kg, criado_em) VALUES ('CARO-1', 'Caro', 999999999.9999,"
            " '999999999.9999', 'BRL', 0.4, ?)", (agora(),)).lastrowid
    pedido = inserir_pedido(produto, valor="54.90", moeda="BRL", quantidade=100)

    worker.analisar_novos()
    assert len(recebidos) == 1
    assert type(recebidos[0]["preco_venda"]) is Decimal
    assert type(recebidos[0]["custo_produto"]) is Decimal
    assert recebidos[0]["custo_produto"] == Decimal("99999999999.9900")
    assert linha("SELECT custo_previsto_dec FROM pedidos WHERE id = ?", pedido)[0] == "99999999999.99"


@pytest.mark.parametrize("peso_sql", ["9e999", "'leve'"], ids=["infinito", "texto"])
def test_peso_ilegivel_manda_so_aquele_pedido_para_problema(nota_fiscal_confirmada, peso_sql):
    """Um 9e999 gravado à mão no banco vira infinito no SQLite. O peso que
    não se lê levantava ValueError e parava a análise de todos os pedidos
    novos, ciclo após ciclo; agora só o pedido daquele produto vai para
    PROBLEMA, com o motivo, e o outro segue."""
    with conectar() as conn:
        ruim = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec,"
            " custo_fornecedor_moeda, peso_kg, criado_em) VALUES ('PESO-1', 'Ruim', 18.5,"
            f" '18.50', 'BRL', {peso_sql}, ?)", (agora(),)).lastrowid
        bom = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec,"
            " custo_fornecedor_moeda, peso_kg, criado_em) VALUES ('PESO-2', 'Bom', 18.5,"
            " '18.50', 'BRL', 0.4, ?)", (agora(),)).lastrowid
    pedido_ruim = inserir_pedido(ruim, id_externo="6000000001")
    pedido_bom = inserir_pedido(bom, id_externo="6000000002")

    assert worker.analisar_novos() == 1
    assert linha("SELECT estado FROM pedidos WHERE id = ?", pedido_bom)[0] == Estado.ANALISADO.value
    gravado = linha("SELECT estado, margem_prevista, custo_previsto_dec FROM pedidos WHERE id = ?",
                    pedido_ruim)
    assert tuple(gravado) == (Estado.PROBLEMA.value, None, None)
    motivo = historico(pedido_ruim)[-1]["motivo"]
    assert motivo == (f"Margem não calculada: peso do produto ilegível no cadastro; corrija o "
                      f"peso_kg do produto {ruim} (--peso no cli.py ou PATCH /api/produtos/<id>); "
                      f"depois, python cli.py pedido reanalisar {pedido_ruim}")


@pytest.mark.parametrize("teto", [float("inf"), float("nan"), 1e20])
def test_teto_ilegivel_rotula_acima_do_teto_sem_quebrar_a_montagem(monkeypatch, teto,
                                                                   nota_fiscal_confirmada):
    """O config.py já troca nan e inf pelo padrão; mexido à mão, o teto que
    não se lê em Decimal fazia Decimal <= None levantar TypeError a cada
    ciclo. O rótulo é só informação: fica o que chama mais atenção."""
    monkeypatch.setattr(config.config.negocio, "teto_compra_automatica", teto)
    with conectar() as conn:
        conn.execute("INSERT INTO fornecedores (id, nome, canal, contato, prazo_dias)"
                     " VALUES (1, 'Fornecedor Teste', 'email', 'pedidos@fornecedor.example', 4)")
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec,"
            " custo_fornecedor_moeda, peso_kg, fornecedor_id, categoria_regulada, criado_em)"
            " VALUES ('TETO-1', 'Teto', 18.5, '18.50', 'BRL', 0.4, 1, 'nenhuma', ?)",
            (agora(),)).lastrowid
    inserir_pedido(produto)

    assert worker.analisar_novos() == 1
    assert worker.montar_ordens_de_compra() == 1
    item, = aprovacao.pendentes()
    assert item.resumo.startswith("[ACIMA DO TETO] Comprar 1x TETO-1 de Fornecedor Teste")


@pytest.mark.parametrize("conteudo, esperado, avisos", [
    ("MARGEM_MINIMA_PCT=nan\nTETO_COMPRA_AUTOMATICA=inf\nALIQUOTA_IMPOSTO_PCT=1e999\n",
     [18.0, 300.0, 4.0],
     ["MARGEM_MINIMA_PCT='nan' não vale; usando 18.",
      "TETO_COMPRA_AUTOMATICA='inf' não vale; usando 300.",
      "ALIQUOTA_IMPOSTO_PCT='1e999' não vale; usando 4."]),
    ("TETO_COMPRA_AUTOMATICA=1e20\nALIQUOTA_IMPOSTO_PCT=-Infinity\n", [18.0, 300.0, 4.0],
     ["TETO_COMPRA_AUTOMATICA='1e20' não vale; usando 300.",
      "ALIQUOTA_IMPOSTO_PCT='-Infinity' não vale; usando 4."]),
    ("MARGEM_MINIMA_PCT=25.5\nTETO_COMPRA_AUTOMATICA=1000\nALIQUOTA_IMPOSTO_PCT=6\n",
     [25.5, 1000.0, 6.0], []),
], ids=["nao-finitos", "grande-demais", "validos"])
def test_config_troca_margem_teto_e_aliquota_que_o_worker_nao_le_pelo_padrao(
        tmp_path, conteudo, esperado, avisos):
    """Importação limpa, em outro processo, com os valores só no .env da pasta
    de dados temporária. nan e inf passavam do config.py e quebravam a etapa
    do worker a cada ciclo."""
    (tmp_path / ".env").write_text(conteudo, encoding="utf-8")
    nomes = ("MARGEM_MINIMA_PCT", "TETO_COMPRA_AUTOMATICA", "ALIQUOTA_IMPOSTO_PCT")
    ambiente = {k: v for k, v in os.environ.items() if k not in nomes}
    ambiente.update(AGENTE_DADOS=str(tmp_path), PYTHONIOENCODING="utf-8")
    script = ("import json, config\n"
              "assert config.DATA_DIR != config.BASE_DIR\n"
              "n = config.config.negocio\n"
              "print(json.dumps([n.margem_minima_pct, n.teto_compra_automatica,"
              " n.aliquota_imposto_pct]))\n")
    feito = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parent.parent,
                           env=ambiente, capture_output=True, text=True, encoding="utf-8",
                           timeout=120)
    assert feito.returncode == 0, feito.stderr
    assert json.loads(feito.stdout) == esperado
    for aviso in avisos:
        assert aviso in feito.stderr
    assert ("não vale" in feito.stderr) is bool(avisos)


def test_ordem_de_compra_leva_valor_em_texto_e_a_moeda(fila_de_compras):
    item = aprovacao.obter_pendente(fila_de_compras[0])
    assert item.payload["valor"] == "18.50" and item.payload["moeda"] == "BRL"
    assert "por R$ 18.50" in item.resumo


def test_pedido_analisado_por_versao_anterior_vai_a_fila_sem_moeda_inventada(nota_fiscal_confirmada):
    """Pedido ANALISADO pela versão anterior: só custo_previsto REAL, sem o
    decimal e sem a moeda. A ordem não presume BRL."""
    with conectar() as conn:
        conn.execute("INSERT INTO fornecedores (id, nome, canal, contato, prazo_dias)"
                     " VALUES (1, 'Fornecedor Teste', 'email', 'pedidos@fornecedor.example', 4)")
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, peso_kg, fornecedor_id,"
            " categoria_regulada, criado_em) VALUES ('VELHO-1', 'Antigo', 18.5, 0.4, 1,"
            " 'nenhuma', ?)", (agora(),)).lastrowid
    pedido = inserir_pedido(produto, estado=Estado.ANALISADO.value, custo_previsto=18.5,
                            margem_prevista=30.0)

    assert worker.montar_ordens_de_compra() == 1
    item, = aprovacao.pendentes()
    assert item.pedido_id == pedido
    assert item.payload["valor"] == "18.50" and item.payload["moeda"] is None
    assert "por 18.50 (moeda não informada)" in item.resumo
    assert "R$" not in item.resumo


def test_ordem_de_compra_da_fila_antiga_sem_moeda_nao_diz_reais():
    """Payload da fila antiga: valor número e nenhuma moeda. O arquivo que
    vai ao fornecedor não pode dizer R$."""
    resultado = worker.executor_compra({
        "pedido_id": 77, "fornecedor": "Fornecedor Teste", "canal": "email",
        "contato": "pedidos@fornecedor.example", "sku": "VELHO-1", "produto": "Antigo",
        "quantidade": 1, "valor": 18.5, "marketplace": "mercadolivre",
        "pedido_externo": "1", "margem_prevista": 30.0})
    texto = (worker.PASTA_ORDENS / "oc_77.txt").read_text(encoding="utf-8")
    assert "Valor: 18.50 (moeda não informada)\n" in texto
    assert "R$" not in texto
    assert aprovacao.foi_simulado(resultado)  # simulação ligada: nada anda


def test_pedido_do_mercado_livre_guarda_a_moeda_informada():
    from apoio import COMPRADOR, MercadoLivreFalso

    brutos = [{"id": 980001 + i, "total_amount": 54.9, "currency_id": moeda,
               "order_items": [{"item": {"seller_sku": "X"}, "quantity": 1}],
               "buyer": dict(COMPRADOR), "shipping": {"id": 1}}
              for i, moeda in enumerate(("BRL", None))]
    assert worker.ingerir_mercadolivre(MercadoLivreFalso(brutos)) == 2
    with conectar() as conn:
        gravados = [tuple(l) for l in conn.execute(
            "SELECT valor_bruto_dec, valor_bruto_moeda FROM pedidos ORDER BY id")]
    assert gravados == [("54.90", "BRL"), ("54.90", None)]  # sem currency_id, nada é presumido


# =============================================================== 5. datas

def test_datas_gravadas_com_fuso_utc(sessao):
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto = criar_produto(cliente, csrf)
    for registro in (fornecedor, produto):
        for campo in ("criado_em", "atualizado_em"):
            assert registro[campo].endswith("+00:00")
            assert datetime.fromisoformat(registro[campo]).utcoffset() == timezone.utc.utcoffset(None)
    assert agora().endswith("+00:00")


# ===================================================== 6. cli.py e a API juntos

def test_cli_cria_edita_lista_e_desativa(monkeypatch, capsys):
    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "fornecedor", "criar", "--nome", "Fábrica",
                                 "--canal", "email", "--contato", "pedidos@fabrica.example",
                                 "--prazo", "4", "--pedido-minimo", "100", "--moeda", "BRL")
    assert codigo == 0 and "Fornecedor 1 criado." in saida and "pedido mínimo R$ 100.00" in saida

    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "produto", "criar", "--sku", "ORG-001",
                                 "--titulo", "Organizador", "--custo", "18.5", "--moeda", "BRL",
                                 "--peso", "0.4", "--fornecedor", "1")
    assert codigo == 0 and "Produto 1 criado." in saida and "custo R$ 18.50" in saida

    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "produto", "editar", "ORG-001",
                                 "--categoria-regulada", "nenhuma", "--reembalagem", "sim")
    assert codigo == 0 and "Produto 1 alterado." in saida
    produto = cadastro.obter_produto(1)
    assert (produto["categoria_regulada"], produto["reembalagem_confirmada"]) == ("nenhuma", True)

    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "produto", "listar")
    assert codigo == 0 and "ORG-001" in saida and "reembalagem: sim" in saida

    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "fornecedor", "desativar", "1")
    assert codigo == 0 and "desativado (nada foi apagado)" in saida
    assert "1 produto(s) ativo(s) ainda usam este fornecedor" in saida

    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "fornecedor", "listar", "--ativo", "false")
    assert "[desativado]" in saida
    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "fornecedor", "editar", "1", "--reativar")
    assert codigo == 0 and cadastro.obter_fornecedor(1)["ativo"] is True

    codigo, saida, _ = rodar_cli(monkeypatch, capsys, "produto", "desativar", "ORG-001")
    assert codigo == 0 and cadastro.obter_produto(1)["ativo"] is False
    with conectar() as conn:
        eventos = [l[0] for l in conn.execute(
            "SELECT mensagem FROM eventos WHERE origem = 'cadastro' ORDER BY id")]
    assert eventos[0] == "Fornecedor 1 criado por cli"
    assert "Produto 1 alterado por cli: categoria_regulada, reembalagem_confirmada" in eventos
    assert all("pedidos@fabrica.example" not in e and "18.5" not in e for e in eventos)


# A mesma entrada inválida pela API e pelo cli.py: os mesmos campos e as
# mesmas mensagens, porque as duas portas chamam core/cadastro.
CASOS_COMPARTILHADOS = [
    (["fornecedor", "criar", "--nome", "F", "--canal", "fax", "--contato", "x", "--prazo", "0"],
     "/api/fornecedores", {"nome": "F", "canal": "fax", "contato": "x", "prazo_dias": 0}),
    (["fornecedor", "criar", "--nome", "F", "--canal", "email", "--contato", "x",
      "--prazo", "cinco"],
     "/api/fornecedores", {"nome": "F", "canal": "email", "contato": "x", "prazo_dias": "cinco"}),
    (["produto", "criar", "--sku", " ", "--titulo", "T", "--custo", "18,50", "--peso", "0"],
     "/api/produtos", {"sku": " ", "titulo": "T", "custo_fornecedor": {"valor": "18,50"},
                       "peso_kg": "0"}),
    (["produto", "criar", "--sku", "S", "--titulo", "T", "--custo", "1", "--moeda", "usd",
      "--peso", "1", "--fornecedor", "77"],
     "/api/produtos", {"sku": "S", "titulo": "T", "custo_fornecedor": {"valor": "1", "moeda": "usd"},
                       "peso_kg": "1", "fornecedor_id": 77}),
    (["produto", "criar", "--sku", "S", "--titulo", "T", "--custo", "nan", "--moeda", "BRL",
      "--peso", "inf"],
     "/api/produtos", {"sku": "S", "titulo": "T", "custo_fornecedor": {"valor": "nan", "moeda": "BRL"},
                       "peso_kg": "inf"}),
]

# A edição também: PATCH e cli.py editar, com um produto e um fornecedor que
# já existem (id 1, SKU ORG-001), recusam igual e não mudam nada.
CASOS_DE_EDICAO = [
    (["produto", "editar", "ORG-001", "--categoria-regulada", "eletronico"],
     "/api/produtos/1", {"categoria_regulada": "eletronico"}),
    (["produto", "editar", "ORG-001", "--peso", "0"], "/api/produtos/1", {"peso_kg": "0"}),
    (["produto", "editar", "ORG-001", "--custo", "NaN", "--moeda", "BRL"],
     "/api/produtos/1", {"custo_fornecedor": {"valor": "NaN", "moeda": "BRL"}}),
    (["produto", "editar", "ORG-001", "--sku", " ", "--fornecedor", "abc"],
     "/api/produtos/1", {"sku": " ", "fornecedor_id": "abc"}),
    (["produto", "editar", "ORG-001", "--custo", "19.90"],
     "/api/produtos/1", {"custo_fornecedor": {"valor": "19.90"}}),
    (["fornecedor", "editar", "1", "--prazo", "0", "--canal", "fax"],
     "/api/fornecedores/1", {"prazo_dias": 0, "canal": "fax"}),
]


@pytest.mark.parametrize("argumentos, rota, corpo", CASOS_COMPARTILHADOS)
def test_cli_e_api_recusam_com_as_mesmas_mensagens(monkeypatch, capsys, sessao,
                                                   argumentos, rota, corpo):
    cliente, csrf = sessao
    pela_api = cliente.post(rota, json=corpo, headers=cabecalho(csrf))
    esperado = [f"  {campo}: {mensagem}" for campo, mensagem in erros(pela_api).items()]

    codigo, saida, erro = rodar_cli(monkeypatch, capsys, *argumentos)
    assert codigo == 1 and saida == ""
    assert erro.splitlines() == ["Dados inválidos:", *esperado]
    assert "Traceback" not in erro
    assert contar("produtos") == 0 and contar("fornecedores") == 0


@pytest.mark.parametrize("argumentos, rota, corpo", CASOS_DE_EDICAO)
def test_cli_editar_e_patch_recusam_com_as_mesmas_mensagens(monkeypatch, capsys, sessao,
                                                           argumentos, rota, corpo):
    cliente, csrf = sessao
    criar_produto(cliente, csrf, fornecedor_id=criar_fornecedor(cliente, csrf)["id"])
    antes = _retrato()

    pela_api = cliente.patch(rota, json=corpo, headers=cabecalho(csrf))
    esperado = [f"  {campo}: {mensagem}" for campo, mensagem in erros(pela_api).items()]

    codigo, saida, erro = rodar_cli(monkeypatch, capsys, *argumentos)
    assert codigo == 1 and saida == ""
    assert erro.splitlines() == ["Dados inválidos:", *esperado]
    assert "Traceback" not in erro
    assert _retrato() == antes


def test_cli_e_api_passam_pela_mesma_funcao_de_validacao(monkeypatch, capsys, sessao):
    cliente, csrf = sessao
    chamadas = []
    original = cadastro._validar

    def espiar(modelo, dados):
        chamadas.append(modelo.__name__)
        return original(modelo, dados)

    monkeypatch.setattr(cadastro, "_validar", espiar)
    criar_fornecedor(cliente, csrf)
    rodar_cli(monkeypatch, capsys, "fornecedor", "criar", "--nome", "F", "--canal", "api",
              "--contato", "x", "--prazo", "3")
    cliente.patch("/api/fornecedores/1", json={"prazo_dias": 9}, headers=cabecalho(csrf))
    rodar_cli(monkeypatch, capsys, "fornecedor", "editar", "2", "--prazo", "9")
    assert chamadas == ["FornecedorNovo", "FornecedorNovo",
                        "FornecedorAlteracao", "FornecedorAlteracao"]

    chamadas.clear()
    criar_produto(cliente, csrf, sku="API-1")
    codigo, _, _ = rodar_cli(monkeypatch, capsys, "produto", "criar", "--sku", "CLI-1",
                             "--titulo", "T", "--custo", "1", "--moeda", "BRL", "--peso", "1")
    assert codigo == 0
    cliente.patch("/api/produtos/1", json={"titulo": "Outro"}, headers=cabecalho(csrf))
    codigo, _, _ = rodar_cli(monkeypatch, capsys, "produto", "editar", "CLI-1", "--titulo", "Outro")
    assert codigo == 0
    assert chamadas == ["ProdutoNovo", "ProdutoNovo", "ProdutoAlteracao", "ProdutoAlteracao"]


def test_cli_responde_conflito_e_nao_encontrado_sem_traceback(monkeypatch, capsys, sessao):
    cliente, csrf = sessao
    criar_produto(cliente, csrf, sku="ORG-001")

    codigo, _, erro = rodar_cli(monkeypatch, capsys, "produto", "criar", "--sku", "ORG-001",
                                "--titulo", "T", "--custo", "1", "--moeda", "BRL", "--peso", "1")
    assert codigo == 1 and "Já existe um produto com o SKU 'ORG-001'" in erro

    codigo, _, erro = rodar_cli(monkeypatch, capsys, "produto", "editar", "NAO-EXISTE",
                                "--titulo", "T")
    assert codigo == 1 and erro.strip() == "Produto com SKU 'NAO-EXISTE' não encontrado."

    for alvo in ("99", "abc"):
        codigo, _, erro = rodar_cli(monkeypatch, capsys, "fornecedor", "desativar", alvo)
        assert codigo == 1 and erro.strip() == "Fornecedor não encontrado."

    codigo, _, erro = rodar_cli(monkeypatch, capsys, "produto", "editar", "ORG-001")
    assert codigo == 1 and "corpo: informe pelo menos um campo para alterar." in erro
    assert "Traceback" not in erro


# ============================================= 7. desativar em vez de apagar

def test_desativar_produto_mantem_pedidos_e_chaves_estrangeiras(sessao):
    cliente, csrf = sessao
    produto = criar_produto(cliente, csrf)
    pedido = inserir_pedido(produto["id"])

    r = cliente.post(f"/api/produtos/{produto['id']}/desativar", headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["ativo"] is False
    assert r.json()["atualizado_em"] >= produto["atualizado_em"]

    with conectar() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT produto_id FROM pedidos WHERE id = ?",
                            (pedido,)).fetchone()[0] == produto["id"]
        assert conn.execute("SELECT COUNT(*) FROM produtos").fetchone()[0] == 1
        # Apagar é o que quebraria: a chave estrangeira do pedido impede.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM produtos WHERE id = ?", (produto["id"],))

    lido = cliente.get(f"/api/produtos/{produto['id']}").json()
    assert lido["sku"] == "ORG-001" and lido["ativo"] is False
    # Reativar é editar.
    r = cliente.patch(f"/api/produtos/{produto['id']}", json={"ativo": True},
                      headers=cabecalho(csrf))
    assert r.json()["ativo"] is True
    assert erros(cliente.patch(f"/api/produtos/{produto['id']}", json={"ativo": 1},
                               headers=cabecalho(csrf))) == {"ativo": "use true ou false, sem aspas."}


def test_desativar_fornecedor_mantem_o_vinculo_e_bloqueia_a_compra(sessao, nota_fiscal_confirmada):
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto = criar_produto(cliente, csrf, fornecedor_id=fornecedor["id"],
                            categoria_regulada="nenhuma")
    inserir_pedido(produto["id"])
    assert worker.analisar_novos() == 1
    assert worker.montar_ordens_de_compra() == 1
    item = aprovacao.pendentes()[0]
    assert not aprovacao.checar(item).bloqueado

    r = cliente.post(f"/api/fornecedores/{fornecedor['id']}/desativar", headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["ativo"] is False
    assert r.json()["aviso"].startswith("1 produto(s) ativo(s) ainda usam este fornecedor.")
    assert linha("SELECT fornecedor_id FROM produtos")[0] == fornecedor["id"]
    with conectar() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # A compra que já estava na fila fica bloqueada, com a saída.
    checagem = aprovacao.checar(item)
    assert [v.regra for v in checagem.bloqueios] == ["FORNECEDOR-DESATIVADO"]
    assert "python cli.py fornecedor editar ID --reativar" in checagem.bloqueios[0].saida
    assert cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf)).status_code == 409

    # Reativado, a mesma compra volta a poder ser aprovada.
    cliente.patch(f"/api/fornecedores/{fornecedor['id']}", json={"ativo": True},
                  headers=cabecalho(csrf))
    assert not aprovacao.checar(item).bloqueado


def test_fornecedor_desativado_antes_do_worker_manda_o_pedido_para_problema(
        sessao, nota_fiscal_confirmada):
    """Desativado não é dado que falta: é bloqueio duro. O worker não põe a
    compra na fila; o pedido vai para PROBLEMA com o motivo."""
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto = criar_produto(cliente, csrf, fornecedor_id=fornecedor["id"],
                            categoria_regulada="nenhuma")
    cliente.post(f"/api/fornecedores/{fornecedor['id']}/desativar", headers=cabecalho(csrf))
    pedido = inserir_pedido(produto["id"])

    assert worker.analisar_novos() == 1
    assert worker.montar_ordens_de_compra() == 0
    assert aprovacao.pendentes() == []
    ultimo = historico(pedido)[-1]
    assert ultimo["para"] == Estado.PROBLEMA.value
    assert "FORNECEDOR-DESATIVADO" in ultimo["motivo"]

    checagem = conformidade.verificar_pendencia("compra_fornecedor", {
        "pedido_id": pedido, "produto_id": produto["id"], "fornecedor_id": fornecedor["id"],
        "marketplace": "mercadolivre", "margem_prevista": 30, "quantidade": 1,
        "valor": "18.50", "moeda": "BRL"})
    assert [(v.regra, v.confirmacao) for v in checagem.bloqueios] == [
        ("FORNECEDOR-DESATIVADO", False)]
    assert not checagem.so_falta_confirmar


def _compra_enfileirada(cliente, csrf, fornecedor_id, sku="ORG-001", id_externo="6000000001"):
    """Produto com o fornecedor, um pedido e a compra montada pelo worker."""
    produto = criar_produto(cliente, csrf, sku=sku, fornecedor_id=fornecedor_id,
                            categoria_regulada="nenhuma")
    inserir_pedido(produto["id"], id_externo=id_externo)
    assert worker.analisar_novos() == 1
    assert worker.montar_ordens_de_compra() == 1
    item, = aprovacao.pendentes()
    return produto, item


def test_trocar_o_fornecedor_do_produto_nao_aprova_a_ordem_do_antigo(sessao, nota_fiscal_confirmada):
    """A ordem na fila foi montada para F1 (nome, canal e contato no payload).
    F1 desativado e o produto religado a F2: aprovar não pode gravar a ordem
    para F1. A conformidade bloqueia, e o worker refaz a ordem com F2."""
    cliente, csrf = sessao
    antigo = criar_fornecedor(cliente, csrf, nome="Fornecedor Antigo", contato="antigo@x.example")
    novo = criar_fornecedor(cliente, csrf, nome="Fornecedor Novo", contato="novo@x.example")
    produto, item = _compra_enfileirada(cliente, csrf, antigo["id"])
    assert (item.payload["fornecedor_id"], item.payload["produto_id"]) == (antigo["id"], produto["id"])
    assert item.payload["fornecedor"] == "Fornecedor Antigo"

    cliente.post(f"/api/fornecedores/{antigo['id']}/desativar", headers=cabecalho(csrf))
    assert [v.regra for v in aprovacao.checar(item).bloqueios] == ["FORNECEDOR-DESATIVADO"]

    r = cliente.patch(f"/api/produtos/{produto['id']}", json={"fornecedor_id": novo["id"]},
                      headers=cabecalho(csrf))
    assert r.status_code == 200
    checagem = aprovacao.checar(item)
    assert [(v.regra, v.confirmacao) for v in checagem.bloqueios] == [
        ("FORNECEDOR-TROCADO", False), ("FORNECEDOR-DESATIVADO", False)]
    assert checagem.bloqueios[0].mensagem == (
        f"A ordem foi montada para o fornecedor {antigo['id']}, mas o produto agora usa o "
        f"fornecedor {novo['id']}.")
    r = cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf))
    assert r.status_code == 409
    assert r.json()["detail"] == conformidade.motivo_do_bloqueio(checagem)
    assert not worker.PASTA_ORDENS.exists()  # nada gravado para o antigo

    # O worker recusa a ordem velha e monta outra, para o fornecedor atual.
    assert worker.montar_ordens_de_compra() == 1
    velha = linha("SELECT status, resultado FROM aprovacoes WHERE id = ?", item.id)
    assert velha["status"] == "recusada"
    assert velha["resultado"].startswith("Substituída pelo worker: A ordem foi montada para o "
                                         f"fornecedor {antigo['id']}")
    refeita, = aprovacao.pendentes()
    assert refeita.pedido_id == item.pedido_id
    assert (refeita.payload["fornecedor_id"], refeita.payload["fornecedor"],
            refeita.payload["contato"]) == (novo["id"], "Fornecedor Novo", "novo@x.example")
    assert "de Fornecedor Novo por R$ 18.50" in refeita.resumo
    assert not aprovacao.checar(refeita).bloqueado
    with conectar() as conn:
        eventos = "\n".join(l[0] for l in conn.execute("SELECT mensagem FROM eventos"))
    assert (f"Pedido {item.pedido_id} voltou para a fila: A ordem foi montada para o "
            f"fornecedor {antigo['id']}") in eventos
    assert historico(item.pedido_id)[-1]["para"] == Estado.AGUARDANDO_APROVACAO.value

    # Aprovar agora grava a ordem para F2, nunca para F1.
    r = cliente.post(f"/api/aprovar/{refeita.id}", headers=cabecalho(csrf))
    assert r.status_code == 200, r.text
    ordem = (worker.PASTA_ORDENS / f"oc_{item.pedido_id}.txt").read_text(encoding="utf-8")
    assert "Fornecedor: Fornecedor Novo\n" in ordem and "Antigo" not in ordem
    assert worker.montar_ordens_de_compra() == 0  # nada mais a refazer


def test_fornecedor_editado_com_a_ordem_na_fila_sai_com_os_dados_de_agora(
        monkeypatch, sessao, nota_fiscal_confirmada):
    """Nome, canal e contato do mesmo fornecedor editados depois de a ordem
    entrar na fila: a conformidade já olhava o canal de agora, mas o arquivo
    e a instrução saíam com os copiados na montagem (canal email, que o
    fornecedor não usa mais). Agora saem com os do cadastro na aprovação."""
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf, nome="Fab A", canal="email",
                                  contato="pedidos@fab-a.example")
    _, item = _compra_enfileirada(cliente, csrf, fornecedor["id"])
    assert (item.payload["fornecedor"], item.payload["canal"]) == ("Fab A", "email")
    r = cliente.patch(f"/api/fornecedores/{fornecedor['id']}",
                      json={"nome": "Fab A Nova", "canal": "whatsapp",
                            "contato": "+55 11 90000-0000"}, headers=cabecalho(csrf))
    assert r.status_code == 200, r.text
    assert not aprovacao.checar(item).bloqueado

    monkeypatch.setattr(config.config, "modo_simulacao", False)
    r = cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf))
    assert r.status_code == 200, r.text
    assert "Envie ao fornecedor Fab A Nova pelo canal whatsapp: o programa não envia." \
        in r.json()["resultado"]
    ordem = (worker.PASTA_ORDENS / f"oc_{item.pedido_id}.txt").read_text(encoding="utf-8")
    assert "Fornecedor: Fab A Nova\nCanal: whatsapp; contato: +55 11 90000-0000\n" in ordem
    assert "email" not in ordem and "fab-a.example" not in ordem

    # Fila de versão anterior, sem o id do fornecedor: os dados copiados na ordem.
    assert worker._fornecedor_da_ordem({"fornecedor": "F", "canal": "email", "contato": "c"}) == (
        "F", "email", "c")


def test_produto_sem_fornecedor_depois_da_fila_vai_para_problema(sessao, nota_fiscal_confirmada):
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto, item = _compra_enfileirada(cliente, csrf, fornecedor["id"])
    cliente.patch(f"/api/produtos/{produto['id']}", json={"fornecedor_id": None},
                  headers=cabecalho(csrf))
    assert [(v.regra, v.confirmacao) for v in aprovacao.checar(item).bloqueios] == [
        ("FORNECEDOR-TROCADO", False)]
    assert cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf)).status_code == 409

    assert worker.montar_ordens_de_compra() == 0
    assert aprovacao.pendentes() == []
    assert linha("SELECT status FROM aprovacoes WHERE id = ?", item.id)[0] == "recusada"
    ultimo = historico(item.pedido_id)[-1]
    assert (ultimo["para"], ultimo["motivo"]) == (Estado.PROBLEMA.value,
                                                  "Produto sem fornecedor vinculado")


def test_sku_editado_nao_desvia_a_checagem_para_outro_produto(sessao, nota_fiscal_confirmada):
    """O SKU é editável: a compra na fila para LUM-1 continua checando o
    produto e o fornecedor dela, mesmo com outro produto usando LUM-1 agora."""
    cliente, csrf = sessao
    desativado = criar_fornecedor(cliente, csrf, nome="F3")
    ativo = criar_fornecedor(cliente, csrf, nome="F2")
    produto, item = _compra_enfileirada(cliente, csrf, desativado["id"], sku="LUM-1")
    cliente.post(f"/api/fornecedores/{desativado['id']}/desativar", headers=cabecalho(csrf))
    assert [v.regra for v in aprovacao.checar(item).bloqueios] == ["FORNECEDOR-DESATIVADO"]

    cliente.patch(f"/api/produtos/{produto['id']}", json={"sku": "LUM-1-OLD"},
                  headers=cabecalho(csrf))
    assert [v.regra for v in aprovacao.checar(item).bloqueios] == ["FORNECEDOR-DESATIVADO"]

    criar_produto(cliente, csrf, sku="LUM-1", fornecedor_id=ativo["id"], categoria_regulada="nenhuma")
    assert [v.regra for v in aprovacao.checar(item).bloqueios] == ["FORNECEDOR-DESATIVADO"]
    assert cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf)).status_code == 409

    # A saída que se copia aponta o SKU de hoje do produto da compra.
    cliente.patch(f"/api/produtos/{produto['id']}", json={"categoria_regulada": None},
                  headers=cabecalho(csrf))
    categoria, = [v for v in aprovacao.checar(item).bloqueios if v.regra == "CATEGORIA-RESTRITA"]
    assert "python cli.py produto editar LUM-1-OLD --categoria-regulada nenhuma" in categoria.saida


def test_fila_de_versao_anterior_sem_o_fornecedor_falha_fechada_e_o_worker_refaz(
        sessao, nota_fiscal_confirmada):
    """Pendência gravada pela versão anterior: o payload não tem produto_id
    nem fornecedor_id. Não há como saber se a ordem vai ao fornecedor atual,
    então bloqueia; o próximo ciclo do worker a monta de novo."""
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto = criar_produto(cliente, csrf, fornecedor_id=fornecedor["id"],
                            categoria_regulada="nenhuma")
    pedido = inserir_pedido(produto["id"], estado=Estado.AGUARDANDO_APROVACAO.value,
                            custo_previsto=18.5, margem_prevista=30.0)
    antiga = aprovacao.enfileirar("compra_fornecedor", "[ROTINA] Comprar 1x ORG-001", {
        "pedido_id": pedido, "fornecedor": "Fornecedor Teste", "canal": "email",
        "contato": "pedidos@fornecedor.example", "sku": "ORG-001", "produto": "Organizador",
        "quantidade": 1, "valor": 18.5, "marketplace": "mercadolivre",
        "pedido_externo": "6000000001", "margem_prevista": 30.0}, pedido_id=pedido, valor=18.5)

    # Sem o id, nada do fornecedor é presumido: o prazo também falta. Sem a
    # moeda, o valor da fila antiga não confere com o custo em BRL de hoje.
    item = aprovacao.obter_pendente(antiga)
    assert [(v.regra, v.confirmacao) for v in aprovacao.checar(item).bloqueios] == [
        ("FORNECEDOR-TROCADO", True), ("CUSTO-ALTERADO", False), ("PRAZO", True)]
    assert cliente.post(f"/api/aprovar/{antiga}", headers=cabecalho(csrf)).status_code == 409

    assert worker.montar_ordens_de_compra() == 1
    assert linha("SELECT status FROM aprovacoes WHERE id = ?", antiga)[0] == "recusada"
    refeita, = aprovacao.pendentes()
    assert (refeita.payload["produto_id"], refeita.payload["fornecedor_id"]) == (
        produto["id"], fornecedor["id"])
    assert not aprovacao.checar(refeita).bloqueado


# ========================= 8. confirmações da conformidade pela API, sem SQL

def test_confirmacao_pela_api_libera_a_compra_que_esperava_na_fila(sessao, nota_fiscal_confirmada):
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto = criar_produto(cliente, csrf, fornecedor_id=fornecedor["id"])  # sem categoria
    inserir_pedido(produto["id"])
    assert worker.analisar_novos() == 1
    assert worker.montar_ordens_de_compra() == 1

    fila = cliente.get("/api/pendencias").json()
    assert fila["bloqueadas"] == 1
    violacao, = [v for v in fila["itens"][0]["violacoes"] if v["severidade"] == "bloqueio"]
    assert violacao["regra"] == "CATEGORIA-RESTRITA" and violacao["confirmacao"] is True
    # A saída aponta o cli.py e a API antes do SQL.
    assert "python cli.py produto editar ORG-001 --categoria-regulada nenhuma" in violacao["saida"]
    assert 'PATCH /api/produtos/<id> com {"categoria_regulada": "nenhuma"}' in violacao["saida"]

    r = cliente.patch(f"/api/produtos/{produto['id']}", json={"categoria_regulada": "nenhuma"},
                      headers=cabecalho(csrf))
    assert r.status_code == 200
    fila = cliente.get("/api/pendencias").json()
    assert fila["bloqueadas"] == 0 and not fila["itens"][0]["bloqueado"]


def test_cadastro_registra_quem_mudou_e_quais_campos_sem_os_valores(sessao):
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf, contato="segredo-do-contato@fornecedor.example")
    cliente.patch(f"/api/fornecedores/{fornecedor['id']}",
                  json={"contato": "outro-contato@fornecedor.example"}, headers=cabecalho(csrf))
    with conectar() as conn:
        eventos = [l[0] for l in conn.execute(
            "SELECT mensagem FROM eventos WHERE origem = 'cadastro' ORDER BY id")]
    assert eventos == [f"Fornecedor {fornecedor['id']} criado por {USUARIO}",
                       f"Fornecedor {fornecedor['id']} alterado por {USUARIO}: contato"]


def test_demo_continua_igual_com_a_moeda_explicita(monkeypatch, capsys, nota_fiscal_confirmada):
    import runpy
    from pathlib import Path

    monkeypatch.setattr(sys, "path", list(sys.path))
    runpy.run_path(str(Path(__file__).resolve().parent.parent / "demo.py"), run_name="__main__")
    saida = capsys.readouterr().out
    assert "2000000001    R$ 54.90" in saida
    with conectar() as conn:
        moedas = {l[0] for l in conn.execute(
            "SELECT custo_fornecedor_moeda FROM produtos UNION"
            " SELECT valor_bruto_moeda FROM pedidos UNION"
            " SELECT pedido_minimo_moeda FROM fornecedores")}
    assert moedas == {"BRL"}
    assert json.loads(linha("SELECT payload_json FROM aprovacoes ORDER BY id LIMIT 1")[0]
                      )["moeda"] == "BRL"
    assert conformidade.SEM_CATEGORIA_REGULADA == "nenhuma"


# ============ 9. ordem refeita com o custo de agora; saída de PROBLEMA; moeda

def _margem(venda: str, custo: str) -> Decimal:
    negocio = config.config.negocio
    return precificacao.calcular(Decimal(venda), Decimal(custo), Decimal("0.4"),
                                 aliquota_imposto_pct=negocio.aliquota_imposto_pct,
                                 margem_minima_pct=negocio.margem_minima_pct).margem_pct


@pytest.mark.parametrize("custo, moeda, entra", [
    ("20.00", "BRL", True),    # outro custo, a margem ainda fecha: ordem refeita com ele
    ("50.00", "BRL", False),   # a margem não fecha mais: PROBLEMA, nada na fila
    ("9.90", "USD", False),    # custo em outra moeda: PROBLEMA, sem misturar moedas
], ids=["refeita", "prejuizo", "outra-moeda"])
def test_troca_de_fornecedor_refaz_a_margem_com_o_custo_do_novo(sessao, nota_fiscal_confirmada,
                                                                 custo, moeda, entra):
    """A ordem refeita depois da troca de fornecedor levava o custo, a moeda e
    a margem calculados para o antigo: com o novo cobrando R$ 50.00, a compra
    saía por R$ 18.50 com margem de 36.99% e passava como Conforme. Agora a
    margem é refeita com o custo atual do produto, na regra de moeda da
    análise, e a pendência velha diz o que de fato aconteceu."""
    cliente, csrf = sessao
    antigo = criar_fornecedor(cliente, csrf, nome="Fab A")
    novo = criar_fornecedor(cliente, csrf, nome="Fab B")
    produto, item = _compra_enfileirada(cliente, csrf, antigo["id"])
    r = cliente.patch(f"/api/produtos/{produto['id']}",
                      json={"fornecedor_id": novo["id"],
                            "custo_fornecedor": {"valor": custo, "moeda": moeda}},
                      headers=cabecalho(csrf))
    assert r.status_code == 200
    assert {v.regra for v in aprovacao.checar(item).bloqueios} == {"FORNECEDOR-TROCADO",
                                                                    "CUSTO-ALTERADO"}
    assert cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf)).status_code == 409

    assert worker.montar_ordens_de_compra() == int(entra)
    velha = linha("SELECT status, resultado FROM aprovacoes WHERE id = ?", item.id)
    assert velha["status"] == "recusada"
    gravado = linha("SELECT estado, custo_previsto_dec, margem_prevista FROM pedidos"
                    " WHERE id = ?", item.pedido_id)
    if entra:
        refeita, = aprovacao.pendentes()
        assert (refeita.payload["fornecedor"], refeita.payload["valor"],
                refeita.payload["moeda"]) == ("Fab B", custo, "BRL")
        assert Decimal(str(refeita.payload["margem_prevista"])) == _margem("54.90", custo)
        assert f"de Fab B por R$ {custo} (margem " in refeita.resumo
        assert not aprovacao.checar(refeita).bloqueado
        assert gravado["estado"] == Estado.AGUARDANDO_APROVACAO.value
        assert velha["resultado"].startswith("Substituída pelo worker: A ordem foi montada para "
                                             f"o fornecedor {antigo['id']}")
        assert velha["resultado"].endswith("foi refeita com os dados atuais do produto e está "
                                           "na fila.")
        return

    assert aprovacao.pendentes() == []
    assert gravado["estado"] == Estado.PROBLEMA.value
    motivo = historico(item.pedido_id)[-1]["motivo"]
    assert motivo.startswith("A ordem de compra não foi montada. ")
    if moeda == "USD":
        assert "o custo está em USD e a venda em BRL" in motivo
        assert (gravado["custo_previsto_dec"], gravado["margem_prevista"]) == (None, None)
    else:
        assert f"Margem {_margem('54.90', custo)}% < mínimo" in motivo
        assert "com o custo atual do produto" in motivo
        assert gravado["custo_previsto_dec"] == custo
    # A pendência velha não diz que a ordem foi refeita: diz que não foi, e por quê.
    assert velha["resultado"].startswith("Recusada pelo worker: ")
    assert velha["resultado"].endswith(f"não foi refeita; o pedido foi para PROBLEMA: {motivo}")
    assert not worker.PASTA_ORDENS.exists()


def test_custo_editado_com_a_ordem_na_fila_bloqueia_e_o_worker_refaz(sessao,
                                                                      nota_fiscal_confirmada):
    """Só o custo mudou, com o mesmo fornecedor: aprovar pagaria o valor velho
    com a margem velha. CUSTO-ALTERADO bloqueia, e o worker refaz a ordem."""
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto, item = _compra_enfileirada(cliente, csrf, fornecedor["id"])
    cliente.patch(f"/api/produtos/{produto['id']}",
                  json={"custo_fornecedor": {"valor": "22.00", "moeda": "BRL"}},
                  headers=cabecalho(csrf))

    checagem = aprovacao.checar(item)
    assert [(v.regra, v.confirmacao) for v in checagem.bloqueios] == [("CUSTO-ALTERADO", False)]
    assert checagem.bloqueios[0].mensagem == ("A ordem diz R$ 18.50, mas o custo atual do "
                                              "produto dá R$ 22.00 (1 x R$ 22.00).")
    assert checagem.bloqueios[0].saida == conformidade.REFAZER_A_ORDEM
    r = cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf))
    assert r.status_code == 409 and r.json()["detail"] == conformidade.motivo_do_bloqueio(checagem)

    assert worker.montar_ordens_de_compra() == 1
    assert linha("SELECT status FROM aprovacoes WHERE id = ?", item.id)[0] == "recusada"
    refeita, = aprovacao.pendentes()
    assert (refeita.payload["valor"], refeita.payload["moeda"]) == ("22.00", "BRL")
    assert Decimal(str(refeita.payload["margem_prevista"])) == _margem("54.90", "22.00")
    assert not aprovacao.checar(refeita).bloqueado
    assert linha("SELECT custo_previsto_dec FROM pedidos WHERE id = ?",
                 item.pedido_id)[0] == "22.00"


def test_analisado_cujo_custo_mudou_vai_para_a_fila_com_o_custo_de_agora(sessao,
                                                                          nota_fiscal_confirmada):
    """O custo mudou entre a análise e a montagem (ou o ANALISADO é de outra
    época): a ordem sai com o custo e a margem de agora, não com o previsto."""
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto = criar_produto(cliente, csrf, fornecedor_id=fornecedor["id"],
                            categoria_regulada="nenhuma")
    pedido = inserir_pedido(produto["id"])
    assert worker.analisar_novos() == 1
    cliente.patch(f"/api/produtos/{produto['id']}",
                  json={"custo_fornecedor": {"valor": "25.00", "moeda": "BRL"}},
                  headers=cabecalho(csrf))

    assert worker.montar_ordens_de_compra() == 1
    item, = aprovacao.pendentes()
    assert item.pedido_id == pedido
    assert (item.payload["valor"], item.payload["moeda"]) == ("25.00", "BRL")
    assert Decimal(str(item.payload["margem_prevista"])) == _margem("54.90", "25.00")
    assert not aprovacao.checar(item).bloqueado


def test_compra_simulada_volta_para_a_fila_com_o_custo_de_agora(monkeypatch, sessao,
                                                                fila_de_compras):
    """Aprovada em simulação, a compra volta para a fila quando a simulação
    desliga; se o custo mudou nesse meio-tempo, volta com o custo novo. A
    outra compra do mesmo produto, que esperava na fila, é refeita também."""
    cliente, csrf = sessao
    simulada = fila_de_compras[0]
    assert cliente.post(f"/api/aprovar/{simulada}",
                        headers=cabecalho(csrf)).json()["simulado"] is True
    cliente.patch("/api/produtos/1", json={"custo_fornecedor": {"valor": "21.00", "moeda": "BRL"}},
                  headers=cabecalho(csrf))
    monkeypatch.setattr(config.config, "modo_simulacao", False)

    assert worker.montar_ordens_de_compra() == 2
    fila = aprovacao.pendentes()
    assert len(fila) == 2
    assert {(i.payload["valor"], i.payload["moeda"]) for i in fila} == {("21.00", "BRL")}
    assert not any(aprovacao.checar(i).bloqueado for i in fila)
    assert worker.montar_ordens_de_compra() == 0  # não duplica


def test_produto_desativado_nao_e_comprado(sessao, nota_fiscal_confirmada):
    """Desativar o produto não mudava nada: os pedidos dele eram importados,
    analisados, postos na fila e aprováveis. Agora a conformidade bloqueia a
    compra (PRODUTO-DESATIVADO), na fila e antes dela; reativado, a compra da
    fila libera e o pedido em PROBLEMA volta pela reanálise."""
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto, item = _compra_enfileirada(cliente, csrf, fornecedor["id"])
    r = cliente.post(f"/api/produtos/{produto['id']}/desativar", headers=cabecalho(csrf))
    assert r.status_code == 200 and r.json()["ativo"] is False

    checagem = aprovacao.checar(item)
    assert [(v.regra, v.confirmacao) for v in checagem.bloqueios] == [("PRODUTO-DESATIVADO", False)]
    assert "python cli.py produto editar ORG-001 --reativar" in checagem.bloqueios[0].saida
    assert cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf)).status_code == 409

    # O pedido novo é importado e analisado (a venda existe), mas não vai à fila.
    novo = inserir_pedido(produto["id"], id_externo="6000000002")
    assert worker.analisar_novos() == 1
    assert worker.montar_ordens_de_compra() == 0
    ultimo = historico(novo)[-1]
    assert ultimo["para"] == Estado.PROBLEMA.value and "PRODUTO-DESATIVADO" in ultimo["motivo"]
    assert [i.id for i in aprovacao.pendentes()] == [item.id]

    cliente.patch(f"/api/produtos/{produto['id']}", json={"ativo": True}, headers=cabecalho(csrf))
    assert not aprovacao.checar(item).bloqueado
    r = cliente.post(f"/api/pedidos/{novo}/reanalisar", headers=cabecalho(csrf))
    assert r.status_code == 200, r.text
    assert worker.analisar_novos() == 1 and worker.montar_ordens_de_compra() == 1
    assert linha("SELECT estado FROM pedidos WHERE id = ?", novo)[0] == \
        Estado.AGUARDANDO_APROVACAO.value


def test_pedido_preso_por_falta_de_moeda_volta_com_pedido_reanalisar(monkeypatch, capsys,
                                                                     nota_fiscal_confirmada):
    """Banco migrado: o produto tem custo e não tem moeda. O pedido ia para
    PROBLEMA mandando cadastrar a moeda, e cadastrar não bastava: o worker só
    analisa NOVO, e PROBLEMA -> ANALISADO esbarrava no custo previsto ausente.
    Agora o motivo termina no comando que devolve o pedido à análise."""
    with conectar() as conn:
        conn.execute("INSERT INTO fornecedores (id, nome, canal, contato, prazo_dias)"
                     " VALUES (1, 'Fornecedor Teste', 'email', 'pedidos@fornecedor.example', 4)")
        produto = conn.execute(
            "INSERT INTO produtos (sku, titulo, custo_fornecedor, custo_fornecedor_dec, peso_kg,"
            " fornecedor_id, categoria_regulada, criado_em) VALUES ('VELHO-1', 'Antigo', 18.5,"
            " '18.50', 0.4, 1, 'nenhuma', ?)", (agora(),)).lastrowid
    pedido = inserir_pedido(produto)
    assert worker.analisar_novos() == 0
    assert historico(pedido)[-1]["motivo"].endswith(
        "Cadastre o custo com a moeda: python cli.py produto editar VELHO-1 --custo VALOR "
        f"--moeda BRL; depois, python cli.py pedido reanalisar {pedido}")

    codigo, _, erro = rodar_cli(monkeypatch, capsys, "produto", "editar", "VELHO-1",
                                "--custo", "18.50", "--moeda", "BRL")
    assert codigo == 0, erro
    worker.analisar_novos()
    worker.montar_ordens_de_compra()
    assert linha("SELECT estado FROM pedidos WHERE id = ?", pedido)[0] == Estado.PROBLEMA.value

    codigo, saida, erro = rodar_cli(monkeypatch, capsys, "pedido", "reanalisar", str(pedido))
    assert codigo == 0, erro
    assert saida.strip() == worker.REANALISE_PEDIDA.format(id=pedido)
    ultimo = historico(pedido)[-1]
    assert (ultimo["de"], ultimo["para"], ultimo["motivo"], ultimo["automatico"]) == (
        "PROBLEMA", "NOVO", "Reanálise pedida por cli", 0)

    assert worker.analisar_novos() == 1
    assert worker.montar_ordens_de_compra() == 1
    item, = aprovacao.pendentes()
    assert item.pedido_id == pedido
    assert (item.payload["valor"], item.payload["moeda"]) == ("18.50", "BRL")
    assert not aprovacao.checar(item).bloqueado


def test_moeda_da_venda_nao_informada_entra_so_pelo_dono_e_fica_registrada(cliente, sessao,
                                                                            nota_fiscal_confirmada):
    """Pedido gravado antes desta versão (ou sem currency_id): sem moeda da
    venda, não havia como dar uma a ele. A reanálise aceita a moeda dita pelo
    dono, só para pedido sem nenhuma, e registra quem disse."""
    logado, csrf = sessao
    fornecedor = criar_fornecedor(logado, csrf)
    produto = criar_produto(logado, csrf, fornecedor_id=fornecedor["id"],
                            categoria_regulada="nenhuma")
    pedido = inserir_pedido(produto["id"], moeda=None)
    assert worker.analisar_novos() == 0
    assert (f"Se você sabe a moeda desta venda, registre-a: python cli.py pedido reanalisar "
            f"{pedido} --moeda-venda BRL") in historico(pedido)[-1]["motivo"]

    caminho = f"/api/pedidos/{pedido}/reanalisar"
    assert cliente.post(caminho, json={"moeda_venda": "BRL"}).status_code == 401
    assert logado.post(caminho, json={"moeda_venda": "BRL"}).status_code == 403
    assert erros(logado.post(caminho, json={"moeda_venda": "brl"}, headers=cabecalho(csrf))) == {
        "moeda_venda": "use 3 letras maiúsculas (ISO 4217), por exemplo BRL ou USD."}
    assert erros(logado.post(caminho, json={"moeda": "BRL"}, headers=cabecalho(csrf))) == {
        "moeda": "campo desconhecido; confira o nome (campo extra não é aceito)."}
    assert tuple(linha("SELECT estado, valor_bruto_moeda FROM pedidos WHERE id = ?", pedido)) == (
        Estado.PROBLEMA.value, None)  # recusado não grava nada

    r = logado.post(caminho, json={"moeda_venda": "BRL"}, headers=cabecalho(csrf))
    assert r.status_code == 200, r.text
    assert r.json() == {"pedido_id": pedido, "estado": "NOVO", "moeda_venda": "BRL",
                        "mensagem": worker.REANALISE_PEDIDA.format(id=pedido)}
    assert linha("SELECT valor_bruto_moeda FROM pedidos WHERE id = ?", pedido)[0] == "BRL"
    with conectar() as conn:
        eventos = [l[0] for l in conn.execute("SELECT mensagem FROM eventos WHERE origem = 'pedidos'")]
    assert eventos == [f"Pedido {pedido}: moeda da venda registrada como BRL por {USUARIO}; o "
                       "marketplace não a informou."]
    assert historico(pedido)[-1]["motivo"] == (f"Reanálise pedida por {USUARIO}, com a moeda da "
                                               f"venda BRL informada por {USUARIO}")
    assert worker.analisar_novos() == 1

    # A moeda que o marketplace informou não muda.
    outro = inserir_pedido(produto["id"], id_externo="6000000002", estado=Estado.PROBLEMA.value)
    assert erros(logado.post(f"/api/pedidos/{outro}/reanalisar", json={"moeda_venda": "USD"},
                             headers=cabecalho(csrf))) == {
        "moeda_venda": "o pedido já tem a moeda da venda (BRL), a que o marketplace informou; "
                       "ela não muda."}


def test_reanalisar_recusa_pedido_do_qual_uma_compra_pode_ter_saido(monkeypatch, capsys, sessao,
                                                                    nota_fiscal_confirmada):
    """Reanalisar devolve o pedido à fila de compras: se alguma ordem dele pode
    ter sido gravada, ou ainda está na fila, comprar de novo seria comprar
    duas vezes. Recusa com 409, e nada muda."""
    cliente, csrf = sessao
    fornecedor = criar_fornecedor(cliente, csrf)
    produto, item = _compra_enfileirada(cliente, csrf, fornecedor["id"])
    pedido = item.pedido_id
    caminho = f"/api/pedidos/{pedido}/reanalisar"

    r = cliente.post(caminho, headers=cabecalho(csrf))
    assert r.status_code == 409
    assert r.json()["detail"] == ("Só pedido em PROBLEMA é reanalisado; o pedido "
                                  f"{pedido} está em AGUARDANDO_APROVACAO.")

    transicionar(pedido, Estado.PROBLEMA, "teste")
    r = cliente.post(caminho, headers=cabecalho(csrf))
    assert r.status_code == 409
    assert f"está na fila (pendência {item.id}); aprove-a ou recuse-a" in r.json()["detail"]

    # A ordem de verdade foi gravada: o pedido passou por COMPRA_ENVIADA.
    monkeypatch.setattr(config.config, "modo_simulacao", False)
    assert cliente.post(f"/api/aprovar/{item.id}", headers=cabecalho(csrf)).status_code == 200
    transicionar(pedido, Estado.PROBLEMA, "o fornecedor não respondeu")
    r = cliente.post(caminho, headers=cabecalho(csrf))
    assert r.status_code == 409 and "já passou por COMPRA_ENVIADA" in r.json()["detail"]
    codigo, _, erro = rodar_cli(monkeypatch, capsys, "pedido", "reanalisar", str(pedido))
    assert codigo == 1 and "já passou por COMPRA_ENVIADA" in erro and "Traceback" not in erro
    assert linha("SELECT estado FROM pedidos WHERE id = ?", pedido)[0] == Estado.PROBLEMA.value

    # Aprovação que falhou pode ter gravado o arquivo; a simulada não gravou nada.
    def em_problema_com_aprovacao(id_externo, status, resultado):
        outro = inserir_pedido(produto["id"], id_externo=id_externo, estado=Estado.PROBLEMA.value)
        aprovacao_id = aprovacao.enfileirar("compra_fornecedor", "x", {"pedido_id": outro},
                                            pedido_id=outro)
        with conectar() as conn:
            conn.execute("UPDATE aprovacoes SET status = ?, resultado = ? WHERE id = ?",
                         (status, resultado, aprovacao_id))
        return outro

    falhou = em_problema_com_aprovacao("6000000002", "erro", "disco cheio")
    r = cliente.post(f"/api/pedidos/{falhou}/reanalisar", headers=cabecalho(csrf))
    assert r.status_code == 409 and "ficou 'erro'" in r.json()["detail"]
    simulada = em_problema_com_aprovacao("6000000003", "executada",
                                         aprovacao.simular("ordem de teste"))
    assert cliente.post(f"/api/pedidos/{simulada}/reanalisar",
                        headers=cabecalho(csrf)).status_code == 200

    # Pedido que não existe: 404, pela API e pelo cli.py, sem traceback.
    r = cliente.post("/api/pedidos/999/reanalisar", headers=cabecalho(csrf))
    assert r.status_code == 404 and r.json()["detail"] == "Pedido não encontrado."
    for alvo in ("999", "abc"):
        codigo, _, erro = rodar_cli(monkeypatch, capsys, "pedido", "reanalisar", alvo)
        assert (codigo, erro.strip()) == (1, "Pedido não encontrado.")


def test_api_devolve_a_moeda_dos_pedidos_e_da_fila(sessao):
    """O painel ainda formata tudo como R$ (painel.html espera o dono). A API
    passa a devolver a moeda ao lado do valor; null é moeda não informada."""
    cliente, _ = sessao
    inserir_pedido(None, valor="9.90", moeda="USD", id_externo="7000000001")
    inserir_pedido(None, valor="54.90", moeda=None, id_externo="7000000002")
    pedidos = {p["id_externo"]: p for p in cliente.get("/api/pedidos").json()["pedidos"]}
    assert (pedidos["7000000001"]["valor_bruto_dec"],
            pedidos["7000000001"]["valor_bruto_moeda"]) == ("9.90", "USD")
    assert (pedidos["7000000002"]["valor_bruto_dec"],
            pedidos["7000000002"]["valor_bruto_moeda"]) == ("54.90", None)

    aprovacao.enfileirar("compra_fornecedor", "nova", {"valor": "18.50", "moeda": "BRL"}, valor=18.5)
    aprovacao.enfileirar("compra_fornecedor", "antiga", {"valor": 18.5}, valor=18.5)
    itens = {i["resumo"]: i for i in cliente.get("/api/pendencias").json()["itens"]}
    assert (itens["nova"]["moeda"], itens["antiga"]["moeda"]) == ("BRL", None)


def test_readme_diz_a_saida_de_problema_o_produto_desativado_e_o_limite_do_painel():
    """Os achados de documentação: o que o README promete é o que o código faz."""
    texto = " ".join((Path(__file__).resolve().parent.parent / "README.md")
                     .read_text(encoding="utf-8").split())
    # Atualizar um banco antigo: cadastrar a moeda antes do worker, e a saída de PROBLEMA.
    assert "before the worker runs" in texto
    assert "python cli.py pedido reanalisar 12 --moeda-venda BRL" in texto
    assert "POST /api/pedidos/{id}/reanalisar" in texto
    # Produto desativado não é comprado.
    assert "A deactivated product is not purchased either (`PRODUTO-DESATIVADO`)" in texto
    # O limite do painel e da API.
    assert "The panel screen (`painel.html`) and `cli.py pendencias` still format every amount " \
           "as R$" in texto
    # A ordem refeita passa pela margem de novo.
    assert "the margin is computed again with the current cost" in texto
    assert "(`CUSTO-ALTERADO`)" in texto
