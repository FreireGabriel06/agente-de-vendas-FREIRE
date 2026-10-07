"""
Configuração central. Tudo vem de variáveis de ambiente — nenhuma credencial
no código, nenhuma credencial no git.

Copie .env.example para .env e preencha. Segredos de marketplace (client
secret, partner key, refresh token, chave da API) são gravados pelo painel no
cofre cifrado (core/cofre.py), não no .env.
"""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Não importa nada do projeto: dá para importar daqui sem ciclo.
from core import dinheiro

# Código e arquivos que vêm com ele (.env.example, painel.html).
BASE_DIR = Path(__file__).resolve().parent


def _pasta_dados() -> Path:
    """
    Pasta dos arquivos de execução: .env, banco (com o cofre de credenciais),
    chaves .chave_lgpd e .chave_cofre, arquivos de token antigos e ordens de
    compra.

      1. AGENTE_DADOS, se existir no ambiente do sistema. Não vale no .env,
         porque o .env é lido desta pasta.
      2. A pasta do executável, no binário do PyInstaller. No modo onefile o
         código roda de uma pasta temporária que some ao fechar; sem isso o
         .env, a chave e o banco não ficariam ao lado do executável.
      3. A pasta do código.
    """
    definida = os.environ.get("AGENTE_DADOS", "").strip()
    if definida:
        pasta = Path(definida).expanduser().resolve()
        pasta.mkdir(parents=True, exist_ok=True)
        return pasta
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return BASE_DIR


DATA_DIR = _pasta_dados()


def avisar_pasta_de_dados() -> None:
    """Sem AGENTE_DADOS no ambiente, diz na saída de erro qual pasta de dados
    vai ser usada. Os pontos de entrada de manutenção (cli.py, worker.py)
    chamam isto antes de qualquer gravação: é nesta pasta que ficam o banco,
    as chaves e os tokens de verdade."""
    if os.environ.get("AGENTE_DADOS", "").strip():
        return
    print(f"Pasta de dados: {DATA_DIR} (AGENTE_DADOS não definida). O banco, as chaves "
          "e os tokens desta pasta serão lidos e gravados.", file=sys.stderr)


# Nomes que vieram do arquivo .env, e não do ambiente do sistema. O cofre
# (core/cofre.py) usa isto na ordem de leitura dos segredos: o ambiente do
# sistema vem antes do cofre, e o valor antigo em texto puro no .env vem depois.
DO_ARQUIVO_ENV: set[str] = set()

# Só valem no ambiente do sistema. A chave do cofre não pode morar no .env,
# que é justamente o arquivo de onde os segredos saíram.
SO_DO_AMBIENTE = {"CHAVE_COFRE"}


def ler_arquivo_env(caminho: Path) -> dict[str, str]:
    """Lê um .env sem alterar nada: CHAVE=valor por linha, # comenta. Chave
    repetida: vale a primeira linha, como sempre valeu."""
    valores = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        valores.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))
    return valores


def _carregar_env(caminho: Path | None = None):
    """Loader mínimo de .env, sem dependência externa. O ambiente do sistema
    tem precedência: uma variável que já existe não é trocada."""
    caminho = caminho or DATA_DIR / ".env"
    if not caminho.exists():
        return
    for chave, valor in ler_arquivo_env(caminho).items():
        if chave in SO_DO_AMBIENTE:
            print(f"Aviso: {chave} no .env é ignorada; defina no ambiente do sistema.",
                  file=sys.stderr)
            continue
        if chave not in os.environ:
            os.environ[chave] = valor
            DO_ARQUIVO_ENV.add(chave)


_carregar_env()


def _segredo(variavel: str) -> str:
    """Segredo lido na hora do uso, pela regra única do cofre. Importação
    tardia: core.cofre importa db, que importa este módulo."""
    from core.cofre import segredo
    return segredo(variavel)


def _caminho_banco() -> str:
    """DB_PATH relativo, como o do .env.example, conta a partir de DATA_DIR, e
    não da pasta de onde o programa foi chamado."""
    caminho = Path(os.getenv("DB_PATH") or "agente.db").expanduser()
    return str(caminho if caminho.is_absolute() else DATA_DIR / caminho)


