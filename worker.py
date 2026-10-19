"""
Worker: o laço principal do robô.

Roda a cada N minutos (INTERVALO_WORKER) e executa, em ordem:

  1. Ingestão        — puxa pedidos novos dos marketplaces
  2. Análise         — calcula margem real; recusa o que não fecha
  3. Roteamento      — monta a ordem de compra, passa pela conformidade e põe
                       na fila de aprovação. Toda compra espera o seu OK; o
                       teto (TETO_COMPRA_AUTOMATICA) só muda o rótulo da fila,
                       ROTINA ou ACIMA DO TETO
  4. Atendimento     — redige respostas às perguntas abertas que o bot ainda
                       não tratou (pra fila); cada pergunta vai ao modelo uma
                       vez, salvo falha temporária
  5. Rastreio        — atualiza status de quem já está em trânsito

Nenhuma etapa gasta dinheiro ou publica texto. Publicar resposta e mudar
preço só acontecem depois que você aprova na fila, e só com MODO_SIMULACAO
desligado. A compra aprovada vira um arquivo de ordem de compra: quem envia
ao fornecedor, e paga, é você. Aprovação em simulação não consome o item:
a compra e a resposta voltam para a fila quando MODO_SIMULACAO for
desligado. É isso que mantém o robô seguro rodando desatendido.

Um ciclo de cada vez: com outro em andamento, neste processo ou em outro,
a passada é pulada (ciclo).

Onde fica cada etapa: 1, 4 e 5 em tarefas/marketplace.py; 2 e 3, com a
reanálise, em tarefas/compras.py. Aqui ficam o laço, a trava do ciclo, os
executores das aprovações e as etapas curtas (expurgo, Amazon, alertas).
"""
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config import config, DATA_DIR, avisar_pasta_de_dados
from db import conectar, agora, inicializar, registrar_evento
from core import aprovacao, cofre, dinheiro, privacidade
# A mesma trava de arquivo que o cofre usa para a renovação de tokens.
from core.cofre import _soltar_arquivo, _trancar
from core.erros import pilha_sem_mensagem
from core.estados import Estado, transicionar
from conectores.mercadolivre import MercadoLivre

from tarefas.compras import (  # noqa: F401 (reexportados: cli, painel, demo e testes)
    ESTADOS_DEPOIS_DA_COMPRA, REANALISE_PEDIDA, _compra_que_impede_reanalise,
    analisar_novos, montar_ordens_de_compra, reanalisar,
)
from tarefas.marketplace import atender_perguntas, atualizar_rastreio, ingerir_mercadolivre

PASTA_ORDENS = DATA_DIR / "ordens_de_compra"


# ------------------------------------------------------------------- Executores

AVISO_SIMULACAO_OC = ("SIMULAÇÃO (MODO_SIMULACAO=true): ordem de teste. "
                      "Não envie ao fornecedor.\n\n")


def _fornecedor_da_ordem(payload: dict) -> tuple:
    """(nome, canal, contato) do fornecedor da ordem, como estão no cadastro
    agora, achado pelo id gravado na fila: editar o nome, o canal ou o contato
    do mesmo fornecedor depois de a ordem entrar na fila não deixa o arquivo
    nem a instrução com os dados velhos (a conformidade também olha o canal
    de agora). Fila de versão anterior, sem o id, ou fornecedor que não
    existe mais: os dados copiados na ordem."""
    copiado = (payload.get("fornecedor"), payload.get("canal"), payload.get("contato"))
    fornecedor_id = payload.get("fornecedor_id")
    if not isinstance(fornecedor_id, int) or isinstance(fornecedor_id, bool):
        return copiado
    with conectar() as conn:
        atual = conn.execute("SELECT nome, canal, contato FROM fornecedores WHERE id = ?",
                             (fornecedor_id,)).fetchone()
    return copiado if atual is None else (atual["nome"], atual["canal"], atual["contato"])


