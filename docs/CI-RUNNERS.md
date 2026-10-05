# Linux CI на Red

Все Linux jobs используют repository-scoped ARC scale set
`arc-prod-adler-home-frigate` для `krotname/HomeFrigateOllamaIaC`.
GitHub-hosted fallback отсутствует. Имена required checks, таймауты,
concurrency, hash-locked зависимости и закреплённые SHA actions сохранены.

Домашние jobs допускают только PR владельца `krotname` из этого же репозитория,
запущенные владельцем, и push владельца в `main`. Fork, Dependabot и PR других
авторов пропускаются до исполнения checkout и кода. Scorecard дополнительно
допускает schedule на `main` и ручной запуск владельцем с `main`. Релизный
архив собирается только по тегу `v*`, отправленному владельцем, либо по ручному
запуску владельцем с `main`; релиз не запускают для проверки CI.

В настройках Actions подтверждён `approval_policy=all_external_contributors`:
повторный внешний fork PR тоже требует проверки до допуска workflow. Одного
изменяемого PR-автором `if` недостаточно; внешний workflow нельзя approve
до проверки diff, особенно runner routing. На момент подготовки единственный
collaborator с правом записи — `krotname`.

## Совместимость пула

- Непривилегированный direct runner с tokenless `arc-job`, существующими
  admission, квотами и ограничениями ресурсов.
- Базовый CI-образ ProdOps: `pwsh`, `gh`, Docker CLI с Compose plugin,
  Python toolcache с правом записи для `actions/setup-python` и системные
  зависимости Python 3.13. Pester 5.7.1 устанавливается в job, как прежде.
- `docker compose config --quiet` только валидирует модель; ни daemon, ни
  host socket, ни Docker action этим jobs не нужны. Scorecard уже использует
  CLI с проверкой SHA-256 и также работает без Docker.

## Приёмка

1. Подтвердить в ProdOps применение repository-scoped scale set, Argo
   `Synced/Healthy` и работающий listener. При `minRunners: 0` отсутствие idle
   runner в GitHub нормально.
2. Проверить workflows локальным `actionlint`; открыть owner PR в `main`.
3. Подтвердить все required checks на окончательном PR SHA и точный runner
   `arc-prod-adler-home-frigate` через Jobs API.
4. После merge подтвердить push CI на `main` и отсутствие зависших jobs.

До готовности пула изменения остаются reviewable веткой: успешный локальный
lint не является доказательством выполнения CI на Red. Откат выполняется
отдельным PR; возвращение платного hosted маршрута требует отдельного решения.
