"""
Cadastro de produtos e fornecedores, sem SQL à mão.

A API do painel (painel/app.py) e o cli.py (produto, fornecedor) chamam as
funções daqui: a validação é uma só, então uma porta não aceita o que a outra
recusa.

  - Campo desconhecido é recusado: um nome digitado errado não some calado.
  - Erro de campo vem em português, com o caminho do campo
    (ErroValidacao.erros = [{"campo": ..., "mensagem": ...}]). O painel
    responde 422; o cli.py imprime e sai com código 1.
  - Dinheiro é um objeto {"valor": "18.50", "moeda": "BRL"}: não há como
    mandar valor sem moeda. Decimal do começo ao fim (core/dinheiro.py).
  - Nada é apagado. Desativar mantém o histórico e as referências de pedidos
    e da fila. Fornecedor desativado não recebe produto novo, e a
    conformidade bloqueia a compra que dependeria dele (FORNECEDOR-DESATIVADO):
    o worker manda o pedido para PROBLEMA e a fila recusa a aprovação. Produto
    desativado também não é comprado (PRODUTO-DESATIVADO); o pedido dele
    continua sendo importado, porque a venda existe no marketplace.
  - As confirmações da conformidade (categoria_regulada,
    habilitacao_confirmada, reembalagem_confirmada) entram aqui com tipo
    estrito: true, false ou null.
  - Datas gravadas por db.agora(): UTC, ISO 8601 com o fuso.
"""
import sqlite3
import unicodedata
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError
from pydantic_core import PydanticCustomError

from core import conformidade, dinheiro
from db import agora, conectar, registrar_evento

CANAIS = ("email", "whatsapp", "api", "portal")
CATEGORIAS_REGULADAS = (conformidade.SEM_CATEGORIA_REGULADA, *conformidade.CATEGORIAS_RESTRITAS)

TAMANHO_MAXIMO = {"sku": 64, "titulo": 200, "categoria_ml": 40, "nome": 120,
                  "contato": 200, "observacoes": 1000}
PRAZO_MINIMO, PRAZO_MAXIMO = 1, 365
PESO_MAXIMO = Decimal("1000")
CASAS_PESO = 3
ID_MAXIMO = 2 ** 63 - 1  # maior inteiro do SQLite

MOEDA_OBRIGATORIA = ("obrigatória: todo valor leva a moeda (código ISO 4217), "
                     "por exemplo BRL ou USD.")
USE_OBJETO_DINHEIRO = 'use um objeto com valor e moeda, por exemplo {"valor": "18.50", "moeda": "BRL"}.'


# ------------------------------------------------------------------ Erros

class ErroValidacao(ValueError):
    """Dados recusados. erros: [{"campo": "custo_fornecedor.moeda", "mensagem": "..."}]."""

    def __init__(self, erros: list[dict]):
        self.erros = erros
        super().__init__("; ".join(f"{e['campo']}: {e['mensagem']}" for e in erros))


class Conflito(ValueError):
    """O dado já existe (SKU repetido). O painel responde 409."""

    def __init__(self, campo: str, mensagem: str):
        self.campo = campo
        super().__init__(mensagem)


class NaoEncontrado(LookupError):
    """O painel responde 404."""


def _erro_de_campo(campo: str, mensagem: str) -> ErroValidacao:
    return ErroValidacao([{"campo": campo, "mensagem": mensagem}])


# -------------------------------------------------------------- Validadores
#
# Cada regra levanta a própria mensagem em português. O template é fixo e a
# mensagem vai no contexto, para que chaves ({}) no texto não sejam lidas
# como campos de formatação.

def _recusa(mensagem: str) -> PydanticCustomError:
    return PydanticCustomError("cadastro", "{mensagem}", {"mensagem": mensagem})


# Categorias Unicode recusadas no texto: controle (Cc: C0, DEL e C1, como o
# U+0085, que splitlines() lê como quebra de linha), formato (Cf: as
# marcas de direção, como o U+202E, que embaralham a linha da fila na tela) e
# os separadores de linha e de parágrafo (Zl, Zp: U+2028, U+2029).
_CATEGORIAS_DE_CONTROLE = {"Cc", "Cf", "Zl", "Zp"}


