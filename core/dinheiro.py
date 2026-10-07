"""
Dinheiro: valor em Decimal, sempre acompanhado da moeda (ISO 4217).

  - O valor nunca passa por float no código novo. Entra como texto
    ("18.50"), Decimal ou inteiro; o JSON das rotas do cadastro é lido com
    parse_float=Decimal, e float do Python é recusado na validação.
  - No banco, o valor fica em texto decimal canônico (coluna <nome>_dec) ao
    lado da moeda (<nome>_moeda). A coluna REAL antiga continua sendo gravada
    como espelho (CAST no SQL, sem float no Python), para bancos e código
    antigos. O código novo só lê a REAL quando a coluna decimal está vazia:
    linha gravada por SQL à mão ou por uma versão anterior.
  - Moeda: 3 letras maiúsculas, dentro de MOEDAS_ACEITAS. Para aceitar outra,
    inclua o código ISO 4217 no conjunto abaixo; nada mais muda (o símbolo
    em SIMBOLOS é opcional: sem ele, a tela mostra o código). A margem,
    porém, só é calculada na moeda do modelo de taxas
    (inteligencia/precificacao.py, MOEDA), até existir modelo para a outra.
  - Moeda desconhecida é None ("moeda não informada"). Nada aqui presume BRL
    ou USD para dado antigo.

Este módulo não importa nada do projeto: db.py usa as funções daqui na
migração.
"""
import math
import re
from decimal import Decimal, InvalidOperation

# Para aceitar outra moeda, acrescente aqui o código ISO 4217 (3 letras).
MOEDAS_ACEITAS = frozenset({"BRL", "USD", "EUR", "GBP", "MXN", "CAD"})

# Casas decimais aceitas num valor: custo unitário de atacado passa de 2.
CASAS_MAXIMAS = 4
# Acima disto é erro de digitação, não valor de compra.
VALOR_MAXIMO = Decimal("1000000000")
# Na leitura (banco antigo, SQL à mão, marketplace), o que passa disto é
# tratado como ilegível. Folga para total de pedido = custo x quantidade, e
# para o REAL antigo lido pelos 15 algarismos (0.0000123456789012345).
LIMITE_LEITURA = Decimal("1E+15")
CASAS_LEITURA = 30

NAO_INFORMADA = "moeda não informada"
SIMBOLOS = {"BRL": "R$"}

_CODIGO_ISO = re.compile(r"[A-Z]{3}")
# Só algarismos ASCII: \d do Python e o próprio Decimal() aceitam outros
# sistemas de algarismos ('١٢' viraria 12), e isso não é valor digitado aqui.
_DECIMAL_SIMPLES = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")


class ValorInvalido(ValueError):
    """A mensagem já vem em português, pronta para o campo."""


def validar_moeda(codigo) -> str:
    """Código de moeda de entrada nova (cadastro): estrito, sem corrigir."""
    if not isinstance(codigo, str):
        raise ValorInvalido('use o código ISO 4217 da moeda em texto, por exemplo "BRL".')
    if not _CODIGO_ISO.fullmatch(codigo):
        raise ValorInvalido("use 3 letras maiúsculas (ISO 4217), por exemplo BRL ou USD.")
    if codigo not in MOEDAS_ACEITAS:
        raise ValorInvalido(f"a moeda {codigo} não está na lista aceita: "
                            f"{', '.join(sorted(MOEDAS_ACEITAS))}.")
    return codigo


# Contas sem o contexto do Decimal: normalize() e abs() arredondam a 28
# algarismos e levantam decimal.Overflow (que não é ValueError) diante de um
# 1E+9999999; 1E-9999999 vira 0. Aqui só entram as_tuple(), comparação e
# format(), que são exatos para qualquer número finito.

def casas_decimais(valor: Decimal) -> int:
    """Casas decimais significativas: 18.50 tem 1, 0.0125 tem 4, 1000 tem 0."""
    _, digitos, expoente = valor.as_tuple()
    if not isinstance(expoente, int) or expoente >= 0 or not any(digitos):
        return 0
    zeros_a_direita = len(digitos) - len("".join(map(str, digitos)).rstrip("0"))
    return max(0, -expoente - zeros_a_direita)


def _menor_que(numero: Decimal, limite: Decimal) -> bool:
    """|numero| < limite, por comparação (abs() usa o contexto)."""
    return -limite < numero < limite


