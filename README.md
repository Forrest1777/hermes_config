# Hermes Config

Snapshot versionado da configuração estática do meu ambiente **Hermes Agent**, mantido para facilitar auditoria, manutenção, diagnóstico de problemas operacionais e análise assistida por IA.

> Este repositório **não é um backup completo do Hermes** e não contém o estado de execução do ambiente. O objetivo é preservar apenas os arquivos que explicam **como o ambiente está configurado e como seus agentes se comportam**.

## Objetivo

Este repositório existe para fornecer uma visão limpa e versionada dos componentes que influenciam o funcionamento do ambiente Hermes, incluindo:

- configuração do Docker Compose;
- configuração global do Hermes;
- `SOUL.md` e configuração dos profiles;
- skills disponibilizadas ao ambiente;
- plugins customizados e operacionais;
- políticas de governança;
- scripts de auditoria, manutenção e recuperação;
- hardening e correções operacionais aplicadas ao ambiente.

Isso permite comparar versões do ambiente, investigar regressões e fornecer contexto suficiente para ferramentas como GPT analisarem a configuração sem precisar acessar logs, bancos de dados ou outros arquivos de runtime.

## Estrutura

```text
.
├── compose/
│   ├── docker-compose.yml
│   ├── hermes-config-preflight.py
│   └── 99-hermes-path.sh
│
├── hermes-data/
│   ├── SOUL.md
│   ├── config.yaml
│   ├── gitconfig
│   │
│   ├── profiles/
│   │   ├── implementation-architect/
│   │   ├── implementation-orchestrator/
│   │   ├── implementation-worker/
│   │   └── execution-governor/
│   │
│   ├── skills/
│   ├── plugins/
│   ├── operational-hardening/
│   ├── scripts/
│   └── bin/
│
├── TOOLs/
│
├── backup-hermes.bat
├── backup-kanban.bat
└── restore-kanban.bat
```

### `compose/`

Define a infraestrutura Docker utilizada pelo Hermes.

Inclui:

- imagem e comando de inicialização;
- portas do Gateway e Dashboard;
- bind mounts do ambiente;
- volume persistente do Kanban;
- mapeamento centralizado dos plugins para os profiles;
- variáveis de ambiente esperadas;
- preflight de validação do `config.yaml`.

Os valores secretos utilizados pelo Compose devem ser fornecidos externamente por variáveis de ambiente ou `.env` local não versionado.

### `hermes-data/`

Representa a parte estática principal do ambiente Hermes.

#### `config.yaml`

Configuração global do Hermes, incluindo modelo/provider, limites do agente, toolsets, plugins, LSP, Kanban, compressão, cache e demais opções operacionais.

Arquivos versionados neste repositório **não devem conter credenciais reais**.

#### `profiles/`

Profiles especializados utilizados no fluxo de desenvolvimento:

- **implementation-architect** — atividades de arquitetura e contratos;
- **implementation-orchestrator** — decomposição, coordenação e integração do trabalho;
- **implementation-worker** — implementação e validações direcionadas;
- **execution-governor** — governança e recuperação operacional da execução.

Para cada profile são preservados principalmente:

- `SOUL.md`;
- `config.yaml`;
- `profile.yaml`, quando aplicável;
- `.bundled_manifest` das skills;
- políticas ou scripts específicos do profile.

As cópias completas das skills instaladas dentro de cada profile não são versionadas para evitar duplicação.

#### `skills/`

Fonte canônica das skills disponíveis no ambiente.

Para skills genéricas, o repositório mantém principalmente o `SKILL.md`, que representa o contrato comportamental relevante para inspeção.

Implementações auxiliares são mantidas apenas quando fazem parte diretamente do fluxo operacional customizado do ambiente, como algumas skills de compressão ou preflight de worktree.

#### `plugins/`

Fonte canônica dos plugins customizados do Hermes.

Os plugins são centralizados nesta pasta e montados nos profiles pelo Docker Compose. Isso evita manter cópias independentes em cada profile.

Esta pasta é importante para diagnosticar comportamento operacional porque contém a implementação real de componentes como governança, integração Godot/LSP, execução de testes, recuperação e gerenciamento de worktrees.

#### `operational-hardening/`

Correções, patchers, documentação, manifests e verificadores relacionados ao hardening operacional do ambiente.

Serve como registro das adaptações feitas sobre o Hermes para tornar execução, recuperação e integração mais seguras e previsíveis.

