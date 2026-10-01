"""Constantes e dublês usados por mais de um arquivo de teste.

Não importa nada do projeto no topo: o conftest.py precisa preparar o
ambiente antes disso.
"""
from pathlib import Path

USUARIO = "operador.teste"
SENHA = "senha-de-teste-12345"

COMPRADOR = {"id": 424242, "first_name": "Comprador", "last_name": "Sintetico"}


def pasta_de_teste_recusada(pasta_dados, banco, pasta_teste, raiz) -> str | None:
    """Por que os testes não podem rodar, ou None. A pasta do repositório pode
    ser a pasta de dados de verdade do dono (.env, agente.db, chaves, tokens):
    nenhum teste usa essa pasta, nem pasta dentro dela, nem banco nela."""
    pasta_dados, banco, pasta_teste, raiz = (
        Path(p).resolve() for p in (pasta_dados, banco, pasta_teste, raiz))
    if pasta_dados == raiz or pasta_dados.is_relative_to(raiz):
        return f"a pasta de dados é a do repositório ({pasta_dados})"
    if banco.is_relative_to(raiz):
        return f"o banco está na pasta do repositório ({banco})"
    if pasta_dados != pasta_teste or banco.parent != pasta_teste:
        return f"o projeto não está usando a pasta de teste ({pasta_dados})"
    return None


def cabecalho(csrf: str) -> dict:
    return {"X-CSRF-Token": csrf}


class MercadoLivreFalso:
    """Devolve pedidos no formato da API do ML, sem rede."""

    def __init__(self, brutos):
        self.brutos = brutos

    def pedidos_recentes(self, limit=50):
        return list(self.brutos)

    def normalizar_pedido(self, bruto):
        from conectores.mercadolivre import MercadoLivre
        return MercadoLivre.normalizar_pedido(None, bruto)
