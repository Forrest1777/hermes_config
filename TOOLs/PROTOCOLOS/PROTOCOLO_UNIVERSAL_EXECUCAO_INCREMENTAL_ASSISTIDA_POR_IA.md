# Protocolo Universal de Execução Incremental Assistida por IA

## Visão geral

Este documento descreve uma técnica geral para executar trabalhos técnicos com auxílio de uma IA conversacional em qualquer ambiente: desenvolvimento de software, infraestrutura, dados, automação, configuração de serviços, migração, testes, manutenção, documentação, operações, análise de incidentes ou tarefas híbridas.

O protocolo é **agnóstico de sistema, linguagem, sistema operacional, ferramenta, repositório ou plataforma**.

Ele não pressupõe:

- Git;
- PowerShell;
- Bash;
- Docker;
- CI/CD;
- cloud;
- banco de dados;
- uma linguagem de programação específica;
- uma arquitetura específica;
- uma ferramenta de IA específica.

Quando alguma dessas ferramentas existir, ela pode ser integrada ao protocolo. Quando não existir, os mesmos princípios continuam válidos.

A ideia central é transformar a interação com a IA em um processo de execução disciplinado:

```text
entender o estado
→ definir o contrato
→ automatizar uma etapa
→ executar
→ coletar evidência
→ registrar checkpoint
→ continuar do ponto exato
→ validar
→ fechar a etapa
→ publicar/versionar quando aplicável
```

O maior ganho não vem de “gerar comandos mais rápido”.

O maior ganho vem de **evitar repetir trabalho já comprovado** e de transformar cada falha em uma continuação localizada, em vez de reiniciar todo o processo.

---

# 1. Princípio central

A unidade de trabalho não deve ser um comando isolado.

A unidade de trabalho deve ser uma **etapa verificável de transformação de estado**.

Uma etapa possui:

```text
estado inicial conhecido
+
ação controlada
+
critérios de sucesso
+
evidência
+
estado final conhecido
```

A IA deve trabalhar sobre esse ciclo.

O usuário, operador ou sistema externo mantém a autoridade final sobre:

- execução;
- credenciais;
- produção;
- publicação;
- ações irreversíveis;
- acesso a sistemas externos;
- aprovação de resultados.

---

# 2. O problema que este protocolo resolve

Trabalhos técnicos assistidos por IA tendem a degradar quando ocorre um ou mais destes padrões:

- muitos comandos manuais em sequência;
- repetição de testes já aprovados;
- perda de contexto após uma falha;
- rollback desnecessário;
- alteração de código de produção para corrigir um problema de teste;
- mistura de diagnóstico, correção e publicação;
- ausência de registro do que já foi validado;
- mudanças fora do escopo original;
- confiança excessiva em mensagens como “comando terminou com sucesso”;
- reinício de toda a operação após qualquer erro;
- processos longos dirigidos por tentativa e erro.

O protocolo substitui isso por:

```text
estado explícito
+
scripts/ações idempotentes
+
checkpoints
+
evidência
+
continuação incremental
```

---

# 3. Conceitos fundamentais

## 3.1. Baseline

Baseline é o estado inicial que foi observado e aceito antes da alteração.

Pode incluir:

- versão atual;
- arquivos existentes;
- configuração;
- status de serviços;
- estado de banco;
- contagem de testes;
- métricas;
- hash;
- snapshot;
- versão de API;
- comportamento funcional;
- inventário de recursos.

Exemplo genérico:

```text
versão: 3.4.2
testes: 812
falhas conhecidas: 2
serviço: healthy
configuração: válida
```

O baseline não precisa ser perfeito.

Ele precisa ser **conhecido**.

---

## 3.2. Invariante

Invariante é algo que deve permanecer verdadeiro durante ou após a mudança.

Exemplos:

```text
nenhum dado deve ser perdido
o número de testes não pode diminuir
o serviço deve continuar autenticando
a API pública não pode mudar
o schema deve continuar compatível
a latência não pode piorar acima do limite acordado
```

Invariantes transformam “parece funcionar” em validação objetiva.

---

## 3.3. Checkpoint

