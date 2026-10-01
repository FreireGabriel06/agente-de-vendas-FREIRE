"""
Cofre de credenciais de marketplace.

Client secret, partner key, refresh e access token e a chave da API de
redação ficam cifrados no próprio SQLite, na tabela `credenciais`, com Fernet
(AES-128-CBC com HMAC-SHA256, da biblioteca cryptography). A chave é OUTRA,
separada da que protege o dado de comprador (.chave_lgpd), e nunca fica no
banco nem no repositório:

  1. CHAVE_COFRE no ambiente do sistema. Uma chave, ou várias separadas por
     vírgula: a primeira cifra, as outras só abrem (MultiFernet). É assim que
     se troca de chave sem perder o que já está guardado.
  2. Senão, o arquivo .chave_cofre na pasta de dados, criado na primeira
     gravação, com permissão 600 onde o sistema aceita. Uma chave por linha,
     mesma regra de ordem.

Uma linha CHAVE_COFRE no .env é ignorada (config.py): o .env é justamente o
arquivo de onde os segredos saíram.

Cada valor é cifrado junto com o provedor e o nome da própria linha. Quem
consegue escrever no banco, mas não tem a chave, não troca um segredo de
linha: o valor copiado para outra linha é recusado na leitura, sem aparecer
na mensagem de erro.

Ciclo de vida (OWASP Cryptographic Storage Cheat Sheet; OWASP ASVS 5.0,
criptografia e gestão de segredos):

  - geração: automática na primeira gravação, ou CHAVE_COFRE definida por você;
  - acesso: só por este módulo. Segredo fixo vem do ambiente do sistema
    primeiro (servidor e CI injetam por ali), depois do cofre, e por último do
    valor antigo em texto puro no .env, que continua funcionando;
  - rotação: chave nova na frente, antiga atrás, `python cli.py cofre
    rotacionar`, depois a antiga sai;
  - revogação: revogue no portal do marketplace e apague a entrada com
    `python cli.py cofre apagar PROVEDOR NOME`;
  - recuperação: backup da chave. Sem ela não há como abrir o que foi
    cifrado: `python cli.py cofre apagar --tudo` apaga as entradas sem pedir
    a chave, e aí é reconectar os marketplaces e digitar os segredos de novo.

Falha fechada: chave errada, ausente ou malformada levanta ErroCofre com
mensagem clara. Não há volta para texto puro, e nenhuma chave nova é gerada
por cima de um cofre que já tem dados. Valor e chave nunca entram em evento,
exceção ou resposta de API — nem pelo encadeamento de exceções.

Refresh token de uso único (ML e Shopee): uma renovação de cada vez por
provedor (trava_de_tokens), e o par novo vai para a memória do processo antes
do banco (guardar_tokens). Se a gravação falhar, o token segue valendo da
memória e a gravação é tentada de novo a cada uso.

Protege o banco copiado, enviado ou esquecido num backup. Não protege contra
quem já controla a máquina: a chave está no mesmo computador.
"""
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

import config
from db import agora, conectar, registrar_evento

ARQ_CHAVE = config.DATA_DIR / ".chave_cofre"
VARIAVEL_CHAVE = "CHAVE_COFRE"

SCHEMA_COFRE = """
CREATE TABLE IF NOT EXISTS credenciais (
    provedor      TEXT NOT NULL,
    nome          TEXT NOT NULL,
    valor_cifrado TEXT NOT NULL,
    atualizado_em TEXT NOT NULL,
    PRIMARY KEY (provedor, nome)
)
"""

# Variável de ambiente -> (provedor, nome no cofre). Tudo o que está aqui é
# segredo: o painel grava no cofre, nunca no .env.
SEGREDOS = {
    "ML_CLIENT_SECRET": ("mercadolivre", "client_secret"),
    "ML_REFRESH_TOKEN": ("mercadolivre", "refresh_token"),
    "SHOPEE_PARTNER_KEY": ("shopee", "partner_key"),
    "SHOPEE_REFRESH_TOKEN": ("shopee", "refresh_token"),
    "AMZ_LWA_CLIENT_SECRET": ("amazon", "lwa_client_secret"),
    "AMZ_REFRESH_TOKEN": ("amazon", "refresh_token"),
    "ANTHROPIC_API_KEY": ("anthropic", "api_key"),
}