def _inteiro(variavel: str, padrao: int, minimo: int, maximo: int | None = None) -> int:
    """Número inteiro do ambiente (ou do .env, já carregado acima). Vazio vale
    o padrão. Valor que não é inteiro ou fica fora da faixa (o .env não corta
    comentário no fim da linha: '8777 # painel' não é número) não derruba a
    importação: vale o padrão, com aviso na saída de erro."""
    bruto = os.getenv(variavel, "").strip()
    if not bruto:
        return padrao
    try:
        valor = int(bruto)
    except ValueError:
        valor = None
    if valor is None or valor < minimo or (maximo is not None and valor > maximo):
        faixa = f"de {minimo} a {maximo}" if maximo is not None else f"a partir de {minimo}"
        print(f"Aviso: {variavel}={bruto!r} não vale; usando {padrao}. Use um número "
              f"inteiro {faixa}.", file=sys.stderr)
        return padrao
    return valor


def _numero(variavel: str, padrao: str) -> float:
    """Número com casas do ambiente (ou do .env): margem, teto e alíquota.
    Texto que não é número interrompe a importação, como sempre interrompeu
    (ValueError do float). Número que
    o worker não lê em Decimal (nan, inf, 1e999, ou grande demais:
    core/dinheiro.do_real) não derruba a importação: vale o padrão, com aviso
    na saída de erro. Antes ele passava daqui e quebrava a etapa do worker a
    cada ciclo, na conta da margem ou na comparação com o teto."""
    bruto = os.getenv(variavel, padrao)
    valor = float(bruto)
    if dinheiro.do_real(valor) is None:
        print(f"Aviso: {variavel}={bruto.strip()!r} não vale; usando {padrao}. Use um número "
              "finito, com ponto (nan, inf e 1e999 não valem).", file=sys.stderr)
        return float(padrao)
    return valor


# Menor intervalo do worker, em segundos. Abaixo disso o laço martela o
# marketplace e a Claude API sem pausa.
INTERVALO_MINIMO = 30


_DESLIGADO = {"false", "0", "nao", "não", "no", "off"}
_LIGADO = {"true", "1", "sim", "yes", "on"}


def _modo_simulacao() -> bool:
    """MODO_SIMULACAO só desliga com um valor explícito de "não" (false, 0,
    nao...). Vazio, ausente ou digitado errado deixa a simulação ligada: o
    engano fica do lado que não manda nada para fora."""
    return os.getenv("MODO_SIMULACAO", "true").strip().lower() not in _DESLIGADO


def _confirmacao(variavel: str) -> bool | None:
    """Confirmação do dono: True, False, ou None quando ainda não foi dada.
    None não vira sim nem não: a conformidade trata como "falta confirmar"."""
    valor = os.getenv(variavel, "").strip().lower()
    if valor in _LIGADO:
        return True
    if valor in _DESLIGADO:
        return False
    return None


# Níveis de esforço aceitos pela Claude API (output_config.effort).
ESFORCOS_CLAUDE = ("low", "medium", "high", "xhigh", "max")


def _esforco_claude() -> str:
    valor = os.getenv("ESFORCO_CLAUDE", "").strip().lower()
    if not valor:
        return "low"
    if valor not in ESFORCOS_CLAUDE:
        print(f"Aviso: ESFORCO_CLAUDE={valor!r} não existe; usando 'low'. "
              f"Valores aceitos: {', '.join(ESFORCOS_CLAUDE)}.", file=sys.stderr)
        return "low"
    return valor


@dataclass
class ConfigMercadoLivre:
    client_id: str = os.getenv("ML_CLIENT_ID", "")
    seller_id: str = os.getenv("ML_SELLER_ID", "")
    site_id: str = os.getenv("ML_SITE_ID", "MLB")  # MLB = Brasil
    base_url: str = "https://api.mercadolibre.com"

    # Segredos não viram campo: são lidos a cada uso, do ambiente do sistema
    # ou do cofre cifrado, e nunca ficam presos ao valor da importação.
    @property
    def client_secret(self) -> str:
        return _segredo("ML_CLIENT_SECRET")

    @property
    def refresh_token(self) -> str:
        return _segredo("ML_REFRESH_TOKEN")

    @property
    def configurado(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)