def _texto(maximo: int, *, quebra_de_linha: bool = False):
    permitidos = {"\n", "\r", "\t"} if quebra_de_linha else set()

    def validar(valor):
        if valor is None:
            raise _recusa("não pode ser null.")
        if not isinstance(valor, str):
            raise _recusa("use texto.")
        texto = valor.strip()
        if not texto:
            raise _recusa("não pode ficar vazio.")
        if len(texto) > maximo:
            raise _recusa(f"use no máximo {maximo} caracteres (veio com {len(texto)}).")
        if any(unicodedata.category(c) in _CATEGORIAS_DE_CONTROLE and c not in permitidos
               for c in texto):
            raise _recusa("tem caractere de controle (quebra de linha, tabulação); tire-o.")
        return texto
    return BeforeValidator(validar)


def _inteiro(minimo: int, maximo: int, faixa: str | None = None):
    faixa = faixa or f"use um número inteiro de {minimo} a {maximo}."

    def validar(valor):
        # bool é int no Python: true não pode virar 1 dia.
        if isinstance(valor, bool) or not isinstance(valor, int):
            raise _recusa("use um número inteiro.")
        if not minimo <= valor <= maximo:
            raise _recusa(faixa)
        return valor
    return BeforeValidator(validar)


def _numero(casas: int, *, maior_que_zero: bool, maximo: Decimal | None = None):
    def validar(valor):
        if valor is None:
            raise _recusa("não pode ser null.")
        try:
            numero = dinheiro.validar_valor(valor, casas=casas)
        except dinheiro.ValorInvalido as e:
            raise _recusa(str(e))
        if maior_que_zero and numero <= 0:
            raise _recusa("precisa ser maior que zero.")
        if not maior_que_zero and numero < 0:
            raise _recusa("não pode ser negativo.")
        if maximo is not None and numero > maximo:
            raise _recusa(f"use no máximo {maximo}.")
        return numero
    return BeforeValidator(validar)


def _moeda(valor):
    try:
        return dinheiro.validar_moeda(valor)
    except dinheiro.ValorInvalido as e:
        raise _recusa(str(e))


def _sim_nao(valor):
    if not isinstance(valor, bool):
        raise _recusa('use true, false ou null, sem aspas (1, 0 e "sim" não valem aqui).')
    return valor


def _ligado(valor):
    if not isinstance(valor, bool):
        raise _recusa("use true ou false, sem aspas.")
    return valor


def _canal(valor):
    if not isinstance(valor, str) or valor not in CANAIS:
        raise _recusa(f"use um destes: {', '.join(CANAIS)}.")
    return valor


def _categoria_regulada(valor):
    if isinstance(valor, str) and valor.strip().lower() in CATEGORIAS_REGULADAS:
        return valor.strip().lower()
    raise _recusa(f"use '{conformidade.SEM_CATEGORIA_REGULADA}' ou uma destas: "
                  f"{', '.join(conformidade.CATEGORIAS_RESTRITAS)}.")


Sku = Annotated[str, _texto(TAMANHO_MAXIMO["sku"])]
Titulo = Annotated[str, _texto(TAMANHO_MAXIMO["titulo"])]
CategoriaML = Annotated[str, _texto(TAMANHO_MAXIMO["categoria_ml"])]
Nome = Annotated[str, _texto(TAMANHO_MAXIMO["nome"])]
Contato = Annotated[str, _texto(TAMANHO_MAXIMO["contato"])]
Observacoes = Annotated[str, _texto(TAMANHO_MAXIMO["observacoes"], quebra_de_linha=True)]
Peso = Annotated[Decimal, _numero(CASAS_PESO, maior_que_zero=True, maximo=PESO_MAXIMO)]
Prazo = Annotated[int, _inteiro(PRAZO_MINIMO, PRAZO_MAXIMO)]
IdFornecedor = Annotated[int, _inteiro(1, ID_MAXIMO, "use o id de um fornecedor (inteiro positivo).")]
Canal = Annotated[str, BeforeValidator(_canal)]
CategoriaRegulada = Annotated[str, BeforeValidator(_categoria_regulada)]
Confirmacao = Annotated[bool, BeforeValidator(_sim_nao)]
Ativo = Annotated[bool, BeforeValidator(_ligado)]
Moeda = Annotated[str, BeforeValidator(_moeda)]


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ValorPositivo(_Modelo):
    valor: Annotated[Decimal, _numero(dinheiro.CASAS_MAXIMAS, maior_que_zero=True)]
    moeda: Moeda


