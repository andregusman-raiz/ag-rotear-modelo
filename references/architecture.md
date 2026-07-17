# Arquitetura do roteador adaptativo

## Fluxo

```text
TaskRequest + permissões
        |
        v
catálogo observado -> gates globais e por rota
        |                         |
        |                         +-> rotas eliminadas (códigos estruturais)
        v
evidência local > externa independente > agregador > fornecedor
        |
        v
fronteira de Pareto -> econômica | ideal | máxima segurança
        |
        v
execução -> validação -> observação local -> novo Pareto -> escalada
        |              |
        |              +-> verificador independente read-only quando exigido
        v
decision.json privado + linha de rota + artefato validado
```

O serviço é o único loop de orquestração. `select` calcula e registra as três escolhas sem executar um filho. `run` começa pela rota ideal, valida cada resultado e só tenta outra rota quando a classe de falha indica ganho plausível.

## Evidência e Pareto

A precedência é: coorte local comparável, benchmark externo exato, benchmark externo de domínio, agregador independente e prior oficial. Evidência retirada ou em quarentena não participa da seleção. Coortes só são comparadas quando projeto, perfil, versão, arquétipo, engine e versão observável do modelo forem compatíveis.

Os gates de autorização, orçamento, decomposição, isolamento de worktree e ferramentas são aplicados antes da avaliação. `apply_gates` recebe o catálogo observado; suporte de ferramenta desconhecido permanece evidência desconhecida, enquanto incompatibilidade conhecida elimina a rota.

A fronteira de Pareto considera qualidade, custo, latência e risco apenas dentro de partições comparáveis. A escolha econômica procura o menor custo que satisfaça o piso; a ideal minimiza arrependimento normalizado; a de máxima segurança prioriza evidência, qualidade e risco residual. Após cada observação local, o serviço executa novamente `assess_routes` e `select_routes` antes de escolher a escalada.

## Validação, escalada e recuperação

O progresso usa exclusivamente métricas declaradas em `Profile.progress_metrics` e presentes no `ChildReport`; o campo legado `passed_checks` não é aceito. Tentativas e tempo pertencem ao `BudgetLedger`. Tokens observados vêm de `ExecutionResult.usage` e são agregados separadamente.

`failure_kind` e exit code têm precedência sobre qualquer relatório anexado. `timeout` e `spawn` são transitórios e permitem no máximo uma repetição da mesma rota; `missing-agent-message` indica profundidade; evento Codex, processo genérico ou exit não zero bloqueiam conservadoramente como dependência externa. `ExecutionProtocolError` é sempre falha técnica terminal do executor.

O JSONL atual não oferece códigos causais estáveis para distinguir combinação modelo×esforço inválida, aprovação ou credencial sem examinar conteúdo privado. Portanto o roteador não infere causa por `stderr`; usa apenas `failure_kind` e exit code. Esse limite é deliberado e evita transformar segredo em telemetria.

Tarefas críticas com verificação fraca usam outro thread, rota de máxima segurança e sandbox read-only. IDs de execução iguais ou ausentes falham fechados. Em operações com mutação parcial, nenhuma nova execução mutável é permitida: o próximo passo é somente uma checagem de recuperação read-only, e qualquer resultado encerra o loop. Rollback mutável depende de autorização já existente.

## Runtime e privacidade

O runtime padrão é `~/.codex/model-router`, substituível por `AG_MODEL_ROUTER_RUNTIME_ROOT` ou `--runtime-root`. Snapshots ficam em `registry/`, decisões em `runs/<run_id>/decision.json` e observações em `telemetry/observations.jsonl`. Escritas são privadas, atômicas e protegidas por testemunhas duráveis de resultado de commit. Cada decisão recebe HMAC-SHA-256 com chave privada do runtime; o audit abre cada componente por descritor sem seguir symlinks, verifica a identidade do root, limita a leitura e compara a tag em tempo constante antes de revalidar o envelope terminal completo.

`decision.json` contém somente fingerprint estrutural, perfil, catálogo, rotas eliminadas, três escolhas, IDs de evidência, histórico, validação, orçamento e timestamps. Texto da tarefa, critérios de aceitação, deliverable, nomes de ferramentas eliminadas e thread IDs brutos não são persistidos. Valores abertos e IDs locais/de execução usam digests SHA-256 com separação de domínio. `audit` lê exclusivamente `RuntimeState.read_decision`, que bloqueia symlinks, JSON alterado e identificadores hostis e revalida todo o envelope.

