# Instalação e distribuição

Código-fonte: [yanmfs/devswitch](https://github.com/yanmfs/devswitch). Licença MIT.

## Homebrew (macOS)

```sh
brew install yanmfs/tap/devswitch
devswitch doctor
devswitch
```

A fórmula fica em [yanmfs/homebrew-tap](https://github.com/yanmfs/homebrew-tap). Instala Python e um ambiente isolado, sem mudar o perfil do shell ou as configurações globais dos agentes. Claude Code e Codex CLI precisam estar instalados e autenticados; as assinaturas pertencem a cada usuário.

Atualizar:

```sh
brew update
brew upgrade yanmfs/tap/devswitch
```

O tap é do autor, separado do catálogo principal do Homebrew. A fonte é uma release fixa, com SHA-256. Setuptools é usado apenas na construção do pacote, com versão e checksum fixados na fórmula; o orquestrador não tem dependências Python externas em runtime. O teste funcional da fórmula grava e relê a configuração de um projeto temporário, sem acessar provedores. `devswitch demo` pode validar o ciclo completo com provedores simulados.

## Código-fonte

```sh
git clone https://github.com/yanmfs/devswitch.git
cd devswitch
python3 install.py
~/.local/bin/devswitch doctor
```

Requer macOS e Python 3.11+. O instalador usa somente a biblioteca padrão e cria um ambiente isolado na cópia local. Adicione `~/.local/bin` ao PATH se necessário. Mantenha o diretório clonado no disco: o launcher local aponta para ele. Não sobrescreve um comando de terceiros preexistente.

Se já existir uma instalação local em `~/.local/bin`, a ordem do seu PATH determina qual comando é executado. Veja `command -v devswitch`; a instalação do Homebrew pode ser chamada diretamente por `brew --prefix devswitch` seguido de `/bin/devswitch`. Seus estados e preferências por projeto são compartilhados, independentemente do método de instalação.

## Releases

Depois de testar e commitar os arquivos públicos, `python3 scripts/package_release.py` cria `dist/devswitch-VERSAO.tar.gz`, `SHA256SUMS` e um manifesto do commit. A origem é `git archive HEAD`, sem diretórios de estado, `.venv`, logs, credenciais ou evidências locais. Revise a lista antes de publicar. A release e a fórmula devem usar o mesmo arquivo/checksum.

Não há publicação no PyPI. Não use `pipx install devswitch` supondo que o nome pertença a este projeto.

Referências: [taps e instalação direta](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap), [pacotes Python no Homebrew](https://docs.brew.sh/Language-Specific-Formulae#python).