class ValorNaoNegativo(_Modelo):
    valor: Annotated[Decimal, _numero(dinheiro.CASAS_MAXIMAS, maior_que_zero=False)]
    moeda: Moeda


class ProdutoNovo(_Modelo):
    sku: Sku
    titulo: Titulo
    categoria_ml: CategoriaML | None = None
    custo_fornecedor: ValorPositivo
    peso_kg: Peso
    fornecedor_id: IdFornecedor | None = None
    categoria_regulada: CategoriaRegulada | None = None
    habilitacao_confirmada: Confirmacao | None = None
    reembalagem_confirmada: Confirmacao | None = None


class ProdutoAlteracao(_Modelo):
    """Só os campos enviados mudam. O padrão None não é validado; null
    enviado num campo obrigatório do cadastro é recusado pelo validador."""
    sku: Sku = None
    titulo: Titulo = None
    categoria_ml: CategoriaML | None = None
    custo_fornecedor: ValorPositivo = None
    peso_kg: Peso = None
    fornecedor_id: IdFornecedor | None = None
    categoria_regulada: CategoriaRegulada | None = None
    habilitacao_confirmada: Confirmacao | None = None
    reembalagem_confirmada: Confirmacao | None = None
    ativo: Ativo = None


class FornecedorNovo(_Modelo):
    nome: Nome
    canal: Canal
    contato: Contato
    prazo_dias: Prazo
    pedido_minimo: ValorNaoNegativo | None = None
    observacoes: Observacoes | None = None


class FornecedorAlteracao(_Modelo):
    nome: Nome = None
    canal: Canal = None
    contato: Contato = None
    prazo_dias: Prazo = None
    pedido_minimo: ValorNaoNegativo | None = None
    observacoes: Observacoes | None = None
    ativo: Ativo = None


class ReanalisePedido(_Modelo):
    """Corpo de POST /api/pedidos/{id}/reanalisar e opções do cli.py pedido
    reanalisar (worker.reanalisar): a moeda da venda, só quando o pedido não
    tem nenhuma; a mesma lista de moedas do cadastro."""
    moeda_venda: Moeda | None = None


# --------------------------------------------------------------- Tradução

_OBJETOS_DE_DINHEIRO = {"custo_fornecedor", "pedido_minimo"}


def _traduzir(erro: dict) -> dict:
    loc = [str(parte) for parte in erro.get("loc", ())]
    campo = ".".join(loc) or "corpo"
    tipo = erro.get("type")
    if tipo == "cadastro":
        mensagem = erro["msg"]
    elif tipo == "missing":
        mensagem = MOEDA_OBRIGATORIA if loc and loc[-1] == "moeda" else "obrigatório."
    elif tipo == "extra_forbidden":
        mensagem = "campo desconhecido; confira o nome (campo extra não é aceito)."
    elif loc and loc[-1] in _OBJETOS_DE_DINHEIRO:
        mensagem = USE_OBJETO_DINHEIRO
    else:
        mensagem = "valor inválido."
    return {"campo": campo, "mensagem": mensagem}


def _validar(modelo: type[_Modelo], dados) -> _Modelo:
    if not isinstance(dados, dict):
        raise _erro_de_campo("corpo", "envie um objeto JSON com os campos.")
    try:
        return modelo.model_validate(dados)
    except ValidationError as e:
        raise ErroValidacao([_traduzir(x) for x in e.errors(include_url=False)]) from None


def _alteracoes(modelo: type[_Modelo], dados) -> dict:
    validado = _validar(modelo, dados)
    campos = {nome: getattr(validado, nome) for nome in validado.model_fields_set}
    if not campos:
        raise _erro_de_campo("corpo", "informe pelo menos um campo para alterar.")
    return campos


def validar_reanalise(dados) -> str | None:
    """A moeda da venda informada para a reanálise, ou None. Corpo vazio
    (null) vale um objeto sem campos; campo desconhecido é recusado."""
    return _validar(ReanalisePedido, {} if dados is None else dados).moeda_venda