Checkpoint é um registro persistente de que determinada condição já foi comprovada.

Exemplo abstrato:

```json
{
  "stage": "VALIDATED",
  "migration_applied": true,
  "focused_tests": true,
  "full_suite": false,
  "restart_required": true
}
```

O formato não importa.

Pode ser:

- JSON;
- YAML;
- arquivo texto;
- registro em banco;
- comentário em ticket;
- tag;
- metadata;
- documento;
- estado interno de uma ferramenta.

O que importa é que ele permita responder:

```text
onde estamos?
o que já foi comprovado?
o que ainda falta?
```

---

## 3.4. Evidência

Evidência é qualquer artefato verificável que sustenta um checkpoint.

Exemplos:

- log;
- relatório;
- diff;
- resultado de teste;
- checksum;
- screenshot;
- resposta de API;
- dump;
- query;
- arquivo gerado;
- métrica;
- evento;
- estado persistido.

Regra:

```text
checkpoint sem evidência é opinião
```

---

# 4. A principal lição aprendida

A técnica mais importante é:

## Não reiniciar o trabalho inteiro após uma falha.

Quando uma operação falha, a primeira pergunta não deve ser:

```text
como tentamos tudo novamente?
```

Deve ser:

```text
qual foi exatamente o último estado válido?
```

Depois:

```text
o que já foi persistido?
o que já foi validado?
o que a falha realmente invalida?
qual é a menor continuação segura?
```

Essa mudança de mentalidade reduz drasticamente:

- tempo;
- custo;
- risco;
- ruído;
- consumo computacional;
- consumo de contexto da IA.

---

# 5. Automação por etapas

Quando um trabalho exigir várias ações, preferir uma unidade automatizada.

Essa unidade pode ser:

- script;
- notebook;
- workflow;
- playbook;
- job;
- migration;
- task runner;
- programa temporário;
- ferramenta interna;
- sequência estruturada executada por agente.

A tecnologia é secundária.

O contrato é:

```text
uma execução
→ várias ações determinísticas
→ validação integrada
→ checkpoint
```

---

# 6. Um comando ou ação principal por rodada

Sempre que possível, a interação entre IA e operador deve ser simples.

Exemplo conceitual:

### IA

```text
Execute o pacote de continuação X.
```

### Operador

```text
<resultado>
```

### IA

```text
Classificação:
falha de ambiente.

Próxima continuação:
Y.
```

Isso é mais robusto do que transformar a conversa em uma longa lista de comandos independentes.

---

# 7. Scripts e automações devem se autovalidar

Uma automação bem construída deve verificar suas próprias pré-condições.

Exemplos:

```text
local correto?
versão correta?
serviço esperado está ativo?
há operação concorrente?
há espaço suficiente?
credenciais existem?
baseline ainda é o mesmo?
estado parcial anterior é compatível?
```

Se uma pré-condição crítica não for satisfeita:

```text
parar
```

Não improvisar.

---

# 8. Fail-closed

Este protocolo prefere falha segura a adivinhação.

Se a automação espera:

```text
1 processo
```

e encontra:

```text
3 processos
```

não deve escolher um arbitrariamente.

Se espera:

```text
1 arquivo
```

e encontra:

```text
2 candidatos
```

não deve assumir.

Se espera:

```text
baseline A
```

e encontra:

```text
baseline B
```

deve parar.

Padrão:

```text
estado reconhecido → continua
estado já concluído → reutiliza
estado desconhecido → falha
```

---

# 9. Idempotência

Sempre que possível, executar novamente a mesma automação não deve causar dano.

Exemplo:

```text
se já está VALIDATED:
    não aplique novamente
    não repita testes caros
    continue da próxima etapa
```

Uma automação idempotente pode funcionar como uma pequena máquina de estado.

---

# 10. Scripts como máquinas de estado

Uma automação madura conhece seus estados.

Exemplo:

```text
NOT_STARTED
↓
PREPARED
↓
MATERIALIZED
↓
VALIDATED
↓
WAITING_EXTERNAL_ACTION
↓
ACTIVE
```

Na reexecução:

```text
ler estado
→ descobrir estágio
→ continuar
```