def executor_compra(payload: dict) -> str:
    """
    Executa a compra depois de aprovada: grava a ordem de compra em
    ordens_de_compra/oc_<pedido>.txt, na pasta de dados. O programa NÃO envia
    nada ao fornecedor: a ordem sai do arquivo pelas suas mãos, pelo canal
    cadastrado no fornecedor. O nome, o canal e o contato são os do cadastro
    na hora da aprovação (_fornecedor_da_ordem).

    A maioria das fábricas não tem API. O caminho realista para automatizar
    depois é mandar o pedido pelo canal que o fornecedor usa (e-mail por SMTP,
    WhatsApp Business API); nada disso existe ainda.

    Com MODO_SIMULACAO ligado, o arquivo sai marcado como teste e o resultado
    leva [SIMULAÇÃO]. O pedido NÃO anda: nada foi comprado, então ele fica em
    AGUARDANDO_APROVACAO (um estado que pede atenção) e volta para a fila
    quando MODO_SIMULACAO for desligado (montar_ordens_de_compra). A ordem de
    verdade, gravada depois, substitui o arquivo de teste. Sem simulação, o
    pedido vai para COMPRA_ENVIADA.
    """
    simulacao = config.modo_simulacao
    # Fila antiga traz o valor como número e sem moeda; a nova, texto e moeda.
    valor = dinheiro.formatar(dinheiro.do_real(payload["valor"]),
                              dinheiro.moeda_lida(payload.get("moeda")))
    nome, canal, contato = _fornecedor_da_ordem(payload)
    canal = canal or "canal não cadastrado"
    texto = (
        f"PEDIDO DE COMPRA\n"
        f"Fornecedor: {nome}\n"
        f"Canal: {canal}; contato: {contato or 'não cadastrado'}\n"
        f"SKU: {payload['sku']} — {payload['produto']}\n"
        f"Quantidade: {payload['quantidade']}\n"
        f"Valor: {valor}\n"
        f"Referência: {payload['marketplace']}#{payload['pedido_externo']}\n"
    )
    if simulacao:
        texto = AVISO_SIMULACAO_OC + texto
    PASTA_ORDENS.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA_ORDENS / f"oc_{payload['pedido_id']}.txt"
    arquivo.write_text(texto, encoding="utf-8")

    if simulacao:
        registrar_evento("info", "worker",
                         f"Pedido {payload['pedido_id']}: compra simulada ({arquivo.name}). "
                         "O pedido continua em AGUARDANDO_APROVACAO e volta para a fila "
                         "quando MODO_SIMULACAO for desligado.")
        return aprovacao.simular(
            f"Ordem de compra gravada em {arquivo}, marcada como teste. Nada foi enviado "
            "ao fornecedor. O pedido continua esperando a compra de verdade: volta para a "
            "fila quando MODO_SIMULACAO for desligado.")

    transicionar(payload["pedido_id"], Estado.COMPRA_ENVIADA,
                 f"Ordem gerada em {arquivo.name}; o envio ao fornecedor é manual",
                 automatico=False)
    return (f"Ordem de compra gravada em {arquivo}. Envie ao fornecedor "
            f"{nome} pelo canal {canal}: o programa não envia.")


def executor_resposta(payload: dict) -> str:
    if config.modo_simulacao:
        return aprovacao.simular(f"Resposta à pergunta {payload['question_id']} não "
                                 "publicada: nada foi enviado ao marketplace.")
    ml = MercadoLivre()
    ml.responder_pergunta(payload["question_id"], payload["resposta"])
    return f"Resposta publicada na pergunta {payload['question_id']}"


def executor_preco(payload: dict) -> str:
    if config.modo_simulacao:
        return aprovacao.simular(f"Preço do {payload['item_id']} seria R$ "
                                 f"{payload['preco']:.2f}; nada foi enviado ao marketplace.")
    ml = MercadoLivre()
    ml.atualizar_preco(payload["item_id"], payload["preco"])
    return f"Preço do {payload['item_id']} atualizado para R$ {payload['preco']:.2f}"


EXECUTORES = {
    "compra_fornecedor": executor_compra,
    "resposta_cliente": executor_resposta,
    "ajuste_preco": executor_preco,
}


# ------------------------------------------------------------------ Laço

_pilha = pilha_sem_mensagem


# ------------------------------------------------------- Um ciclo de cada vez
#
# Duas passadas ao mesmo tempo (a tecla 'c' do painel durante o ciclo do laço
# de fundo do executar.py, a tecla apertada duas vezes, ou um `cli.py ciclo`
# ao lado do painel) leem o mesmo estado antes de qualquer uma gravar: a
# mesma pergunta iria duas vezes ao modelo (duas chamadas pagas) e a mesma
# compra entraria duas vezes na fila. Entre threads deste processo vale um
# Lock; entre processos, o primeiro byte de um arquivo vazio ao lado do banco
# (.trava_ciclo), que o sistema solta sozinho se o processo morrer. Quem chega
# com um ciclo em andamento não espera: pula a passada.

CICLO_EM_ANDAMENTO = ("Já há um ciclo em andamento (neste processo ou em outro, como um "
                      "cli.py rodar ao lado); esta passada foi pulada.")

_trava_ciclo = threading.Lock()


def _arquivo_trava_ciclo() -> Path:
    """Ao lado do banco, que é o que as passadas disputam."""
    return Path(config.db_path).resolve().parent / ".trava_ciclo"


def _travar_arquivo_do_ciclo() -> int | None:
    """O descritor com a trava na mão, ou None se outro processo está no ciclo."""
    fd = os.open(_arquivo_trava_ciclo(), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
                 0o600)
    try:
        _trancar(fd)
    except OSError:
        os.close(fd)
        return None
    return fd


@contextmanager
def _um_ciclo_por_vez():
    """Entrega True com a vez de rodar, ou False se outro ciclo está em andamento."""
    if not _trava_ciclo.acquire(blocking=False):
        yield False
        return
    fd = None
    try:
        fd = _travar_arquivo_do_ciclo()
        yield fd is not None
    finally:
        if fd is not None:
            _soltar_arquivo(fd)
        _trava_ciclo.release()