# Refresh tokens que o marketplace troca a cada renovação: o antigo deixa de
# valer. Para eles o cofre vem primeiro, porque guarda o mais novo; o valor
# do ambiente só serve de ponto de partida. Era a mesma regra dos antigos
# .token_*.json, que também venciam o .env.
ROTATIVOS = frozenset({"ML_REFRESH_TOKEN", "SHOPEE_REFRESH_TOKEN"})

# Nomes de token que os conectores guardam por provedor.
NOMES_TOKEN = ("access_token", "refresh_token", "expira_em")

# Saída quando a chave se perdeu; citada nas mensagens de chave ausente ou errada.
SAIDA_SEM_CHAVE = ("Sem backup da chave, 'python cli.py cofre apagar --tudo' apaga o que "
                   "está cifrado (pede confirmação, não precisa da chave); depois reconecte "
                   "os marketplaces e digite os segredos de novo.")


class ErroCofre(RuntimeError):
    """Chave errada, ausente ou malformada, ou cofre ilegível. A mensagem
    nunca traz valor guardado nem chave."""


def avisar(nivel: str, origem: str, mensagem: str) -> None:
    """Registra um evento sem nunca levantar exceção: com o banco travado, o
    aviso vai para a saída de erro. A mensagem não pode trazer valor."""
    try:
        registrar_evento(nivel, origem, mensagem)
    except (sqlite3.Error, OSError):
        print(f"[{nivel}] {origem}: {mensagem}", file=sys.stderr)


# ------------------------------------------------------------------ Chave

def inicializar_cofre():
    """Cria a tabela. Não gera chave: ela só nasce na primeira gravação."""
    with conectar() as conn:
        conn.execute(SCHEMA_COFRE)


def _separar(texto: str) -> list[str]:
    return [parte.strip() for parte in texto.replace("\n", ",").split(",") if parte.strip()]


def _montar(chaves: list[str], origem: str) -> MultiFernet:
    try:
        return MultiFernet([Fernet(chave.encode("ascii")) for chave in chaves])
    except (ValueError, TypeError):
        raise ErroCofre(f"{origem} inválida: cada chave precisa ser uma chave Fernet "
                        "(32 bytes em base64 url-safe). O cofre não abre sem ela.") from None


def _do_arquivo() -> MultiFernet:
    try:
        texto = ARQ_CHAVE.read_text(encoding="ascii")
    except (OSError, ValueError):
        raise ErroCofre(f"Não foi possível ler {ARQ_CHAVE.name} na pasta de dados. "
                        "O cofre não abre sem a chave.") from None
    chaves = _separar(texto)
    if not chaves:
        raise ErroCofre(f"{ARQ_CHAVE.name} está vazio. Restaure a chave do backup "
                        f"ou defina {VARIAVEL_CHAVE}.")
    return _montar(chaves, ARQ_CHAVE.name)


def _gerar_chave() -> MultiFernet:
    """Cria .chave_cofre com uma chave nova. O_EXCL: se outro processo criou o
    arquivo no mesmo instante, vale a chave dele."""
    chave = Fernet.generate_key()
    modo = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(ARQ_CHAVE, modo, 0o600)
    except FileExistsError:
        return _do_arquivo()
    except OSError:
        raise ErroCofre(f"Não foi possível criar {ARQ_CHAVE.name} na pasta de dados. "
                        "Nada foi gravado no cofre.") from None
    try:
        with os.fdopen(fd, "wb") as arquivo:
            arquivo.write(chave)
    except OSError:
        # Arquivo criado agora e ainda sem uso: não cifrou nada, pode sair.
        ARQ_CHAVE.unlink(missing_ok=True)
        raise ErroCofre(f"Não foi possível gravar {ARQ_CHAVE.name} na pasta de dados. "
                        "Nada foi gravado no cofre.") from None
    registrar_evento(
        "atencao", "cofre",
        f"Chave do cofre de credenciais gerada em {ARQ_CHAVE.name}, na pasta de dados. "
        "Faça backup dela agora, longe do banco: sem ela os tokens e segredos de "
        "marketplace guardados no cofre não abrem, e será preciso reconectar e "
        "digitar tudo de novo.")
    return MultiFernet([Fernet(chave)])


