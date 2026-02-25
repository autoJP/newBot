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
4. Файл окружения **`~/.n8n-env/.env`** с обязательными переменными (синхронизировано с `.env.example`):
   - `DOJO_BASE_URL`
   - `DOJO_API_TOKEN`
   - `ACUNETIX_BASE_URL`
   - `ACUNETIX_API_KEY`
   - `ACUNETIX_INSTANCES_JSON`
   - `SUBDOMAINS_CONCURRENCY`
   - `SUBDOMAINS_RUNNING_TIMEOUT_MINUTES`
   - `NMAP_CONCURRENCY`
   - `PT_WINDOW_SIZE`

Дополнительно для обратной совместимости поддерживаются legacy-алиасы Acunetix (необязательные):

- `ACU_API_TOKEN` — fallback, если `ACUNETIX_API_KEY` не задан.
- `ACU_BASE_URL` — fallback, если `ACUNETIX_BASE_URL` не задан.

### Стандарт переменных Acunetix

Чтобы исключить двусмысленность, единый стандарт для API-ключа: **`ACUNETIX_API_KEY`**.

- Основные переменные: `ACUNETIX_BASE_URL`, `ACUNETIX_API_KEY`.
- На переходный период сохранена обратная совместимость: если `ACUNETIX_API_KEY` пустой, workflow/скрипты читают legacy-ключ `ACU_API_TOKEN`.
- В `WF_D_PT_AcunetixScan` и `WF_D_ProductScan` добавлена ранняя валидация: при пустом токене stage завершается с диагностикой и **не** отправляет запросы с пустым `X-Auth`.
- Для base URL также поддерживается legacy-алиас `ACU_BASE_URL`, но рекомендуется использовать только `ACUNETIX_BASE_URL`.

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

- `SUBDOMAINS_CONCURRENCY` — максимум одновременно активных **внутренних subdomain jobs** на стадии `subdomains` (по умолчанию `5`). Оркестратор раскладывает Stage A в очередь отдельных jobs внутри PT и ограничивает именно worker-пул этих jobs. Ограничение применяется к суммарному значению `counters.subdomains_running` в PT-state, а не к количеству PT в `subdomains_running`.
- `PT_LOCK_TTL_MINUTES` — TTL блокировки PT-state (`lock_owner`, `lock_until`) для защиты от дублей при параллельных trigger.
- `SUBDOMAINS_RUNNING_TIMEOUT_MINUTES` — TTL для зависших PT в `subdomains_running`; при истечении PT переводится в `error` для автоматического восстановления после рестарта.
- `NMAP_CONCURRENCY` — ограничение количества Product-задач в этапе nmap за проход.
- `PT_WINDOW_SIZE` — сколько PT анализируется за проход планировщика.

Важно: лимит применяется по фактическому числу внутренних subdomain jobs (`counters.subdomains_running`) во всех PT.
Это означает, что один PT с несколькими параллельными subdomain jobs может занять несколько слотов `SUBDOMAINS_CONCURRENCY`, а второй PT в этот момент может не получить слот даже при малом числе PT-jobs.


Рекомендуемые значения в `.env`:

```env
SUBDOMAINS_CONCURRENCY=5
SUBDOMAINS_RUNNING_TIMEOUT_MINUTES=60
NMAP_CONCURRENCY=5
PT_WINDOW_SIZE=300
```

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
- `subdomains_total` / `subdomains_done` / `subdomains_failed` / `subdomains_running` — счетчики внутренней subdomains-стадии на уровне PT (дублируются в `counters.*` для обратной совместимости) и используются барьером завершения `subdomains_done` по факту выполнения всех внутренних jobs.
- `last_update` — время последнего обновления (ISO8601, UTC)
- `retry_count` — число ретраев
- `last_error` — последняя ошибка (строка или `null`)
- `last_stage` — последняя стадия, для stage-based retry policy
- `lock_owner`, `lock_until` — временная блокировка PT-state для идемпотентных trigger
- `acu_dispatch_policy` — политика диспетчеризации Acunetix для PT в состоянии `acu_running` (`fairness`, `node_selection`, `sticky_assignment`)

Если в `description` есть произвольный текст, он сохраняется, а state-блок обновляется/пере-записывается отдельно внизу.


## Acunetix pool (multi-instance)

Для распределения scan job между несколькими Acunetix-инстансами используйте переменные:

> Если `ACUNETIX_API_KEY` не задан, fallback идёт на legacy `ACU_API_TOKEN` (временный переходный режим).