@dataclass
class ConfigAmazon:
    """Amazon SP-API. Exige conta de vendedor aprovada e app registrado."""
    lwa_client_id: str = os.getenv("AMZ_LWA_CLIENT_ID", "")
    marketplace_id: str = os.getenv("AMZ_MARKETPLACE_ID", "A2Q3Y263D00KWC")  # BR
    regiao_endpoint: str = os.getenv("AMZ_ENDPOINT", "https://sellingpartnerapi-na.amazon.com")

    @property
    def lwa_client_secret(self) -> str:
        return _segredo("AMZ_LWA_CLIENT_SECRET")

    @property
    def refresh_token(self) -> str:
        return _segredo("AMZ_REFRESH_TOKEN")

    @property
    def configurado(self) -> bool:
        return bool(self.lwa_client_id and self.lwa_client_secret and self.refresh_token)


@dataclass
class ConfigNegocio:
    """Regras de negócio: margem mínima, rótulo da fila, imposto, prazo e as
    confirmações do dono que a conformidade exige. Nenhuma delas faz o robô
    comprar sozinho: toda compra espera aprovação na fila."""

    # Margem líquida mínima aceitável. Abaixo disso o pedido é recusado
    # automaticamente (estado RECUSADO_MARGEM) — é a trava que impede vender
    # no prejuízo.
    margem_minima_pct: float = _numero("MARGEM_MINIMA_PCT", "18")

    # Só muda o rótulo na fila: compra até este valor aparece como [ROTINA],
    # acima dele como [ACIMA DO TETO]. Não existe compra automática abaixo do
    # teto: toda compra espera o seu OK. O nome da variável ficou da versão
    # antiga.
    teto_compra_automatica: float = _numero("TETO_COMPRA_AUTOMATICA", "300")

    # Imposto estimado sobre a venda (Simples Nacional, anexo de comércio).
    # Ajuste pra sua faixa real de faturamento.
    aliquota_imposto_pct: float = _numero("ALIQUOTA_IMPOSTO_PCT", "4")

    # Quantos dias de prazo o fornecedor leva. Entra no cálculo de risco
    # de estourar o prazo do marketplace.
    prazo_fornecedor_dias: int = int(os.getenv("PRAZO_FORNECEDOR_DIAS", "5"))

    # EMITE_NOTA_FISCAL: você emite nota fiscal em toda venda? true ou false.
    # Sem resposta (None), a conformidade bloqueia as compras pedindo a
    # confirmação — não existe "sim" presumido.
    emite_nota: bool | None = _confirmacao("EMITE_NOTA_FISCAL")


@dataclass
class ConfigClaude:
    """Claude API, usada para redigir as respostas aos compradores.

    O modelo é decisão do dono, em MODELO_CLAUDE. Padrão: claude-opus-5-5, a
    US$ 4 / US$ 20 por milhão de tokens de entrada / saída. O claude-sonnet-5-5
    custa metade (US$ 2 / US$ 10); trocar é só mudar a variável. O programa
    não troca de modelo sozinho; só o fallback da API, numa recusa por
    política, pode responder (e cobrar) pelo modelo de reserva, e isso fica
    registrado num evento.

    ESFORCO_CLAUDE vai em output_config.effort: low (padrão, bom para um
    rascunho curto), medium, high, xhigh ou max. No claude-opus-5-5 o
    raciocínio fica sempre ligado; o esforço é o que controla custo e demora,
    e também define max_tokens e o tempo de espera (claude_api.LIMITES_POR_ESFORCO).
    """
    modelo: str = os.getenv("MODELO_CLAUDE", "").strip() or "claude-opus-5-5"
    esforco: str = _esforco_claude()


@dataclass
class Config:
    ml: ConfigMercadoLivre = field(default_factory=ConfigMercadoLivre)
    amazon: ConfigAmazon = field(default_factory=ConfigAmazon)
    negocio: ConfigNegocio = field(default_factory=ConfigNegocio)
    claude: ConfigClaude = field(default_factory=ConfigClaude)
    db_path: str = _caminho_banco()
    # Ligado (padrão): aprovar uma ação não manda nada para fora — nem
    # resposta ao comprador, nem preço ao marketplace — e a ordem de compra sai
    # marcada como teste. Os executores em worker.py só registram o que fariam.
    modo_simulacao: bool = _modo_simulacao()
    # Lidos aqui, depois do .env: o executar.py pega daqui, e não do ambiente
    # na importação, quando o .env ainda não tinha sido lido.
    porta_painel: int = _inteiro("PORTA_PAINEL", 8777, 1, 65535)
    intervalo_worker: int = _inteiro("INTERVALO_WORKER", 300, INTERVALO_MINIMO)


config = Config()
