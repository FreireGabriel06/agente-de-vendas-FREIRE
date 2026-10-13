"""
Backup e restauração do banco junto com as chaves (lote S3; achados S-28,
S-11 e S-84 da auditoria de 06/10/2026).

O banco sem as chaves é ilegível (dados de comprador e credenciais são
cifrados), e as chaves sem o banco não servem para nada: por isso vão juntos,
num arquivo só, cifrado com uma senha de backup. Quem tem o arquivo e não tem
a senha não lê nada.

Formato do arquivo (.agvbkp): a linha AGVBKP1, 16 bytes de sal e um token
Fernet com um zip dentro. A chave do Fernet sai da senha por scrypt. O zip
leva agente.db (cópia consistente pela API de backup do SQLite), as chaves
(.chave_lgpd e .chave_cofre, venham do arquivo ou do ambiente) e um
manifesto com o sha256 de cada parte.

A restauração vai sempre para uma pasta nova ou vazia: nunca escreve por cima
de dados que existem. Depois de restaurar, reaplica a retenção (expurgo) e as
eliminações atendidas depois do backup, que ficam fora do banco.
"""
import base64
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from config import DATA_DIR, config
from core import cofre, privacidade
from db import agora, registrar_evento

CABECALHO = b"AGVBKP1\n"
SCRYPT_N = 2 ** 17
SENHA_MINIMA = 12
VERSAO = 1


class ErroBackup(Exception):
    pass


def _fernet(senha: str, sal: bytes) -> Fernet:
    if len(senha or "") < SENHA_MINIMA:
        raise ErroBackup(f"A senha do backup precisa ter pelo menos {SENHA_MINIMA} caracteres.")
    chave = Scrypt(salt=sal, length=32, n=SCRYPT_N, r=8, p=1).derive(senha.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(chave))