Isso elimina a suposição perigosa de que “toda execução começa do zero”.

---

# 11. Continuação após falha

Quando uma execução falha, o próximo artefato deve ser uma **continuação**, não uma nova implementação completa.

Fluxo:

```text
APPLY
↓
falha no passo 4
↓
CONTINUE_AFTER_<CAUSE>
```

A continuação deve primeiro verificar:

```text
passo 1 está aplicado?
passo 2 está aplicado?
passo 3 está aplicado?
passo 4 não foi aplicado ou ficou incompleto?
```

Somente depois continuar.

---

# 12. Nomear continuidades pela causa

Evitar nomes como:

```text
fix2
fix_final
fix_final_2
tentativa3
```

Preferir:

```text
CONTINUE_AFTER_SCHEMA_VALIDATION
CONTINUE_AFTER_PORT_CONFLICT
CONTINUE_AFTER_PERMISSION_FAILURE
CONTINUE_AFTER_RESTART
```

O nome já documenta o histórico operacional.

---

# 13. Preservar trabalho válido

Se uma execução fez:

```text
A
B
C
```

e falhou em:

```text
D
```

não desfazer A/B/C automaticamente.

Primeiro determinar:

```text
A é válido?
B é válido?
C é válido?
```

Se sim:

```text
A/B/C viram baseline da próxima continuação
```

Rollback deve ser uma decisão explícita, não uma reação automática.

---

# 14. Quando fazer rollback

Rollback é apropriado quando:

- o estado parcial é inconsistente;
- a alteração parcial coloca o sistema em risco;
- a operação deveria ser atômica;
- o estado intermediário impede continuação confiável;
- o contrato exige reversão;
- dados ou segurança podem ser comprometidos.

Não fazer rollback apenas porque “houve um erro depois”.

---

# 15. Classificar a falha antes de corrigir

Nem toda falha pertence ao produto ou sistema alvo.

Uma classificação útil:

## A. Falha da automação

Exemplos:

- erro de sintaxe;
- quoting incorreto;
- path escrito errado;
- parser da própria ferramenta.

## B. Falha do ambiente

Exemplos:

- permissão;
- porta indisponível;
- recurso ausente;
- variável de ambiente;
- versão externa diferente.

## C. Falha do harness ou validação

Exemplos:

- teste usa premissa inválida;
- fixture está errada;
- timeout inadequado;
- range fixo incompatível com o host.

## D. Falha de integração

Exemplos:

- dois componentes corretos isoladamente não interoperam;
- lifecycle incompleto;
- contrato divergente.

## E. Falha funcional real

Exemplos:

- comportamento incorreto;
- cálculo incorreto;
- perda de dados;
- violação de regra de negócio.

Regra:

```text
não modificar lógica funcional para corrigir uma falha de harness
```

---

# 16. Validação proporcional ao impacto

Nem toda alteração exige o maior teste disponível.

Estratégia:

```text
teste focal
→ teste do componente
→ teste de integração
→ suíte completa
```

Subir de nível somente quando necessário.

Isso reduz tempo e custo.

---

# 17. Gates já aprovados são ativos reutilizáveis

Se uma etapa provou:

```text
componente A funciona
```

e a próxima alteração afeta apenas:

```text
componente B
```

não repetir automaticamente A.

Um gate só perde validade se uma alteração posterior puder afetá-lo.

Essa regra é essencial.

---

# 18. Composição de evidências

Em processos longos, a prova final pode ser composta.

Exemplo:

```text
Etapa 1 prova compatibilidade
Etapa 2 prova concorrência
Etapa 3 prova restart
Etapa 4 prova recuperação
```

Não é necessário refazer tudo na Etapa 4.

A evidência final é:

```text
E1 + E2 + E3 + E4
```

Isso transforma um roadmap em uma cadeia acumulativa de confiança.

---

# 19. Separar mudança estrutural de mudança funcional

Quando possível, não misturar:

```text
refatoração
+
nova funcionalidade
+
migração
+
mudança de comportamento
```

Exemplo melhor:

```text
Etapa A: reorganizar estrutura sem mudar comportamento
Etapa B: validar invariantes
Etapa C: alterar comportamento
```

