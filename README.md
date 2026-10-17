# newsvoice

Monorepo que unifica **news-engine** (`apps/news`) y **tts-api / gateway**
(`apps/gateway`) en una única solución desplegable en **1..N servidores** por
configuración (overlays de compose), no por fork.

Este repositorio se creó importando las dos historias completas con
`git subtree add` (sin `--squash`): `git log --follow` conserva la procedencia
por fichero.

## Layout

```
newsvoice/
├── apps/
│   ├── news/       # ← jordi/news-summary (news-engine)
│   └── gateway/    # ← choldi/ov_api (tts-api)
├── libs/
│   └── tts_contract/   # modelos Pydantic compartidos (P4)
├── deploy/             # compose unificado + overlays (P2)
└── scripts/            # ops fusionados (P7)
```

## Estado

En construcción según `apps/news/MERGE_PLAN.md` (fases P0–P10). Remotos
originales (`jordi/news-summary`, `choldi/ov_api`) pasan a ser archivos de solo
lectura.
