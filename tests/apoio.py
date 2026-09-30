"""Constantes e dublês usados por mais de um arquivo de teste.

Não importa nada do projeto no topo: o conftest.py precisa preparar o
ambiente antes disso.
"""
USUARIO = "operador.teste"
SENHA = "senha-de-teste-12345"

COMPRADOR = {"id": 424242, "first_name": "Comprador", "last_name": "Sintetico"}


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