def _chaves(guardadas: int, para_gravar: bool) -> MultiFernet | None:
    """As chaves em uso. None só quando não há chave nem nada guardado e a
    operação é de leitura: não há o que abrir, e não vale criar chave à toa."""
    do_ambiente = _separar(os.environ.get(VARIAVEL_CHAVE, ""))
    if do_ambiente:
        return _montar(do_ambiente, VARIAVEL_CHAVE)
    if ARQ_CHAVE.exists():
        return _do_arquivo()
    if guardadas:
        raise ErroCofre(
            f"Chave do cofre ausente: há {guardadas} credencial(is) cifrada(s), mas "
            f"nem {VARIAVEL_CHAVE} nem {ARQ_CHAVE.name} existem. Restaure "
            f"{ARQ_CHAVE.name} do backup ou defina {VARIAVEL_CHAVE}. Nenhuma chave "
            f"nova é gerada por cima de um cofre com dados. {SAIDA_SEM_CHAVE}")
    return _gerar_chave() if para_gravar else None


def quantidade_de_chaves() -> int:
    """Quantas chaves estão configuradas (1 = só a atual). Nunca as chaves."""
    do_ambiente = _separar(os.environ.get(VARIAVEL_CHAVE, ""))
    if do_ambiente:
        return len(do_ambiente)
    try:
        return len(_separar(ARQ_CHAVE.read_text(encoding="ascii")))
    except (OSError, ValueError):
        return 0


def origem_da_chave() -> str | None:
    """De onde a chave vem hoje, para as mensagens ao operador. Nunca a chave."""
    if _separar(os.environ.get(VARIAVEL_CHAVE, "")):
        return VARIAVEL_CHAVE
    if ARQ_CHAVE.exists():
        return str(ARQ_CHAVE)
    return None


def _estado(espera: float = 5.0) -> tuple[int, str | None]:
    """Quantas credenciais há e uma delas, cifrada, para conferir a chave."""
    with conectar(espera) as conn:
        conn.execute(SCHEMA_COFRE)
        total = conn.execute("SELECT COUNT(*) FROM credenciais").fetchone()[0]
        amostra = conn.execute("SELECT valor_cifrado FROM credenciais LIMIT 1").fetchone()
    return total, amostra[0] if amostra else None


def _para_gravar(espera: float = 5.0) -> MultiFernet:
    """Chaves para cifrar, já conferidas contra o que está guardado: com a
    chave errada, gravar misturaria duas chaves na mesma tabela."""
    total, amostra = _estado(espera)
    fernet = _chaves(total, para_gravar=True)
    if amostra is not None:
        try:
            fernet.decrypt(amostra.encode())
        except InvalidToken:
            raise ErroCofre(
                "A chave do cofre não confere com as credenciais já guardadas. Nada "
                f"foi gravado. Confira {VARIAVEL_CHAVE} ou {ARQ_CHAVE.name}. "
                f"{SAIDA_SEM_CHAVE}") from None
    return fernet


def conferir() -> None:
    """Levanta ErroCofre se o cofre não aceitaria uma gravação agora. Serve
    para falhar ANTES de gastar um código de autorização ou um refresh token
    de uso único. Faz a mesma conferência de chave que a gravação."""
    total, amostra = _estado()
    fernet = _chaves(total, para_gravar=False)
    if fernet is not None and amostra is not None:
        try:
            fernet.decrypt(amostra.encode())
        except InvalidToken:
            raise ErroCofre(
                "A chave do cofre não confere com as credenciais já guardadas. "
                f"Confira {VARIAVEL_CHAVE} ou {ARQ_CHAVE.name}. {SAIDA_SEM_CHAVE}") from None


# --------------------------------------------------------------- Envelope
#
# O que é cifrado não é só o valor: é {"provedor", "nome", "valor"}. Na
# leitura, provedor e nome precisam ser os da linha.

def _envelope(provedor: str, nome: str, valor: str) -> bytes:
    return json.dumps({"provedor": provedor, "nome": nome, "valor": valor},
                      ensure_ascii=False).encode("utf-8")


def _abrir_envelope(claro: bytes) -> tuple[str, str, str] | None:
    """(provedor, nome, valor), ou None se não é um envelope: valor gravado
    por uma versão anterior, cifrado sozinho."""
    try:
        dados = json.loads(claro.decode("utf-8"))
        aberto = (dados["provedor"], dados["nome"], dados["valor"])
    except (ValueError, TypeError, KeyError):
        # Sem `raise` aqui dentro: a exceção de leitura carrega o texto claro.
        aberto = None
    if aberto is None or not all(isinstance(parte, str) for parte in aberto):
        return None
    return aberto