Isso reduz drasticamente a área de diagnóstico.

---

# 20. Restringir o escopo de mudança

Antes da alteração, definir:

```text
o que pode mudar?
```

Pode ser:

- lista de arquivos;
- tabelas;
- recursos;
- endpoints;
- módulos;
- configurações;
- serviços.

Depois da execução, comparar:

```text
esperado
versus
observado
```

Qualquer drift deve ser investigado.

---

# 21. Controle de versão é opcional, mas disciplina de publicação não

Se houver versionamento, usar.

Se não houver, substituir por:

- snapshots;
- backups;
- hashes;
- export;
- cópia imutável;
- release artifact.

O princípio geral é:

```text
execução
≠
validação
≠
publicação
```

Nunca misturar essas três fases sem necessidade.

---

# 22. Publicar apenas após estado validado

Fluxo recomendado:

```text
aplicar
→ validar
→ fechar checkpoint
→ revisar
→ publicar/versionar
```

Não:

```text
aplicar
→ publicar
→ descobrir depois se funcionou
```

---

# 23. Ações irreversíveis devem permanecer explícitas

Operações irreversíveis ou de alto impacto não devem ficar escondidas dentro de uma automação genérica.

Exemplos:

- apagar dados;
- publicar em produção;
- revogar credenciais;
- remover infraestrutura;
- migrar dados sem rollback;
- enviar artefatos externos;
- executar pagamentos;
- trocar DNS;
- alterar permissões críticas.

Essas ações devem exigir:

```text
checkpoint anterior
+
aprovação explícita
```

---

# 24. Reinícios e ações externas como fronteiras de fase

Algumas ações são naturalmente externas ao script.

Exemplos:

- restart manual;
- login;
- MFA;
- aprovação humana;
- deploy manual;
- troca física;
- mudança de rede;
- reboot.

Padrão:

```text
script
→ PREPARED
→ EXTERNAL_ACTION_REQUIRED
→ operador executa ação
→ mesmo script novamente
→ valida resultado
→ continua
```

Isso preserva o checkpoint e evita repetição.

---

# 25. Evidência compacta no chat, detalhada em arquivo

O chat deve receber:

```text
[OK] etapa A
[OK] etapa B
[FAIL] etapa C
```

O detalhe deve ficar em:

- arquivo de log;
- relatório;
- JSON;
- trace;
- artifact.

Assim:

```text
chat = controle
arquivo = auditoria
```

---

# 26. Não confiar apenas em exit code

Um processo pode terminar com sucesso e ainda produzir estado incorreto.

Verificar também:

- contagens;
- conteúdo;
- invariantes;
- estado final;
- ausência de drift;
- saúde;
- consistência.

Exemplo:

```text
exit code = 0
```

não é suficiente se:

```text
faltam 20 registros
```

---

# 27. Dívida preexistente

Um sistema pode possuir problemas conhecidos fora do escopo atual.

Esses problemas podem ser aceitos como baseline.

Exemplo:

```text
warnings conhecidos = 12
```

A regra pode ser:

```text
warnings novos <= 12
```

Assim o roadmap atual não precisa resolver tudo, mas também não pode piorar silenciosamente o sistema.

---

# 28. Critério de sucesso deve ser definido antes

Antes de executar, escrever:

```text
o que significa sucesso?
```

Exemplo:

```text
dados migrados
zero perda
API responde
latência abaixo de X
backup validado
serviço reinicia
teste integrado passa
```

Não decidir o critério depois de ver o resultado.

---

# 29. Economia de contexto da IA

A IA não deve redescobrir tudo a cada rodada.

Usar:

- snapshots;
- checkpoints;
- relatórios;
- markers;
- resumos estruturados;
- nomes de etapas.

Evitar:

- reenviar logs enormes sem necessidade;
- reexplicar decisões consolidadas;
- executar novamente discovery completo.

---

# 30. Economia computacional

O mesmo princípio vale para recursos reais.

Evitar:

- suites completas desnecessárias;
- rebuilds;
- downloads repetidos;
- scans completos;
- polling;
- reinicialização de ambientes;
- recriação de fixtures.

