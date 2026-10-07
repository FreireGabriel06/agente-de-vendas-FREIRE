"""
Motor de conformidade.

A questão da Amazon não se resolve com um aviso no README — resolve-se com
código que impede a ação. Este módulo é consultado pelo worker antes de pôr
uma compra na fila e de novo na aprovação, pelo painel e pelo cli.py, com a
mesma função (verificar_pendencia). Violação dura manda o pedido para
PROBLEMA; falta de confirmação deixa o item na fila, bloqueado, com o motivo
e a saída, até alguém preencher o dado.

Falha fechada: a regra que se aplica ao item e não tem o dado de que precisa
não passa em branco. Ela devolve um bloqueio "falta confirmar", que diz qual
dado falta e onde preencher. Nenhum contexto presume o valor que libera.

Cada regra tem fonte declarada. Quando a plataforma mudar a política, você
sabe qual regra revisar e onde conferir.
"""
import re
from dataclasses import dataclass, field
from enum import Enum

from config import _DESLIGADO, _LIGADO, config
from core import dinheiro
from db import conectar


class Severidade(str, Enum):
    BLOQUEIO = "bloqueio"      # a ação não sai, ponto
    ALERTA = "alerta"          # sai, mas você é avisado


@dataclass
class Violacao:
    regra: str
    severidade: Severidade
    mensagem: str
    saida: str
    fonte: str
    # True: bloqueia porque falta um dado, não porque o dado reprova. Some
    # sozinho quando alguém preenche a confirmação.
    confirmacao: bool = False


@dataclass
class Resultado:
    violacoes: list[Violacao] = field(default_factory=list)

    @property
    def bloqueios(self) -> list[Violacao]:
        return [v for v in self.violacoes if v.severidade == Severidade.BLOQUEIO]

    @property
    def bloqueado(self) -> bool:
        return bool(self.bloqueios)

    @property
    def so_falta_confirmar(self) -> bool:
        """Bloqueado só por dado que falta: nenhuma regra reprovou um dado."""
        return self.bloqueado and all(v.confirmacao for v in self.bloqueios)

    @property
    def resumo(self) -> str:
        if not self.violacoes:
            return "Conforme"
        return " | ".join(f"{v.regra}: {v.mensagem}" for v in self.violacoes)


def motivo_do_bloqueio(resultado: Resultado) -> str:
    """O texto único da recusa: a API do painel (HTTP 409) e o cli.py aprovar
    mostram exatamente esta frase."""
    return "Bloqueado pela conformidade: " + "; ".join(v.mensagem for v in resultado.bloqueios)


# Tipos de ação da fila (core/aprovacao.py) que têm regras definidas aqui.
# Tipo fora desta lista é bloqueado: sem regra, não há como dizer que passa.
TIPOS = ("compra_fornecedor", "resposta_cliente", "ajuste_preco", "publicar_anuncio")

# Sem o prazo prometido no anúncio, o prazo do fornecedor não pode passar
# disto. É o mesmo limite que o worker aplica antes de montar a compra.
PRAZO_MAXIMO_SEM_ANUNCIO = 7

CATEGORIAS_RESTRITAS = {
    "suplemento": "Registro/notificação na Anvisa e rotulagem conforme RDC.",
    "cosmetico": "Registro ou notificação Anvisa.",
    "medicamento": "Venda restrita a farmácia licenciada.",
    "alimento": "Registro sanitário e rastreabilidade de lote.",
    "brinquedo": "Certificação compulsória Inmetro.",
    "eletrico": "Certificação Inmetro para produtos energizados.",
    "eletronico_radio": "Homologação Anatel para qualquer produto com rádio (Wi-Fi, Bluetooth).",
    "puericultura": "Certificação Inmetro compulsória.",
    "airsoft": "Registro no Exército.",
    "agrotoxico": "Registro Mapa/Ibama.",
}
SEM_CATEGORIA_REGULADA = "nenhuma"


def _falta(regra: str, mensagem: str, saida: str, fonte: str) -> Violacao:
    return Violacao(regra=regra, severidade=Severidade.BLOQUEIO, mensagem=mensagem,
                    saida=saida, fonte=fonte, confirmacao=True)