def _erro_de_linha(provedor: str, nome: str) -> ErroCofre:
    return ErroCofre(
        f"O valor guardado em {provedor}/{nome} pertence a outra linha do cofre: o banco "
        "foi alterado fora do programa. Nada foi usado. Apague a entrada com 'python "
        f"cli.py cofre apagar {provedor} {nome}' e grave de novo pelo painel.")


def _desembrulhar(claro: bytes, provedor: str, nome: str) -> str:
    aberto = _abrir_envelope(claro)
    if aberto is None:
        raise ErroCofre(
            f"A credencial {provedor}/{nome} foi gravada por uma versão anterior do cofre. "
            "Rode 'python cli.py cofre rotacionar' para regravá-la presa à própria linha, "
            f"ou apague com 'python cli.py cofre apagar {provedor} {nome}'.")
    if aberto[:2] != (provedor, nome):
        raise _erro_de_linha(provedor, nome)
    return aberto[2]


# -------------------------------------------------------------------- API

def _validar(provedor: str, nome: str):
    if not provedor or not nome:
        raise ValueError("Informe provedor e nome da credencial.")


def _gravar(provedor: str, valores: dict[str, str], *, espera: float = 5.0,
            so_se_vazio: bool = False) -> bool:
    """Grava numa transação só. so_se_vazio: só grava se o provedor ainda não
    tem token nenhum, conferido dentro da transação, e nunca troca linha que
    já existe. Devolve se gravou."""
    for nome, valor in valores.items():
        _validar(provedor, nome)
        if not isinstance(valor, str) or not valor:
            raise ValueError(f"Valor vazio para {provedor}/{nome}; nada foi gravado.")
    if not valores:
        return False
    fernet = _para_gravar(espera)
    momento = agora()
    linhas = [(provedor, nome, fernet.encrypt(_envelope(provedor, nome, valor)).decode(), momento)
              for nome, valor in valores.items()]
    with conectar(espera) as conn:
        conn.execute(SCHEMA_COFRE)
        if so_se_vazio:
            conn.execute("BEGIN IMMEDIATE")
            existentes = conn.execute(
                "SELECT COUNT(*) FROM credenciais WHERE provedor = ?"
                " AND nome IN ('access_token', 'refresh_token')", (provedor,)).fetchone()[0]
            if existentes:
                return False
            conn.executemany(
                "INSERT OR IGNORE INTO credenciais (provedor, nome, valor_cifrado, atualizado_em)"
                " VALUES (?,?,?,?)", linhas)
            return True
        conn.executemany(
            "INSERT OR REPLACE INTO credenciais (provedor, nome, valor_cifrado, atualizado_em)"
            " VALUES (?,?,?,?)", linhas)
    return True


def guardar_lote(provedor: str, valores: dict[str, str]) -> None:
    """Grava vários valores do mesmo provedor numa transação só — o par
    access/refresh não pode ficar pela metade."""
    _gravar(provedor, valores)


def guardar(provedor: str, nome: str, valor: str) -> None:
    guardar_lote(provedor, {nome: valor})


def ler(provedor: str, nome: str) -> str | None:
    """O valor em claro, ou None se não existe. Chave errada, ou valor que
    pertence a outra linha, levanta ErroCofre. Um token recém-emitido que
    ainda não chegou ao banco vale antes do banco (ver guardar_tokens)."""
    _validar(provedor, nome)
    with _trava_pendentes:
        pendente = _pendentes.get(provedor, {}).get(nome)
    if pendente is not None:
        return pendente
    total, _ = _estado()
    fernet = _chaves(total, para_gravar=False)
    if fernet is None:
        return None
    with conectar() as conn:
        linha = conn.execute(
            "SELECT valor_cifrado FROM credenciais WHERE provedor = ? AND nome = ?",
            (provedor, nome)).fetchone()
    if linha is None:
        return None
    try:
        claro = fernet.decrypt(linha[0].encode())
    except InvalidToken:
        raise ErroCofre(
            f"A credencial {provedor}/{nome} não abre com a chave atual do cofre. "
            f"Confira {VARIAVEL_CHAVE} ou {ARQ_CHAVE.name}. Se trocou de chave, deixe "
            "a antiga depois da nova até rodar 'python cli.py cofre rotacionar'.") from None
    return _desembrulhar(claro, provedor, nome)


