# ag-rotear-modelo

Skill Codex para escolher, executar e validar rotas GPT-5.6 Luna/Terra/Sol com esforço `low`, `medium`, `high`, `xhigh`, `max` e `ultra`.

## O que inclui

- Roteamento adaptativo por fingerprint de tarefa, perfil, permissões, orçamento, evidência e fronteira de Pareto.
- Catálogo local de modelos e benchmarks externos/oficiais.
- Guardian de execução que protege input efêmero, remove plaintext antes do spawn e supervisiona sessões destacadas.
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

Também é recomendado:

```bash
ruff check scripts
shellcheck scripts/run-route.sh
```