# Opção do `cli.py produto editar` que preenche cada confirmação do produto.
_OPCAO_NO_CLI = {
    "categoria_regulada": "--categoria-regulada",
    "habilitacao_confirmada": "--habilitacao",
    "reembalagem_confirmada": "--reembalagem",
}


# SKU que vai como está num comando para colar no terminal (PowerShell, cmd,
# bash): só letras e algarismos ASCII, ponto, hífen e sublinhado, sem começar
# por hífen (o argparse leria como opção). Aspas não bastam: dentro delas o
# PowerShell e o bash ainda expandem $(...).
_SKU_PARA_TERMINAL = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")


def sku_no_comando(sku) -> str:
    """O SKU para a linha de comando que se copia, ou o marcador SKU quando
    ele tem caractere que o terminal interpretaria (X;calc, X$(calc), A&B)."""
    texto = str(sku or "")
    return texto if _SKU_PARA_TERMINAL.fullmatch(texto) else "SKU"


def _no_produto(ctx: dict, coluna: str, valor: bool | str) -> str:
    """Como preencher a confirmação no cadastro do produto (core/cadastro.py):
    pelo cli.py ou pela API do painel. O SQL equivalente fica por último, para
    quem ainda usa banco à mão. valor: True ou o texto da categoria."""
    sku = str(ctx.get("sku") or "SKU")
    no_cli = sku_no_comando(sku)
    if valor is True:
        cli, em_json, em_sql = "sim", "true", "1"
    else:
        cli, em_json, em_sql = valor, f'"{valor}"', f"'{valor}'"
    sku_sql = sku.replace("'", "''")
    troque = (" (troque SKU pelo SKU do produto, que está no SQL abaixo: ele tem caracteres "
              "que o terminal interpretaria)" if no_cli != sku else "")
    return (f"python cli.py produto editar {no_cli} {_OPCAO_NO_CLI[coluna]} {cli}{troque} "
            f"(ou PATCH /api/produtos/<id> com {{\"{coluna}\": {em_json}}}; em SQL: "
            f"UPDATE produtos SET {coluna} = {em_sql} WHERE sku = '{sku_sql}';)")


# ---------------------------------------------------------------- Regras
#
# Cada regra declara a que tipos de ação se aplica. Dentro do tipo, dado que
# falta vira bloqueio "falta confirmar" (_falta), nunca um "passa".

REGRAS: list[tuple] = []


def _regra(*tipos: str):
    def registrar(funcao):
        REGRAS.append((funcao, frozenset(tipos)))
        return funcao
    return registrar


@_regra("compra_fornecedor")
def _amazon_remetente_terceiro(ctx: dict) -> Violacao | None:
    """
    A Amazon exige que o vendedor registrado seja o único identificado em nota,
    embalagem e romaneio. Envio direto da fábrica ao comprador viola a política
    de dropshipping e é motivo de suspensão com retenção de saldo.
    """
    if ctx.get("marketplace") != "amazon":
        return None
    fonte = "Amazon Seller Central — Política de Dropshipping"
    envio_direto = ctx.get("envio_direto_fornecedor")
    if envio_direto is None:
        return _falta(
            "AMZ-DROPSHIP",
            "Falta saber se o fornecedor despacha direto ao comprador (pedido Amazon).",
            "Vincule o produto a um fornecedor com canal cadastrado (email, whatsapp, "
            "api ou portal): python cli.py produto editar SKU --fornecedor ID; o canal "
            "muda com python cli.py fornecedor editar ID --canal email (ou pela API do "
            "painel, /api/produtos e /api/fornecedores).",
            fonte,
        )
    if envio_direto:
        return Violacao(
            regra="AMZ-DROPSHIP",
            severidade=Severidade.BLOQUEIO,
            mensagem="Envio direto do fornecedor ao comprador é proibido na Amazon.",
            saida="Receba a mercadoria, reembale sem identificação do fornecedor "
                  "e despache com seus dados. Ou use FBA.",
            fonte=fonte,
        )
    return None


