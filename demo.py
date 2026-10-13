"""Popula o banco com dados de exemplo e roda o fluxo sem tocar em API nenhuma."""
import sys; sys.path.insert(0, '.')
from config import config
from db import inicializar, conectar, agora
from core.estados import Estado, historico
from core import aprovacao, dinheiro
from worker import analisar_novos, montar_ordens_de_compra, EXECUTORES

inicializar()

# Trava (achado S-81 da auditoria de 06/10/2026): o demo apaga pedidos,
# produtos, fornecedores e a fila. Só faz isso num banco vazio ou que só tem
# os próprios dados de exemplo; com qualquer outro dado, recusa, a menos que
# a pessoa peça com --apagar-dados-existentes.
SKUS_DEMO = ("ORG-001", "LUM-114", "SUP-300")
PEDIDOS_DEMO = ("2000000001", "2000000002", "2000000003", "2000000004")


def _dados_que_nao_sao_do_demo(c) -> dict:
    marcas = lambda n: ",".join("?" * n)
    return {k: v for k, v in {
        "pedidos": c.execute(f"SELECT COUNT(*) FROM pedidos WHERE NOT (marketplace = 'mercadolivre'"
                             f" AND id_externo IN ({marcas(len(PEDIDOS_DEMO))}))",
                             PEDIDOS_DEMO).fetchone()[0],
        "produtos": c.execute(f"SELECT COUNT(*) FROM produtos WHERE sku NOT IN ({marcas(len(SKUS_DEMO))})",
                              SKUS_DEMO).fetchone()[0],
        "fornecedores": c.execute("SELECT COUNT(*) FROM fornecedores WHERE NOT (id IN (1, 2)"
                                  " AND nome IN ('Fábrica Aurora', 'Metalpar'))").fetchone()[0],
        "itens da fila que não são compra": c.execute(
            "SELECT COUNT(*) FROM aprovacoes WHERE tipo != 'compra_fornecedor'").fetchone()[0],
    }.items() if v}


with conectar() as c:
    alheios = _dados_que_nao_sao_do_demo(c)
if alheios and "--apagar-dados-existentes" not in sys.argv[1:]:
    print("O exemplo não foi carregado: este banco já tem dados que não são do demo ("
          + ", ".join(f"{n} {k}" for k, n in alheios.items()) + ").")
    print("O demo apagaria tudo isso. Para apagar mesmo assim, rode:"
          " python demo.py --apagar-dados-existentes")
    sys.exit(2)

