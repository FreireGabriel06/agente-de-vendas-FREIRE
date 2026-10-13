"""Lote S2 da auditoria de segurança de 06/10/2026.

S-05: uma quebra de linha num valor de "salvar configuração" virava uma linha
nova no .env (MODO_SIMULACAO=false, DB_PATH de outra instância, ...).
S-81: fora do pytest não havia trava. O demo.py apagava pedidos, produtos e
fornecedores de qualquer banco, e o `cofre listar` criava tabela num banco
que só devia ler.
"""
import runpy
import sqlite3
import sys
from pathlib import Path

import pytest

from apoio import cabecalho
from db import agora, conectar

RAIZ = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ S-05: .env

@pytest.mark.parametrize("valor", [
    "id-publico\nMODO_SIMULACAO=false",
    "id-publico\r\nDB_PATH=C:/outra-instancia/agente.db",
    "id-publico\rHOST_PAINEL=0.0.0.0",
    "id\x00publico",
    "id\x1bpublico",
])
def test_salvar_configuracao_recusa_caractere_de_controle_e_nao_toca_no_env(sessao, isolamento, valor):
    cliente, csrf = sessao
    env = isolamento / ".env"
    env.write_text("ML_CLIENT_ID=antigo\n", encoding="utf-8")

    r = cliente.post("/api/configuracao/salvar", json={"ML_CLIENT_ID": valor},
                     headers=cabecalho(csrf))

    assert r.status_code == 400
    assert "ML_CLIENT_ID" in r.json()["detail"]
    assert env.read_text(encoding="utf-8") == "ML_CLIENT_ID=antigo\n"


def test_salvar_configuracao_valida_continua_gravando(sessao, isolamento):
    cliente, csrf = sessao
    r = cliente.post("/api/configuracao/salvar", json={"ML_CLIENT_ID": "id-publico-ml"},
                     headers=cabecalho(csrf))
    assert r.status_code == 200
    linhas = (isolamento / ".env").read_text(encoding="utf-8").splitlines()
    assert "ML_CLIENT_ID=id-publico-ml" in linhas


@pytest.mark.parametrize("chaves", [
    {"ML_CLIENT_ID": "a\nMODO_SIMULACAO=false"},
    {"MARGEM_MINIMA_PCT": "18\r\nDB_PATH=x"},
    {"ml_client_id": "minusculo"},
    {"ML_CLIENT_ID=X": "nome com igual"},
    {"": "vazio"},
])
def test_gravar_env_recusa_valor_ou_nome_que_quebraria_o_arquivo(isolamento, chaves):
    from painel import configurar

    env = isolamento / ".env"
    env.write_text("# configuração\n", encoding="utf-8")
    with pytest.raises(ValueError):
        configurar.gravar_env(chaves)
    assert env.read_text(encoding="utf-8") == "# configuração\n"


# ------------------------------------------------------------ S-81: demo.py

def rodar_demo(monkeypatch, *argumentos):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", ["demo.py", *argumentos])
    runpy.run_path(str(RAIZ / "demo.py"), run_name="__main__")


def criar_produto_real():
    with conectar() as conn:
        conn.execute("INSERT INTO produtos (sku, titulo, custo_fornecedor, criado_em)"
                     " VALUES ('REAL-1', 'Produto do cliente', 10, ?)", (agora(),))


def test_demo_nao_apaga_banco_com_dados_que_nao_sao_dele(monkeypatch, capsys):
    criar_produto_real()

    with pytest.raises(SystemExit) as saida:
        rodar_demo(monkeypatch)

    assert saida.value.code == 2
    texto = capsys.readouterr().out
    assert "não foi carregado" in texto and "--apagar-dados-existentes" in texto
    with conectar() as conn:
        assert [l["sku"] for l in conn.execute("SELECT sku FROM produtos")] == ["REAL-1"]


def test_demo_nao_apaga_fila_de_resposta_ao_comprador(monkeypatch):
    with conectar() as conn:
        conn.execute("INSERT INTO aprovacoes (tipo, resumo, payload_json, status, criado_em)"
                     " VALUES ('resposta_cliente', 'rascunho', '{}', 'pendente', ?)", (agora(),))

    with pytest.raises(SystemExit) as saida:
        rodar_demo(monkeypatch)

    assert saida.value.code == 2
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM aprovacoes").fetchone()[0] == 1


def test_demo_roda_de_novo_sobre_os_proprios_dados(monkeypatch, capsys, nota_fiscal_confirmada):
    rodar_demo(monkeypatch)
    rodar_demo(monkeypatch)
    assert "ANÁLISE DE MARGEM" in capsys.readouterr().out
    with conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM produtos").fetchone()[0] == 3


def test_demo_apaga_dados_existentes_so_quando_pedido(monkeypatch, nota_fiscal_confirmada):
    criar_produto_real()
    rodar_demo(monkeypatch, "--apagar-dados-existentes")
    with conectar() as conn:
        skus = {l["sku"] for l in conn.execute("SELECT sku FROM produtos")}
    assert "REAL-1" not in skus and "ORG-001" in skus


@pytest.mark.parametrize("iniciador", ["iniciar.bat", "iniciar.sh"])
def test_iniciador_nao_diz_que_carregou_o_exemplo_quando_o_demo_recusa(iniciador):
    texto = (RAIZ / iniciador).read_text(encoding="utf-8")
    linha = next(l for l in texto.splitlines() if "demo.py" in l)
    # O aviso de sucesso só sai quando o demo.py terminou bem.
    assert "&&" in linha and "||" in linha


# ------------------------------------------------------------ S-81: cofre listar

def test_cofre_listar_nao_cria_tabela_num_banco_sem_cofre(isolamento):
    from config import config
    from core import cofre

    with conectar() as conn:
        conn.execute("DROP TABLE IF EXISTS credenciais")

    assert cofre.listar() == []

    with sqlite3.connect(config.db_path) as conn:
        tabelas = {l[0] for l in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "credenciais" not in tabelas


def test_cofre_listar_nao_cria_banco_que_nao_existe(isolamento, monkeypatch):
    from config import config
    from core import cofre

    monkeypatch.setattr(config, "db_path", str(isolamento / "nao-existe.db"))
    assert cofre.listar() == []
    assert not (isolamento / "nao-existe.db").exists()


def test_cli_cofre_listar_so_le(isolamento, monkeypatch, capsys):
    import cli
    from config import config

    with conectar() as conn:
        conn.execute("DROP TABLE IF EXISTS credenciais")
    monkeypatch.setattr(sys, "argv", ["cli.py", "cofre", "listar"])
    cli.main()

    assert "Cofre vazio" in capsys.readouterr().out
    with sqlite3.connect(config.db_path) as conn:
        tabelas = {l[0] for l in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "credenciais" not in tabelas