@_regra("compra_fornecedor")
def _amazon_identificacao_fornecedor(ctx: dict) -> Violacao | None:
    if ctx.get("marketplace") != "amazon":
        return None
    fonte = "Amazon Seller Central — Política de Dropshipping"
    confirmada = ctx.get("reembalagem_confirmada")
    if confirmada is None:
        return _falta(
            "AMZ-REEMBALAGEM",
            "Falta confirmar a reembalagem deste produto para pedido Amazon.",
            "Depois de garantir que nota, caixa e romaneio saem sem o nome do "
            "fornecedor, marque no produto: " + _no_produto(ctx, "reembalagem_confirmada", True),
            fonte,
        )
    if not confirmada:
        return Violacao(
            regra="AMZ-REEMBALAGEM",
            severidade=Severidade.BLOQUEIO,
            mensagem="Produto marcado como sem reembalagem (reembalagem_confirmada = 0 "
                     "ou 'não') em pedido Amazon.",
            saida="Só marque 1 depois de garantir que nota, caixa e romaneio saem "
                  "sem o nome do fornecedor. Ou use FBA.",
            fonte=fonte,
        )
    return None


FORNECEDOR_TROCADO = "FORNECEDOR-TROCADO"
CUSTO_ALTERADO = "CUSTO-ALTERADO"
# Regras cujo bloqueio o worker resolve sozinho: recusa a ordem velha e monta
# outra com os dados atuais do produto, refazendo a margem (worker.py).
REFAZEM_A_ORDEM = frozenset({FORNECEDOR_TROCADO, CUSTO_ALTERADO})
REFAZER_A_ORDEM = ("O worker recusa esta ordem e a monta de novo, com o fornecedor e o custo "
                   "atuais do produto e a margem refeita, no próximo ciclo (ou agora: python "
                   "cli.py ciclo).")


@_regra("compra_fornecedor")
def _fornecedor_trocado(ctx: dict) -> Violacao | None:
    """A ordem na fila vai para o fornecedor gravado nela quando foi montada
    (nome, canal e contato copiados no payload). Se o produto passou a outro
    fornecedor, aprovar mandaria a compra ao antigo: bloqueia, e o worker
    monta a ordem de novo com o fornecedor atual (montar_ordens_de_compra).
    Fila gravada por versão anterior, sem o id do fornecedor: falha fechada."""
    fonte = "Cadastro de produtos e fornecedores (core/cadastro.py)"
    da_ordem = ctx.get("fornecedor_da_ordem")
    if da_ordem is None:
        return _falta(
            FORNECEDOR_TROCADO,
            "Falta o id do fornecedor desta ordem (fila gravada por uma versão anterior).",
            REFAZER_A_ORDEM,
            fonte,
        )
    do_produto = ctx.get("fornecedor_do_produto")
    if do_produto != da_ordem:
        agora_usa = (f"usa o fornecedor {do_produto}" if do_produto is not None
                     else "não tem fornecedor")
        return Violacao(
            regra=FORNECEDOR_TROCADO,
            severidade=Severidade.BLOQUEIO,
            mensagem=f"A ordem foi montada para o fornecedor {da_ordem}, mas o produto "
                     f"agora {agora_usa}.",
            saida=f"{REFAZER_A_ORDEM} Para manter esta ordem, volte o produto ao "
                  f"fornecedor {da_ordem}.",
            fonte=fonte,
        )
    return None


@_regra("compra_fornecedor")
def _fornecedor_desativado(ctx: dict) -> Violacao | None:
    """Fornecedor desativado no cadastro (core/cadastro.py) não recebe compra,
    nem a que já estava na fila quando ele foi desativado. O fornecedor é o
    da ordem, para quem ela vai. Sem fornecedor, quem pede o dado é a regra
    do prazo."""
    if ctx.get("fornecedor_ativo") is False:
        return Violacao(
            regra="FORNECEDOR-DESATIVADO",
            severidade=Severidade.BLOQUEIO,
            mensagem="O fornecedor desta ordem está desativado no cadastro.",
            saida="Reative o fornecedor (python cli.py fornecedor editar ID --reativar), "
                  "ou vincule outro ao produto (python cli.py produto editar SKU "
                  "--fornecedor ID): aí o worker recusa esta ordem e a monta de novo "
                  "com o novo fornecedor.",
            fonte="Cadastro de fornecedores (core/cadastro.py)",
        )
    return None