def ler_validade(provedor: str) -> int:
    """expira_em do provedor, em epoch; 0 se não há. Um valor que não é número
    levanta ErroCofre sem repetir o valor na mensagem."""
    texto = ler(provedor, "expira_em")
    if not texto:
        return 0
    try:
        return int(texto)
    except ValueError:
        pass  # sem `raise` aqui dentro: a exceção do int() repete o valor
    raise ErroCofre(
        f"A validade guardada do token de {provedor} não é um número: o cofre foi "
        f"alterado fora do programa. Reconecte {provedor} no painel.")


def apagar(provedor: str, nome: str) -> bool:
    """Remove a entrada. Não precisa da chave: revogar tem de funcionar mesmo
    com a chave perdida."""
    _validar(provedor, nome)
    with conectar() as conn:
        conn.execute(SCHEMA_COFRE)
        removidas = conn.execute(
            "DELETE FROM credenciais WHERE provedor = ? AND nome = ?",
            (provedor, nome)).rowcount
    if removidas:
        registrar_evento("info", "cofre", f"Credencial apagada do cofre: {provedor}/{nome}")
    return bool(removidas)


def apagar_tudo() -> int:
    """Remove todas as entradas, sem precisar da chave: é a saída quando a
    chave se perdeu. Depois é reconectar os marketplaces e digitar os segredos
    de novo. A chave (CHAVE_COFRE ou .chave_cofre) não é tocada."""
    with conectar() as conn:
        conn.execute(SCHEMA_COFRE)
        removidas = conn.execute("DELETE FROM credenciais").rowcount
    with _trava_pendentes:
        _pendentes.clear()
        _proxima_tentativa.clear()
    if removidas:
        registrar_evento("atencao", "cofre",
                         f"Cofre esvaziado: {removidas} credencial(is) apagada(s). Reconecte "
                         "os marketplaces e digite os segredos de novo no painel.")
    return removidas


def listar() -> list[dict]:
    """Provedor, nome e data de cada entrada. Nunca o valor."""
    with conectar() as conn:
        conn.execute(SCHEMA_COFRE)
        linhas = conn.execute(
            "SELECT provedor, nome, atualizado_em FROM credenciais"
            " ORDER BY provedor, nome").fetchall()
    return [dict(l) for l in linhas]


def rotacionar() -> int:
    """Recifra todas as entradas com a chave atual (a primeira). Tudo numa
    transação: se uma entrada não abre com nenhuma chave, ou pertence a outra
    linha, nada muda. Entrada gravada por uma versão anterior, com o valor
    cifrado sozinho, é regravada presa à própria linha."""
    with conectar() as conn:
        conn.execute(SCHEMA_COFRE)
        conn.execute("BEGIN IMMEDIATE")  # renovação de token no meio não se perde
        linhas = conn.execute(
            "SELECT provedor, nome, valor_cifrado FROM credenciais").fetchall()
        if not linhas:
            return 0
        fernet = _chaves(len(linhas), para_gravar=False)
        novas = []
        antigas = 0
        for l in linhas:
            provedor, nome = l["provedor"], l["nome"]
            try:
                claro = fernet.decrypt(l["valor_cifrado"].encode())
            except InvalidToken:
                raise ErroCofre(
                    f"A credencial {provedor}/{nome} não abre com nenhuma das "
                    f"chaves de {VARIAVEL_CHAVE} ou {ARQ_CHAVE.name}. Nada foi "
                    "recifrado: coloque a chave antiga depois da nova e rode de novo.") from None
            aberto = _abrir_envelope(claro)
            if aberto is None:
                antigas += 1
                claro = _envelope(provedor, nome, claro.decode("utf-8"))
            elif aberto[:2] != (provedor, nome):
                erro = _erro_de_linha(provedor, nome)
                raise ErroCofre(f"{erro} Nada foi recifrado.")
            novas.append((fernet.encrypt(claro).decode(), agora(), provedor, nome))
        conn.executemany(
            "UPDATE credenciais SET valor_cifrado = ?, atualizado_em = ?"
            " WHERE provedor = ? AND nome = ?", novas)
    sobra = " A chave antiga já pode sair." if quantidade_de_chaves() > 1 else ""
    formato = (f" {antigas} no formato anterior foram regravada(s) presa(s) à própria linha."
               if antigas else "")
    registrar_evento("info", "cofre",
                     f"Cofre recifrado com a chave atual: {len(novas)} credencial(is).{formato}{sobra}")
    return len(novas)