def filtro_ativo(valor: str | None) -> bool | None:
    """?ativo=true|false da listagem (e --ativo do cli.py); sem ele, lista todos."""
    if valor is None:
        return None
    if valor in ("true", "false"):
        return valor == "true"
    raise _erro_de_campo("ativo", "use true ou false.")


def id_do_texto(texto, oque: str) -> int:
    """Id que veio como texto (caminho da rota, argumento do cli.py). O que
    não for um inteiro positivo não existe: 404, sem a mensagem em inglês do
    conversor do FastAPI ou do argparse."""
    texto = str(texto).strip()
    if texto.isascii() and texto.isdigit() and 0 < len(texto) <= 18 and int(texto) > 0:
        return int(texto)
    raise NaoEncontrado(f"{oque} não encontrado.")


# ------------------------------------------------------------ Banco: escrita

class _Real:
    """Valor para coluna REAL antiga, gravado com CAST(? AS REAL) a partir
    do texto decimal: o float nasce no SQLite, não aqui."""

    def __init__(self, texto: str):
        self.texto = texto


def _colunas_de_dinheiro(nome: str, valor) -> dict:
    if valor is None:
        return {nome: None, f"{nome}_dec": None, f"{nome}_moeda": None}
    texto = dinheiro.texto(valor.valor)
    return {nome: _Real(texto), f"{nome}_dec": texto, f"{nome}_moeda": valor.moeda}


def _colunas(campos: dict) -> dict:
    colunas = {}
    for nome, valor in campos.items():
        if nome in _OBJETOS_DE_DINHEIRO:
            colunas.update(_colunas_de_dinheiro(nome, valor))
        elif nome == "peso_kg":
            colunas[nome] = _Real(dinheiro.texto_exato(valor))
        elif isinstance(valor, bool):
            colunas[nome] = int(valor)
        else:
            colunas[nome] = valor
    return colunas


def _marcadores(colunas: dict) -> tuple[list[str], list]:
    marcadores, parametros = [], []
    for valor in colunas.values():
        if isinstance(valor, _Real):
            marcadores.append("CAST(? AS REAL)")
            parametros.append(valor.texto)
        else:
            marcadores.append("?")
            parametros.append(valor)
    return marcadores, parametros


def _inserir(conn, tabela: str, colunas: dict) -> int:
    marcadores, parametros = _marcadores(colunas)
    # Os nomes de coluna vêm dos modelos acima, nunca do corpo da requisição
    # (campo desconhecido é recusado antes).
    return conn.execute(f"INSERT INTO {tabela} ({', '.join(colunas)}) "
                        f"VALUES ({', '.join(marcadores)})", parametros).lastrowid


def _atualizar(conn, tabela: str, registro_id: int, colunas: dict):
    marcadores, parametros = _marcadores(colunas)
    atribuicoes = ", ".join(f"{c} = {m}" for c, m in zip(colunas, marcadores))
    conn.execute(f"UPDATE {tabela} SET {atribuicoes} WHERE id = ?", [*parametros, registro_id])


def _sku_repetido(erro: sqlite3.IntegrityError) -> bool:
    return "produtos.sku" in str(erro)


def _checar_sku(conn, sku: str, proprio_id: int | None = None):
    linha = conn.execute("SELECT id FROM produtos WHERE sku = ?", (sku,)).fetchone()
    if linha is not None and linha["id"] != proprio_id:
        raise Conflito("sku", f"Já existe um produto com o SKU '{sku}' (id {linha['id']}).")


def _checar_fornecedor(conn, fornecedor_id: int | None):
    if fornecedor_id is None:
        return
    linha = conn.execute("SELECT ativo FROM fornecedores WHERE id = ?", (fornecedor_id,)).fetchone()
    if linha is None:
        raise _erro_de_campo("fornecedor_id", f"o fornecedor {fornecedor_id} não existe.")
    if linha["ativo"] == 0:
        raise _erro_de_campo("fornecedor_id", f"o fornecedor {fornecedor_id} está desativado; "
                                              "reative-o ou escolha outro.")


# ------------------------------------------------------------ Banco: leitura

