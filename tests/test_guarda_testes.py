"""Lote de estrutura E9: a guarda dos testes cobre o que a trava por teste não
cobria (achado S-82 da auditoria): UDP, DNS reverso, a coleta e os
subprocessos, que também recusam a pasta de dados do repositório. Os
endereços de fora são do bloco de documentação (TEST-NET-1)."""
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import guarda_rede

RAIZ = Path(__file__).resolve().parent.parent
INSTALADA_NA_COLETA = guarda_rede.INSTALADA  # lido na importação do módulo, na coleta


def test_guarda_ja_vale_na_coleta():
    assert INSTALADA_NA_COLETA is True


def test_udp_para_fora_e_barrado():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(PermissionError, match="guarda dos testes: socket.sendto"):
            s.sendto(b"x", ("192.0.2.1", 9))


def test_dns_reverso_e_getnameinfo_sao_barrados():
    with pytest.raises(PermissionError, match="socket.gethostbyaddr"):
        socket.gethostbyaddr("192.0.2.1")
    with pytest.raises(PermissionError, match="socket.getnameinfo"):
        socket.getnameinfo(("192.0.2.1", 80), 0)


def test_loopback_continua_livre():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as recebe, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as envia:
        recebe.bind(("127.0.0.1", 0))
        recebe.settimeout(5)
        envia.sendto(b"ok", recebe.getsockname())
        assert recebe.recv(10) == b"ok"


def filho(codigo, **ambiente):
    env = {**os.environ, **ambiente}
    for nome, valor in ambiente.items():
        if valor is None:
            env.pop(nome)
    return subprocess.run([sys.executable, "-c", codigo], cwd=RAIZ, env=env,
                          capture_output=True, text=True, timeout=60)


def test_subprocesso_herda_a_guarda_de_rede(tmp_path):
    r = filho("import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM)"
              ".sendto(b'x', ('192.0.2.1', 9))", AGENTE_DADOS=str(tmp_path))
    assert r.returncode != 0 and "guarda dos testes: socket.sendto" in r.stderr


def test_subprocesso_com_pasta_temporaria_roda(tmp_path):
    r = filho("print('rodou')", AGENTE_DADOS=str(tmp_path))
    assert r.returncode == 0 and r.stdout.strip() == "rodou", r.stderr


@pytest.mark.parametrize("pasta", [None, "", "raiz", "dentro"])
def test_subprocesso_recusa_pasta_de_dados_do_repositorio(pasta):
    valor = {"raiz": str(RAIZ), "dentro": str(RAIZ / "qualquer")}.get(pasta, pasta)
    r = filho("print('rodou')", AGENTE_DADOS=valor)
    assert r.returncode != 0 and "rodou" not in r.stdout
    assert "guarda dos testes" in r.stderr