@_regra("compra_fornecedor")
def _produto_desativado(ctx: dict) -> Violacao | None:
    """Produto desativado no cadastro (core/cadastro.py) não é comprado: nem a
    compra nova (o worker manda o pedido para PROBLEMA), nem a que já estava
    na fila quando ele foi desativado. O pedido continua sendo importado e
    analisado: a venda existe no marketplace e pede uma decisão sua."""
    if ctx.get("produto_ativo") is False:
        sku = sku_no_comando(ctx.get("sku"))
        return Violacao(
            regra="PRODUTO-DESATIVADO",
            severidade=Severidade.BLOQUEIO,
            mensagem="O produto desta ordem está desativado no cadastro.",
            saida=f"Reative o produto (python cli.py produto editar {sku} --reativar, ou PATCH "
                  "/api/produtos/<id> com {\"ativo\": true}): a compra que está na fila fica "
                  "liberada, e o pedido que já foi para PROBLEMA volta com python cli.py "
                  "pedido reanalisar ID. Ou cancele a venda no marketplace.",
            fonte="Cadastro de produtos (core/cadastro.py)",
        )
    return None


def _quantidade(valor) -> int | None:
    """Quantidade gravada no payload da fila: inteiro positivo, ou None."""
    if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0:
        return valor
    return None


def custo_confere(valor, moeda, quantidade, custo, moeda_custo) -> bool | None:
    """O valor (Decimal) e a moeda de uma ordem são o custo atual do produto
    vezes a quantidade, na mesma moeda? None quando algum deles não se lê.
    Moeda None dos dois lados confere: os dois são "moeda não informada". A
    regra CUSTO-ALTERADO e o worker (antes de montar a ordem) usam esta
    mesma conta."""
    quantidade = _quantidade(quantidade)
    if valor is None or custo is None or quantidade is None:
        return None
    return valor == custo * quantidade and moeda == moeda_custo


@_regra("compra_fornecedor")
def _custo_alterado(ctx: dict) -> Violacao | None:
    """O valor e a moeda da ordem são o custo do produto vezes a quantidade de
    quando ela foi montada, e a margem dela foi calculada com eles. Se o
    custo ou a moeda mudou no cadastro depois (inclusive numa troca de
    fornecedor), aprovar pagaria o valor velho, com a margem velha: bloqueia,
    e o worker monta a ordem de novo, refazendo a margem com o custo atual.
    Ordem sem moeda (fila de versão anterior) não confere com custo que já
    tem moeda. Valor, quantidade ou custo que não se lê: falha fechada."""
    fonte = "Cadastro de produtos (core/cadastro.py)"
    valor = ctx.get("valor_da_ordem")
    custo = ctx.get("custo_do_produto")
    quantidade = _quantidade(ctx.get("quantidade"))
    da_ordem, do_custo = ctx.get("moeda_da_ordem"), ctx.get("moeda_do_custo")
    confere = custo_confere(valor, da_ordem, quantidade, custo, do_custo)
    if confere is None:
        return _falta(
            CUSTO_ALTERADO,
            "Falta conferir o valor da ordem com o custo atual do produto (o valor, a "
            "quantidade ou o custo não se lê).",
            "Confira o custo do produto (python cli.py produto editar SKU --custo VALOR "
            f"--moeda BRL). {REFAZER_A_ORDEM}",
            fonte,
        )
    if not confere:
        esperado = custo * quantidade
        return Violacao(
            regra=CUSTO_ALTERADO,
            severidade=Severidade.BLOQUEIO,
            mensagem=f"A ordem diz {dinheiro.formatar(valor, da_ordem)}, mas o custo atual do "
                     f"produto dá {dinheiro.formatar(esperado, do_custo)} ({quantidade} x "
                     f"{dinheiro.formatar(custo, do_custo)}).",
            saida=REFAZER_A_ORDEM,
            fonte=fonte,
        )
    return None