Preferir:

```text
incremental
determinístico
orientado a mudança
```

---

# 31. Eventos são melhores que polling quando disponíveis

Para operações longas:

```text
iniciar
→ aguardar evento
→ retomar
```

é preferível a:

```text
consultar
esperar
consultar
esperar
consultar
```

Isso vale para:

- jobs;
- testes;
- filas;
- workflows;
- deploys;
- processamento batch.

---

# 32. Concorrência deve ser tratada como parte do contrato

Se há múltiplas execuções simultâneas, validar:

- isolamento;
- locks;
- recursos exclusivos;
- nomes;
- portas;
- arquivos temporários;
- estados compartilhados;
- teardown.

Não assumir que “funciona com uma instância” significa que “funciona com várias”.

---

# 33. Teardown é parte do teste

Um teste E2E não termina quando a função principal passa.

Também deve validar:

```text
processos encerrados
arquivos temporários removidos
locks liberados
conexões fechadas
recursos liberados
estado reutilizável
```

O cleanup é parte do comportamento.

---

# 34. Observabilidade é um gate

Uma operação pode funcionar e ainda ser impossível de manter.

Verificar:

- logs;
- métricas;
- status;
- IDs;
- timestamps;
- erro legível;
- correlação;
- capacidade de diagnóstico.

Sistema sem observabilidade suficiente deve ser tratado como parcialmente validado.

---

# 35. Segurança contra mudanças inesperadas

Após automação:

```text
inventariar diferenças
```

Se o ambiente suportar diff:

```text
diff
```

Caso contrário:

```text
hash
snapshot
inventário
export
```

Pergunta obrigatória:

```text
mudou apenas o que deveria mudar?
```

---

# 36. A técnica é transacional, não necessariamente atômica

O protocolo não exige que tudo seja uma única transação real.

Ele cria uma transação lógica:

```text
estado inicial
→ alteração
→ checkpoint
→ validação
→ commit lógico
```

Cada checkpoint funciona como um ponto seguro de retomada.

---

# 37. Processo universal recomendado

## Fase 1 — Descoberta

Antes de alterar:

- entender o objetivo;
- observar o estado atual;
- localizar componentes envolvidos;
- identificar dependências;
- identificar risco;
- coletar baseline.

Saída:

```text
BASELINE_KNOWN
```

---

## Fase 2 — Contrato

Definir:

```text
objetivo
escopo
invariantes
paths/recursos permitidos
gates
evidências
estado final
```

Saída:

```text
CONTRACT_DEFINED
```

---

## Fase 3 — Automação

Criar uma unidade executável.

Ela deve:

- validar precondições;
- aplicar mudança;
- registrar estado;
- validar resultados;
- parar em fronteiras externas.

Saída:

```text
AUTOMATION_READY
```

---

## Fase 4 — Execução

O operador executa.

Saída possível:

```text
PASS
FAIL
EXTERNAL_ACTION_REQUIRED
```

---

## Fase 5 — Classificação

Se falhar:

```text
automação?
ambiente?
harness?
integração?
funcional?
```

Nunca corrigir antes de classificar.

---

## Fase 6 — Continuação

Gerar a menor continuação possível.

Saída:

```text
CONTINUE_AFTER_<CAUSE>
```

---

## Fase 7 — Validação progressiva

Executar:

```text
focused
→ component
→ integration
→ full
```

conforme impacto.

---

## Fase 8 — Fechamento

Produzir:

- checkpoint;
- relatório;
- inventário de mudanças;
- estado final.

Saída:

```text
LIVE_ACTIVE
```

ou equivalente.

---

## Fase 9 — Publicação

Somente após fechamento:

- commit;
- release;
- deploy;
- upload;
- publicação;
- documentação oficial.

---

## Fase 10 — Verificação pós-publicação

Confirmar que o estado publicado corresponde ao validado.

---

# 38. Estrutura recomendada de uma automação

Estrutura conceitual:

```text
HEADER

CONFIGURAÇÃO / PATHS / RECURSOS

BASELINES

HELPERS

PREFLIGHT

CHECKPOINT LOAD

MUTATION

FOCUSED VALIDATION

EXPENSIVE VALIDATION

EVIDENCE

CHECKPOINT WRITE

FINAL MARKER
```