#### `scripts/`

Scripts estáticos utilizados por integrações ou componentes auxiliares do ambiente.

#### `bin/`

Somente pequenos wrappers/scripts relevantes são versionados. Binários instalados e links gerados pelo ambiente são ignorados.

### `TOOLs/`

Ferramentas locais de apoio ao ambiente, principalmente auditoria e manutenção.

Exemplos:

- coleta de logs para auditoria;
- configuração central de caminhos;
- limpeza controlada de logs.

### Scripts legados no root

Os scripts:

```text
backup-hermes.bat
backup-kanban.bat
restore-kanban.bat
```

são mantidos por valor histórico/operacional e podem ser revisados ou modernizados futuramente.

## O que não é versionado

O `.gitignore` adota uma estratégia de **allowlist**: arquivos são ignorados por padrão e somente conteúdo estático relevante é liberado.

Entre os itens intencionalmente excluídos estão:

- logs;
- sessões;
- bancos SQLite e arquivos `WAL/SHM`;
- estado do Kanban;
- estado do Execution Governor;
- caches;
- `.venv`, `site-packages`, `node_modules` e dependências instaladas;
- arquivos temporários;
- archives e bundles gerados;
- binários grandes;
- workspaces e worktrees de projetos;
- backups do ambiente;
- credenciais e arquivos `.env`;
- links/binários Linux que não representam configuração do ambiente.

Diretórios locais como estes existem no ambiente real, mas não fazem parte do snapshot:

```text
backups/
logs/
teste/
workspace/
```

## Segurança

**Nunca versionar credenciais reais neste repositório.**

Antes de qualquer commit ou push, revisar especialmente:

- `hermes-data/config.yaml`;
- `hermes-data/profiles/*/config.yaml`;
- arquivos de Compose;
- scripts que definem variáveis de ambiente;
- novos plugins e integrações.

Credenciais devem ser fornecidas por mecanismos externos ao repositório, como variáveis de ambiente, `.env` ignorado ou outro mecanismo de secrets compatível com o componente consumidor.

O `.gitignore` ajuda a evitar arquivos conhecidos contendo segredos, mas ele **não detecta valores secretos escritos dentro de arquivos YAML, Python, JavaScript ou scripts que precisam ser versionados**.

## Atualização do snapshot

Quando a configuração do ambiente mudar:

1. atualizar os arquivos reais do ambiente Hermes;
2. revisar `git status`;
3. confirmar que nenhum arquivo de runtime ou segredo novo entrou na seleção;
4. revisar o diff;
5. atualizar este repositório com um commit descritivo.

Mudanças que normalmente merecem atualização neste repositório:

- alteração em `SOUL.md`;
- alteração de configuração global ou de profile;
- instalação/remoção/alteração de plugin;
- atualização relevante de skills;
- alteração no Compose;
- nova política de governança;
- mudança em scripts operacionais;
- aplicação ou revisão de hardening.

## Uso como contexto para IA

Ao usar este repositório para diagnosticar o ambiente, uma ordem de leitura útil é:

1. `compose/docker-compose.yml` — topologia e montagem do ambiente;
2. `hermes-data/config.yaml` — configuração global;
3. `hermes-data/profiles/*/SOUL.md` e `config.yaml` — comportamento por profile;
4. `hermes-data/plugins/` — extensões e guardrails operacionais;
5. `hermes-data/skills/` — capacidades/instruções disponíveis;
6. `hermes-data/operational-hardening/` — correções locais sobre o ambiente;
7. `TOOLs/` e scripts — processos de auditoria e manutenção.

Para incidentes específicos, logs e bancos de runtime podem ser fornecidos separadamente quando necessário. Eles não pertencem a este repositório.

## Ambiente local

O snapshot corresponde a um ambiente executado em Windows com Docker Compose, usando a árvore local conceitual:

```text
E:\dev\ai_agents\hermes\
├── compose/
├── hermes-data/
├── logs/          # não versionado
├── workspace/     # não versionado
├── backups/       # não versionado
├── TOOLs/
└── scripts .bat
```

Os caminhos absolutos presentes em alguns arquivos refletem esse ambiente local e fazem parte da configuração observada. Para portar o ambiente para outra máquina, esses caminhos precisam ser adaptados.

---

Este repositório deve ser tratado como uma **fotografia versionada da configuração e do comportamento operacional do Hermes**, e não como uma distribuição independente do Hermes Agent.