@_regra("compra_fornecedor")
def _prazo_incompativel(ctx: dict) -> Violacao | None:
    """Prazo de fábrica maior que a promessa do anúncio gera atraso sistêmico."""
    fonte = "Regra operacional própria"
    prazo_forn = ctx.get("prazo_fornecedor_dias")
    if prazo_forn is None:
        return _falta(
            "PRAZO",
            "Falta o prazo do fornecedor (ou o prazo_dias gravado não é um número de dias).",
            "Preencha prazo_dias do fornecedor com um número de dias (python cli.py "
            "fornecedor editar ID --prazo 5, ou PATCH /api/fornecedores/<id>), ou "
            "PRAZO_FORNECEDOR_DIAS no .env.",
            fonte,
        )
    prazo_anuncio = ctx.get("prazo_anuncio_dias")
    if prazo_anuncio:
        if prazo_forn >= prazo_anuncio:
            return Violacao(
                regra="PRAZO",
                severidade=Severidade.BLOQUEIO,
                mensagem=f"Fornecedor leva {prazo_forn}d; anúncio promete {prazo_anuncio}d.",
                saida="Mantenha estoque mínimo deste SKU ou aumente o prazo do anúncio.",
                fonte=fonte,
            )
        return None
    # Prazo do anúncio desconhecido: vale o limite fixo, não um "passa".
    if prazo_forn > PRAZO_MAXIMO_SEM_ANUNCIO:
        return Violacao(
            regra="PRAZO",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Fornecedor leva {prazo_forn}d; sem o prazo do anúncio, o limite "
                     f"é {PRAZO_MAXIMO_SEM_ANUNCIO}d.",
            saida="Mantenha estoque mínimo deste SKU ou troque de fornecedor.",
            fonte=fonte,
        )
    return None


@_regra("compra_fornecedor", "publicar_anuncio")
def _categoria_restrita(ctx: dict) -> Violacao | None:
    """
    Categorias que exigem registro, licença ou laudo. Vender sem habilitação
    gera remoção do anúncio e, dependendo do item, responsabilidade sanitária.
    """
    fonte = "Legislação setorial brasileira (Anvisa / Inmetro / Anatel)"
    lista = ", ".join(CATEGORIAS_RESTRITAS)
    bruta = ctx.get("categoria_regulada")
    if bruta is None or not str(bruta).strip():
        return _falta(
            "CATEGORIA-RESTRITA",
            "Falta dizer se o produto é de categoria regulada.",
            f"No cadastro do produto, preencha categoria_regulada com "
            f"'{SEM_CATEGORIA_REGULADA}' ou com uma destas: {lista}. Exemplo: "
            + _no_produto(ctx, "categoria_regulada", SEM_CATEGORIA_REGULADA),
            fonte,
        )
    cat = str(bruta).strip().lower()
    if cat == SEM_CATEGORIA_REGULADA:
        return None
    if cat not in CATEGORIAS_RESTRITAS:
        return _falta(
            "CATEGORIA-RESTRITA",
            f"Categoria regulada '{cat}' desconhecida.",
            f"Use '{SEM_CATEGORIA_REGULADA}' ou uma destas: {lista}.",
            fonte,
        )
    habilitacao = ctx.get("habilitacao_confirmada")
    if habilitacao is None:
        return _falta(
            "CATEGORIA-RESTRITA",
            f"Categoria '{cat}' exige habilitação; falta confirmar.",
            f"{CATEGORIAS_RESTRITAS[cat]} Com ela em dia: "
            + _no_produto(ctx, "habilitacao_confirmada", True),
            fonte,
        )
    if not habilitacao:
        return Violacao(
            regra="CATEGORIA-RESTRITA",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Categoria '{cat}' exige habilitação não confirmada.",
            saida=CATEGORIAS_RESTRITAS[cat],
            fonte=fonte,
        )
    return None