---

# 39. Formato genérico de checkpoint

Exemplo:

```json
{
  "stage": "VALIDATED",
  "baseline_id": "...",
  "started_at": "...",
  "updated_at": "...",
  "changes_applied": true,
  "focused_validation": true,
  "integration_validation": true,
  "full_validation": false,
  "external_action_required": true,
  "publication_performed": false
}
```

Adaptar conforme o sistema.

---

# 40. Marcadores finais

Cada execução importante deve terminar com um marcador inequívoco.

Exemplos:

```text
STEP_COMPLETE
VALIDATION_COMPLETE
RESTART_REQUIRED
APPROVAL_REQUIRED
LIVE_ACTIVE
```

Evitar mensagens vagas como:

```text
parece tudo certo
```

---

# 41. Padrão de comunicação IA ↔ operador

## IA

```text
Diagnóstico:
<curto>

Artefato:
<arquivo/workflow/script>

Execute:
<uma ação principal>
```

## Operador

```text
<output>
```

## IA

```text
Classificação:
<tipo>

Estado preservado:
<checkpoints>

Próxima ação:
<continuação>
```

---

# 42. Lições aprendidas

## Lição 1 — Continuidade vale mais que reinício

O maior ganho de produtividade surgiu quando cada falha virou uma continuação localizada.

---

## Lição 2 — Checkpoint é memória operacional

Sem checkpoint, a IA precisa reconstruir o passado.

Com checkpoint, ela só precisa resolver o próximo delta.

---

## Lição 3 — Teste caro deve virar ativo reutilizável

Um teste E2E aprovado possui valor futuro.

Não o desperdiçar repetindo sem motivo.

---

## Lição 4 — Falhas precisam ser classificadas

Grande parte do tempo desperdiçado em debugging vem de corrigir a camada errada.

---

## Lição 5 — Mudanças pequenas e isoladas são mais rápidas de validar

Reforma estrutural e alteração funcional devem ser separadas sempre que possível.

---

## Lição 6 — O estado real vence a intenção

Não importa o que o script pretendia fazer.

Importa o que realmente foi persistido.

Toda continuação deve partir do estado observado.

---

## Lição 7 — A automação deve carregar contexto operacional

Um bom script sabe:

```text
onde estou
o que já passou
o que posso mudar
o que falta provar
```

---

## Lição 8 — O operador continua sendo a autoridade

A IA deve automatizar trabalho, não remover governança.

---

## Lição 9 — Evidência reduz discussão

Um relatório ou checkpoint claro elimina muitas rodadas de “será que passou?”.

---

## Lição 10 — Velocidade vem da redução de repetição

A automação ajuda.

Mas a maior aceleração vem de:

```text
não refazer discovery
não rerodar gate
não reaplicar mudança
não rediagnosticar estado conhecido
```

---

# 43. Anti-padrões

## Reiniciar tudo após qualquer erro

Quase sempre desperdiça informação operacional.

---

## Corrigir a camada errada

Exemplo:

```text
falha de teste
→ alterar produto
```

sem prova.

---

## Publicar antes de validar

Mistura duas fases que deveriam ser independentes.

---

## Executar ações irreversíveis escondidas

Reduz governança.

---

## Repetir suíte completa por hábito

Usar validação proporcional.

---

## Ignorar drift

Toda mudança inesperada precisa ser explicada.

---

## Usar polling quando há eventos

Consome recursos e contexto.

---

## Fazer uma automação presumir demais

Automação boa valida.

Automação ruim adivinha.

---

## Misturar múltiplos objetivos

Quanto mais objetivos por etapa, mais difícil diagnosticar falhas.

---

# 44. Checklist universal

## Antes

```text
[ ] objetivo claro
[ ] baseline conhecido
[ ] escopo definido
[ ] invariantes definidos
[ ] recursos permitidos definidos
[ ] riscos identificados
[ ] critério de sucesso definido
```

## Durante