# ------------------------------------------------------ Leitura de segredo

def definido_no_sistema(variavel: str) -> bool:
    """A variável tem valor no ambiente do sistema, e não só no arquivo .env."""
    return (bool(os.environ.get(variavel, "").strip())
            and variavel not in config.DO_ARQUIVO_ENV)


def segredo(variavel: str) -> str:
    """Regra única de leitura de segredo, chamada na hora do uso.

    Segredo fixo: ambiente do sistema, depois o cofre, depois o valor em
    texto puro que já estava no .env (continua valendo até você migrar).
    Refresh token rotativo: o cofre, depois o ambiente (ver ROTATIVOS).
    Devolve "" quando não há valor em lugar nenhum."""
    provedor, nome = SEGREDOS[variavel]
    valor = os.environ.get(variavel, "").strip()
    if variavel not in ROTATIVOS and definido_no_sistema(variavel):
        return valor
    return ler(provedor, nome) or valor


def guardar_segredo(variavel: str, valor: str) -> None:
    provedor, nome = SEGREDOS[variavel]
    guardar(provedor, nome, valor)


# ------------------------------------------- Tokens recém-emitidos
#
# O ML e a Shopee trocam o refresh token a cada renovação: quando a resposta
# chega, o antigo já não vale. O par novo vai primeiro para a memória do
# processo, que ler() consulta antes do banco, e só depois para o cofre.

_pendentes: dict[str, dict[str, str]] = {}
_proxima_tentativa: dict[str, float] = {}
_trava_pendentes = threading.Lock()

TENTATIVAS_TOKEN = 3        # gravações do par novo antes de desistir por agora
ESPERA_BANCO_TOKEN = 30.0   # segundos esperando um banco ocupado, por tentativa
PAUSA_TOKEN = 1.0           # entre uma tentativa e outra
INTERVALO_REGRAVAR = 30.0   # depois disso, uma tentativa a cada tanto, no uso


def guardar_tokens(provedor: str, valores: dict[str, str]) -> bool:
    """Guarda o par que o marketplace ACABOU de emitir, sem nunca levantar
    exceção: memória do processo primeiro, depois o cofre, com novas
    tentativas e espera longa por banco ocupado. Se ainda assim não gravar, o
    token segue valendo da memória, um evento 'atencao' (sem valor) avisa para
    não reiniciar o programa, e cada uso seguinte tenta de novo
    (regravar_pendente). Devolve se chegou ao cofre.

    Toda gravação de token pendente passa pela trava do provedor: sem ela, um
    par mais antigo gravado por último poderia cobrir o mais novo.

    O que chega se soma ao que ainda está só na memória: uma resposta sem
    refresh token não descarta o refresh novo que ainda não foi gravado."""
    with _trava_local(provedor):
        with _trava_pendentes:
            _pendentes[provedor] = {**_pendentes.get(provedor, {}), **valores}
        try:
            with trava_de_tokens(provedor):
                return _gravar_pendente(provedor, TENTATIVAS_TOKEN)
        except ErroCofre:  # trava de outro processo presa: grava no próximo uso
            return _reter(provedor)


def _gravar_pendente(provedor: str, tentativas: int) -> bool:
    """Chamada só com trava_de_tokens(provedor) na mão."""
    with _trava_pendentes:
        valores = dict(_pendentes.get(provedor, {}))
    if not valores:
        return True
    for tentativa in range(tentativas):
        if tentativa:
            time.sleep(PAUSA_TOKEN)
        try:
            _gravar(provedor, valores, espera=ESPERA_BANCO_TOKEN)
        except (ErroCofre, sqlite3.Error, OSError, ValueError):
            continue
        with _trava_pendentes:
            if _pendentes.get(provedor) == valores:
                del _pendentes[provedor]
            retido = _proxima_tentativa.pop(provedor, None) is not None
        if retido:
            avisar("info", "cofre", f"Os tokens de {provedor} que estavam só na memória "
                                    "foram gravados no cofre.")
        return True
    return _reter(provedor)


