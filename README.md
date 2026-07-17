# ag-rotear-modelo

Skill Codex para escolher, executar e validar rotas GPT-5.6 Luna/Terra/Sol com esforço `low`, `medium`, `high`, `xhigh`, `max` e `ultra`.

## O que inclui

- Roteamento adaptativo por fingerprint de tarefa, perfil, permissões, orçamento, evidência e fronteira de Pareto.
- Fallback de capacidade: erro estruturado “Selected model is at capacity” troca para outro modelo viável, sem repetir cegamente a mesma rota.
- Catálogo local de modelos e benchmarks externos/oficiais.
- Guardian de execução que protege input efêmero, remove plaintext antes do spawn e supervisiona sessões destacadas.
- Compatibilidade macOS/Linux/Windows: POSIX usa fd anônimo; Windows usa lock `msvcrt`, launcher Python, DACL protegida e snapshot temporário privado.
- Visual interativo em `assets/framework-visual.html`.
- Suíte de testes em `scripts/tests`.

## Instalação local

Copie este diretório para:

```bash
~/.codex/skills/ag-rotear-modelo
```

A skill fica disponível no próximo turno do Codex.

## Verificação

Use o Python do sistema que tenha `jsonschema` disponível:

```bash
/usr/bin/python3 -m unittest discover -s scripts/tests
/usr/bin/python3 scripts/validate-registry.py --skill-root .
```

A matriz oficial em `.github/workflows/test.yml` executa Python 3.9 e 3.13 em Linux, macOS e Windows.

Também é recomendado:

```bash
ruff check scripts
shellcheck scripts/run-route.sh
```

## Windows

No Windows, invoque os scripts Python diretamente:

```powershell
py scripts\router.py select --request request.json --workdir .
py scripts\guarded-run.py --workdir . --private-temp-root D:\ag-model-router-private --sandbox read-only --approval-policy never
```

O guardian escolhe o TEMP nesta ordem: `--private-temp-root`, `AG_MODEL_ROUTER_PRIVATE_TEMP_ROOT`, subdiretório dedicado por usuário no TEMP do sistema. O caminho deve ser absoluto, não pode ficar dentro de `--workdir` e não pode atravessar symlink, junction ou outro reparse point. No Windows, o guardian aplica uma DACL protegida ao diretório, permitindo acesso ao owner, `SYSTEM` e administradores; ele nunca restringe o diretório TEMP compartilhado em si.

Quando o TEMP do usuário está dentro de um workdir amplo, configure um diretório externo uma vez ou passe-o em cada execução:

```powershell
$env:AG_MODEL_ROUTER_PRIVATE_TEMP_ROOT = "D:\ag-model-router-private"
py scripts\guarded-run.py --workdir C:\Users\gustavo.fagundes --sandbox read-only --approval-policy never
```

O guardian publica o mesmo evento `input-ready`. Para cancelar uma preparação pelo PID anunciado:

```powershell
Stop-Process -Id <guardian_pid>
```

Diferença de segurança: POSIX usa `pass_fds` e request anônimo unlinkado; Windows não oferece essa primitive. A versão Windows remove os plaintexts originais antes do spawn, passa um snapshot temporário privado por `--request`, remove esse snapshot no `finally` e usa `MoveFileExW(..., MOVEFILE_WRITE_THROUGH)` nas publicações atômicas.