```text
[ ] automação autocontida
[ ] fail-closed
[ ] checkpoint persistente
[ ] evidência gerada
[ ] mudanças restritas ao escopo
[ ] gates proporcionais ao impacto
```

## Se falhar

```text
[ ] identificar ponto exato da falha
[ ] observar estado real
[ ] classificar a falha
[ ] preservar trabalho válido
[ ] preservar gates válidos
[ ] gerar continuação mínima
```

## Ao concluir

```text
[ ] estado final explícito
[ ] invariantes preservados
[ ] teardown validado
[ ] drift verificado
[ ] relatório criado
[ ] publicação separada
```

---

# 45. Prompt universal reutilizável

O texto abaixo pode ser usado em qualquer novo chat.

---

## PROMPT — PROTOCOLO UNIVERSAL DE EXECUÇÃO INCREMENTAL

Quero que você conduza este trabalho usando execução incremental assistida por IA.

Siga estas regras:

1. Antes de alterar qualquer coisa, estabeleça o baseline real do sistema.
2. Defina objetivo, escopo, invariantes, riscos, gates e estado final esperado.
3. Quando houver múltiplas ações, prefira gerar uma automação autocontida em vez de uma longa lista de comandos manuais.
4. A automação deve validar suas próprias pré-condições.
5. Estados inesperados devem falhar fechado; não adivinhe.
6. Use checkpoints persistentes para registrar o que já foi comprovado.
7. Torne as automações idempotentes sempre que possível.
8. Se uma execução falhar parcialmente:
   - observe o estado real;
   - identifique o que já foi persistido;
   - preserve mudanças válidas;
   - preserve gates já aprovados;
   - classifique a falha;
   - gere a menor continuação segura a partir do ponto exato.
9. Não reinicie todo o processo automaticamente após uma falha.
10. Não repita testes, scans ou operações caras sem que uma alteração posterior tenha invalidado sua evidência.
11. Diferencie falha da automação, ambiente, harness, integração e funcionalidade antes de corrigir algo.
12. Não altere lógica funcional para mascarar uma falha de ambiente ou teste.
13. Use validação progressiva: focal → componente → integração → completa.
14. Trate teardown e liberação de recursos como parte da validação.
15. Verifique mudanças inesperadas após cada mutação.
16. Gere evidência objetiva e um relatório final.
17. Separe alteração, validação, ação externa, publicação e verificação pós-publicação.
18. Ações irreversíveis, publicação ou mudanças de alto impacto exigem aprovação explícita.
19. Quando uma ação externa for necessária, grave checkpoint, peça a ação e permita que a mesma automação continue depois.
20. Ao concluir uma etapa, informe:
    - o que foi comprovado;
    - quais invariantes foram preservados;
    - o que mudou;
    - qual debt conhecido permanece;
    - o que deve ser publicado/versionado, se aplicável;
    - qual é o próximo passo.
21. Nunca invente detalhes específicos do ambiente que ainda não tenham sido observados ou confirmados.
22. Prefira evidência real ao que a automação pretendia fazer.
23. Use este princípio como prioridade:
    **continuar do último estado válido é melhor do que recomeçar.**

Objetivo: maximizar velocidade, segurança, auditabilidade, reprodutibilidade e economia de trabalho.

---

# 46. Fórmula resumida

A técnica pode ser resumida assim:

```text
OBSERVAR
→ CONTRATAR
→ AUTOMATIZAR
→ EXECUTAR
→ PROVAR
→ CHECKPOINT
→ CONTINUAR
→ VALIDAR
→ FECHAR
→ PUBLICAR
```

Quando houver falha:

```text
FALHA
→ CLASSIFICAR
→ PRESERVAR
→ CONTINUAR DO ÚLTIMO ESTADO VÁLIDO
```

---

# 47. Nome da técnica

Nome recomendado:

## Execução Incremental Assistida por IA com Checkpoints

Forma curta:

## IA Incremental com Checkpoints

A técnica não depende de uma ferramenta específica.

Seu núcleo é:

```text
estado conhecido
+
automação controlada
+
evidência
+
checkpoints
+
continuação mínima
+
governança humana
```

Esse conjunto de práticas é aplicável a praticamente qualquer trabalho técnico assistido por IA.
