"""
Guarda dos testes que vale para o processo inteiro (achado S-82 da auditoria).

O conftest instala esta guarda na importação, antes da coleta, e o
sitecustomize.py ao lado instala a mesma em todo Python filho que um teste
abre (a pasta está no PYTHONPATH dos testes). Ela usa um gancho de auditoria
do Python (sys.addaudithook), que não pode ser desfeito e vê o que a trava
por teste do conftest não vê:

  - conexão, envio UDP (sendto, sendmsg), resolução de nome e DNS reverso
    para fora do loopback levantam PermissionError antes de sair qualquer
    pacote;
  - vale também na coleta (código no topo dos módulos de teste) e nos
    subprocessos.

Não importa nada do projeto.
"""
import ipaddress
import sys

INSTALADA = False

_EVENTOS_DE_DESTINO = {"socket.connect", "socket.sendto", "socket.sendmsg"}
_EVENTOS_DE_NOME = {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                    "socket.gethostbyaddr", "socket.getnameinfo"}


def loopback(host) -> bool:
    if host in (None, "", b"", "localhost", b"localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _vigiar(evento: str, argumentos: tuple) -> None:
    if evento in _EVENTOS_DE_DESTINO:
        destino = argumentos[1] if len(argumentos) > 1 else None
        if isinstance(destino, tuple) and destino and not loopback(destino[0]):
            raise PermissionError(f"guarda dos testes: {evento} para {destino[0]!r} bloqueado")
    elif evento in _EVENTOS_DE_NOME:
        alvo = argumentos[0] if argumentos else None
        if evento == "socket.getnameinfo" and isinstance(alvo, tuple):
            alvo = alvo[0]
        if not loopback(alvo):
            raise PermissionError(f"guarda dos testes: {evento} de {alvo!r} bloqueado")


def instalar() -> None:
    global INSTALADA
    if not INSTALADA:
        sys.addaudithook(_vigiar)
        INSTALADA = True