with conectar() as c:
    c.execute("DELETE FROM transicoes"); c.execute("DELETE FROM aprovacoes")
    c.execute("DELETE FROM pedidos"); c.execute("DELETE FROM produtos"); c.execute("DELETE FROM fornecedores")
    c.execute("INSERT INTO fornecedores (id,nome,canal,contato,prazo_dias,pedido_minimo,pedido_minimo_dec,pedido_minimo_moeda,criado_em) VALUES (1,'Fábrica Aurora','email','pedidos@fabrica-aurora.example',4,0,'0.00','BRL',?)",(agora(),))
    c.execute("INSERT INTO fornecedores (id,nome,canal,contato,prazo_dias,pedido_minimo,pedido_minimo_dec,pedido_minimo_moeda,criado_em) VALUES (2,'Metalpar','whatsapp','+55 00 00000-0000',12,0,'0.00','BRL',?)",(agora(),))
    # Produtos sintéticos já com a confirmação que a conformidade exige:
    # nenhum deles é de categoria regulada. Valores com moeda explícita (BRL):
    # sem ela, o worker não calcula margem.
    c.execute("INSERT INTO produtos (id,sku,titulo,custo_fornecedor,custo_fornecedor_dec,custo_fornecedor_moeda,peso_kg,fornecedor_id,categoria_regulada,criado_em) VALUES (1,'ORG-001','Organizador de gaveta 6 divisórias',18.50,'18.50','BRL',0.4,1,'nenhuma',?)",(agora(),))
    c.execute("INSERT INTO produtos (id,sku,titulo,custo_fornecedor,custo_fornecedor_dec,custo_fornecedor_moeda,peso_kg,fornecedor_id,categoria_regulada,criado_em) VALUES (2,'LUM-114','Luminária de mesa articulada',62.00,'62.00','BRL',1.2,1,'nenhuma',?)",(agora(),))
    c.execute("INSERT INTO produtos (id,sku,titulo,custo_fornecedor,custo_fornecedor_dec,custo_fornecedor_moeda,peso_kg,fornecedor_id,categoria_regulada,criado_em) VALUES (3,'SUP-300','Suporte de monitor em aço',95.00,'95.00','BRL',3.0,2,'nenhuma',?)",(agora(),))
    # 4 pedidos: margem boa / margem ruim / venda maior (custo de R$ 62, abaixo
    # do teto: o rótulo compara o custo da compra) / fornecedor lento
    for eid, pid, val in [('2000000001',1,'54.90'), ('2000000002',1,'26.00'), ('2000000003',2,'289.00'), ('2000000004',3,'198.00')]:
        c.execute("INSERT INTO pedidos (marketplace,id_externo,produto_id,quantidade,valor_bruto,valor_bruto_dec,valor_bruto_moeda,estado,comprador_nome,endereco_json,criado_em,atualizado_em) VALUES ('mercadolivre',?,?,1,CAST(? AS REAL),?,'BRL',?,'Comprador Teste','{}',?,?)",
                  (eid,pid,val,val,Estado.NOVO.value,agora(),agora()))

if config.modo_simulacao:
    print("Modo simulação ligado (MODO_SIMULACAO=true): a ordem aprovada sai marcada como "
          "teste e o pedido continua em AGUARDANDO_APROVACAO.\n")

print("=== 1. ANÁLISE DE MARGEM ===")
print(f"  {analisar_novos()} pedido(s) aprovados na margem\n")
with conectar() as c:
    for p in c.execute("SELECT id_externo,valor_bruto,valor_bruto_dec,valor_bruto_moeda,margem_prevista,estado FROM pedidos ORDER BY id"):
        m = f"{p['margem_prevista']}%" if p['margem_prevista'] is not None else "—"
        # O valor em Decimal, com a moeda gravada (dinheiro.formatar: "R$ 54.90").
        venda = dinheiro.formatar(dinheiro.ler(p['valor_bruto_dec'], p['valor_bruto']),
                                  dinheiro.moeda_lida(p['valor_bruto_moeda']))
        print(f"  {p['id_externo']}  {venda:>10}  margem {m:>8}  →  {p['estado']}")

print("\n=== 2. MONTAGEM DAS ORDENS DE COMPRA ===")
print(f"  {montar_ordens_de_compra()} ordem(ns) na fila\n")
for a in aprovacao.pendentes():
    print(f"  [{a.id}] {a.resumo}")
    checagem = aprovacao.checar(a)
    for v in checagem.bloqueios:
        print(f"        bloqueada — {v.regra}: {v.mensagem}\n        saída: {v.saida}")

print("\n=== 3. APROVANDO A PRIMEIRA (simula seu 'pode comprar') ===")
ids = [a.id for a in aprovacao.pendentes()]
if ids:
    try:
        print("  " + aprovacao.aprovar(ids[0], EXECUTORES))
    except aprovacao.BloqueadoPelaConformidade as e:
        print(f"  Não executada. {e}")
        for v in e.resultado.bloqueios:
            print(f"  Saída ({v.regra}): {v.saida}")
    else:
        with conectar() as c:
            pid = c.execute("SELECT pedido_id FROM aprovacoes WHERE id=?", (ids[0],)).fetchone()[0]
        print("\n  Histórico do pedido:")
        for h in historico(pid):
            auto = "auto" if h['automatico'] else "VOCÊ"
            print(f"    {h['de']} → {h['para']:<22} [{auto}] {h['motivo']}")