def _reter(provedor: str) -> bool:
    """O token fica só na memória por enquanto: marca a próxima tentativa e
    avisa uma vez, sem valor."""
    with _trava_pendentes:
        ja_avisado = provedor in _proxima_tentativa
        _proxima_tentativa[provedor] = time.monotonic() + INTERVALO_REGRAVAR
    if not ja_avisado:
        avisar("atencao", "cofre",
               f"Os tokens novos de {provedor} não puderam ser gravados no cofre e estão só "
               "na memória deste processo. Não feche nem reinicie o programa: o refresh "
               "antigo já não vale, e sem o novo será preciso autorizar a conta de novo. "
               "Confira se o banco está aberto em outro programa e se a chave do cofre "
               "confere; a gravação é tentada de novo a cada uso.")
    return False


def regravar_pendente(provedor: str) -> None:
    """Se há token só na memória, tenta gravá-lo de novo, no máximo a cada
    INTERVALO_REGRAVAR segundos. Nunca levanta exceção."""
    with _trava_pendentes:
        if provedor not in _pendentes:
            return
        if time.monotonic() < _proxima_tentativa.get(provedor, 0.0):
            return
    try:
        with trava_de_tokens(provedor):
            _gravar_pendente(provedor, 1)
    except ErroCofre:
        _reter(provedor)


def so_na_memoria(provedor: str) -> bool:
    """Há token deste provedor que ainda não chegou ao cofre."""
    with _trava_pendentes:
        return provedor in _pendentes


# ------------------------------------------------ Uma renovação por vez
#
# Refresh token de uso único mandado duas vezes: a segunda falha com
# invalid_grant, e o marketplace pode até revogar a autorização inteira.
# Entre threads deste processo vale um RLock por provedor; entre processos
# (o painel e um `cli.py rodar` ao lado), o primeiro byte de um arquivo vazio
# por provedor (.trava_tokens.mercadolivre, ...), que o sistema solta sozinho
# se o processo morrer. Um arquivo por provedor, e não um só: no Linux e no
# macOS, fechar qualquer descritor de um arquivo solta TODAS as travas que o
# processo tem nele, e a Shopee soltaria a trava do ML.

ARQ_TRAVA = config.DATA_DIR / ".trava_tokens"  # base do nome; ver _arquivo_trava
ESPERA_TRAVA = 120.0  # segundos esperando a renovação de outro processo

_travas: dict[str, threading.RLock] = {}
_profundidade: dict[str, int] = {}
_descritores: dict[str, int] = {}
_trava_das_travas = threading.Lock()

if os.name == "nt":
    import msvcrt

    def _trancar(fd: int):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _destrancar(fd: int):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _trancar(fd: int):
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)

    def _destrancar(fd: int):
        fcntl.lockf(fd, fcntl.LOCK_UN, 1, 0, os.SEEK_SET)


def _arquivo_trava(provedor: str) -> Path:
    """O arquivo de trava do provedor, igual em todos os processos."""
    return ARQ_TRAVA.with_name(f"{ARQ_TRAVA.name}.{provedor}")


