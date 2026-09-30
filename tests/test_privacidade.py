"""Dado do comprador: cifrado no banco, mascarado nas respostas da API."""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

from apoio import COMPRADOR
from config import config
from core import privacidade
from db import conectar

NOME = f"{COMPRADOR['first_name']} {COMPRADOR['last_name']}"


def test_pii_fica_cifrado_em_repouso(pedido_cifrado):
    with conectar() as conn:
        nome, identificador, endereco = conn.execute(
            "SELECT comprador_nome, comprador_id, endereco_json FROM pedidos WHERE id = ?",
            (pedido_cifrado,)).fetchone()

    for cifrado in (nome, identificador, endereco):
        assert "Sintetico" not in cifrado and "424242" not in cifrado
        assert "shipping_id" not in cifrado

    # Só a chave do ambiente abre o dado.
    fernet = Fernet(os.environ["CHAVE_LGPD"].encode())
    assert fernet.decrypt(nome.encode()).decode() == NOME
    assert fernet.decrypt(identificador.encode()).decode() == "424242"
    assert json.loads(fernet.decrypt(endereco.encode())) == {"shipping_id": "777"}

    # Nem o arquivo do banco guarda o texto em claro em lugar nenhum.
    bruto = Path(config.db_path).read_bytes()
    assert b"Sintetico" not in bruto and b"424242" not in bruto


def test_api_mascara_o_comprador_e_nao_registra_leitura(sessao, pedido_cifrado):
    cliente, _ = sessao

    r = cliente.get(f"/api/pedido/{pedido_cifrado}")
    assert r.status_code == 200
    pedido = r.json()
    assert pedido["comprador_nome"] == "Comp" + "•" * 8
    assert "comprador_id" not in pedido and "endereco_json" not in pedido
    assert "Sintetico" not in r.text and "424242" not in r.text

    lista = cliente.get("/api/pedidos")
    assert lista.status_code == 200
    assert "comprador" not in lista.text and "Sintetico" not in lista.text

    lgpd = cliente.get("/api/lgpd").json()
    assert lgpd["registros_com_pii"] == 1
    assert lgpd["acessos"] == []  # a tela mascarada não conta como leitura de PII


def test_revelar_devolve_o_dado_e_deixa_rastro(sessao, pedido_cifrado):
    cliente, _ = sessao

    r = cliente.get(f"/api/pedido/{pedido_cifrado}/comprador")
    assert r.json() == {"nome": NOME, "identificador": "424242",
                        "endereco": {"shipping_id": "777"}}

    acessos = cliente.get("/api/lgpd").json()["acessos"]
    assert [(a["operacao"], a["finalidade"]) for a in acessos] == [
        ("leitura", "conferência de entrega pelo operador")]


def test_chave_vem_do_ambiente_e_nenhum_arquivo_de_chave_e_criado(pedido_cifrado):
    assert privacidade.decifrar(privacidade.cifrar("teste")) == "teste"
    assert not privacidade.ARQ_CHAVE.exists()
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM eventos WHERE origem = 'lgpd'").fetchone()[0] == 0
