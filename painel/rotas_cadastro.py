"""
Rotas do cadastro de produtos e fornecedores, e a reanálise de pedido em
PROBLEMA. instalar_erros(app) registra as respostas 422, 409 e 404.
"""
import json
from decimal import Decimal

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core import cadastro
from fastapi import APIRouter

rotas = APIRouter()

# ------------------------------------------------- Produtos e fornecedores
#
# Cadastro sem SQL à mão. A sessão e o CSRF são os de painel/protecao.py, como
# nas outras rotas; a validação é a de core/cadastro, a mesma do cli.py.
# Não existe DELETE: desativar mantém pedidos, fila e histórico apontando
# para o registro. Erro de campo: 422 com a lista "erros" em português;
# SKU repetido: 409; id que não existe: 404.

async def _erro_de_cadastro(request: Request, erro: cadastro.ErroValidacao):
    return JSONResponse({"detail": "Dados inválidos: confira os campos.", "erros": erro.erros},
                        status_code=422)


async def _conflito_de_cadastro(request: Request, erro: cadastro.Conflito):
    return JSONResponse({"detail": str(erro),
                         "erros": [{"campo": erro.campo, "mensagem": str(erro)}]},
                        status_code=409)


async def _nao_encontrado(request: Request, erro: cadastro.NaoEncontrado):
    return JSONResponse({"detail": str(erro)}, status_code=404)


def _sem_nan(constante: str):
    raise ValueError(f"{constante} não é número")


async def _corpo_do_cadastro(request: Request):
    """O JSON do corpo com os números decimais lidos direto em Decimal
    (parse_float), sem passar por float. NaN e Infinity são recusados."""
    try:
        return json.loads(await request.body() or b"null",
                          parse_float=Decimal, parse_constant=_sem_nan)
    # ValueError: JSON quebrado e UTF-8 inválido; RecursionError: aninhamento
    # sem fim; ArithmeticError: decimal.InvalidOperation, que não é ValueError,
    # de um expoente que nem o Decimal representa (1e9999999999999999999).
    except (ValueError, RecursionError, ArithmeticError):
        raise cadastro.ErroValidacao([{"campo": "corpo",
                                       "mensagem": "JSON inválido; envie um objeto JSON."}]) from None


async def _gravar(funcao, *args, request: Request):
    """Lê o corpo e grava fora do laço de eventos: o SQLite pode esperar até
    5 s por um banco ocupado, e isso não pode travar o painel."""
    dados = await _corpo_do_cadastro(request)
    return await run_in_threadpool(funcao, *args, dados, ator=request.state.operador)


@rotas.get("/api/produtos")
def api_produtos(ativo: str | None = None):
    produtos = cadastro.listar_produtos(cadastro.filtro_ativo(ativo))
    return {"produtos": produtos, "total": len(produtos)}


@rotas.get("/api/produtos/{produto_id}")
def api_produto(produto_id: str):
    return cadastro.obter_produto(cadastro.id_do_texto(produto_id, "Produto"))


@rotas.post("/api/produtos", status_code=201)
async def api_criar_produto(request: Request):
    return await _gravar(cadastro.criar_produto, request=request)


@rotas.patch("/api/produtos/{produto_id}")
async def api_editar_produto(produto_id: str, request: Request):
    alvo = cadastro.id_do_texto(produto_id, "Produto")
    return await _gravar(cadastro.editar_produto, alvo, request=request)


@rotas.post("/api/produtos/{produto_id}/desativar")
def api_desativar_produto(produto_id: str, request: Request):
    return cadastro.desativar_produto(cadastro.id_do_texto(produto_id, "Produto"),
                                      ator=request.state.operador)


@rotas.get("/api/fornecedores")
def api_fornecedores(ativo: str | None = None):
    fornecedores = cadastro.listar_fornecedores(cadastro.filtro_ativo(ativo))
    return {"fornecedores": fornecedores, "total": len(fornecedores)}


@rotas.get("/api/fornecedores/{fornecedor_id}")
def api_fornecedor(fornecedor_id: str):
    return cadastro.obter_fornecedor(cadastro.id_do_texto(fornecedor_id, "Fornecedor"))


@rotas.post("/api/fornecedores", status_code=201)
async def api_criar_fornecedor(request: Request):
    return await _gravar(cadastro.criar_fornecedor, request=request)


@rotas.patch("/api/fornecedores/{fornecedor_id}")
async def api_editar_fornecedor(fornecedor_id: str, request: Request):
    alvo = cadastro.id_do_texto(fornecedor_id, "Fornecedor")
    return await _gravar(cadastro.editar_fornecedor, alvo, request=request)


@rotas.post("/api/fornecedores/{fornecedor_id}/desativar")
def api_desativar_fornecedor(fornecedor_id: str, request: Request):
    return cadastro.desativar_fornecedor(cadastro.id_do_texto(fornecedor_id, "Fornecedor"),
                                         ator=request.state.operador)


@rotas.post("/api/pedidos/{pedido_id}/reanalisar")
async def api_reanalisar_pedido(pedido_id: str, request: Request):
    """Pedido em PROBLEMA de volta a NOVO, depois de corrigido o cadastro
    (worker.reanalisar, o mesmo do cli.py pedido reanalisar). Corpo opcional
    {"moeda_venda": "BRL"}, só para pedido sem moeda. Pedido de que alguma
    compra pode ter saído: 409."""
    from worker import reanalisar
    alvo = cadastro.id_do_texto(pedido_id, "Pedido")
    return await _gravar(reanalisar, alvo, request=request)


def instalar_erros(app) -> None:
    app.add_exception_handler(cadastro.ErroValidacao, _erro_de_cadastro)
    app.add_exception_handler(cadastro.Conflito, _conflito_de_cadastro)
    app.add_exception_handler(cadastro.NaoEncontrado, _nao_encontrado)