- `ACUNETIX_MAX_SCANS_PER_NODE` — глобальный лимит активных сессий на ноду (по умолчанию `5`).
- `ACUNETIX_INSTANCES_JSON` — JSON-массив нод с полями `endpoint`, `api_key` (рекомендуется) / `token` (legacy alias), `max_scans_per_node` (optional, per-node override), `scan_limit` (legacy alias), `name` (optional), `weight` (optional, для policy `weighted`).
- `ACUNETIX_NODE_SELECTION_POLICY` — фиксирует правило выбора ноды: `least_loaded` (по умолчанию) или `weighted`.
- `ACUNETIX_STICKY_ASSIGNMENT` — sticky назначение ноды на PT (`true` по умолчанию).

Пример:

```env
ACUNETIX_MAX_SCANS_PER_NODE=5
ACUNETIX_INSTANCES_JSON=[{"name":"acu-1","endpoint":"https://acu-1.local:3443","api_key":"token1","max_scans_per_node":8},{"name":"acu-2","endpoint":"https://acu-2.local:3443","api_key":"token2"}]
```

`WF_D_PT_AcunetixScan` делает health-check (`/api/v1/me`) каждой ноды, собирает активные сессии (`/api/v1/scans`), рассчитывает `free_slots = max_scans_per_node - active_sessions` и запускает новые задачи только в доступные слоты. Если API-ключ отсутствует (включая пустые/битые `ACUNETIX_INSTANCES_JSON` без `api_key/token`), workflow завершает stage явной ошибкой конфигурации.

Политика диспетчеризации:

- **fairness:** `round_robin_by_pt` — очередь формируется по PT, чтобы один крупный PT не занял все доступные слоты.
- **node selection:** фиксируется через `ACUNETIX_NODE_SELECTION_POLICY` (`least_loaded` или `weighted`).
- **sticky assignment:** при `ACUNETIX_STICKY_ASSIGNMENT=true` PT стабильно тяготеет к одной и той же Acunetix-ноде между запусками (с fallback на policy selection при недоступности/переполнении sticky-ноды).

В итоговом output workflow добавлены поля `dispatch_policy` и `dispatch_by_pt`, а для каждого dispatch item — его policy snapshot.

`WF_Dojo_Master` пишет policy snapshot в PT-state (`acu_dispatch_policy`) при переводе PT в `acu_running`, а также пробрасывает policy в `queue_wf_d_pt_acunetixscan`/итог плана.

`WF_D_ProductScan` принимает выбранную ноду (`acunetix_endpoint` + `acunetix_api_key`, legacy alias: `acunetix_token`) на входе и использует её для всех запросов scan/report в рамках конкретного job. В начале stage выполняется явная проверка endpoint/API-key; при пустом токене выполнение останавливается с диагностикой.

### Явные payload для `executeWorkflow`

Во всех очередях, которые `WF_Dojo_Master` передает в `executeWorkflow`, теперь используются явные поля:

- `pt_id` — обязательный идентификатор Product Type (дублируется с `product_type_id` для совместимости).
- `stage` — ожидаемая стадия subworkflow (`subdomains`, `nmap`, `targets`, `acu`).
- `job_metadata` — служебный объект (`source_workflow`, `queue`, `transition`, `lock_owner`).
- `selected_acu_node` — выбранная Acunetix-нода (для ACU-ветки; до диспетчеризации `null`, после диспетчеризации содержит `name/endpoint/api_key`, а `token` оставлен как legacy alias).

Минимальные схемы:

```json
{
  "pt_id": 123,
  "product_type_id": 123,
  "stage": "subdomains|nmap|targets|acu",
  "job_metadata": {
    "source_workflow": "WF_Dojo_Master",
    "queue": "wf_*",
    "transition": "old_state->new_state",
    "lock_owner": "dojo-master:*"
  }
}
```

Для ACU-диспетчеризации (`WF_D_PT_AcunetixScan -> WF_D_ProductScan`) payload дополняется:

```json
{
  "selected_acu_node": {
    "name": "acu-1",
    "endpoint": "https://acu-1.local:3443",
    "api_key": "***",
    "token": "***"
  }
}
```

Правило интерпретации тегов продуктов:

- product-теги (`targets:ready`, `acunetix:active`) больше не являются gate-условием для ACU-запуска;
- они сохраняются как вспомогательные `tag_signals` в dispatch item для диагностики/аналитики.


### Правила переходов между стадиями

