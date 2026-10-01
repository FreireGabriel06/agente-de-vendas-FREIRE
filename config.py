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
    """Regras que definem quando o robô age sozinho e quando ele te chama."""

    # Margem líquida mínima aceitável. Abaixo disso o pedido é recusado
    # automaticamente — é a trava que impede vender no prejuízo.
    margem_minima_pct: float = float(os.getenv("MARGEM_MINIMA_PCT", "18"))

    # Valor acima do qual QUALQUER compra no fornecedor exige seu OK explícito,
    # mesmo que a margem esteja boa.
    teto_compra_automatica: float = float(os.getenv("TETO_COMPRA_AUTOMATICA", "300"))

    # Imposto estimado sobre a venda (Simples Nacional, anexo de comércio).
    # Ajuste pra sua faixa real de faturamento.
    aliquota_imposto_pct: float = float(os.getenv("ALIQUOTA_IMPOSTO_PCT", "4"))

    # Quantos dias de prazo o fornecedor leva. Entra no cálculo de risco
    # de estourar o prazo do marketplace.
    prazo_fornecedor_dias: int = int(os.getenv("PRAZO_FORNECEDOR_DIAS", "5"))


@dataclass
class Config:
    ml: ConfigMercadoLivre = field(default_factory=ConfigMercadoLivre)
    amazon: ConfigAmazon = field(default_factory=ConfigAmazon)
    negocio: ConfigNegocio = field(default_factory=ConfigNegocio)
    db_path: str = _caminho_banco()
    modo_simulacao: bool = os.getenv("MODO_SIMULACAO", "true").lower() == "true"


config = Config()
