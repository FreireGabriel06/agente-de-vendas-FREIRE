"""
Saúde da instância, para monitorar cada cliente sem abrir o painel.

Cada verificação devolve um nível: "ok", "atencao" (funciona, mas alguém
precisa olhar) ou "falha" (algo parou ou vai parar). O status geral é o pior
nível. Nada aqui grava no banco nem cria chave: verificar não muda o estado.

Quem usa:
  - GET /saude, sem login: só o status (HTTP 503 em falha), para um monitor
    externo (UptimeRobot, cron com curl);
  - GET /api/saude, só para admin: os detalhes;
  - python cli.py saude [--completo]: os detalhes e código de saída 0, 1 ou 2.

As mensagens não levam segredo nem dado de comprador.
"""
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config as config_mod
from config import DATA_DIR, config
from db import agora, conectar, verificar_trilha

NIVEIS = ("ok", "atencao", "falha")
DISCO_ATENCAO_MB = 1024
DISCO_FALHA_MB = 200
# O worker está atrasado depois de tantos intervalos sem terminar um ciclo.
CICLOS_DE_TOLERANCIA = 3


def _pior(niveis) -> str:
    return max(niveis, key=NIVEIS.index, default="ok")


def _banco(completo: bool) -> tuple[str, str]:
    try:
        with conectar() as conn:
            conn.execute("SELECT 1").fetchone()
            if completo:
                resultado = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if resultado != "ok":
                    return "falha", "o SQLite acusou corrupção (integrity_check); restaure um backup"
    except sqlite3.Error as e:
        return "falha", f"banco inacessível ({type(e).__name__})"
    return "ok", "acessível" + (" e íntegro" if completo else "")


def _cofre() -> tuple[str, str]:
    from core import cofre
    try:
        cofre.conferir()
    except cofre.ErroCofre as e:
        return "falha", str(e)
    return "ok", "a chave abre as credenciais guardadas"