def validar_valor(valor, casas: int = CASAS_MAXIMAS) -> Decimal:
    """Número de entrada nova (cadastro) em Decimal. Aceita texto decimal com
    ponto, Decimal e inteiro. Recusa float, bool, vírgula, NaN e infinito."""
    if isinstance(valor, bool):
        raise ValorInvalido('use um número decimal, por exemplo "18.50".')
    if isinstance(valor, float):
        raise ValorInvalido('use texto decimal, por exemplo "18.50": float perde precisão.')
    if isinstance(valor, int):
        numero = Decimal(valor)
    elif isinstance(valor, Decimal):
        numero = valor
    elif isinstance(valor, str):
        texto = valor.strip()
        if "," in texto:
            raise ValorInvalido('use ponto como separador decimal, por exemplo "18.50".')
        if not _DECIMAL_SIMPLES.fullmatch(texto):
            raise ValorInvalido('use um número decimal, por exemplo "18.50".')
        numero = Decimal(texto)
    else:
        raise ValorInvalido('use um número decimal, por exemplo "18.50".')
    if not numero.is_finite():
        raise ValorInvalido("use um número finito.")
    if not _menor_que(numero, VALOR_MAXIMO):
        raise ValorInvalido(f"use um valor menor que {VALOR_MAXIMO}.")
    if casas_decimais(numero) > casas:
        raise ValorInvalido(f"use no máximo {casas} casas decimais.")
    return numero


def texto_exato(valor: Decimal) -> str:
    """Ponto fixo, sem expoente e sem zero sobrando: 0.40 vira '0.4', 1E+2
    vira '100'. Exato: format(valor, 'f') não arredonda. Serve ao peso."""
    if valor == 0:
        return "0"  # também o -0
    inteiro, _, fracao = format(valor, "f").partition(".")
    fracao = fracao.rstrip("0")
    return f"{inteiro}.{fracao}" if fracao else inteiro


def texto(valor: Decimal) -> str:
    """Forma canônica do dinheiro no banco: como texto_exato, com pelo menos
    duas casas. 18.5 vira '18.50'; 0.0125 fica '0.0125'; 1E+2 vira '100.00'."""
    inteiro, _, fracao = texto_exato(valor).partition(".")
    return f"{inteiro}.{fracao.ljust(2, '0')}"


def _legivel(numero: Decimal) -> Decimal | None:
    """Finito e de tamanho plausível. Um 1E+999999 digitado à mão no banco
    viraria um texto de um milhão de algarismos na tela: é ilegível."""
    if not numero.is_finite() or not _menor_que(numero, LIMITE_LEITURA):
        return None
    if casas_decimais(numero) > CASAS_LEITURA:
        return None
    return numero


def do_texto(bruto) -> Decimal | None:
    """Valor de uma coluna decimal (<nome>_dec). Ilegível vira None: quem lê
    trata como valor ausente, sem cair na coluna REAL."""
    if bruto is None:
        return None
    try:
        numero = Decimal(str(bruto).strip())
    except InvalidOperation:
        return None
    return _legivel(numero)


def do_real(bruto) -> Decimal | None:
    """Valor de uma coluna REAL antiga, ou de um JSON de marketplace que já
    chegou como float. Lido pelos 15 algarismos significativos com que o
    próprio SQLite mostra um REAL, sem o ruído binário: 0.1 + 0.2 gravado como
    0.30000000000000004 vira 0.3. Texto numa coluna REAL (SQL à mão) é lido
    como texto decimal; o resto é None."""
    if bruto is None or isinstance(bruto, bool):
        return None
    if isinstance(bruto, int):
        return _legivel(Decimal(bruto))
    if isinstance(bruto, float):
        return _legivel(Decimal(format(bruto, ".15g"))) if math.isfinite(bruto) else None
    if isinstance(bruto, Decimal):
        return _legivel(bruto)
    if isinstance(bruto, str):
        return do_texto(bruto)
    return None


def ler(decimal_texto, real) -> Decimal | None:
    """O valor de uma linha: a coluna decimal primeiro; a REAL só quando a
    decimal está vazia (linha antiga ou gravada por SQL à mão)."""
    if decimal_texto is not None:
        return do_texto(decimal_texto)
    return do_real(real)


def moeda_lida(codigo) -> str | None:
    """Moeda lida do banco ou de um marketplace: o código ISO bem formado, ou
    None ("moeda não informada"). Não confere a lista aceita: um pedido em
    outra moeda guarda a moeda verdadeira, e a margem diz por que não calcula."""
    if isinstance(codigo, str) and _CODIGO_ISO.fullmatch(codigo.strip()):
        return codigo.strip()
    return None


def formatar(valor: Decimal | None, moeda: str | None) -> str:
    """Valor para texto de tela, fila e ordem de compra: 'R$ 62.00',
    'USD 9.90', ou '18.50 (moeda não informada)'."""
    if valor is None:
        return "—"
    if moeda is None:
        return f"{texto(valor)} ({NAO_INFORMADA})"
    return f"{SIMBOLOS.get(moeda, moeda)} {texto(valor)}"