def _dinheiro_da_linha(linha, nome: str) -> dict | None:
    """O que o worker enxerga: valor em Decimal (texto) e moeda lida como ele
    lê; moeda None é "moeda não informada" (linha antiga ou SQL à mão)."""
    valor = dinheiro.ler(linha[f"{nome}_dec"], linha[nome])
    moeda = dinheiro.moeda_lida(linha[f"{nome}_moeda"])
    if valor is None and moeda is None and linha[nome] is None and linha[f"{nome}_dec"] is None:
        return None
    return {"valor": dinheiro.texto(valor) if valor is not None else None, "moeda": moeda}


def _ativo(valor) -> bool:
    return valor != 0  # NULL (linha antiga) conta como ativo, como o DEFAULT 1


def _produto(linha) -> dict:
    peso = dinheiro.do_real(linha["peso_kg"])
    return {
        "id": linha["id"],
        "sku": linha["sku"],
        "titulo": linha["titulo"],
        "categoria_ml": linha["categoria_ml"],
        "custo_fornecedor": _dinheiro_da_linha(linha, "custo_fornecedor"),
        "peso_kg": dinheiro.texto_exato(peso) if peso is not None else None,
        "fornecedor_id": linha["fornecedor_id"],
        "ativo": _ativo(linha["ativo"]),
        "categoria_regulada": linha["categoria_regulada"],
        # A mesma leitura da conformidade: o que sai aqui é o que ela enxerga.
        "habilitacao_confirmada": conformidade._sim_nao(linha["habilitacao_confirmada"]),
        "reembalagem_confirmada": conformidade._sim_nao(linha["reembalagem_confirmada"]),
        "criado_em": linha["criado_em"],
        "atualizado_em": linha["atualizado_em"],
    }


def _fornecedor(linha) -> dict:
    return {
        "id": linha["id"],
        "nome": linha["nome"],
        "canal": linha["canal"],
        "contato": linha["contato"],
        "prazo_dias": linha["prazo_dias"],
        "pedido_minimo": _dinheiro_da_linha(linha, "pedido_minimo"),
        "observacoes": linha["observacoes"],
        "ativo": _ativo(linha["ativo"]),
        "criado_em": linha["criado_em"],
        "atualizado_em": linha["atualizado_em"],
    }


def _listar(tabela: str, ativo: bool | None) -> list:
    sql = f"SELECT * FROM {tabela}"
    parametros: tuple = ()
    if ativo is not None:
        sql += " WHERE (COALESCE(ativo, 1) != 0) = ?"
        parametros = (int(ativo),)
    with conectar() as conn:
        return conn.execute(sql + " ORDER BY id", parametros).fetchall()


# ------------------------------------------------------------------ Produtos

def listar_produtos(ativo: bool | None = None) -> list[dict]:
    return [_produto(l) for l in _listar("produtos", ativo)]


def obter_produto(produto_id: int) -> dict:
    with conectar() as conn:
        linha = conn.execute("SELECT * FROM produtos WHERE id = ?", (produto_id,)).fetchone()
    if linha is None:
        raise NaoEncontrado("Produto não encontrado.")
    return _produto(linha)


def id_do_sku(sku: str) -> int:
    with conectar() as conn:
        linha = conn.execute("SELECT id FROM produtos WHERE sku = ?", (sku.strip(),)).fetchone()
    if linha is None:
        raise NaoEncontrado(f"Produto com SKU '{sku.strip()}' não encontrado.")
    return linha["id"]


def criar_produto(dados, ator: str) -> dict:
    produto = _validar(ProdutoNovo, dados)
    colunas = _colunas(dict(produto))
    colunas.update(ativo=1, criado_em=agora(), atualizado_em=agora())
    try:
        with conectar() as conn:
            _checar_sku(conn, produto.sku)
            _checar_fornecedor(conn, produto.fornecedor_id)
            produto_id = _inserir(conn, "produtos", colunas)
    except sqlite3.IntegrityError as e:
        if _sku_repetido(e):  # outro cadastro gravou o mesmo SKU no meio do caminho
            raise Conflito("sku", f"Já existe um produto com o SKU '{produto.sku}'.") from None
        raise
    registrar_evento("info", "cadastro", f"Produto {produto_id} criado por {ator}")
    return obter_produto(produto_id)


