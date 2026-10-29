# fla-reels

Gerador de artes (e, na fase 2, dos Reels) do **Fla Manchete**, portal independente de notícias do Flamengo. Não é perfil oficial do clube.

- `arte/arte.html`: templates aprovados (URG, NOT, MER, SNOT, SURG), preenchidos pela URL.
- `arte.py`: abre o template no Chrome sem tela e salva `arte-<Nº>-<modelo>.jpg`.
- `.github/workflows/arte.yml`: disparado pelo robô (repository_dispatch `arte`); publica no release `artes`.

Fonte: Archivo (SIL Open Font License). Fotos de jogador só com licença (Wikimedia Commons) ou foto oficial do clube com crédito.