Para observações locais, `route.model` é a versão de modelo observável disponível e `model_router.__version__` é a versão do engine. Isso não detecta revisões internas silenciosas publicadas sob o mesmo slug; mudanças não observáveis invalidam a força causal da comparação e devem ser tratadas como limitação da evidência.

## Guardian de inputs e lifecycle

O guardian publica um evento JSON flushed `input-ready` com PID real, paths absolutos controlados e `ready_nonce` aleatório de 256 bits. O diretório temporário fica fora do repositório em modo `0700`; `request.json` e `task.txt` são plaintext privado em modo `0600`. O agente escreve ambos antes de chamar `publish-ready.py`, que publica o marker `READY-last` também em `0600`. READY é um manifesto JSON canônico de até 512 bytes que vincula protocolo, nonce, tamanho e SHA-256 dos dois inputs. Request e task aceitam no máximo 1 MiB cada. Esses plaintexts existem somente para o transporte efêmero e não entram em `decision.json` nem em `telemetry`.

Depois de READY, o guardian opera somente sobre o `dirfd` original. Abre os três arquivos com `O_NOFOLLOW` e `O_NONBLOCK` quando disponíveis; valida owner, modo, tipo, tamanho, inode e timestamps; faz duas leituras limitadas idênticas de request e task; e compara tamanho e digest em tempo constante com o manifesto. Em seguida remove todos os plaintexts descriptor-relative antes do spawn. A task segue como snapshot imutável por `stdin=PIPE` com `communicate(input=...)`; o request segue por `TemporaryFile` anônimo, unlinkado e herdado somente via `pass_fds`/`--request-fd`. O child não recebe nem reabre o path compartilhado.

O timeout de preparação é monotônico e limitado. Handlers de `TERM`, `INT` e `HUP` são instalados antes da criação do diretório; o child inicia em grupo próprio, recebe `TERM` em interrupção e tem fallback limitado com `SIGKILL`. O cleanup lista e remove entradas não-diretório pelo descritor, nunca usa `rmtree`, e só aplica `rmdir` vazio quando `dev+ino` do nome ainda corresponde ao diretório criado. Se o nome for substituído, o replacement é preservado; um original movido pode restar vazio e a execução retorna falha de cleanup, mas os plaintexts já foram removidos.

Quando o executor real precisa iniciar outra sessão POSIX, `CodexExecutor` é embrulhado por `codex-child-supervisor.py`. O guardian passa um pipe privado via `AG_MODEL_ROUTER_CONTROL_FD`; o supervisor registra o grupo com `+PID` antes de spawnar o Codex real, remove esse env do filho, mantém stdin/stdout/stderr transparentes e deregistra com `-PID` no término normal. Em interrupção, o guardian drena esse protocolo limitado, envia `TERM` ao grupo do launcher e aos grupos registrados, e depois aplica `SIGKILL` como fallback. Assim um executor destacado não continua mutando depois que o guardian é cancelado.

O transporte interno por `pass_fds` é POSIX e falha fechado fora dessa plataforma. O nonce e o digest vinculam o READY aos bytes observados, mas não formam uma fronteira contra um adversário que execute sob o mesmo UID e altere payload e manifesto coerentemente. Há ainda uma janela stdlib inevitável entre `stat` e `rmdir`, limitada a remover somente um diretório vazio. Um `SIGKILL` contra o guardian ou power loss antes do unlink não pode executar `finally` e pode deixar plaintext temporário até limpeza externa ou uma política futura de recuperação; o sistema não promete cleanup impossível nesses eventos.

## Catálogo e atualização externa

O catálogo tenta descoberta live, cache local, catálogo bundled e seed, nessa ordem. O runner recebe argv como lista/tupla, captura texto, nunca usa shell e aplica timeout configurável por `AG_MODEL_ROUTER_CATALOG_TIMEOUT_SECONDS`; timeout cai para as fontes seguintes. Requests e decisões têm limite de 1 MiB, com aceitação no limite exato. Cada chamada pública do serviço inicia um ledger novo com os mesmos limites e clock configurados. A promoção do benchmark usa `promote-registry`, valida schema e referências antes de substituir o snapshot e preserva o último estado válido quando o commit não pode ser confirmado.

Atualizações de benchmark são externas ao loop: preparar um candidato, executar `validate-registry.py --skill-root`, revisar fontes/status/rotas e então promover. A skill nunca converte automaticamente telemetria local em benchmark compartilhado.

## Ativação implícita

`allow_implicit_invocation` e a descrição da skill dão alta probabilidade de ativação quando a tarefa é não trivial e a rota não foi fixada. Isso não intercepta de forma infalível todo entrypoint. O guard de recursão continua obrigatório no agente pai e nos filhos.