def _chave_lgpd() -> tuple[str, str]:
    from core import privacidade
    tem_chave = bool(os.getenv("CHAVE_LGPD")) or privacidade.ARQ_CHAVE.exists()
    try:
        with conectar() as conn:
            amostra = conn.execute(
                "SELECT comprador_nome FROM pedidos WHERE comprador_nome LIKE 'gAAAA%' LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        amostra = None
    if not tem_chave:
        if amostra is not None:
            return "falha", "há dado de comprador cifrado e a chave LGPD sumiu; restaure .chave_lgpd"
        return "ok", "sem chave ainda; nasce no primeiro dado cifrado"
    if amostra is not None:
        from cryptography.fernet import Fernet, InvalidToken
        chave = os.getenv("CHAVE_LGPD", "").encode() or privacidade.ARQ_CHAVE.read_bytes()
        try:
            Fernet(chave).decrypt(amostra[0].encode())
        except (InvalidToken, ValueError):
            return "falha", "a chave LGPD não abre os dados de comprador guardados"
    return "ok", "presente" + (" e abre os dados guardados" if amostra is not None else "")


def _worker() -> tuple[str, str]:
    if os.getenv("WORKER_ATIVO", "true").strip().lower() != "true":
        return "ok", "desligado (WORKER_ATIVO=false)"
    try:
        with conectar() as conn:
            linha = conn.execute("SELECT valor, atualizado_em FROM estado_sistema"
                                 " WHERE chave = 'ciclo_fim'").fetchone()
    except sqlite3.OperationalError:
        linha = None
    if linha is None:
        return "atencao", "nenhum ciclo terminado registrado"
    fim = datetime.fromisoformat(linha["atualizado_em"])
    limite = timedelta(seconds=config.intervalo_worker * CICLOS_DE_TOLERANCIA)
    atraso = datetime.now(timezone.utc) - fim
    if atraso > limite:
        return "falha", f"último ciclo terminou há {int(atraso.total_seconds() // 60)} min"
    falhas = int(linha["valor"] or 0)
    if falhas:
        return "atencao", f"último ciclo em {linha['atualizado_em']} com {falhas} etapa(s) falhando"
    return "ok", f"último ciclo em {linha['atualizado_em']}"


def _backup() -> tuple[str, str]:
    """Backup mais velho que BACKUP_MAX_DIAS (padrão 7) pede atenção: sem
    cópia recente, um disco perdido leva os dados (DPP 2.7.4)."""
    maximo = config_mod._inteiro("BACKUP_MAX_DIAS", 7, 1, 365)
    try:
        with conectar() as conn:
            linha = conn.execute("SELECT atualizado_em FROM estado_sistema"
                                 " WHERE chave = 'backup_ultimo'").fetchone()
    except sqlite3.OperationalError:
        linha = None
    if linha is None:
        return "atencao", "nenhum backup registrado; rode python cli.py backup criar"
    idade = datetime.now(timezone.utc) - datetime.fromisoformat(linha["atualizado_em"])
    if idade > timedelta(days=maximo):
        return "atencao", f"último backup há {idade.days} dia(s); o limite é {maximo}"
    return "ok", f"último backup em {linha['atualizado_em']}"


def _contagem(sql: str, nome: str, conselho: str) -> tuple[str, str]:
    try:
        with conectar() as conn:
            n = conn.execute(sql).fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    return ("atencao", f"{n} {nome}; {conselho}") if n else ("ok", f"nenhum(a) {nome}")


def _banco_na_pasta() -> tuple[str, str]:
    """DB_PATH absoluto fora da pasta de dados pode ser o banco de outra
    instância (outro cliente). Achado S-35 da auditoria."""
    bruto = os.getenv("DB_PATH", "").strip()
    if bruto and Path(bruto).expanduser().is_absolute() and \
            not Path(bruto).expanduser().resolve().is_relative_to(Path(DATA_DIR).resolve()):
        return "atencao", ("DB_PATH aponta para fora da pasta de dados desta instância; "
                           "confira se não é o banco de outro cliente")
    return "ok", "dentro da pasta de dados"


def _disco() -> tuple[str, str]:
    livre_mb = shutil.disk_usage(DATA_DIR).free // (1024 * 1024)
    if livre_mb < DISCO_FALHA_MB:
        return "falha", f"{livre_mb} MB livres na pasta de dados"
    if livre_mb < DISCO_ATENCAO_MB:
        return "atencao", f"{livre_mb} MB livres na pasta de dados"
    return "ok", f"{livre_mb} MB livres"


def _trilha() -> tuple[str, str]:
    v = verificar_trilha()
    if v["quebra"] is not None:
        return "falha", f"a trilha de eventos não confere a partir do evento {v['quebra']}"
    return "ok", f"{v['eventos']} evento(s) conferidos"


def verificar(completo: bool = False) -> dict:
    """completo: também integrity_check do SQLite e a trilha de eventos
    (leem o banco inteiro; para o cli.py, não para o monitor)."""
    verificacoes = [
        ("banco", lambda: _banco(completo)),
        ("banco_na_pasta", _banco_na_pasta),
        ("cofre", _cofre),
        ("chave_lgpd", _chave_lgpd),
        ("worker", _worker),
        ("aprovacoes_incertas", lambda: _contagem(
            "SELECT COUNT(*) FROM aprovacoes WHERE status = 'incerta'",
            "aprovação(ões) incerta(s)", "confira e use cli.py resolver")),
        ("alertas", lambda: _contagem(
            "SELECT COUNT(*) FROM alertas WHERE resolvido_em IS NULL",
            "alerta(s) aberto(s)", "veja /api/alertas")),
        ("disco", _disco),
        ("backup", _backup),
    ]
    if completo:
        verificacoes.append(("trilha", _trilha))
    itens = []
    for nome, func in verificacoes:
        try:
            nivel, detalhe = func()
        except Exception as e:  # uma verificação quebrada não esconde as outras
            nivel, detalhe = "falha", f"a verificação falhou ({type(e).__name__})"
        itens.append({"nome": nome, "nivel": nivel, "detalhe": detalhe})
    return {"status": _pior(i["nivel"] for i in itens), "itens": itens, "verificado_em": agora()}
