# Interfaces verificadas em 07/11/2026

Mac local:

- Claude Code `2.1.319`, `~/.local/bin/claude`; login `claude.ai`, assinatura Pro.
- Codex CLI `0.166.0`, `/opt/homebrew/bin/codex`; login ChatGPT, plano Plus.
- Python `3.14.3`; ambiente próprio em `auto-ai-switch/.venv`; Git do macOS.

Na revalidação do console DevSwitch 1.2.0, em 07/11/2026, o Codex instalado já era `0.167.0`. `devswitch doctor --json --refresh` confirmou flags compatíveis, login ChatGPT e consulta oficial de cotas nessa versão; o teste real Claude → Codex com unittest também passou. O bundle de schemas arquivado registra a inspeção inicial de 0.166.0.

Foram consultados `--help`, `--version`, `claude auth status`, `codex login status`, `codex exec --help`, `codex app-server --help` e o gerador de schemas da instalação `codex app-server generate-json-schema --out ...`. O bundle local está em `docs/codex-schema/` (ignorado pelo Git; só documentação, não executado). Não foram lidos arquivos de tokens, cookies ou Chaveiro diretamente.

O sandbox da ferramenta de desenvolvimento inicialmente impediu acesso normal do Claude ao Chaveiro. A validação fora desse sandbox confirmou o login. Isso não exige copiar credenciais e não afeta o comando no terminal normal do usuário.

Fontes oficiais abertas e utilizadas:

- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference): print/stream-json, schemas, max-turns, modo restricted, ferramentas e permissões.
- [Claude programmatic execution](https://code.claude.com/docs/en/headless): stream de eventos, erros estruturados e `system/api_retry`.
- [Claude Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/typescript): `SDKRateLimitEvent`, `status`, `utilization` e `resetsAt` opcionais. A presença do tipo na referência não significa que toda execução envie percentual.
- [Claude model configuration](https://code.claude.com/docs/en/model-config): seleção explícita de modelo e `--effort`; o modelo resolvido é coletado de `system/init` quando informado.
- [Claude models overview](https://platform.claude.com/docs/en/models/overview): IDs explícitos verificados em 07/11/2026: `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5` e `claude-fable-5-1`. O menu distingue versões numeradas de aliases automáticos, sem consultar endpoints internos nem inferir acesso à conta.
- [Claude status line](https://code.claude.com/docs/en/statusline): janelas oficiais 5h/7d e diferença entre uso de contexto e cota; status line não é raspada pelo controlador.
- [Claude costs](https://code.claude.com/docs/en/costs): uso incluído na assinatura e opções de créditos de uso do provedor.
- [Codex app-server](https://developers.openai.com/codex/app-server): protocolo stdio, initialize, account/read, account/rateLimits/read, primary/secondary, usedPercent, resetsAt. A consulta foi validada contra a conta real.
- [Codex noninteractive](https://developers.openai.com/codex/noninteractive): exec, JSONL e schema de resposta.
- A mesma referência documenta `--skip-git-repo-check` para ambientes seguros sem repositório; a flag existe no help do Codex 0.167.0 instalado. O DevSwitch a aplica somente em pastas sem Git e mantém o sandbox. O modo sem Git foi validado com os dois agentes reais, handoff e unittest, sem executar `git init`.
- [Codex configuration reference](https://developers.openai.com/codex/config-reference): forced_login_method, model_provider, sandbox, hooks e features.
- [Codex developer settings](https://learn.chatgpt.com/docs/developer-settings): seleção explícita com `--model` e `-c model_reasoning_effort='"high"'`. O menu consulta o catálogo do binário instalado com `codex debug models --bundled`, sem inferência nem chamadas de rede.
- [Codex hooks](https://developers.openai.com/codex/hooks): hooks inline, matcher, PreToolUse, tool_input.command, permissionDecision=deny e limitações de cobertura/falha aberta.

O app-server é marcado experimental no help desta versão; o comando `doctor` testa capacidade em vez de presumir compatibilidade futura. Uma mudança de protocolo pode deixar o percentual desconhecido; o fallback continua por erros do `exec`. Se flags obrigatórias desaparecerem, a execução é recusada sem modificar o projeto.

O software não usa SDKs HTTP, endpoints de consumo privados, scraping de UI, APIs de faturamento, resets de créditos nem autenticação de terceiros. O uso de app-server é local e pessoal, sem expor um serviço comercial ou hospedado.
