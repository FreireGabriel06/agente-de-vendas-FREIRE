"""Constantes e dublês usados por mais de um arquivo de teste.

Não importa nada do projeto no topo: o conftest.py precisa preparar o
ambiente antes disso.
"""
import types
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


class AnthropicFalso:
    """Faz as vezes da classe anthropic.Anthropic, sem rede. Guarda os
    argumentos de cada cliente criado, de cada beta.messages.create e de cada
    models.retrieve, e devolve a resposta (ou levanta o erro) que o teste
    escolheu."""

    def __init__(self, resposta=None, erro=None):
        self.resposta = resposta
        self.erro = erro
        self.criados = []     # kwargs de anthropic.Anthropic(...)
        self.mensagens = []   # kwargs de client.beta.messages.create(...)
        self.consultas = []   # model_id de client.models.retrieve(...)

    def __call__(self, **kwargs):
        self.criados.append(kwargs)
        return types.SimpleNamespace(
            # Só o caminho beta: é ele que aceita betas=[...] e fallbacks.
            beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=self._criar)),
            models=types.SimpleNamespace(retrieve=self._consultar),
        )

    def _criar(self, **kwargs):
        self.mensagens.append(kwargs)
        if self.erro is not None:
            raise self.erro
        return self.resposta

    def _consultar(self, model_id, **kwargs):
        self.consultas.append(model_id)
        if self.erro is not None:
            raise self.erro
        return types.SimpleNamespace(id=model_id, display_name="Claude", type="model")


def resposta_claude(*textos, stop_reason="end_turn"):
    """Uma resposta no formato do SDK: bloco de raciocínio (vazio, como vem
    com display omitido) e os blocos de texto."""
    blocos = [types.SimpleNamespace(type="thinking", thinking="", signature="sig")]
    blocos += [types.SimpleNamespace(type="text", text=t) for t in textos]
    return types.SimpleNamespace(stop_reason=stop_reason, content=blocos,
                                 model="claude-opus-5-5", stop_details=None)


class MercadoLivreFalso:
    """Devolve pedidos no formato da API do ML, sem rede."""

    def __init__(self, brutos):
        self.brutos = brutos

    def pedidos_recentes(self, limit=50):
        return list(self.brutos)

    def normalizar_pedido(self, bruto):
        from conectores.mercadolivre import MercadoLivre
        return MercadoLivre.normalizar_pedido(None, bruto)