@_regra("compra_fornecedor", "ajuste_preco")
def _margem_negativa(ctx: dict) -> Violacao | None:
    fonte = "Regra operacional própria"
    m = ctx.get("margem_pct")
    if m is None:
        return _falta(
            "MARGEM-NEGATIVA",
            "Margem prevista não calculada.",
            "Cadastre o custo do produto com a moeda (python cli.py produto editar SKU "
            "--custo 18.50 --moeda BRL) para o worker calcular a margem, que só sai com "
            "custo e venda em BRL; ou informe a margem do novo preço.",
            fonte,
        )
    if m < 0:
        return Violacao(
            regra="MARGEM-NEGATIVA",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Margem prevista de {m}%. A venda dá prejuízo.",
            saida="Reprecifique o anúncio ou renegocie o custo antes de comprar.",
            fonte=fonte,
        )
    return None


@_regra("publicar_anuncio")
def _garantia_legal(ctx: dict) -> Violacao | None:
    """
    CDC art. 26: 30 dias (não durável) / 90 dias (durável) de garantia legal,
    independente do que o anúncio diga. Vendedor no marketplace responde
    solidariamente. Prometer menos que isso é cláusula abusiva.
    """
    prometido = ctx.get("garantia_prometida_dias")
    durabilidade = ctx.get("durabilidade") or "duravel"
    minimo = 90 if durabilidade == "duravel" else 30
    if prometido is None:
        return _falta(
            "CDC-GARANTIA",
            "Falta a garantia prometida no anúncio.",
            f"Informe a garantia do anúncio: no mínimo {minimo} dias.",
            "CDC art. 26",
        )
    if prometido < minimo:
        return Violacao(
            regra="CDC-GARANTIA",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Anúncio promete {prometido}d de garantia; o CDC exige {minimo}d.",
            saida=f"Corrija o anúncio para no mínimo {minimo} dias.",
            fonte="CDC art. 26",
        )
    return None


@_regra("publicar_anuncio", "resposta_cliente")
def _direito_arrependimento(ctx: dict) -> Violacao | None:
    """CDC art. 49: 7 dias de arrependimento em compra fora do estabelecimento.

    Regra de detecção: bloqueia quando alguém marcou que o texto nega o
    direito. Hoje nada marca isso sozinho; o bot de atendimento escala para
    você toda pergunta sobre devolução, troca ou garantia antes de redigir."""
    if ctx.get("recusa_arrependimento"):
        return Violacao(
            regra="CDC-ARREPENDIMENTO",
            severidade=Severidade.BLOQUEIO,
            mensagem="Anúncio ou resposta nega o direito de arrependimento.",
            saida="7 dias corridos a contar do recebimento, sem justificativa, "
                  "com frete de devolução por sua conta.",
            fonte="CDC art. 49",
        )
    return None


@_regra("compra_fornecedor", "publicar_anuncio")
def _nota_fiscal(ctx: dict) -> Violacao | None:
    fonte = "Convênio ICMS / obrigação acessória do marketplace"
    emite = ctx.get("emite_nota")
    if emite is None:
        return _falta(
            "FISCAL-NF",
            "Falta confirmar a emissão de nota fiscal nas vendas.",
            "Se você emite nota fiscal em toda venda, defina EMITE_NOTA_FISCAL=true "
            "no .env e reinicie o programa.",
            fonte,
        )
    if emite is False:
        return Violacao(
            regra="FISCAL-NF",
            severidade=Severidade.BLOQUEIO,
            mensagem="Venda sem emissão de nota fiscal.",
            saida="Marketplaces retêm e declaram o repasse. Venda sem NF gera "
                  "divergência automática na Receita.",
            fonte=fonte,
        )
    return None


@_regra("ajuste_preco", "publicar_anuncio")
def _preco_fora_da_curva(ctx: dict) -> Violacao | None:
    """Preço muito abaixo da mediana costuma indicar erro de cadastro. É só
    alerta: sem mediana, não há o que comparar."""
    preco = ctx.get("preco")
    mediana = ctx.get("preco_mediano_mercado")
    if preco and mediana and mediana > 0 and preco < mediana * 0.4:
        return Violacao(
            regra="PRECO-SUSPEITO",
            severidade=Severidade.ALERTA,
            mensagem=f"Preço R$ {preco:.2f} é menos de 40% da mediana (R$ {mediana:.2f}).",
            saida="Confira se houve erro de vírgula ou de unidade antes de publicar.",
            fonte="Regra operacional própria",
        )
    return None


