# n8n orchestration for DefectDojo + Acunetix

Этот репозиторий содержит набор workflow для n8n и вспомогательные Python-скрипты для автоматизации цикла:

- сбор субдоменов,
- nmap-обогащение,
- синхронизация целей в DefectDojo,
- запуск сканов в Acunetix,
- базовая системная диагностика.

## Prerequisites

Перед запуском убедитесь, что доступно следующее:

1. **DefectDojo** (API доступен из n8n).
2. **Acunetix** (API доступен из n8n).
3. **n8n** (инстанс, куда будут импортированы workflow).
4. Файл окружения **`~/.n8n-env/.env`** с переменными:
   - `DOJO_*`
   - `ACUNETIX_*`
   - `NMAP_XML_DIR`
   - `N8N_*`
   - `SUBDOMAINS_CONCURRENCY`, `SUBDOMAINS_RUNNING_TIMEOUT_MINUTES`, `NMAP_CONCURRENCY`, `PT_WINDOW_SIZE`

> Рекомендуется загружать этот `.env` в окружение n8n/контейнера n8n, чтобы все workflow и скрипты видели одинаковые значения.

## Импорт workflow в n8n

Импортируйте **все JSON** из корня репозитория в n8n:

- `WF_A_Subdomains_PT.json`
- `WF_B_Nmap_Product.json`
- `WF_C_Targets_For_PT.json`
- `WF_D_PT_AcunetixScan.json`
- `WF_D_ProductScan.json`
- `WF_Dojo_Master.json`
- `WF_E_System_Health.json`

### Требуемые credentials

Для корректной работы проверьте, что в n8n настроены credentials/заголовки:

1. **`Header Auth account`** (используется HTTP Request узлами).
2. Заголовки для DefectDojo, как минимум API-key заголовок Dojo (например, `Authorization: Token ...`, в зависимости от вашей схемы).
3. Заголовки для Acunetix, как минимум API-key заголовок Acunetix (например, `X-Auth: ...`).

Если в workflow используются разные HTTP-ноды для Dojo/Acunetix, убедитесь, что каждая нода ссылается на правильный credentials-профиль.

## Порядок запуска

### 1) Ручной запуск

Основная точка входа — **`WF_Dojo_Master`**.

- Откройте `WF_Dojo_Master` в n8n.
- Запустите через **Execute Workflow Trigger**.
- Убедитесь, что дочерние workflow вызываются последовательно и завершаются без ошибок.

### 2) Запуск по расписанию

Для периодической работы используйте **Cron/Trigger** в n8n:

- добавьте/включите планировщик,
- направьте его на тот же entrypoint: **`WF_Dojo_Master`**.

Таким образом ручной и плановый режим используют одну и ту же оркестрацию.

## Ожидаемый цикл обработки (A → B → C → D)

Базовый цикл:

1. **A (`WF_A_Subdomains_PT`)** — сбор/обновление субдоменов для PT.
2. **B (`WF_B_Nmap_Product`)** — nmap-обработка и обогащение по Product.
3. **C (`WF_C_Targets_For_PT`)** — подготовка/синхронизация targets для PT.
4. **D (`WF_D_PT_AcunetixScan` / `WF_D_ProductScan`)** — запуск и управление сканами Acunetix.

`WF_Dojo_Master` выступает оркестратором этого цикла.

## Поведение при ошибках

Система предполагает **изоляцию по Product/PT**:

- ошибка на одном Product/PT не должна блокировать обработку остальных;
- частично упавшие элементы догоняются в следующем цикле;
- после устранения причины сбоя восстановление происходит автоматически при следующем запуске `WF_Dojo_Master`.

Рекомендуется вести историю запусков n8n и отслеживать repeatable ошибки по конкретным сущностям Product/PT.

## Базовая диагностика: `WF_E_System_Health`

Используйте `WF_E_System_Health` для первичной диагностики:

- проверка доступности внешних API (Dojo/Acunetix),
- проверка критичных параметров окружения,
- быстрый smoke-check перед массовым запуском по расписанию.

Практика: запускать `WF_E_System_Health` перед включением Cron и после изменений credentials/переменных окружения.


### Лимиты и диспетчеризация задач

Оркестратор (`WF_Dojo_Master`) использует лимиты из `.env`:

- `SUBDOMAINS_CONCURRENCY` — максимум одновременно активных PT в `subdomains_running`.
- `SUBDOMAINS_RUNNING_TIMEOUT_MINUTES` — TTL для зависших PT в `subdomains_running`; при истечении PT переводится в `error` для автоматического восстановления после рестарта.
- `NMAP_CONCURRENCY` — ограничение количества Product-задач в этапе nmap за проход.
- `PT_WINDOW_SIZE` — сколько PT анализируется за проход планировщика.

`WF_A_Subdomains_PT` теперь фиксирует результат этапа subdomains в PT-state:

- при успехе переводит PT в `subdomains_done`,
- при ошибке переводит PT в `error`, увеличивает `retry_count` и записывает `last_error`.

## PT state-machine в `product_type.description`

В оркестрации используется конечный набор состояний PT:

- `new`
- `subdomains_running`
- `subdomains_done`
- `nmap_running`
- `nmap_done`
- `targets_ready`
- `acu_running`
- `done`
- `error`

`WF_Dojo_Master` читает состояние из `product_type.description` и пишет обновления через DefectDojo API (`PATCH /product_types/{id}/`).

### Единый формат хранения

Состояние хранится как JSON-блок внутри `description` между маркерами:

```text
PT_STATE_JSON_START
{"version":1,"state":"nmap_running","counters":{"nmap_runs":2},"last_update":"2026-01-01T10:00:00+00:00","retry_count":0,"last_error":null}
PT_STATE_JSON_END
```

Поля блока:

- `version` — версия формата (сейчас `1`)
- `state` — текущее состояние PT
- `counters` — счетчики этапов (`subdomains_runs`, `nmap_runs`, `targets_runs`, `acu_runs`)
- `last_update` — время последнего обновления (ISO8601, UTC)
- `retry_count` — число ретраев
- `last_error` — последняя ошибка (строка или `null`)
- `acu_dispatch_policy` — политика диспетчеризации Acunetix для PT в состоянии `acu_running` (`fairness`, `node_selection`, `sticky_assignment`)

Если в `description` есть произвольный текст, он сохраняется, а state-блок обновляется/пере-записывается отдельно внизу.


## Acunetix pool (multi-instance)

Для распределения scan job между несколькими Acunetix-инстансами используйте переменные:

- `ACUNETIX_MAX_SCANS_PER_NODE` — глобальный лимит активных сессий на ноду (по умолчанию `5`).
- `ACUNETIX_INSTANCES_JSON` — JSON-массив нод с полями `endpoint`, `token`, `max_scans_per_node` (optional, per-node override), `scan_limit` (legacy alias), `name` (optional), `weight` (optional, для policy `weighted`).
- `ACUNETIX_NODE_SELECTION_POLICY` — фиксирует правило выбора ноды: `least_loaded` (по умолчанию) или `weighted`.
- `ACUNETIX_STICKY_ASSIGNMENT` — sticky назначение ноды на PT (`true` по умолчанию).

Пример:

```env
ACUNETIX_MAX_SCANS_PER_NODE=5
ACUNETIX_INSTANCES_JSON=[{"name":"acu-1","endpoint":"https://acu-1.local:3443","token":"token1","max_scans_per_node":8},{"name":"acu-2","endpoint":"https://acu-2.local:3443","token":"token2"}]
```

`WF_D_PT_AcunetixScan` делает health-check (`/api/v1/me`) каждой ноды, собирает активные сессии (`/api/v1/scans`), рассчитывает `free_slots = max_scans_per_node - active_sessions` и запускает новые задачи только в доступные слоты.

Политика диспетчеризации:

- **fairness:** `round_robin_by_pt` — очередь формируется по PT, чтобы один крупный PT не занял все доступные слоты.
- **node selection:** фиксируется через `ACUNETIX_NODE_SELECTION_POLICY` (`least_loaded` или `weighted`).
- **sticky assignment:** при `ACUNETIX_STICKY_ASSIGNMENT=true` PT стабильно тяготеет к одной и той же Acunetix-ноде между запусками (с fallback на policy selection при недоступности/переполнении sticky-ноды).

В итоговом output workflow добавлены поля `dispatch_policy` и `dispatch_by_pt`, а для каждого dispatch item — его policy snapshot.

`WF_Dojo_Master` пишет policy snapshot в PT-state (`acu_dispatch_policy`) при переводе PT в `acu_running`, а также пробрасывает policy в `queue_wf_d_pt_acunetixscan`/итог плана.

`WF_D_ProductScan` принимает выбранную ноду (`acunetix_endpoint` + `acunetix_token`) на входе и использует её для всех запросов scan/report в рамках конкретного job.
