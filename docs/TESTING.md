# Testes e limites da validação

Execute em macOS com Python 3.11+:

```sh
python3 install.py
./.venv/bin/python -m unittest discover -s tests -v
devswitch demo
```

A suíte usa processos, arquivos, locks e terminais PTY reais, com CLIs e respostas dos provedores simulados. Não acessa contas nem consome assinaturas. Cobre alternância nos dois sentidos, esgotamento e renovação, espera de ambos, recuperação, SIGKILL, locks de pastas sobrepostas, backups, handoff, hooks, seleção de modelos, Git opcional, menus, painel, sugestões, edição Unicode, colagem, redimensionamento e restauração do terminal.

`scripts/real_smoke.py` é um teste separado e opcional que **consome cota real**: Claude cria uma função, Codex recebe o contexto, cria e executa um unittest, preservando um arquivo existente. A opção `--without-git` testa uma pasta comum. Não execute esse script em CI automática.

Antes da primeira publicação, escrita/handoff/teste pelos dois CLIs foram validados na instalação do autor, com e sem Git. A consulta de cotas do Codex foi verificada com login ChatGPT. Esses resultados não garantem acesso para outras contas, versões ou modelos. Esgotamento e renovação de assinatura foram simulados; nenhuma cota foi esgotada deliberadamente. Não houve teste de dias de execução nem reboot físico.

As evidências detalhadas da máquina do autor são mantidas localmente, fora deste repositório público. Não publique estado, logs de conta, tokens ou credenciais ao abrir um issue. Informe versão do macOS, `devswitch --version`, versões dos CLIs e passos para reproduzir; revise qualquer saída antes de compartilhar.