- `new|error|subdomains_running -> subdomains_running` (диспетчеризация очереди внутренних Stage-A jobs до исчерпания `SUBDOMAINS_CONCURRENCY`; переход в `subdomains_done` только после `subdomains_done + subdomains_failed == subdomains_total` и `subdomains_running == 0`).
- `subdomains_done|nmap_running -> nmap_running` (батч jobs до `NMAP_CONCURRENCY`).
- `nmap_done -> targets_ready`.
- `targets_ready -> acu_running`.
- `acu_running -> done` после завершения scan/report/import в `WF_D_ProductScan`.

Стадия в payload (`stage`) должна совпадать с ожидаемой стадией subworkflow; несоответствие трактуется как некорректный вход orchestration.


### Retry policy по стадиям

`WF_Dojo_Master` учитывает stage-based лимиты ретраев:

- `PT_RETRY_SUBDOMAINS_MAX`
- `PT_RETRY_NMAP_MAX`
- `PT_RETRY_TARGETS_MAX`
- `PT_RETRY_ACU_MAX`

Если PT находится в `error` и `retry_count` достиг лимита для `last_stage`, PT больше не ставится в очередь автоматически до ручного вмешательства/сброса состояния. Ошибки Nmap переводятся в `error` с диагностикой (`last_error`).

## Операционная памятка (новые параметры и восстановление)

### Новые `.env` параметры для health/reporting

- `PT_HEALTH_WINDOW_SIZE` — сколько Product Type анализируется в `WF_E_System_Health` за один запуск (по умолчанию `300`).
- `HEALTH_MAX_LOG_EVENTS` — лимит массива `log_events` в health-отчете (по умолчанию `500`).
- `ACUNETIX_MAPPING_DB` — обязательный persistent SQLite-path для PT↔target mapping (по умолчанию `/data/n8n/acunetix_mapping_store.sqlite3`). Допускается только путь внутри `/data/...`; debug-fallback отключен.

### Что теперь показывает `WF_E_System_Health`

- `pt_state_counts` — агрегированное количество PT по состояниям (`new`, `subdomains_running`, `subdomains_done`, `nmap_running`, `nmap_done`, `targets_ready`, `acu_running`, `done`, `error`).
- `queue_status` — оценка очередей по этапам:
  - `subdomains`: PT в `new`/`error`;
  - `nmap`: PT в `subdomains_done`/`nmap_running`;
  - `acu`: PT в `targets_ready`/`acu_running`.
- `active_slots` — текущие активные PT по running-этапам (`subdomains_running`, `nmap_running`, `acu_running`).
- `node_errors` — ошибки внешних сервисов и Acunetix-нод.
- `mapping_backend` — состояние backend-хранилища mapping (доступность SQLite + объем/целостность записей).
- `pt_errors` — ошибки PT из state-блока (`last_stage` + `last_error`).

### Единый формат log-событий

Во всех итоговых отчетах/диспетчеризации, где доступны события, используется единый формат:

```json
{
  "pt_id": 123,
  "stage": "acu_dispatch",
  "job_id": "456",
  "server": "acu-1",
  "status": "queued",
  "duration": null
}
```

Поля:
- `pt_id` — идентификатор Product Type (или `null` для системных событий),
- `stage` — стадия (`health_*`, `acu_pool_probe`, `acu_dispatch`, `subdomains`, `nmap`, `targets`, `acu`),
- `job_id` — идентификатор задачи/события,
- `server` — узел/инстанс/источник,
- `status` — `ok` / `queued` / `error`,
- `duration` — длительность (если доступна), иначе `null`.

### Процедура восстановления после ошибок

1. Запустить `WF_E_System_Health` и проверить блоки `critical`, `node_errors`, `pt_errors`.
2. Если есть `dojo_unavailable`/`n8n_unavailable`/ошибки Acunetix-ноды — сначала восстановить инфраструктуру/credentials.
3. Для PT в `error`:
   - проверить `last_stage`, `last_error`, `retry_count` в `product_type.description`;
   - устранить причину (скрипт subdomains/nmap, доступ к Dojo/Acunetix, лимиты);
   - при необходимости вручную сбросить state в `new` или на предыдущий валидный этап.
4. Повторно запустить `WF_Dojo_Master`.
5. Контролировать, что PT переходят по цепочке `subdomains_running -> subdomains_done -> nmap_running -> nmap_done -> targets_ready -> acu_running -> done` без повторного возврата в `error`.