def verificar(contexto: dict) -> Resultado:
    """
    Roda, contra o contexto, as regras do tipo da ação (contexto["tipo"]; sem
    tipo, vale compra_fornecedor). Tipo sem regras é bloqueado.
    """
    tipo = contexto.get("tipo") or "compra_fornecedor"
    r = Resultado()
    if tipo not in TIPOS:
        r.violacoes.append(Violacao(
            regra="TIPO-SEM-REGRAS",
            severidade=Severidade.BLOQUEIO,
            mensagem=f"Não há regras de conformidade para '{tipo}'.",
            saida="Defina as regras deste tipo em core/conformidade.py antes de aprovar.",
            fonte="Regra operacional própria",
        ))
        return r
    for regra, tipos in REGRAS:
        if tipo in tipos:
            v = regra(contexto)
            if v:
                r.violacoes.append(v)
    return r


# ------------------------------------------------- Contexto de uma pendência

def _sim_nao(valor) -> bool | None:
    """Confirmação lida do cadastro. O cadastro novo (core/cadastro.py) grava
    só 1, 0 ou NULL; o banco antigo pode ter texto digitado à mão em SQL.
    Leitura estrita, sem bool() no valor cru: numa coluna INTEGER do SQLite,
    'nao' fica TEXT e bool('nao') daria True.

      - 1, ou true/sim/yes/on: True;
      - 0, ou false/nao/não/no/off: False (as mesmas palavras do .env);
      - qualquer outra coisa, inclusive NULL e '': None, "falta confirmar".
    """
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return {1: True, 0: False}.get(valor)
    if isinstance(valor, str):
        texto = valor.strip().lower()
        if texto in _LIGADO:
            return True
        if texto in _DESLIGADO:
            return False
    return None


def prazo_do_fornecedor(valor) -> int | float | None:
    """prazo_dias do fornecedor. O cadastro novo só aceita inteiro de 1 a 365;
    o banco antigo pode ter o que foi digitado à mão em SQL. Vazio ou 0
    vale PRAZO_FORNECEDOR_DIAS, como sempre valeu; um número vale ele mesmo.
    Texto que não é número ('6 dias' fica TEXT numa coluna INTEGER do SQLite)
    é None, "falta o prazo", em vez de quebrar a comparação com TypeError."""
    if not valor:
        return config.negocio.prazo_fornecedor_dias
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        return valor
    if isinstance(valor, str) and valor.strip().isascii() and valor.strip().isdigit():
        return int(valor.strip())
    return None


def _envio_direto(canal: str | None) -> bool | None:
    """Fornecedor pedido por API ou portal despacha direto ao comprador; por
    e-mail ou WhatsApp, a mercadoria passa por você. Canal desconhecido:
    ninguém sabe."""
    if canal in ("api", "portal"):
        return True
    if canal in ("email", "whatsapp"):
        return False
    return None


def _id(valor) -> int | None:
    """Id gravado no payload da fila: inteiro positivo, ou None."""
    if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0:
        return valor
    return None