def _travar_arquivo(provedor: str) -> int:
    arquivo = _arquivo_trava(provedor)
    try:
        fd = os.open(arquivo, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        raise ErroCofre(f"Não foi possível abrir {arquivo.name} na pasta de dados. A "
                        f"renovação do token de {provedor} não foi feita.") from None
    limite = time.monotonic() + ESPERA_TRAVA
    while True:
        try:
            _trancar(fd)
            return fd
        except OSError:
            if time.monotonic() >= limite:
                break
        time.sleep(0.2)
    os.close(fd)
    raise ErroCofre(f"Outro processo está renovando o token de {provedor} há mais de "
                    f"{int(ESPERA_TRAVA)} s. Nada foi enviado ao marketplace; tente de novo.")


def _soltar_arquivo(fd: int):
    try:
        _destrancar(fd)
    except OSError:
        pass  # fechar o arquivo solta a trava de qualquer jeito
    finally:
        os.close(fd)


def _trava_local(provedor: str) -> threading.RLock:
    with _trava_das_travas:
        return _travas.setdefault(provedor, threading.RLock())


@contextmanager
def trava_de_tokens(provedor: str):
    """Uma renovação de token de cada vez por provedor, entre threads e entre
    processos. Reentrante na mesma thread. Quem entra deve reler o cofre: o
    refresh token que conhecia pode ter sido gasto por quem saiu."""
    with _trava_local(provedor):
        if not _profundidade.get(provedor):
            _descritores[provedor] = _travar_arquivo(provedor)
        _profundidade[provedor] = _profundidade.get(provedor, 0) + 1
        try:
            yield
        finally:
            _profundidade[provedor] -= 1
            if not _profundidade[provedor]:
                _soltar_arquivo(_descritores.pop(provedor))


# --------------------------------------------------------- Arquivo legado

def tem_tokens(provedor: str) -> bool:
    return bool(ler(provedor, "refresh_token") or ler(provedor, "access_token"))


def importar_legado(provedor: str, arquivo: Path) -> bool:
    """Importa um .token_*.json antigo, em texto puro, se o cofre ainda não
    tem tokens deste provedor. A gravação confere de novo, dentro da própria
    transação, que não há token, e nunca troca linha existente: um token
    renovado no meio do caminho não é trocado pelo do arquivo, já gasto. O
    arquivo não é apagado nem alterado: um evento avisa que ele pode ser
    apagado à mão depois de conferir a conexão."""
    if not arquivo.exists() or tem_tokens(provedor):
        return False
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        valores = {n: str(dados[n]) for n in ("access_token", "refresh_token") if dados.get(n)}
        if dados.get("expira_em"):
            valores["expira_em"] = str(int(float(dados["expira_em"])))
    except (OSError, ValueError, TypeError, AttributeError):
        valores = None
    if valores is None:
        registrar_evento("atencao", "cofre",
                         f"{arquivo.name} está ilegível; nada foi importado para o cofre "
                         "e o arquivo não foi alterado.")
        return False
    if not valores:
        return False
    if not _gravar(provedor, valores, so_se_vazio=True):
        return False  # outro chegou antes, com tokens mais novos
    if any(ler(provedor, n) != v for n, v in valores.items()):
        raise ErroCofre(f"A conferência da importação de {arquivo.name} falhou. "
                        "O arquivo não foi alterado.")
    registrar_evento("atencao", "cofre",
                     f"Tokens de {provedor} importados de {arquivo.name} para o cofre "
                     "cifrado. O arquivo em texto puro continua no disco, sem alteração: "
                     f"confira a conexão no painel e apague {arquivo.name} à mão.")
    return True


# -------------------------------------------------------------- Migração

@dataclass
class RelatorioMigracao:
    copiados: list[str] = field(default_factory=list)      # copiados agora e conferidos
    ja_no_cofre: list[str] = field(default_factory=list)   # mesmo valor já guardado
    diferentes: list[str] = field(default_factory=list)    # o cofre tem outro, que é o que vale
    no_sistema: list[str] = field(default_factory=list)    # também no ambiente do sistema
    linhas_env: list[str] = field(default_factory=list)    # podem sair do .env, à mão
    arquivos: list[Path] = field(default_factory=list)     # podem ser apagados, à mão


def migrar(arquivo_env: Path, legados: dict[str, Path]) -> RelatorioMigracao:
    """Copia para o cofre os segredos do .env e os tokens dos arquivos legados,
    conferindo cada um por leitura. Só lê o .env e os arquivos: quem apaga é
    você, depois de conferir as conexões."""
    relatorio = RelatorioMigracao()

    # Os arquivos primeiro: o token deles sempre venceu o do .env. A trava
    # espera uma renovação em andamento no painel terminar.
    for provedor, arquivo in legados.items():
        if not arquivo.exists():
            continue
        with trava_de_tokens(provedor):
            importar_legado(provedor, arquivo)
            if tem_tokens(provedor):
                relatorio.arquivos.append(arquivo)

    valores = config.ler_arquivo_env(arquivo_env) if arquivo_env.exists() else {}
    for variavel, (provedor, nome) in SEGREDOS.items():
        valor = valores.get(variavel, "").strip()
        if not valor:
            continue
        guardado = ler(provedor, nome)
        if guardado is None:
            guardar(provedor, nome, valor)
            if ler(provedor, nome) != valor:
                raise ErroCofre(f"A conferência de {variavel} no cofre falhou. O .env "
                                "não foi alterado e continua valendo.")
            relatorio.copiados.append(variavel)
        elif guardado == valor:
            relatorio.ja_no_cofre.append(variavel)
        else:
            relatorio.diferentes.append(variavel)
        relatorio.linhas_env.append(variavel)
        if definido_no_sistema(variavel):
            relatorio.no_sistema.append(variavel)

    if relatorio.copiados:
        registrar_evento("info", "cofre",
                         f"Migração: {len(relatorio.copiados)} segredo(s) do .env copiados "
                         f"para o cofre ({', '.join(relatorio.copiados)}). O .env não foi alterado.")
    return relatorio