def ciclo() -> dict:
    """Uma passada completa. Cada etapa é isolada: falha numa não para as outras.

    Com outro ciclo em andamento, neste processo ou em outro, não roda nada e
    devolve {"pulado": CICLO_EM_ANDAMENTO}."""
    with _um_ciclo_por_vez() as vez:
        if not vez:
            return {"pulado": CICLO_EM_ANDAMENTO}
        return _passada()


def expurgo_diario():
    """Retenção de dados pessoais (RETENCAO_PII_DIAS) uma vez por dia (UTC).
    Antes, o expurgo só rodava se alguém clicasse no painel."""
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with conectar() as conn:
        linha = conn.execute("SELECT valor FROM estado_sistema WHERE chave = 'expurgo_ultimo'"
                             ).fetchone()
    if linha is not None and linha[0] == hoje:
        return "já rodou hoje"
    expurgados = privacidade.expurgar()
    with conectar() as conn:
        conn.execute("INSERT INTO estado_sistema (chave, valor, atualizado_em) VALUES"
                     " ('expurgo_ultimo', ?, ?) ON CONFLICT(chave) DO UPDATE SET"
                     " valor = excluded.valor, atualizado_em = excluded.atualizado_em",
                     (hoje, agora()))
    return expurgados


def sincronizar_amazon():
    """Pedidos da Amazon, só leitura (core/sincronizacao.py). Sem credenciais
    da Amazon, a etapa não roda e diz por quê. Leitura acontece mesmo com a
    simulação ligada: ler não muda nada na Amazon."""
    from core import sincronizacao
    try:
        configurada = config.amazon.configurado
    except cofre.ErroCofre:
        configurada = False
    if not configurada:
        return "não configurada"
    from conectores.amazon import Amazon
    amz = Amazon()  # um token LWA para as três leituras
    return {"pedidos": sincronizacao.sincronizar_amazon(amz),
            "anuncios": sincronizacao.sincronizar_anuncios(amz),
            "estoque_fba": sincronizacao.sincronizar_estoque_fba(amz)}


def avaliar_alertas():
    """Abre, atualiza e fecha alertas (core/alertas.py). Roda sempre: um
    alerta de sincronização com falha precisa aparecer justo quando a
    sincronização não anda."""
    from core import alertas
    return alertas.avaliar()


def _passada() -> dict:
    """As cinco etapas, com a vez de rodar já garantida por ciclo()."""
    inicializar()
    privacidade.criar_tabelas_lgpd()  # o rastreio registra o acesso em acessos_pii
    resumo = {}
    ml = MercadoLivre()

    for nome, func in [
        ("interrompidas", aprovacao.recuperar_interrompidas),
        ("expurgo", expurgo_diario),
        ("ingeridos", lambda: ingerir_mercadolivre(ml)),
        ("analisados", analisar_novos),
        ("ordens_montadas", montar_ordens_de_compra),
        ("perguntas", lambda: atender_perguntas(ml)),
        ("rastreios", lambda: atualizar_rastreio(ml)),
        ("amazon", sincronizar_amazon),
        ("alertas", avaliar_alertas),
    ]:
        try:
            resumo[nome] = func()
        except Exception as e:
            # Só o tipo e onde falhou, nunca a mensagem: a de uma exceção
            # inesperada pode repetir um valor decifrado do cofre.
            resumo[nome] = f"erro: {type(e).__name__}"
            registrar_evento("erro", "worker", f"Etapa '{nome}' falhou: {type(e).__name__}",
                             {"traceback": _pilha(e)})

    resumo["pendentes_aprovacao"] = len(aprovacao.pendentes())
    # Para a checagem de saúde (core/saude.py): quando terminou e quantas
    # etapas falharam.
    falhas = sum(1 for v in resumo.values() if isinstance(v, str) and v.startswith("erro:"))
    with conectar() as conn:
        conn.execute("INSERT INTO estado_sistema (chave, valor, atualizado_em)"
                     " VALUES ('ciclo_fim', ?, ?) ON CONFLICT(chave) DO UPDATE SET"
                     " valor = excluded.valor, atualizado_em = excluded.atualizado_em",
                     (str(falhas), agora()))
    return resumo


def rodar(intervalo_seg: int | None = None):
    """Laço contínuo. Sem intervalo, vale INTERVALO_WORKER (padrão 300 s)."""
    intervalo_seg = intervalo_seg or config.intervalo_worker
    modo = " em modo simulação" if config.modo_simulacao else ""
    registrar_evento("info", "worker", f"Worker iniciado{modo}, ciclo a cada {intervalo_seg}s")
    while True:
        r = ciclo()
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {r}")
        time.sleep(intervalo_seg)


def principal():
    """`python worker.py`: sem AGENTE_DADOS, diz qual pasta de dados vai ser
    usada antes da primeira gravação, e roda o laço."""
    avisar_pasta_de_dados()
    rodar()


if __name__ == "__main__":
    principal()