def _sha256(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def _chave_lgpd_atual() -> bytes | None:
    env = os.getenv("CHAVE_LGPD")
    if env:
        return env.encode()
    if privacidade.ARQ_CHAVE.exists():
        return privacidade.ARQ_CHAVE.read_bytes()
    return None


def _chave_cofre_atual() -> bytes | None:
    env = os.getenv(cofre.VARIAVEL_CHAVE)
    if env and env.strip():
        return env.strip().encode()
    if cofre.ARQ_CHAVE.exists():
        return cofre.ARQ_CHAVE.read_bytes()
    return None


def _copia_do_banco() -> bytes:
    """Cópia consistente mesmo com o worker gravando (API de backup do SQLite)."""
    origem_caminho = Path(config.db_path)
    if not origem_caminho.exists():
        raise ErroBackup("Não há banco para copiar nesta pasta de dados.")
    with tempfile.TemporaryDirectory() as pasta:
        copia = Path(pasta) / "agente.db"
        origem = sqlite3.connect(f"{origem_caminho.resolve().as_uri()}?mode=ro", uri=True)
        destino = sqlite3.connect(copia)
        try:
            origem.backup(destino)
        finally:
            destino.close()
            origem.close()
        return copia.read_bytes()


def criar(destino: Path, senha: str) -> Path:
    """Grava um backup cifrado em `destino` (pasta) e devolve o caminho."""
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    partes = {"agente.db": _copia_do_banco()}
    chave_lgpd, chave_cofre = _chave_lgpd_atual(), _chave_cofre_atual()
    if chave_lgpd:
        partes[".chave_lgpd"] = chave_lgpd
    if chave_cofre:
        partes[".chave_cofre"] = chave_cofre
    # criado_em_exato (microssegundos) decide se uma exclusão da Amazon veio
    # depois deste backup (_reaplicar).
    manifesto = {"versao": VERSAO, "criado_em": agora(),
                 "criado_em_exato": datetime.now(timezone.utc).isoformat(),
                 "partes": {nome: _sha256(dados) for nome, dados in partes.items()}}

    pacote = io.BytesIO()
    with zipfile.ZipFile(pacote, "w", zipfile.ZIP_DEFLATED) as z:
        for nome, dados in partes.items():
            z.writestr(nome, dados)
        z.writestr("manifesto.json", json.dumps(manifesto, indent=1))

    sal = os.urandom(16)
    token = _fernet(senha, sal).encrypt(pacote.getvalue())
    nome = f"agente-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.agvbkp"
    arquivo = destino / nome
    arquivo.write_bytes(CABECALHO + sal + token)
    try:
        arquivo.chmod(0o600)
    except OSError:
        pass
    faltando = [n for n in (".chave_lgpd", ".chave_cofre") if n not in partes]
    registrar_evento("info", "backup", f"Backup cifrado criado: {nome}"
                     + (f" (sem {', '.join(faltando)}: ainda não existiam)" if faltando else ""))
    return arquivo


def _abrir(arquivo: Path, senha: str) -> tuple[dict, dict[str, bytes]]:
    bruto = Path(arquivo).read_bytes()
    if not bruto.startswith(CABECALHO) or len(bruto) < len(CABECALHO) + 17:
        raise ErroBackup("Este arquivo não é um backup do agente.")
    sal = bruto[len(CABECALHO):len(CABECALHO) + 16]
    try:
        pacote = _fernet(senha, sal).decrypt(bruto[len(CABECALHO) + 16:])
    except InvalidToken:
        raise ErroBackup("Senha errada ou arquivo alterado: o backup não abre.") from None
    with zipfile.ZipFile(io.BytesIO(pacote)) as z:
        manifesto = json.loads(z.read("manifesto.json"))
        partes = {nome: z.read(nome) for nome in manifesto["partes"]}
    for nome, dados in partes.items():
        if _sha256(dados) != manifesto["partes"][nome]:
            raise ErroBackup(f"A parte {nome} do backup não confere com o manifesto.")
    if "agente.db" not in partes:
        raise ErroBackup("O backup não tem o banco.")
    return manifesto, partes


def verificar(arquivo: Path, senha: str) -> dict:
    """Abre e confere o backup sem gravar nada. Devolve o manifesto."""
    manifesto, partes = _abrir(arquivo, senha)
    with tempfile.TemporaryDirectory() as pasta:
        banco = Path(pasta) / "agente.db"
        banco.write_bytes(partes["agente.db"])
        conn = sqlite3.connect(banco)
        try:
            integridade = conn.execute("PRAGMA integrity_check").fetchone()[0]
            pedidos = conn.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0]
        finally:
            conn.close()
    if integridade != "ok":
        raise ErroBackup("O banco do backup não passou na verificação de integridade do SQLite.")
    return {**manifesto, "pedidos": pedidos}