def _fatos_da_compra(payload: dict) -> dict:
    """Fatos de produto e fornecedor lidos do banco na hora da checagem, e não
    copiados para a fila: a confirmação preenchida depois libera o item que
    já está esperando, e a retirada volta a bloquear.

    O produto vem pelo id gravado na fila (ou pelo do pedido), nunca pelo SKU:
    o SKU pode ser editado e passar a outro produto. O fornecedor é o da
    ordem, o id gravado na fila, porque é para ele que a ordem vai; o atual
    do produto só serve para ver se houve troca (FORNECEDOR-TROCADO).

    O valor, a moeda e a quantidade vêm da ordem; o custo, do cadastro de
    agora: CUSTO-ALTERADO confere os dois."""
    produto_id = _id(payload.get("produto_id"))
    fornecedor_id = _id(payload.get("fornecedor_id"))
    pedido_id = _id(payload.get("pedido_id"))
    produto = fornecedor = None
    with conectar() as conn:
        if produto_id is None and pedido_id is not None:
            pedido = conn.execute("SELECT produto_id FROM pedidos WHERE id = ?",
                                  (pedido_id,)).fetchone()
            produto_id = _id(pedido["produto_id"]) if pedido else None
        if produto_id is not None:
            produto = conn.execute(
                "SELECT sku, categoria_regulada, habilitacao_confirmada,"
                " reembalagem_confirmada, fornecedor_id, ativo, custo_fornecedor,"
                " custo_fornecedor_dec, custo_fornecedor_moeda FROM produtos WHERE id = ?",
                (produto_id,)).fetchone()
        if fornecedor_id is not None:
            fornecedor = conn.execute("SELECT canal, prazo_dias, ativo FROM fornecedores"
                                      " WHERE id = ?", (fornecedor_id,)).fetchone()
    fatos = {
        "fornecedor_da_ordem": fornecedor_id,
        "fornecedor_do_produto": produto["fornecedor_id"] if produto else None,
        "categoria_regulada": produto["categoria_regulada"] if produto else None,
        "habilitacao_confirmada": _sim_nao(produto["habilitacao_confirmada"]) if produto else None,
        "reembalagem_confirmada": _sim_nao(produto["reembalagem_confirmada"]) if produto else None,
        "envio_direto_fornecedor": None, "prazo_fornecedor_dias": None, "fornecedor_ativo": None,
        # Fila antiga traz o valor como número e sem moeda; a nova, texto e moeda.
        "valor_da_ordem": dinheiro.do_real(payload.get("valor")),
        "moeda_da_ordem": dinheiro.moeda_lida(payload.get("moeda")),
        "quantidade": payload.get("quantidade"),
        "produto_ativo": None, "custo_do_produto": None, "moeda_do_custo": None,
    }
    if produto is not None:
        fatos.update(
            sku=produto["sku"],  # o SKU de hoje, para a saída que se copia
            produto_ativo=produto["ativo"] != 0,  # NULL (banco antigo) conta como ativo
            custo_do_produto=dinheiro.ler(produto["custo_fornecedor_dec"],
                                          produto["custo_fornecedor"]),
            moeda_do_custo=dinheiro.moeda_lida(produto["custo_fornecedor_moeda"]),
        )
    if fornecedor is not None:
        fatos.update(
            envio_direto_fornecedor=_envio_direto(fornecedor["canal"]),
            prazo_fornecedor_dias=prazo_do_fornecedor(fornecedor["prazo_dias"]),
            fornecedor_ativo=fornecedor["ativo"] != 0,  # NULL (banco antigo) conta como ativo
        )
    return fatos


def contexto_da_pendencia(tipo: str, payload: dict) -> dict:
    """O contexto que as regras recebem para uma ação da fila. Nada aqui tem
    valor padrão que libera: o que não se sabe vai como None."""
    contexto = {
        "tipo": tipo,
        "sku": payload.get("sku"),
        "marketplace": payload.get("marketplace"),
        "margem_pct": payload.get("margem_prevista"),
        "preco": payload.get("preco"),
        "preco_mediano_mercado": payload.get("preco_mediano_mercado"),
        "prazo_anuncio_dias": payload.get("prazo_anuncio_dias"),
        "categoria_regulada": payload.get("categoria_regulada"),
        "habilitacao_confirmada": payload.get("habilitacao_confirmada"),
        "garantia_prometida_dias": payload.get("garantia_prometida_dias"),
        "durabilidade": payload.get("durabilidade"),
        "recusa_arrependimento": payload.get("recusa_arrependimento"),
        # Confirmação do vendedor, vale para todas as vendas.
        "emite_nota": config.negocio.emite_nota,
    }
    if tipo == "compra_fornecedor":
        # Produto e fornecedor: o banco é a fonte, achado pelos ids da fila.
        contexto.update(_fatos_da_compra(payload))
    return contexto


def verificar_pendencia(tipo: str, payload: dict) -> Resultado:
    """A checagem única de uma ação da fila. O worker chama antes de
    enfileirar; core/aprovacao.aprovar chama antes de executar (painel e
    cli.py passam por lá); o painel chama para mostrar a fila."""
    return verificar(contexto_da_pendencia(tipo, payload))
