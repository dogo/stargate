# Findings inheritance across runs

Ideação. Nenhum código neste documento.

## O problema

Hoje `APPROVED` com findings é um beco sem saída: os findings ficam em `review-N.md`
e em `state.json` do run que os gerou, mas não existem para nenhum run futuro. Isso é
aceitável quando um run é isolado, e é um problema quando você está construindo sobre
trabalho anterior com `--base-ref stargate/<branch-anterior>`. O reviewer do segundo
run parte de uma árvore limpa, sem memória do que o primeiro achou.

Isso só é tratável com dado estruturado. Findings em prosa não são extraíveis de forma
confiável; a V1 do output estruturado (já implementada) é o que torna esse plano
possível.

## A ideia

Quando `--base-ref` aponta para uma branch `stargate/*`, o orchestrator lê os findings
do **último review completo** do run que gerou aquela branch e os injeta no prompt do
architect como **dívida conhecida**. O architect planeja sabendo o que o reviewer anterior
viu e não foi corrigido.

"Último review completo" não é a mesma coisa que "o estado daquela branch", e a diferença
importa: se o run 1 parou por token budget **depois** de um fixer editar a árvore, os
findings gravados descrevem a árvore de antes daquela edição. O architect pode receber
dívida que a edição não revisada já resolveu. Não é motivo para bloquear a feature — é
motivo para o prompt dizer de qual review os findings vieram, em vez de apresentá-los
como o estado atual do código.

```text
run 1:  stargate run "Add pagination"
        → branch: stargate/add-pagination-20260910-120000
        → findings: [{ severity: medium, finding: "N+1 query on /users" }]
        → verdict: APPROVED   ← findings sobrevivem no state.json do run

run 2:  stargate run --base-ref stargate/add-pagination-20260910-120000 "Add export"
        → architect recebe: findings não corrigidos do run 1
        → pode planejar a correção, ou documentar a decisão de não corrigir agora
```

## Por que não é uma flag de opt-in

A tentação é `--inherit-findings`. O problema: o valor da feature é que **a cadeia não
esquece**. Se o comportamento é opt-in, a cadeia esquece sempre que você esquecer a
flag. A memória automática é a feature; memória opcional é outro recurso diferente.

O critério de ativação natural é o próprio `--base-ref`. Se você está apontando para
uma branch Stargate anterior, você está continuando trabalho — herdar a dívida é o
comportamento correto. Se não está, você está começando do zero — sem dívida faz sentido.

O caso residual onde isso incomoda: você usa `--base-ref stargate/...` para continuar,
mas os findings do run anterior foram uma decisão consciente de não corrigir (dívida
aceita, não esquecida). Aí os findings viram ruído no prompt.

Para esse caso, uma **flag de escape** faz mais sentido do que opt-in:
`--no-inherit-findings`. O comportamento padrão é herdar; a flag desliga explicitamente
quando você sabe que não quer.

## O que "herdar" significa concretamente

Não é repassar todos os findings cegamente. O que faz sentido:

1. Ler `state.json` do run que gerou o base-ref — o campo `findings` já existe lá. Duas
   propriedades dele que a implementação não pode presumir: ele guarda **apenas o último
   review completo** (uma passada posterior substitui a anterior), e lista vazia é gravada
   como `null`, não `[]`, seguindo a convenção das chaves vizinhas `fanout` e `review`.
2. Filtrar findings do run anterior que não têm correspondência resolvida na árvore
   atual (ou, numa versão mais simples, passar todos e deixar o architect julgar).
3. Injetar no prompt do architect como uma seção `## Known unresolved findings` antes
   de `{task}`.

A versão simples (passar todos, deixar o architect julgar) evita a heurística de
"finding foi corrigido?", que é complexa e falível. O architect é o lugar certo para
esse julgamento — ele já lê o repositório e tem contexto.

## Como localizar o run a partir do base-ref

**Não extraindo nada do nome da branch.** A ideia de "remover o prefixo `stargate/`" não
funciona: `run.py:150-151` monta os dois nomes em ordens diferentes —
`run_id = <timestamp>-<slug>` e `branch = stargate/<slug>-<timestamp>`. Exemplo real:

```text
run id : 20260911-202237-audit-v1
branch : stargate/audit-v1-20260911-202237
```

O timestamp é sufixo na branch e prefixo no run id. Com `--name` e o discriminador de
colisão (`-2`, `-3`) a inversão fica ainda menos reconstruível.

O caminho correto é o mais preguiçoso: varrer `.stargate/runs/*/state.json` e achar aquele
cujo campo `branch` é **igual** ao base-ref resolvido. É comparação exata em vez de
reconstrução, usa dado que já está gravado, e `stargate list` já faz exatamente essa
varredura — nenhuma lógica nova de nomes.

Se o base-ref não é uma branch `stargate/*`, se nenhum `state.json` tem aquela branch, ou
se o encontrado não tem `findings` (inclusive `null`), a herança silenciosamente não
acontece — sem erro, sem ruído. O run segue normal.

## Relação com `stargate findings` (item 2 da ideação original)

Um comando `stargate findings` que mostra dívida viva ao longo de uma cadeia de runs
depende desta feature existir e estar acumulando dados. Sem herança, ele mostra
arquivo morto por run. Com herança, ele pode mostrar quais findings sobreviveram a N
runs sem correção — dívida que o reviewer viu múltiplas vezes e que ninguém resolveu.

`stargate findings` é uma feature de segunda camada. Não faz sentido planejar sem
evidência de que o mecanismo de herança existe e está sendo usado.

## O que este plano não decide

- **Formato da injeção no prompt do architect.** É uma seção de texto ou um bloco JSON?
  O architect hoje recebe prosa; injetar JSON estruturado pode ajudar ou confundir
  dependendo do modelo. Isso é decisão de implementação.

- **Herança em fan-out.** Um run fan-out com `--base-ref` em uma branch Stargate anterior
  herda findings para todos os nós ou só para o primeiro? A resposta provavelmente é
  "para o architect, que distribui conforme o plano", mas precisa ser pensado junto com
  a implementação de fan-out.

- **Profundidade da cadeia.** Herdar apenas do run imediatamente anterior, ou acumular
  findings de toda a cadeia? Acumular pode criar ruído crescente; herdar só o imediato
  é mais conservador. Decisão de implementação.

- **`--no-inherit-findings` é necessário agora?** Pode ser adicionado na mesma
  implementação ou depois, dependendo de quanto ruído a herança automática gera na
  prática. Deixar de fora no primeiro corte e adicionar se alguém reclamar é uma
  opção válida.

## Relação com a V2 (política por severidade)

**Não depende dela, e deveria vir antes dela.**

Nada aqui consulta severity para decidir coisa alguma: ela viaja como dado dentro do texto
injetado, e a versão simples ("passar todos, deixar o architect julgar") evita filtro de
propósito. Não existe um ponto onde `blocking_severities` seria lido.

Mas a ordem importa por um motivo menos óbvio: **esta feature muda o cálculo da V2.** O
argumento inteiro pró-política branda era "nit não devia queimar passe de fixer". Hoje o
custo de não bloquear é que o finding **desaparece**. Com herança, um `low` não corrigido
sobrevive como dívida que o próximo architect lê — não bloquear deixa de ser esquecer.

Isso corta nos dois sentidos: enfraquece o medo que sustentava um default estrito, e tira a
urgência do brando, porque o finding não se perde de qualquer jeito.

A consequência é sobre **evidência**. A amostra que decidiria a V2 precisa ser colhida num
mundo onde a herança já existe: a pergunta "bloquear nesse `low` era necessário?" tem
resposta diferente quando a alternativa é "ele é carregado adiante" versus "ele é
esquecido". Colher a amostra antes disso mediria o mundo errado. Ordem: herança → uso real
→ amostra → decidir a V2, se ainda fizer sentido.

## Pré-requisito confirmado

V1 de structured review output está implementada e os `findings` já são gravados em
`state.json`. Este plano depende disso e não precisa repetir aquele trabalho.