def _reaplicar(banco: Path, chave_lgpd: bytes | None, retencao_dias: int,
               eliminacoes: Path | None, criado_em: str | None = None) -> dict:
    """Expurgo por retenção e eliminações posteriores, direto no banco
    restaurado. Se os dados da Amazon foram apagados depois de o backup ser
    feito, apaga de novo (DPP da Amazon, 1.7)."""
    corte = (datetime.now(timezone.utc) - timedelta(days=retencao_dias)).isoformat(timespec="seconds")
    pseudonimos = set()
    amazon_apagado = None
    if eliminacoes and eliminacoes.exists():
        for linha in eliminacoes.read_text(encoding="utf-8").splitlines():
            try:
                registro = json.loads(linha)
            except ValueError:
                continue
            if not isinstance(registro, dict):
                continue
            if isinstance(registro.get("pseudonimo"), str):
                pseudonimos.add(registro["pseudonimo"])
            momento = _momento(registro.get("amazon_apagado_em"))
            if momento is not None:
                amazon_apagado = max(amazon_apagado or momento, momento)
    expurgados = eliminados = amazon = 0
    conn = sqlite3.connect(banco)
    try:
        feito = _momento(criado_em)
        # No empate, apaga: é o lado seguro para a exclusão.
        if amazon_apagado and (feito is None or feito <= amazon_apagado):
            amazon = _apagar_amazon_restaurado(conn)
        cur = conn.execute("UPDATE pedidos SET comprador_nome = NULL, comprador_id = NULL,"
                           " endereco_json = '{}' WHERE criado_em < ? AND comprador_nome IS NOT NULL",
                           (corte,))
        expurgados = cur.rowcount
        if pseudonimos and chave_lgpd:
            fernet = Fernet(chave_lgpd)
            sal = chave_lgpd.decode()[:16]
            for pid, cifrado in conn.execute(
                    "SELECT id, comprador_id FROM pedidos WHERE comprador_id IS NOT NULL").fetchall():
                try:
                    identificador = fernet.decrypt(cifrado.encode()).decode()
                except (InvalidToken, ValueError):
                    continue
                if hashlib.sha256((identificador + sal).encode()).hexdigest()[:24] in pseudonimos:
                    conn.execute("UPDATE pedidos SET comprador_nome = NULL, comprador_id = NULL,"
                                 " endereco_json = '{}' WHERE id = ?", (pid,))
                    eliminados += 1
        conn.commit()
    finally:
        conn.close()
    if amazon:
        conn = sqlite3.connect(banco)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    return {"expurgados": expurgados, "eliminados": eliminados, "amazon_apagados": amazon}


def _momento(texto) -> datetime | None:
    try:
        momento = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)


def _apagar_amazon_restaurado(conn) -> int:
    """As mesmas tabelas de core/sincronizacao.apagar_dados_amazon, no banco
    restaurado. Banco antigo sem alguma delas: pula."""
    from core import sincronizacao
    conn.execute("PRAGMA secure_delete = ON")
    total = 0
    comandos = [(f"DELETE FROM {t}", ()) for t in sincronizacao.TABELAS_AMAZON] + [
        (f"DELETE FROM sincronizacoes WHERE fonte IN ({', '.join('?' * len(sincronizacao.FONTES))})",
         sincronizacao.FONTES),
        (f"DELETE FROM alertas WHERE tipo IN ({', '.join('?' * len(sincronizacao.ALERTAS_DA_AMAZON))})",
         sincronizacao.ALERTAS_DA_AMAZON),
        ("DELETE FROM credenciais WHERE provedor = 'amazon' AND nome = 'refresh_token'", ()),
    ]
    for sql, parametros in comandos:
        try:
            total += conn.execute(sql, parametros).rowcount
        except sqlite3.OperationalError:
            continue
    return total


def restaurar(arquivo: Path, senha: str, pasta: Path, eliminacoes: Path | None = None) -> dict:
    """Restaura em `pasta` (nova ou vazia). Nunca escreve por cima de dados."""
    pasta = Path(pasta)
    if pasta.resolve() == DATA_DIR.resolve() or (pasta.exists() and any(pasta.iterdir())):
        raise ErroBackup("Restaure numa pasta nova ou vazia: o backup nunca escreve por cima "
                         "de dados que existem. Depois aponte AGENTE_DADOS para ela.")
    manifesto, partes = _abrir(arquivo, senha)
    pasta.mkdir(parents=True, exist_ok=True)
    for nome, dados in partes.items():
        alvo = pasta / nome
        alvo.write_bytes(dados)
        try:
            alvo.chmod(0o600)
        except OSError:
            pass
    reaplicado = _reaplicar(pasta / "agente.db", partes.get(".chave_lgpd"),
                            privacidade.RETENCAO_DIAS,
                            eliminacoes if eliminacoes is not None else privacidade.ARQ_ELIMINACOES,
                            manifesto.get("criado_em_exato") or manifesto.get("criado_em"))
    return {"criado_em": manifesto["criado_em"], "partes": sorted(partes), **reaplicado}
