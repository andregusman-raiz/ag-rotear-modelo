---
name: ag-rotear-modelo
description: Use when Codex must choose a GPT-5.6 model and reasoning effort for a non-trivial task and the user has not fixed the route.
---

# Roteador adaptativo de modelo

## Guard first

Se `AG_MODEL_ROUTER_CHILD=1` ou o texto contiver `[AG_MODEL_ROUTER_CHILD=1]`, não chame o roteador; execute diretamente na rota já recebida.

## Quando rotear

Roteie tarefas não triviais somente quando modelo e esforço não tiverem sido definidos explicitamente.

## Fingerprint

Produza o JSON completo de `task-request-schema.json`. Infira os campos sem interromper quando o contexto for suficiente.

## Permissões

Copie o sandbox e a approval policy observados. Se algum deles não for conhecido, pare como bloqueado.

## Executar

Use estas operações concretas do Codex:

1. Resolva os executáveis absolutos `${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/guarded-run.py`, `${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/publish-ready.py`, `${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/codex-child-supervisor.py`, `${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/run-route.py` e `${CODEX_HOME:-$HOME/.codex}/skills/ag-rotear-modelo/scripts/run-route.sh`; não dependa do diretório de trabalho atual. No Windows, prefira o launcher Python `run-route.py`; no POSIX, `run-route.sh` continua suportado.
2. Com `exec_command`, sem terminal interativo, inicie o guardian com `--workdir` seguido do working directory absoluto observado, `--sandbox` seguido do sandbox observado, `--approval-policy` seguido da policy observada e `--prepare-timeout 60`. O processo permanece ativo, devolve um `session id` e publica em stdout um evento JSON `input-ready` com `guardian_pid`, `ready_nonce` e os caminhos absolutos `input_dir`, `request_path`, `task_path` e `ready_path`. Esse é o diretório temporário privado fora do repositório, criado pelo guardian com modo `0700`.
3. Confirme que `guardian_pid` é um inteiro decimal positivo; que `ready_nonce` tem exatamente 64 caracteres hexadecimais minúsculos; que os quatro caminhos do evento são absolutos; que `request_path`, `task_path` e `ready_path` pertencem exatamente ao `input_dir` anunciado; e que seus nomes são, respectivamente, `request.json`, `task.txt` e `READY`. Em cancelamento ou divergência após um PID válido, use `exec_command` para encerrar o guardian: POSIX usa `/bin/kill -TERM <guardian_pid>`; Windows usa `powershell -NoProfile -Command "Stop-Process -Id <guardian_pid>"`. Faça `poll` do mesmo `session id` até o processo terminar e confirmar o cleanup. Se nem o evento nem o PID forem válidos, não sinalize outro processo: faça `poll` até o timeout de preparação limitado encerrar e limpar o guardian.
4. Com `apply_patch`, serialize o JSON completo do fingerprint em `request.json` e o texto integral da tarefa, byte a byte, em `task.txt`, usando somente os caminhos anunciados. Com `exec_command`, aplique modo `0600` aos dois arquivos. Não coloque o conteúdo da tarefa em variável de ambiente, argv, stdout ou interpolação de shell.
5. Publique `READY` por último usando `exec_command` para executar o publisher absoluto com `--input-dir` seguido do `input_dir` validado e `--nonce` seguido do `ready_nonce` validado. O helper calcula tamanho e SHA-256 dos dois inputs e publica atomicamente um manifesto JSON canônico `READY-last`; não construa esse manifesto manualmente. O guardian rejeita nonce, canonicalização, digest, tamanho, arquivos extras, links, modos ou owners incorretos, inputs vazios e limites excedidos.
6. Faça `poll` do processo usando o `session id` existente até ele terminar; não envie conteúdo adicional à sessão. O guardian abre request e task sem seguir links quando a plataforma oferece esse recurso, confere ambos contra o manifesto e cria snapshots imutáveis limitados. Antes do spawn, remove os plaintexts originais. No POSIX, entrega a task por `stdin=PIPE` com EOF e o request por descritor anônimo interno `--request-fd`, sem reabrir path compartilhado. No Windows, onde `pass_fds` não existe, entrega a task por `stdin=PIPE` e o request por um snapshot temporário privado `--request`, removido no `finally`; o launcher propaga o exit status do filho.
7. O guardian é o único owner do ciclo de vida: instala handlers antes de criar o diretório, aplica timeout de preparação, encerra o grupo de processos filho em interrupção e também alcança sessões de executor registradas pelo `codex-child-supervisor.py`, preserva qualquer path substituto e remove inputs/diretório por identidade em sucesso, erro, timeout ou `TERM`/`INT`/`HUP`. Não prometa nem faça cleanup manual no agente pai.

Não execute a tarefa novamente no agente pai.

## Entregar

Devolva o artefato final, a evidência de validação e a linha `Rota:`. Expanda as três escolhas e IDs de evidência somente em modo auditável.

## Referências

Leia `references/architecture.md`; carregue somente o perfil e o schema necessários para a tarefa atual. Para explicar o framework visualmente, use `assets/framework-visual.html`.