def editar_produto(produto_id: int, dados, ator: str, *, verbo: str = "alterado") -> dict:
    campos = _alteracoes(ProdutoAlteracao, dados)
    colunas = _colunas(campos)
    colunas["atualizado_em"] = agora()
    try:
        with conectar() as conn:
            if conn.execute("SELECT 1 FROM produtos WHERE id = ?", (produto_id,)).fetchone() is None:
                raise NaoEncontrado("Produto não encontrado.")
            if "sku" in campos:
                _checar_sku(conn, campos["sku"], produto_id)
            if "fornecedor_id" in campos:
                _checar_fornecedor(conn, campos["fornecedor_id"])
            _atualizar(conn, "produtos", produto_id, colunas)
    except sqlite3.IntegrityError as e:
        if _sku_repetido(e):
            raise Conflito("sku", f"Já existe um produto com o SKU '{campos['sku']}'.") from None
        raise
    # O registro diz quem mudou e quais campos, nunca os valores.
    registrar_evento("info", "cadastro", f"Produto {produto_id} {verbo} por {ator}: "
                                         f"{', '.join(sorted(campos))}")
    return obter_produto(produto_id)


def desativar_produto(produto_id: int, ator: str) -> dict:
    """Desativa sem apagar: pedidos e fila continuam apontando para ele, e a
    conformidade bloqueia a compra dele (PRODUTO-DESATIVADO) até reativar."""
    return editar_produto(produto_id, {"ativo": False}, ator, verbo="desativado")


# -------------------------------------------------------------- Fornecedores

def listar_fornecedores(ativo: bool | None = None) -> list[dict]:
    return [_fornecedor(l) for l in _listar("fornecedores", ativo)]


def obter_fornecedor(fornecedor_id: int) -> dict:
    with conectar() as conn:
        linha = conn.execute("SELECT * FROM fornecedores WHERE id = ?", (fornecedor_id,)).fetchone()
    if linha is None:
        raise NaoEncontrado("Fornecedor não encontrado.")
    return _fornecedor(linha)


def criar_fornecedor(dados, ator: str) -> dict:
    fornecedor = _validar(FornecedorNovo, dados)
    colunas = _colunas(dict(fornecedor))
    colunas.update(ativo=1, criado_em=agora(), atualizado_em=agora())
    with conectar() as conn:
        fornecedor_id = _inserir(conn, "fornecedores", colunas)
    registrar_evento("info", "cadastro", f"Fornecedor {fornecedor_id} criado por {ator}")
    return obter_fornecedor(fornecedor_id)


def editar_fornecedor(fornecedor_id: int, dados, ator: str, *, verbo: str = "alterado") -> dict:
    campos = _alteracoes(FornecedorAlteracao, dados)
    colunas = _colunas(campos)
    colunas["atualizado_em"] = agora()
    with conectar() as conn:
        if conn.execute("SELECT 1 FROM fornecedores WHERE id = ?",
                        (fornecedor_id,)).fetchone() is None:
            raise NaoEncontrado("Fornecedor não encontrado.")
        _atualizar(conn, "fornecedores", fornecedor_id, colunas)
        vinculados = conn.execute(
            "SELECT COUNT(*) FROM produtos WHERE fornecedor_id = ? AND COALESCE(ativo, 1) != 0",
            (fornecedor_id,)).fetchone()[0]
    registrar_evento("info", "cadastro", f"Fornecedor {fornecedor_id} {verbo} por {ator}: "
                                         f"{', '.join(sorted(campos))}")
    resposta = obter_fornecedor(fornecedor_id)
    if not resposta["ativo"] and vinculados:
        resposta["aviso"] = (f"{vinculados} produto(s) ativo(s) ainda usam este fornecedor. "
                             "As compras deles ficam bloqueadas pela conformidade até você "
                             "trocar o fornecedor do produto ou reativar este.")
    return resposta


def desativar_fornecedor(fornecedor_id: int, ator: str) -> dict:
    """Desativa sem apagar: produtos e ordens antigas continuam apontando para ele."""
    return editar_fornecedor(fornecedor_id, {"ativo": False}, ator, verbo="desativado")
