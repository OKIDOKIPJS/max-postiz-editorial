## 0.14.0 — CI follow-up, 2026-11-02 (Europe/Moscow)

- The existing quality job now installs the built wheel into a new temporary virtual environment, checks its resolved dependencies with `pip check`, and verifies the installed package version through an isolated Python import outside the checkout. Existing required jobs and checks remain unchanged.
- Validation: workflow YAML parsed and the added Bash commands reviewed; the current main wheel installation and GitHub-hosted run have not yet been executed. This CI-only change does not alter the runtime package version.

# История версий

## 0.14.0 — 2026-11-02: ограниченный RFB/WebSocket probe без EOF busy loop

- R20 run 41378325059 прошёл стандартный CI/CodeQL, полную сборку, owner API/UI
  exercise, Linux supervisor и single-worker fence; строгий collector впервые
  сохранил PNG и пройденные сценарии. Native accepted=false: portal client был
  остановлен по 780-секундному timeout, почти всё время потребляя CPU. В прежних
  recv loops подтверждён дефект: пустой recv не завершал цикл. Какой upstream
  закрыл соединение и какой именно loop вызвал R20 timeout, не установлено.
- Portal probe теперь немедленно прекращается при EOF, ограничивает socket reads,
  абсолютный WebSocket deadline 10 секунд и HTTP headers 8192 bytes. До одного
  owner WebSocket handshake выполняется read-only ожидание настоящего RFB banner
  на 5900 не более 15 секунд. Login/prepare_login и другие POST не повторяются;
  реальные 600 секунд selection lease и общий client budget 780 секунд сохранены.
- Проверяются 101, точный nonce accept, Upgrade/Connection и точный 12-byte RFB
  banner. Поток собирается из нескольких binary messages или корректных
  continuation frames с общими пределами 512 bytes / 32 frames; control/close,
  неверные frames, неполный banner и превышения не принимаются. Диагностика
  содержит только фиксированные platform/stage/error literals, без cookie.
- Проверки: 106 passed / 8 Windows skips, включая EOF обоих loops, partial и
  coalesced stream, несколько binary frames, continuation, slow dribble deadline,
  caps, timeout, неверные 101/accept/RFB и отсутствие повторов. Ruff/diff и
  независимое review прошли. Продуктовые auth/units/Node context не изменены;
  server-side race не объявляется доказанной. Полная native приёмка нового
  exact commit ещё обязательна; default boot и image export остаются непроверенными.
  Рабочая VM, Пикабу и внешние публикации не затрагивались.

## 0.14.0 — 2026-11-02: готовность backend после restore и приватный producer PNG

- R19 run 41374913726 прошёл полный native build, реальные owner API проверки,
  загрузку изображений и граничные отказы, создание/правку/preview материалов,
  restart с сохранением истории, browser UI, 29 Linux supervisor tests и запрет
  второго worker. Оба MAX/VK portal механизма прошли native Chrome/profile/VNC
  проверки с настоящим ожиданием 600-секундной lease; это не вход в аккаунты
  платформ и не живая доставка. Стандартный CI R19 прошёл. Native accepted=false:
  проверка восстановленного клона получила 502 на первом auth POST, пока новый
  backend ещё не слушал порт; default boot и image export не были достигнуты.
- После запуска backend-restored добавлено ограниченное read-only ожидание GET
  can-register до первого auth POST. Auth и другие POST не повторяются, 502 не
  принимается за успех. Источник следующего default clone переключается именно
  на восстановленную БД только после успешной проверки restore и прежних точных
  fingerprints; при любом отказе старое binding остаётся, история не меняется.
- Отдельно исправлена переносимость PNG producer: Playwright screenshot(path)
  наследовал umask, поэтому файл мог получить group-write. Теперь точные bytes
  сохраняются один раз через private held directory FD, O_EXCL/NOFOLLOW, 0600 и
  fsync без замены существующего файла. Это подтверждённый дефект переносимости,
  но не доказанная причина R19 collection failure. Строгие collector проверки
  сохранены; диагностика допускает только конечные коды, безопасные file bits и
  четыре известных cleanup-кода без сырых ошибок, путей, содержимого и секретов.
- Проверки: Main 85 passed / 8 Windows skips, Scenario 69 passed / 4 POSIX skips
  на Windows; всего 154 passed / 12 skipped, включая два реальных sandboxed Chrome
  set_content regression. Ruff/diff и независимое review прошли. Полная native
  приёмка нового exact commit ещё обязательна; production service/helper limits,
  Node context, auth/approval/send guards не ослаблены. Рабочий сервер и Пикабу
  не затрагивались, внешних публикаций не выполнялось.

## 0.14.0 — 2026-11-02: строгий UI locator области редакции

- R18 run 41373325763 подтвердил ранний реальный sandboxed Chrome, большой nginx
  transport и весь API exercise до preflight 201 после смены worker, включая
  upload limits/create/revise/preview/restart/history. Безопасная диагностика
  локализовала generic exception: strict_mode при глобальном heading «Редакция».
  Закреплённый Postiz title.tsx и наша панель оба содержат h1 с таким текстом;
  прежний результат не доказывал OOM или ошибку sandbox.
- UI-проверка теперь требует ровно один видимый main и ровно один его видимый
  level-1 heading с точным именем. .first/.last не используются; status/auth,
  JavaScript, наличие трёх каналов и screenshot проверки сохранены. Продуктовый
  frontend и Node context не изменены, уточнён только harness locator.
- Проверки: полный Scenario 67 passed, в том числе два реальных sandboxed Chrome
  set_content regression — внешний одноимённый h1 допускается, дубликат внутри
  main отклоняется. Ruff/diff и независимый review прошли. Полная native/CI
  приёмка нового exact commit обязательна; R18 accepted=false без image export.
  Рабочий сервер и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: диагностика Python/Chrome этапа native exercise

- R17 run 41371284426 подтвердил исправленный 14 MiB nginx transport, полный
  build и реальные API-проверки загрузки ровно 10 MiB, превышения и 15m/1m/200KiB
  limits. Позже exercise завершился SCENARIO_FAILED без HTTP-отказа; unit exit 1,
  peak 848 MiB. Тип/строка исходное сообщение не сохраняло; OOM либо Chrome cause
  не объявляются доказанными. Accepted=false, переносимый image не экспортирован.
- Диагностика теперь публикует только конечный enum исключения, до восьми frames
  точного readiness_scenario.py (static function/line, без paths/message/locals),
  безопасный last_http и фиксированные Playwright boolean hints. Ответы и секреты
  не выводятся; все HTTP/status/auth/assertions и порядок exercise сохранены.
- До build добавлен реальный sandboxed Chrome about:blank prerequisite в own
  loopback namespace/transient unit с NNP/Delegate/45s и отдельным приватным HOME
  существующего nonroot runner. Это не owner login proof. Только тестовые transient
  clients получили явные 2 GiB для HTTP+browser; production service остаётся 1 GiB,
  helper/Engine limits не изменены. --no-sandbox/sysctl обходов нет.
- Проверки: scenario 65 pure passed; native 77 passed, 8 Windows FD/symlink skips;
  Ruff/diff и независимый review прошли. Полные native/canonical/CI проверки
  следующего exact commit обязательны; рабочий сервер и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: лимит тела внутреннего nginx auth subrequest

- R16 run 41370668926 воспроизвёл ошибку до тяжёлой сборки. Точный nginx log:
  client intended to send too large body 14680064 bytes в subrequest
  /_editorial-owner-auth, затем auth request unexpected status 413. Внутренний
  location наследовал server 1m и проверял исходную длину до удаления upstream
  Content-Length; отсюда внешний 500 на разрешённом большом upload.
- Только внутренний /_editorial-owner-auth получил client_max_body_size 15m,
  как точный route-admin. internal, auth_request, Cookie/Origin, body off и
  очистка Content-Length сохранены; общий server остаётся 1m, прочие locations
  не расширены. Это исправление production-шаблона, выполненное только в копии.
- Проверки: 75 pure/mock passed, 8 Windows FD/symlink skips; отдельный contract
  разрешает 15m ровно двум указанным scopes. Все 179 Node contracts прошли,
  Ruff/diff и независимый review прошли. Новый early 14 MiB probe и полная
  native/CI приёмка exact commit обязательны; R16 не принят целиком. Рабочий
  сервер, его nginx и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: ранний probe большого тела и приватный nginx log

- R15 run 41368799645 прошёл configure_target с 201 и дошёл до upload_media;
  там HTTP 500 без известного JSON code. SHA тела совпал с штатным CRLF HTML
  nginx/1.22.1, а не Nest JSON. Причина proxy-ошибки пока не установлена; типовые
  версии о правах или месте не объявляются доказанными. Native acceptance false.
- До тяжёлой сборки добавлен 14 MiB body transport probe через тот же BASE,
  nginx config/read-only/tmpfs и lo-only namespace. Отдельные ограниченные client
  и streaming stub контейнеры проверяют точное число bytes; stub auth 204 явно
  fixture, не owner proof. Proxy остаётся 96 MiB, client/stub — 128 MiB.
  После проверки все три процесса останавливаются и три listener ports свободны.
- При отказе собирается только фиксированный nginx error file после проверки
  собственного ID/label/cgroup/role: NOFOLLOW/regular/singlelink, bounded complete
  lines. Raw остаётся private и не входит в public glob; все известные passwords,
  auth lines и JWT удаляются до public tail и общего privacy gate. Тот же сбор
  применяется к runtime Scenario failure до удаления собственного Engine.
- Проверки: 74 pure/mock passed, 8 Windows FD/symlink skips; Ruff/diff и отдельный
  review прошли. App/Node context, реальные auth/status проверки и default CMD
  не менялись. Новый run должен дать раннюю причину либо выполнить весь native
  сценарий; рабочий сервер и аккаунт Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: точный успешный статус Nest POST в native harness

- R14 run 41367369667 подтвердил первый отказ: POST /api/editorial/route-admin,
  configure_target, HTTP 201. Проверки доступа перед ним прошли. Закреплённый
  Nest 11.1.21 по умолчанию выдаёт CREATED для POST без HttpCode; три BFF handler
  его не переопределяют. Это несовпадение harness с действующим API, не auth отказ.
- У native scenario точная карта: GET и auth register/login — 200; route-admin,
  route-action и control POST — 201. Admin наследует эту карту. Явные отрицательные
  ожидания 401/403/413/422, порядок, cookie/Bearer guards и запрет повтора сохранены.
  Нет разрешения любого 2xx или изменения production handler/авторизации.
- Проверки: 57 pure scenario tests passed, включая отклонение 200/202 для BFF,
  201 для auth и сохранение negative overrides; Ruff/diff и независимый review
  прошли. В этот кандидат также входит локально проверенный monitor fixture
  commit 1656d22. Полные CI/canonical/native проверки обязательны для exact head;
  R14 accepted=false и без image export, рабочий сервер/Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: настоящий Hub context в monitor command fixtures

- Полный R13 CI выявил четыре monitor-command отказа после объединения main:
  устаревший контекст Service не реализовывал новый Hub runtime-generation guard.
  Две команды завершались unknown до смысловой проверки, два прямых start теста
  получали AttributeError. Production Hub/guards не изменены.
- Локальный gateway fixture теперь создаёт настоящий HubService с теми же
  settings/БД и fake editor/publisher/http; валидный пустой publishing сохранён
  до capture поколения, bootstrap_targets=False. Нет noop/monkeypatch guard или
  изменения assertions. Новый Postiz client закрывается отдельно, владельцем
  общей БД/http остаётся исходный fixture.
- Проверки: весь tests/test_monitor_commands.py — 176 passed; focused 6 passed
  сохраняют done/blocked, паузу, mutation и busy-translator ожидания. Ruff/diff
  и независимый review прошли. Полный CI окончательного commit ещё требуется;
  этот test-only фикс не подтверждает HTTP native exercise или перенос на VM.

## 0.14.0 — 2026-11-02: точная безопасная диагностика HTTP native сценария

- R13 run 41365421111 подтвердил полные Next/Nest builds объединённого UI,
  11 compiled artifacts, реальные 8 GiB RAM / 4 GiB swap / CPU 4 и lo-only сеть,
  свежие зависимости, owner register/cookie/API key, migrate на паузе и установку
  portal units. Первый exercise остановился через SCENARIO_HTTP_STATUS_INVALID;
  endpoint/status исходная диагностика не сохранила. Образ не экспортирован.
- При неуспешном HTTP добавляется только method, статический allowlisted path,
  status, разрешённое literal action, body SHA и необязательный response_code из
  конечного набора 35 известных констант. Код извлекается лишь из JSON object
  до 64 KiB и повторно проецируется перед public JSON. Body, headers, cookies,
  неизвестные коды и private canaries не выводятся.
- Исходные auth probes, включая cookie+Bearer с ожиданием 401/403, порядок,
  разрешённые статусы, retries и production guards не изменены. Причина HTTP
  отказа пока не установлена; гипотеза о неправильном Bearer ожидании отозвана
  после чтения явного guard, а не превращена в ослабление проверки.
- Проверки: 41 pure сценарий passed; malformed/unknown/oversize/nonJSON privacy,
  mixed-auth negative, Ruff/diff и независимый review прошли. Полная приёмка
  следующего exact commit обязательна. Рабочий сервер и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: явный root helper-сервисов изолированного стенда

- Независимый разбор следующих native-этапов выявил две конкретные несовместимости
  harness: перенесённые login/restart units оставляли helper default root
  /opt/max-postiz; после завершённого restart ещё действовал его 60-секундный
  manifest, намеренно блокирующий prepare_login. Production contracts не менялись.
- Только в drop-ins двух собственных unit стенда сбрасывается ExecStart и передаётся
  абсолютный helper с --root текущей копии. User/NNP/netns/slice и cleanup сохранены.
  До portal unit добавлено ограниченное ожидание истечения точного приватного
  restart manifest: FD/owner/0600/singlelink/size/shape/TTL и повторная сверка bytes;
  максимум 65 секунд по monotonic. Manifest и сроки не изменяются, POST не повторяется.
- Проверки: 68 pure/mock native tests passed, 8 Windows FD/symlink skips;
  Ruff/diff и независимый review прошли. Незавершённый R12 заменяется новым точным
  кандидатом; его сборка и полная приёмка не объявляются успешными или неуспешными
  без terminal proof. Новому commit требуются все CI/canonical/native проверки.
  Рабочий сервер, production units и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: память полной Next/TypeScript сборки

- R11 run 41363544861 прошёл строгий resource/network probe и оба Nest builds;
  webpack нового объединённого UI собран за 3.7 минуты. Затем TypeScript worker
  получил SIGKILL, BuildKit сообщил ResourceExhausted/cannot allocate memory.
  Kernel OOM event не зафиксирован; кодовая ошибка TypeScript не выведена.
  R11 имеет accepted=false, экспорт образа пропущен, proof/privacy сохранены.
- Максимум native RUN увеличен с 6 до 8 GiB при прежнем общем пределе 10 GiB,
  ограничении common−2 GiB, внешнем запасе не менее 2 GiB, daemon 1 GiB,
  native swap 4 GiB и CPU 4. Фактический probe/guards не ослаблены. Heap 6144 MiB,
  Docker recipe, функции приложения и полная TypeScript-проверка не изменены.
- Проверки: 53 pure/mock Engine tests passed, включая пять границ доступной памяти;
  Ruff/diff и независимый review прошли. Это не успешный новый native build:
  для точного commit остаются обязательны полные CI/canonical и native проверки.
  Рабочий сервер и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: выключенная политика свежего native стенда

- R8 run 41362209481 подтвердил реальный fresh Postiz owner register/login,
  cookie и API key, отслеживаемый User unit и безопасный сбор bootstrap evidence.
  Дальше явный editorial migrate отказал до API-сценария: в новом runtime не было
  data/hourly-article-policy.local.json. Полная приёмка R8 не заявляется.
- В новый приватный data перед первой миграцией эксклюзивно копируется точный
  config/hourly_article_policy.example.json: enabled=false, проверка формы,
  held directory FD/NOFOLLOW, права 0600 и editorial owner. Имеющийся файл не
  перезаписывается; адресаты, согласования и отправки не создаются. В handoff
  уточнена разница fixture нового стенда и сохраняемой политики существующей VM.
- Проверки: 51 pure/mock passed, 7 Windows FD/symlink skips; совместимость
  проверена самим HourlyArticleCycle._policy(enabled=False) без БД и сети.
  Ruff/diff и независимый review прошли. Полный новый CI/canonical checks и
  native acceptance требуются уже для дерева с main 16efb38 и этим исправлением.
  Действий на рабочем сервере или в аккаунте Пикабу не было.

## 0.14.0 — 2026-11-02: объединение актуального main с тремя маршрутами

- Включён main 16efb38 после Android/MAX monitoring 0.13.3: сохранены все команды,
  UI/API мониторинга, исправления MAX, schema/history protections и обязательный
  unified check. Настройки трёх маршрутов, selected login, original revision,
  write-only Telegram token, raster Blob preview и bounded upload сохранены.
- В VK объединены semantic fixture clock из main и локальный WeakMap snapshot;
  production default остаётся real monotonic/1 секунда. Сохранены preview decode
  guards, native_url lineage и все real-expiry/native-action проверки. Более
  полная JUnit privacy диагностика main сохраняет assertions и устраняет прежний
  synthetic-secret canary CodeQL path без suppression.
- Private roots включают monitor/browser profiles и admin/restart/login state.
  Перед первым filesystem stat в browse добавлена явная canonical containment
  проверка с separator; прежние resolve/private guards сохранены. Traversal на
  main не воспроизведён, новый CodeQL нужен для проверки ранее открытого alert.
- Проверки объединения: 179 Node passed, строгий TypeScript/CAS; 40 VK focused
  passed с real expiry/pause/decode/privacy, 224 deselected; 77 security passed,
  6 Windows symlink skips и 6 Пикабу cases deselected. Ruff/diff прошли. Все 214
  записи нашей ветки и 259 записей main сохранены, версии согласованы как 0.14.0.
  Полные unified/CI/native проверки точного объединённого commit ещё обязательны;
  эти локальные результаты не являются доставкой или установкой на VM.

## 0.14.0 — 2026-11-02: сохранение TOPIC при строгой проверке release source

- Для существующей VM с собственным TOPIC.md добавлен явный режим отдельного
  EDITORIAL_RELEASE_ROOT: весь принятый source проверяется по manifest/tree,
  затем каждый публичный runtime-файл — по тем же SHA. Единственное исключение
  TOPIC.md требует точного EDITORIAL_PRESERVED_TOPIC_SHA256 из приватного backup
  до обновления. Неверный SHA, иной source, drift кода/compose и baseline без
  отдельного source блокируются. Строгий режим по умолчанию сохранён.
- В handoff добавлена сверка приватных hash-only снимков TOPIC/config/env до и
  после обновления; config/*.example.json, prompts, schemas и код не исключаются.
  Runtime app/publishing остаются приватными неотслеживаемыми файлами. Новая
  framework миграции или изменение runtime-политик не добавлены.
- Проверки: 40 pure release tests passed; шесть Bash blocks и shell wrapper
  прошли только синтаксический разбор; Ruff/diff check прошли. VM/Docker/БД не
  изменялись. Отдельно полный VK regression текущего pre-merge кода: 589 passed,
  1 Windows symlink skip, 1 предупреждение Starlette; это loopback/временные данные.
  Итоговое дерево после объединения с новым main требует новой CI/native приёмки.

## 0.14.0 — 2026-11-02: пути native evidence и стоимость свежего VK snapshot

- R6 run 41360104726 подтвердил ранний nginx probe, полную Docker/Next сборку,
  строгие лимиты/lo-only сеть и готовность зависимостей. До owner HTTP сценарий
  остановился на EACCES родительского каталога RUNNER_TEMP. Вывод User-сценариев
  перенесён в собственный project/data/ci-public 0700 без изменения общих прав.
- Сценарии, portal/default checks и явная миграция запускаются в отслеживаемых
  transient units пользователя editorial внутри собственного slice/netns с
  RuntimeMaxSec/KillMode. Сбор отчётов допускается после closed/empty cleanup;
  root читает только 11 разрешённых имён через held FD, NOFOLLOW, regular/singlelink,
  owner и size/mtime checks. Полный privacy gate и карантин сохраняются.
- R6 полный Linux CI: 6603 passed, 26 skipped, 1 failure до Next click VK —
  VK_BROWSER_ACTION_AUTHORIZATION_EXPIRED, 0 publish clicks. Для сокращения
  повторных DOM reads состояние одного элемента кешируется только внутри одного
  атомарного evaluate. Следующий snapshot всегда новый; структура/hash, свежая
  SQL-проверка и жёсткий лимит 1 секунды не изменены.
- Проверки: native pure/mock 47 passed, 5 Windows FD/symlink skips; VK с coverage
  31 passed, 218 deselected. На фиксированном DOM прежний exact hash сохранён,
  style/rect reads сокращены с 21 до 6 для шести элементов; изменение checkbox
  между evaluate меняет hash. Slow-authorization/pause и 16 readback races прошли.
  Ruff/diff check и независимый review прошли. Полный новый CI и native acceptance
  обязательны; R6 не принят целиком, portable image из него не экспортирован.
- Рабочий сервер, аккаунты/публикации и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: полный порядок переноса host-сервиса и proxy

- В handoff явно разделены Postiz image и Python host runtime. Добавлены порядок
  остановки после паузы/освобождения claims, адресное обновление source с сохранением
  приватной конфигурации, установка pinned dependencies и проверенного wheel,
  проверка фактического импорта, отдельная миграция только после клона/backup,
  Linux Chrome/profile prerequisites и проверка версии/поколения после рестарта.
- Для неактивного nginx-шаблона добавлены поиск реального listener текущего HTTP
  origin, приватный backup, адресное слияние, nginx -t, reload и откат. Прочие
  listeners сохраняются; прежний общий network-and-tools.sh не применяется.
- Проверки документа: пять Bash blocks прошли синтаксический разбор; CLI argv и
  существование путей сверены с исходниками, diff check прошёл. Команды установки,
  миграции, nginx reload и рестарта на рабочем сервере не выполнялись. После
  включения документа в release commit нужна native/CI приёмка точного дерева;
  инструкция сама по себе не подтверждает перенос или авторизацию аккаунтов.

## 0.14.0 — 2026-11-02: ранняя проверка прокси и диагностика Linux

- R5 повторно подтвердил полную Docker/Next сборку, строгие ресурсные/сетевые
  лимиты и готовность временных PostgreSQL/Redis/Temporal/Elasticsearch. Приёмка
  остановилась на выходе временного nginx до owner-authentication; причина
  выхода без его лога не установлена, image export и полная готовность не заявлены.
- Запуск обоих nginx listeners перенесён перед тяжёлой сборкой. Конфигурация
  повторно используется по SHA; проверяются ожидаемые 502 без приложений и
  освобождение портов. Read-only filesystem сохранена, все пять nginx temporary
  directories направлены в приватный tmpfs. Добавлена ограниченная диагностика
  только собственного контейнера после сверки owner/cgroup; публичный лог проходит
  полное редактирование секретов до усечения и общий privacy gate. Собственный
  диагностический CLI закрывается и при ошибке запуска reader thread.
- Проверки новой native-логики: 35 passed, 1 Windows symlink skip; Ruff и diff
  check прошли. R5 CI: PostgreSQL 237 passed, четыре Windows shards — 6597 passed
  и 24 skipped суммарно, Node/package/CodeQL прошли. Полный Linux — 6593 passed,
  26 skipped и два отказа положительных loopback-сценариев VK; для следующего
  прогона расширена диагностика fixture без ослабления production fence или
  assertions. Узкая повторная проверка этих двух сценариев, отрицательных
  pause/slow-authorization и приватности диагностики: 5 passed, 243 deselected;
  локально отказы не воспроизвелись. Для нового commit нужны повторный CI и
  полная native-приёмка.
- Рабочий сервер, внешние публикации и Пикабу не затрагивались.

## 0.14.0 — 2026-11-02: нативная сборка подтверждена, исправлен argv стенда

- R4 run 41356345817 подтвердил реальные RUN-лимиты 6 GiB RAM / 4 GiB swap /
  4 CPU и lo-only сеть на собственном Docker 29.9.2 / BuildKit 0.34. Полностью
  собраны backend, orchestrator и Next webpack/TypeScript; проверены native
  artifacts, CSS и безопасный CMD. Получен image
  sha256:5df50071b1468c11b074af60dd69689978254f78a5a48a8c655537143fe573f8.
- Свежие PostgreSQL/Redis/Temporal/Elasticsearch готовы. Позднее native run
  остановился до owner-authentication: Next hostname shorthand -H был отклонён
  общим запретом Docker host overrides. В argv стенда использован --hostname;
  Engine guard не ослаблен, production Node sources не менялись.
- R4 не принят целиком: owner/BFF/restart/restore/default-startup приёмка и
  portable export в нём не завершены. Для нового commit требуются полный
  native run и CI; успешный build сам по себе не объявляется полной готовностью.
  CodeQL всех языков и PostgreSQL CI текущего R4 прошли; local Ruff/diff-check
  узкой argv-правки прошли. Рабочий сервер и Пикабу не изменялись.

## 0.14.0 — 2026-11-02: явный TLS для временного набора Docker

- После CodeQL на следующем кандидате в downloader явно зафиксирован минимум
  TLS 1.2. Проверка сертификата и hostname сохраняется; источник, SNI, SHA и
  отсутствие redirects/proxies не меняются. Suppression не добавлен.
- Все 23 pure-теста downloader прошли, включая certificate/hostname/minimum
  TLS; Ruff/diff-check прошли. Новый CodeQL и нативный run требуются для точного
  исправленного commit. Предыдущий незавершённый native-кандидат заменяется этим
  по source identity, а не считается успешной либо неуспешной сборкой приложения.

## 0.14.0 — 2026-11-02: закреплённый Docker для нативного стенда

- Native run 41354862919 прошёл bootstrap/dependencies/pinned pulls и подтвердил
  отсутствие внешней сети в RUN, затем остановился до полной сборки: требуемые
  per-step CPU/memory/swap лимиты не подтвердились на Docker runner. Его отказ
  и доказательства сохранены; он не считается успешным build или приёмкой.
- Для следующего disposable GitHub run добавлен собственный Docker 29.9.2 только
  в RUNNER_TEMP: официальный TLS URL, фиксированные SHA/размер архива и SHA восьми
  бинарников, публичный IPv4/SNI без proxies/redirects, ручное извлечение только
  проверенных regular members. Системный Docker/пакеты/daemon не заменяются.
- Engine использует проверенные CLI/daemon/helper PATH; сохраняются собственные
  socket/data/cgroup, fd cleanup и строгий реальный resource/network probe.
  В диагностике probe добавлены self cgroup и mount information; неограниченные
  значения по-прежнему блокируют тяжёлую сборку.
- Проверки: 97 pure/mock tests passed, 1 Windows symlink skip, Ruff/diff-check
  прошли, независимый review без блокеров. Исполнения нового Linux набора ещё
  не было. В R2 отдельно прошли настоящий PostgreSQL CI (237 tests) и CodeQL;
  общий CI следующего commit и полная нативная приёмка остаются обязательными.

## 0.14.0 — 2026-11-02: исправления первого независимого CI

- Первый native run 41353716315 остановился в preflight до копирования исходников,
  сборки и runtime. Учтена несовместимость systemd при пустом фильтрованном списке:
  проверяются полные inventories с обязательным RC=0 и точными собственными именами.
  Добавлены приватная диагностика этапа/кода и публичные только typed metadata/SHA.
  Общие логи редактируются до усечения, чтобы граница tail не раскрывала секрет.
- Первый PostgreSQL job выявил рассинхронизацию временных MAX fixtures: их
  намеренные правки publishing/TOPIC теперь записываются и во временные файлы.
  Production guards не ослаблены. Локальная затронутая SQLite-регрессия:
  348 passed, 9 deselected; PostgreSQL требует повторного настоящего CI.
- Для отмеченного CodeQL пути локального preview URL создаётся новый Blob
  из тех же загруженных байтов с явным растровым MIME. DOM File не используется
  напрямую как источник URL. SHA, upload и отдельное согласование сохранены.
  CodeQL suppression не добавлен; новый результат сканирования ещё требуется.
- В native default-startup учтены и обычные, и prepared записи PostgreSQL
  log_statement=ddl, включая SQL с комментариями. Проверки: 174 Node passed;
  59 native/engine pure tests passed, 1 Windows symlink skip; Ruff/diff-check прошли.
  Source ZIP, wheel/sdist и relocation первого commit прошли privacy/610-file audit,
  но не заменяют нативную приёмку следующего исправленного commit.

## 0.14.0 — 2026-11-02: заморозка кандидата для независимой нативной приёмки

- Итоговый совместный набор guard/scenario/engine/portal/restart/handoff:
  110 passed; текущие source/runtime модули проверены Ruff без ошибок.
  В обычный Node CI включены новый startup suite и тест фиксированного Polkit
  разрешения; полный результат — 173 passed, без drift 53 проверенных файлов.
- Все изменения остаются в отдельной копии. Новый workflow собирает и принимает
  точный commit/source tree, сохраняет только прошедшие privacy scan доказательства
  и отдельно переносимый image archive. Ошибка cleanup, DDL, приватности или
  выгрузки образа не подменяется общей готовностью. Реальный полный прогон GitHub
  и source ZIP/wheel/sdist выполняются после этой заморозки; их результаты
  хранятся в отдельных проверяемых отчётах, не в runtime исходного архива.

## 0.14.0 — 2026-11-02: запуск без DDL и проверяемая передача образа

- Обычный production CMD заменён на явный запуск nginx и трёх приложений
  через их проверенные PM2 scripts. Унаследованный root alias с prisma-db-push
  не вызывается; схема изменяется отдельной операцией. Read-only build checker
  сверяет три app scripts и SHA startup, shell завершает свои процессы по сигналу.
- Добавлены последний compose override с immutable image ID и pull_policy never,
  отдельный release helper с read-only --check по умолчанию и guard source tree /
  source ZIP manifest, принятого native proof, image ID, SHA/размера архива,
  baked origin/profile и безопасного CMD. Прежние compose/postiz.sh сохранены;
  новый helper не принимает один tag или ручной флаг вместо доказательств приёмки.
- Проверки: 13 startup contract tests отдельно и полный Node runner —
  173 passed без пропусков; 53 файла совпали до и после полного прогона.
  23 pure-теста handoff guard прошли. Для приватности публикации proof —
  19 pure-тестов, включая карантин не прошедшего scan набора и отдельную
  безопасную квитанцию. Ruff, diff-check и сверка 55 dependency pins прошли.
- Нативный workflow проверяет безопасный CMD отдельно на восстановленном
  synthetic клоне с аудитом DDL; его фактический результат обязателен перед
  принятием переносимого образа. Приведённые числа относятся к локальным
  контрактам, а не успешной нативной сборке. Рабочий сервер и Пикабу не менялись.

## 0.14.0 — 2026-11-02: защита нативного runner и публичных доказательств

- Добавлены отдельные pure-тесты, запрещающие запуск вне GitHub-hosted root
  Linux с точным commit, перезапись существующего private state, публикацию
  HAR/env/raw logs и утечку в proof декодированных паролей DSN/вложенных ключей.
  Недостаток диска блокирует build, достаточный объём не вызывает cleanup.
- Проверки: 16 passed, Ruff и diff-check прошли; реальные Docker/systemd,
  сеть, рабочий сервер и аккаунты этими тестами не запускались.

## 0.14.0 — 2026-11-02: полная локальная регрессия VK и сценарий Linux-приёмки

- Восемь модулей VK: 587 passed, 1 skipped из-за права symlink в Windows;
  17 исходных файлов совпали до и после прогона. Проверки использовали только
  mock/loopback Chromium и временные БД, без аккаунта VK и внешних публикаций.
- Сценарий будущей нативной приёмки проверяет первый synthetic owner Postiz,
  cookie/CSRF/BFF, точный предел загрузки 10 MiB, создание и правку оригиналов,
  один запрос restart без повторного POST при потерянной квитанции, новый worker,
  историю и backup/restore с отдельным unknown fixture. Его 24 pure-теста прошли;
  они не являются успешным запуском полного стека.
- Новый workflow использует одноразовый GitHub-hosted Linux runner, собственный
  Docker engine, ограничения cgroup и сеть приложения только через loopback.
  Сохранённый LAN origin не используется для запроса к рабочему серверу.
  Тяжёлая нативная приёмка и поставляемый image принимаются только по её
  фактическому отчёту; production credentials и runtime не входят в артефакты.

## 0.14.0 — 2026-11-02: воспроизводимость проверок исходного пакета

- В sdist явно включены публичные workflow, pinned Node tooling manifests
  и scripts/*.cjs; node_modules и runtime не включаются. Исправлена изолированная
  CI-копия нового публичного backend bootstrap fixture без изменения source pins.
- Проверки: полный Node runner воспроизвёл отсутствие fixture, после исправления
  прошли все 159 mock-тестов. Статический аудит MANIFEST подтвердил новые Python,
  systemd и backend source paths; фактическая сборка пакетов выполняется отдельно
  после заморозки исходников. Внешние аккаунты, сервер и Пикабу не использовались.

## 0.14.0 — 2026-11-02: управление тремя маршрутами и восстановление настроек

- Из панели Postiz доступны адресат MAX из действующего allowlist, Telegram
  и личная страница VK, отдельный write-only ввод токенов, загрузка PNG/JPEG
  с SHA, создание и CAS-редактирование оригиналов. Изменение версии сбрасывает
  прежние проверки; точное согласование публикации сохраняется отдельным действием.
- Вход в браузер и перезапуск Linux-обработчика используют ограниченные
  одноразовые manifests, фиксированные systemd units и Polkit. Обычные serve/status
  не выполняют миграцию и не синхронизируют адресатов поверх существующей истории.
  Перед перезапуском проверяются пауза, leases, unknown и readonly очередь Postiz.
- При сбое между файлом настроек и commit БД сохраняется приватный intent;
  восстановление сверяет старое/новое состояние и audit nonce. Неоднозначность
  блокирует изменение без слепого отката. Секреты не попадают в JSON/ответы.
- Проверки: 73 Python теста настройки, восстановления, bootstrap и maintenance
  прошли; 25 pure/mock тестов отдельного Docker engine прошли. Это проверки
  кода и синтетических состояний, не нативная сборка и не живая доставка.
  README обновлён на 0.14.0. Полная нативная приёмка ещё выполняется отдельно.
- Действующий HTTP origin сохранён отдельным ограниченным build-профилем.
  Две локальные попытки build остановлены RAM guard; следующая локальная
  попытка не стартовала из-за preflight. Тяжёлая приёмка переносится на
  одноразовый GitHub-hosted Linux runner без production credentials.
  Рабочий сервер, его БД и Пикабу не изменялись; новых публикаций нет.

## 0.14.0 — 2026-11-01: состав серверного исходного пакета

- В sdist включены source systemd .path и Polkit .rules для новых фиксированных
  операций входа/перезапуска. Runtime, credentials и volumes в пакет не добавляются.
- Статическая проверка MANIFEST выполнена; окончательная wheel/sdist-приёмка
  проводится после заморозки всех исходников. Первый и второй native build
  безопасно остановлены host RAM guard без OOM; их traces сохранены отдельно.
  После остановки только собственного WSL runtime освобождено около 1,5 GiB RAM;
  следующий build использует прежние функции/типизацию и ограниченные ресурсы.

## 0.14.0 — 2026-11-01: решение владельца о VK и действующем адресе

- В AGENTS сохранены прямые решения владельца: действующий
  http://10.20.30.45:4007 сохраняется; рабочий сервер и Пикабу не участвуют
  в испытаниях; ограниченный native-uniform-edge-lineage-v1 разрешён для
  будущих отдельно согласованных изображений VK. Это не общее разрешение
  публикаций и не переоценка истории.
- Добавлена интеграционная проверка нового policy/evidence binding: теперь
  101 VK media/edge тест прошёл. Offline replay сохранённой геометрии прежней
  картинки подтвердил случай 1400×1000 → 1399×999 с однородной границей;
  сеть, новая публикация и изменение прежней dispatch/history не использовались.
- Read-only Telegram getMe/getChat/getChatMember в копии подтвердили канал и
  право бота публиковать. Это access_only, не новая доставка; существующая
  приёмка фото и API verified=false сохраняются раздельно.
- Предварительные onboarding проверки: 40 Node/TypeScript и 46 Python tests
  прошли. Native-сборка и полная серверная приёмка продолжаются отдельно;
  остановка первого build по host RAM guard сохранена как resource failure,
  не как успешная сборка или ошибка исходников. Межресурсное восстановление
  конфигурации file/DB проходит дополнительную реализацию и проверку.

## 0.14.0 — 2026-11-01: подготовка трёх маршрутов к серверной установке

- Разработка ведётся от main в отдельной копии; исходный runtime, рабочая VM
  и Пикабу не изменяются. Сохраняются история, approvals, unknown и leases.
- Добавлен отдельный выключенный по умолчанию контракт VK
  native-uniform-edge-lineage-v1: при согласованном native asset lineage
  допустимо удалить максимум один непрозрачный одноцветный пиксель границы
  справа/снизу. Требуются точный native crop=0,0,width,height, прежние asset,
  snapshot/source SHA и независимые readbacks. Resize, поворот, crop слева/сверху,
  удаление содержательной/прозрачной границы и неоднозначные параметры запрещены.
  Прежние политики, точные source identities и согласования не переписываются.
- Проверки этого этапа: 100 VK media/edge unit-тестов прошли. Это синтетическая
  регрессия, не новая живая публикация. Новый контракт не активирован без
  отдельного решения владельца; изображения по прежним правилам сохраняют gates.
- Остальные серверные изменения и их приёмка будут зафиксированы отдельной
  записью после завершения. Текущий адрес рабочего Postiz сохраняется по прямому
  указанию владельца; реальный сервер при испытаниях не используется.


## 0.13.3 — 2026-11-02: явные часы семантических VK-fixtures

- CI 1672151 подтвердил normal 7042 passed/35 skips и согласованный Windows
  partition: все 7077 случаев распределены ровно один раз. PG full и coverage
  выявили три отказа ACTION_AUTHORIZATION_EXPIRED при подготовке fake fixture,
  до проверяемых изменений автора/поста/изображения. Все 11 PG cases прошли;
  старый общий CI остаётся FAILED. Диагностика не устанавливает конкретную
  операцию, израсходовавшую окно авторизации; production fail-closed не меняется.
- Private clock вынесен в общий неавтоматический test helper. По независимому
  аудиту он явно подключён к 52 generic functions/98 cases и 5 native groups/9
  cases, проверяющим содержимое/identity/media/reconciliation с fake SQL authority.
  Чистые preflight/read-only/mock tests, real expiry/late-query и три controlled
  expiry cases не включены. Все исходные данные и assertions сохранены.
- Native actions по-прежнему получают реальный timeout не более 1000 мс;
  readiness/decode/result/readback clocks и бюджеты не изменены. Реальный
  late-query test дополнительно фиксирует original budgets 1..1000 и передаёт
  те же значения действию. Production-код, workflow и строгие gates неизменны.
- Helper включён в MANIFEST.in для воспроизводимого source-distribution QA.
  Полный Windows VK/browser-image набор: 350 passed, 0 skips. Ruff, ограниченный
  formatter и diff check прошли; независимое AST-ревью подтвердило границу
  fixture и неизменность проверок. Отдельный Linux/coverage и новый полный
  canonical/платформенный CI выполняются для итогового SHA перед merge.
- Локальные browser QA выполняются последовательно; старые отчёты ошибок
  сохранены отдельно. Рабочий сервер, действующий MAX-профиль и его runtime
  не изменяются, новая живая доставка и развёртывание не заявляются.

## 0.13.3 — 2026-11-02: стабильные идентификаторы полного Windows-набора

- CI 78d4d1c завершил все тестовые jobs успешно: normal/coverage 7041 passed,
  35 skips; PostgreSQL 7070 passed, 6 cgroup skips, все 11 обязательных PG cases
  выполнены. CodeQL без открытых alerts, Node и четыре Windows shards прошли.
  Но CI required правильно отверг несовпадающие collections, поэтому весь run
  остаётся FAILED и не разрешает слияние.
- В двух параметризациях monitor DOM текущее время попадало в автоматический
  pytest ID. Восемь IDs различались между процессами; строгая проверка выявила
  три пропущенных логических сценария и два повторённых. Обе параметризации
  получили явные стабильные уникальные названия. Свежие timestamps в DOM,
  сами сценарии и все защитные assertions сохранены; checker не нормализует IDs.
- Новый regression собирает настоящий monitor-модуль двумя независимыми
  wrapper-процессами. На прежних decorators он воспроизвёл разные hashes;
  после исправления весь shard test module: 26 passed. Все 11 затронутых
  реальных DOM-сценариев на Windows отдельно прошли, включая прежние пропуски.
  Ruff/ограниченный formatter/diff check успешны; новый полный CI требуется
  для итогового SHA и четырёх реальных согласованных shard receipts.
- Production-код, workflow, проверки полноты, интервалы и transport не меняются.
  Рабочий сервер и действующая MAX-копия не затрагиваются, live delivery не заявляется.

## 0.13.3 — 2026-11-02: безопасная диагностика синтетических VK-сценариев

- Включён latest PR #2 head 24244ac. Только тестовые fixtures записывают в JUnit
  разрешённые этапы, коды, тип ошибки, timeout и числовые счётчики. Сообщения,
  snapshot, private URL, call log и credentials не сериализуются. Исходный return
  и exception сохраняются даже при ошибке чтения или записи диагностики.
- Worker byte-identical предыдущему объединённому 72f2e3a; его полный Windows
  VK/browser-image набор 345 passed и Linux-проверки не подменяют полный CI нового SHA.
  После добавления диагностики Windows subset: 11 passed, JUnit проверен на
  разрешённые поля; вложенный canary-контроль охватывает четыре исхода.
  Ruff/formatter/diff check прошли, независимое ревью без замечаний.
- Правки относятся к наблюдаемости, а не к снятию прежних unknown или ослаблению
  таймаутов. Полный новый canonical normal/PG и платформенный CI обязателен перед
  merge. Рабочий сервер, действующие профили и внешние публикации не затрагиваются.

## 0.13.3 — 2026-11-02: актуальные исправления PR #2 и точный PG gate

- Включены оба новых коммита PR #2 до 12f2f70: ограниченное read-only ожидание
  декодирования preview VK до полного safety snapshot, два native-представления
  одного фото и переносимые route PG fixtures. История обеих веток сохранена.
  Exact account/text/media/SHA, свежая авторизация, unknown и запрет повторов
  остаются обязательными; production clock и его лимит не меняются.
- Portable TEST_DATABASE_URL применяется только без явного owned-контракта:
  driver postgresql+psycopg, test_* / *_tests, без query. Явный контракт по-прежнему
  проверяет точные endpoints и SELECT identity до DDL, без portable fallback.
  Синтетические URL-примеры очищают contract env только внутри своего модуля.
- Полный canonical PG gate требует dispatch1 + exact PostgreSQL recovery1 +
  monitor9 без skips. SQLite recovery не может заменить реальный PG case;
  добавлены негативные missing/skip/failure/error и identity-before-DDL проверки.
  Сохранены оба полных canonical режима и два отдельных PG17 вместо старого
  subset job PR #2; новые модули входят в полную collection автоматически.
- На объединённом дереве Windows VK/browser-image: 345 passed; PG guard/check
  helper: 85 passed. Те же 16 URL guard cases отдельно прошли с унаследованным
  owned env без подключений. Linux/coverage: 32 целевых cases, затем 20 cases
  (все 16 readback и 4 native mutation) passed; это раздельные пересекающиеся прогоны.
  Диагностический Linux использует Chromium; полный Chrome CI проверяется отдельно.
- Предыдущий c1924d3 дал normal 7003 passed/35 skips и четыре зелёных Windows
  shards, но PG full и Linux coverage завершились по одному browser failure:
  native caption не достиг mutation hook; readback image_count-2 не получил ID.
  Все 10 прежних PG cases прошли. Их точная ранняя фаза не была записана и не
  объявляется доказанной причиной; добавлены безопасные diagnostic assertions.
  Обе группы проходят на текущем дереве, полный новый CI нужен для итогового SHA.
- Рабочий сервер, действующий MAX-профиль, его БД/очереди и политики не меняются.
  Все транспорты синтетические; новая живая доставка или развёртывание не заявляются.

## 0.13.3 — 2026-11-02: раздельная проверка времени и семантики авторизации

- Полный CI 8b24627 подтвердил оба canonical режима: normal — 7000 passed,
  35 skips; PostgreSQL — 7029 passed, 6 Linux cgroup skips, все 10 обязательных
  PG cases выполнены. CodeQL, Node и четыре Windows shards прошли. Отдельный
  Linux coverage выявил истечение свежей авторизации внутри fake callback,
  который меняет DOM через Playwright, до проверки точного кода подмены аккаунта.
- Внутренний mutation helper получил явный clock для тестов; production по
  умолчанию использует прежний monotonic, лимит 1 секунду и action до 1000 мс.
  Только 13 semantic cases с IPC в fake authorizer используют управляемый clock.
  Ожидаемые причины отказов, real SPA waits и result/readback timers сохранены.
- Добавлены три точных проверки истечения лимита без native action. Прежние
  real expiry и late-query проверки остаются на настоящем времени. Целевой набор:
  Windows 18 passed; отдельная Linux/coverage копия — 18 passed (Chromium,
  диагностическая проверка). Ruff, ограниченный formatter и diff check прошли;
  независимое ревью не выявило замечаний. Новый полный CI нужен для итогового SHA.
- Это проверка синтетических транспортах, без внешних публикаций. Рабочий сервер,
  действующий MAX-наблюдатель, профили и редакционные очереди не изменяются.

## 0.13.3 — 2026-11-01: проверка разрешённых повторных конфликтов

- На дереве с main 0.12.3 и latest PR #2 проверены CAS, benchmark raw-config parity,
  history/image policies и MAX readiness/readback: 324 passed, 1 Windows symlink
  skip, одно известное Starlette/httpx warning. Все 22 embedded CAS-контроля прошли.
- Согласованы fixtures после обеих веток; новые main media/production проверки
  и все monitor tests сохранены. Whole Ruff, ограниченный formatter и diff check
  прошли. Итоговые canonical normal/PG и платформенные CI ожидаются для нового SHA;
  остановленные прогоны старого дерева не засчитываются как успешные.

## 0.13.3 — 2026-11-01: канонические проверки обновлённого дерева

- После интеграции main 0.12.3 оба обязательных режима scripts/check.py выполняются
  параллельными CI matrix jobs с собственными .venv, четырьмя locks и Chrome/Node.
  Обычный режим удаляет PG-переменные; PG-режим требует два отдельных PG17 instance
  и все 10 backend cases без skips. Прежние coverage/Linux и четыре Windows shards
  сохранены, CI required ждёт также весь canonical matrix. Артефакты раздельны.
- Target CI helper: 27 passed. На новом дереве main/monitor privacy + обе namespace
  регрессии: 108 passed, 6 Windows symlink skips. Все шесть Node suites: 139 passed,
  0 skips. Независимые ревью сохранения security/worker/Node контрактов завершены.
- При формальном CAS-прогоне устранены оставшиеся после merge имена JS-fixture:
  group/integrationId привязаны к той же точной строке DRAFT. Все 22 проверки
  остаются обязательными; production CAS guard не меняется.
- Эти целевые результаты не заменяют свежий полный CI для итогового SHA.
  Сервер, действующий MAX-профиль, БД и очереди не используются для проверки.

## 0.13.3 — 2026-11-01: повторная интеграция после обновления main

- Во время проверки main обновился до 4d9149f (PR #20, 0.12.3). Включён полный
  latest PR #2 head 8fa2807, содержащий этот main, исправления 0.11.11 и историю
  обеих веток. Новые VK media/review, Linux production build и приватность
  VK namespace сохранены; мониторинг и configuration fence не потеряны.
- Сохранена новая проверка benchmark raw-config parity с production, включая
  trailing slash и default segment budget. Это более позднее уточнение стенда,
  а не изменение списка production-переводчиков или старых quality evidence.
- Объединены обе namespace-регрессии CI и удалено двойное копирование бинарного
  PNG. Сохранены все Node UI/media/monitor tests и полный source history.
- Старый локальный normal full остановлен на 71% без F/E до изменения source,
  чтобы исключить смешанную проверку двух версий. Он не выдаётся за завершённый.
  Итоговый свежий код проходит оба canonical режима в изолированных CI checkout;
  результаты будут привязаны к новому SHA. Рабочий сервер не изменяется.

## 0.13.3 — 2026-11-01: полная PostgreSQL-проверка и изоляция fixtures

- Полный локальный PG-прогон выявил наследование TEST_DATABASE_URL в SQLite
  fixtures команд/авторского ввода и readiness. Guard правильно отвергал чужой
  для такого runtime порт. Исправлены только три scoped fixtures; product guards,
  реальные PG fixtures и глобальный conftest не ослаблены. Явный запрещённый URL,
  установленный после setup, по-прежнему блокируется отдельными регрессиями.
- В одном owned-PG запуске прошли 216 tests без skips, затем readiness — 15 passed;
  отдельный JUnit gate подтвердил все 10 настоящих PG cases. Старый заведомо
  неуспешный full-PG процесс остановлен только в нашей копии, без чужих процессов.
- GitHub PG job теперь запускает ПОЛНЫЙ python scripts/check.py --postgres
  в своей .venv с четырьмя locks, Node/Chrome и двумя отдельными PG17, вместо
  одного выбранного поднабора. Preflight остаётся до DDL, обязательные 10 cases
  не могут пройти через skips. Архив отчётов не включает временные fixture trees.
  Синтетические CI-контракты: 27 passed; независимое ревью без новых замечаний.

## 0.13.3 — 2026-11-01: диагностика VK fixture и раздельные временные контракты

- Единственный unknown в прежнем Linux CI не содержал полного кода ошибки.
  Его исходная причина не объявляется установленной. Добавлен короткий безопасный
  diagnostic assert; в отдельной Linux-копии исходный кейс прошёл трижды с coverage.
- Положительному тесту фонового DOM дан 5-секундный бюджет результата: он проверяет
  exact form и одну отправку, а не скорость HTTP за одну секунду. Фиксированный
  1-секундный fresh-authorization fence, worker и все product defaults не менялись.
- Добавлена контролируемая задержка ответа fixture: с бюджетом 1 секунда проверяется
  unknown после единственного сохранённого publish и отсутствие повторного Send;
  с 5 секундами — confirmed и две независимые reload-сверки. Оба исхода сохраняют
  все guards. Положительный и оба timing cases: Windows 3 passed; Linux/coverage
  3 passed (изолированный Chromium вместо Chrome, только диагностическая проверка).
- Whole Ruff/diff check прошли. Новый полный GitHub CI для итогового SHA выполняется
  отдельно; прежние результаты не подменяют его. Новых внешних публикаций нет,
  рабочий сервер и действующий MAX-наблюдатель не изменяются.

## 0.13.3 — 2026-11-01: импорт scripts в Windows CI shards

- Реальный GitHub CI выявил collection error: запуск Python-файла wrapper
  не добавлял корень checkout для импорта scripts.probe_pikabu_offline.
  Перед pytest.main добавляется только доверенный parent wrapper, не текущая
  произвольная папка. Pikabu-тест и полный состав collection не исключаются.
- Subprocess regression воспроизвёл исходный exit 2 без PYTHONPATH, затем прошёл
  с исправлением, сохранив receipt/exit/hash guards. Проверки shards: 24 passed,
  Ruff и diff check прошли. Предыдущие локальные полные прогоны продолжаются;
  новый полный GitHub CI проверяет итоговый SHA после этой отдельной CI-правки.
- На предыдущем SHA GitHub подтвердил CodeQL всех трёх языков, сборку/Ruff,
  Node на Linux/Windows и PostgreSQL 247 passed (включая новые route/monitor cases).
  Эти результаты не подменяют обязательный новый полный CI. Рабочий сервер,
  модели, MAX-профили и редакционные очереди не затрагиваются.

## 0.13.3 — 2026-11-01: CI и отдельные PostgreSQL-контракты

- Node CI включает шестой production-build suite, сохраняя CAS hashes и требование
  отсутствия пропусков. Отрицательная проверка fixture-ссылки (#50) проверяет
  реальные href в React tree; положительный контроль требует точный permalink
  только для independent_readback. CodeQL suppression не добавляется.
- GitHub PostgreSQL job использует два отдельных временных PostgreSQL 17 service
  с явными cluster labels, loopback-портами и проверкой разных system identifiers.
  Новые route/fence cases включены в job; JUnit не допускает их skips.
  Прежние guards тестов сохраняются, новый режим включается отдельным контрактом.
- Локальный --postgres с интеграционным контрактом выполняет read-only проверку
  личности обоих test instances до любого pytest/DDL. Провал preflight блокирует
  suite. Полный runner дополнительно проверяет обязательные PG cases в JUnit.
  Non-PG режим удаляет специальные URL/контракт из окружения дочерних процессов.
- CI dependency manifest согласован с четырьмя locks, включая PyYAML deployment.
  MANIFEST включает общий PG helper. Целевые проверки runner/union: 36 passed;
  whole Ruff и pip check прошли. Сквозные и GitHub-проверки выполняются отдельно,
  рабочий сервер/аккаунты/редакционные очереди не используются.

## 0.13.3 — 2026-11-01: устранение препятствий для слияния

- Защита файловой панели распространена на monitor runtime/config, стандартные
  и перенесённые профили VK/Пикабу, включая отключённые адресаты. Workflow не
  может читать/перезаписывать config/monitor.json; невалидная приватная конфигурация
  блокирует доступ без раскрытия путей. Добавлены синтетические regression tests.
- Проверка безопасности: 100 passed, 5 Windows symlink skips, одно известное
  Starlette/httpx warning. Это локальная проверка без живых аккаунтов/серверов.
- Единая проверка очищает дополнительные PG-переменные в обычном режиме;
  явный интеграционный PG-контракт требует route и monitor cases без пропусков.
  Проверки самого runner: 32 passed. Полные объединённые прогоны ещё выполняются.
- Созданы новые отдельные тестовые PostgreSQL 17 clusters на 55450/55451 только
  под data интеграционной копии. Существующие кластеры не используются и не
  останавливаются; credentials остаются в игнорируемых локальных файлах.
- Версия 0.13.3 согласована; документация отделяет интеграцию исходников от
  установки. Рабочий сервер, рабочие БД/очереди и активный MAX-наблюдатель не меняются.

## 0.13.3 — 2026-11-01: включение проверенной ветки 0.11.11

- В интеграционный код включён полный commit 91318ff с сохранением истории:
  readback уже отправленного MAX не требует composer, fallback после timeout
  прокрутки повторно проверяет точный пост/изображение и не создаёт новый Send.
- Сохранены регрессии 0.11.11 и воспроизводимый scripts/check.py. Benchmark
  использует явные segment_chars и нормализованный proof конфигурации стенда.
  Сохранены 22 CAS-контроля и новые fence-тесты. Версия пакета остаётся 0.13.3.
- Разрешены семь пересечений веток. Сквозные тесты объединённого кода пока
  выполняются; прежние зелёные прогоны не выдаются за результат нового дерева.
  Рабочий сервер, MAX-профили и runtime наблюдателя не изменяются.

## 0.13.3 — 2026-11-01: начало интеграции мониторинга с актуальным main

- В отдельной интеграционной копии объединены main a72398f и мониторинг 0.13.2;
  сохранены обе истории, entry points, актуальные CI и security dependency pins.
  Версия согласована в pyproject.toml и __init__.py. Рабочий сервер и работающий
  профиль наблюдателя не изменяются.
- Разрешены восемь текстовых конфликтов: сохранены config-based benchmark,
  строгая проверка числа частей изображения, 22 CAS-контроля, configuration fence
  и явные fixture imports. Остальные проверки выполняются после включения 0.11.11;
  этот промежуточный merge не заявляется готовым релизом или живой интеграцией.

## 0.13.2 — 2026-11-01: завершённая приёмка команд и подготовка публикации кода

- В отдельной копии активированы все 17 действий и DOM v2. Живая проверка:
  31 сценарий, подтверждения/результаты в одном monitoring_proekts_control,
  проверки после reload и ещё пять независимых побуквенных чтений без Send.
  Проверены отмена, пауза/продолжение, диагностика, источник pending_rights,
  установка и очистка фильтров с отдельным подтверждением. Исходные фильтры
  восстановлены, готовая статья и hashes её доказательств сохранены.
- После возобновления один служебный incident не получил подтверждение при
  первом reload и остановил следующую отправку. Штатная read-only сверка нашла
  точный прежний текст и подтвердила доставку без нового Send; защита не снималась.
- Добавлены явная очистка фильтров и безопасная история последних пяти отказов
  кандидатов в «ошибки». Известные блокировки и лимит операций за минуту дают
  понятный однократный ответ. Повторный опрос не повторяет задачу или Send.
- Общий PostgreSQL advisory/SQLite write fence объединяет изменение настроек,
  prepare_only start и получение bridge mutation lease. Активные claims/leases
  и unknown блокируют изменение с откатом revision. Длинный account principal
  компактно хешируется для core Audit.actor varchar(64), полная привязка остаётся
  в закрытом журнале команд. Исторические контракты и данные не переписываются.
- Проверки выбранных Python-наборов: 584 passed, 4 skips (три без TEST_DATABASE_URL,
  один Windows symlink); отдельно собственный PostgreSQL 17.6: 9 passed, 0 skips,
  кластер остановлен. UI/strict TypeScript: 77 passed. Поздние точечные проверки
  очистки/DOM: 91 passed, gateway: 16 passed, истории отказов: 11 passed,
  общего bridge fence: 44 passed. Наборы пересекаются; имеющееся предупреждение
  Starlette/httpx не скрывается. Это не полный прогон репозитория.
- Фоновый наблюдатель возобновлён с прежним заданием пополнения и сохранёнными
  claims. Подтверждена одна quality_ready статья; цель шесть ещё не достигнута.
  Все публикационные адресаты на паузе, MAX publisher — fake, approvals/dispatches
  не создавались. Нового редакционного поста нет. Рабочий сервер не изменялся;
  ранее разрешённое чтение профиля зависимостей не является его установкой.
- Дождались окончания отдельных тестов 0.11.11: проверенный commit 91318ff,
  PostgreSQL 5137 passed, обычный прогон 5119 passed, Linux 244 passed.
  Это другая ветка: её результаты и исправления не выдаются за проверку 0.13.2.
  Код публикуется отдельной веткой с draft PR; согласование с текущим main,
  установка на сервер, автозапуск компьютера и Android push не подтверждены.
  Секреты, профиль MAX и приватные доказательства остаются в игнорируемом runtime.

## 0.13.2 — 2026-11-01: расширение команд удалённого управления

- Версия пакета согласована в pyproject.toml и __init__.py. Новый единый чистый
  реестр из 17 действий используется разбором, справкой и приёмником; команда
  «команды» показывает только разрешённые действия с краткими описаниями.
- Добавлены безопасные просмотр/отмена задач, продолжение очереди подготовки,
  диагностика готовности, ошибки/источники и прежние ограниченные предложения
  фильтров/источников. Отмена выполняющегося задания не прерывает модель и не
  снимает unknown; останавливаются только будущие проходы после известного исхода.
  Идемпотентные пауза/отмена не задерживаются лимитом новых операций за минуту.
- Снятие общей паузы для подготовки имеет атомарный prepare_only guard:
  блокировка Control, SQLite write fence, все адресаты остаются на паузе,
  sending/unknown, владельцы/leases dispatch/bridge и активная подготовка блокируют
  операцию с откатом revision. Обычный старый control-контракт не менялся.
- DOM v2 разрешает только наблюдавшуюся точную inline-ссылку команды предложения
  источника: безопасный HTTPS, совпадающие видимый URL/href, одна ссылка без
  вложенных элементов и фиксированный профиль атрибутов. URL не открывается.
  Строгий v1 и его hashes сохранены; смена DOM требует отдельной живой приёмки.
- API/панель различают отмену, запрос остановки и ожидание моделей; сведения
  о приватных сообщениях и аккаунте не добавляются в read-only историю.
- Начальная локальная проверка реестра/DOM/config: 106 passed, Ruff прошёл.
  Проверки gateway, atomic control, UI и итоговая живая активация ещё выполняются;
  текущий работающий приёмник сохраняет прежние 3 действия до безопасной смены.
  Текущее пополнение не прерывается и не запускается заново.
- По новому поручению публикация кода в Git отложена до окончания тестов
  версии 0.11.11 и завершения собственной проверки. Рабочий сервер не изменяется,
  ключи, конфигурации и runtime остаются только в игнорируемых локальных файлах.

## 0.13.1 — 2026-11-01: живая приёмка единого чата и фоновой подготовки

- По следующему прямому поручению владельца команды, подтверждения, итоги задач,
  почасовые/ежедневные снимки и инциденты переведены в один закрытый solo-чат
  monitoring_proekts_control. Старый канал не удалён: прежние 9 сообщений сохранены,
  новые туда не направляются. Перед сменой адресата pending/sending/unknown отсутствовали.
- В новом маршруте однократно отправлены 3 свежие браузерные команды; все приняты.
  Подтверждены 8 новых сообщений разных типов. Независимые свежие reload нашли
  по одному точному исходящему тексту каждого сообщения в нужной solo-группе;
  повторных Send в этой проверке нет. Проверены account/solo/точный адресат и
  отсутствие обработки собственных отчётов как команд. Приватные доказательства
  остаются в data/monitor; они не включаются в исходный архив.
- Реальный MADLAD уже выполнил новый перевод: 5 сегментов с точным causal-v2 alias,
  контрактом и всеми закреплёнными SHA. Кандидат не выдан за готовый: hard gate
  сохранил отказ TRANSLATION_REFERENCE_CHANGED, затем началась разрешённая
  ограниченная Qwen-правка с обязательной новой независимой проверкой.
  Отдельный отказ прав на изображение также сохранён без ослабления условий.
- Наблюдатель работает в копии с отчётами и очередью команд; публикационные
  адресаты все на паузе, MAX publisher остаётся fake, редакционные dispatch/approval
  не создавались. Чтение профиля рабочего сервера и локальные inference forwards
  не меняют его конфигурацию, процессы, БД или очереди. Рабочие истории не копируются.
- Исправлено отображение статуса: текущая общая пауза определяется paused,
  сохранённая стадия выводится отдельно. Отсутствующий heartbeat не подменяется
  вымышленным; самостоятельная подготовка показана отдельно от отправителя.
- Финальные выбранные наборы: команды/service/config/CLI — 165 passed, 1 Windows
  symlink skip; API/server integration/CLI — 28 passed, 1 dependency warning;
  group transport — 13 passed; owner/config — 55 passed; формат отчётов — 51 passed;
  UI/strict TypeScript — 77 passed. Наборы пересекаются и не суммируются.
  Ruff и git diff --check прошли, зависимости ранее проверены pip check.
- Ограничения этого среза: задача пополнения продолжает работу в фоне, резерв
  из 6 готовых статей пока не подтверждён. Успешная служебная доставка и корректный
  backend перевода не заменяют полный quality_ready или редакционную публикацию.
  Копия работает на этом компьютере; установка на рабочий сервер/его автозапуск
  и Android push-уведомления этой проверкой не подтверждаются.

## 0.13.1 — 2026-11-01: живые команды и единый закрытый чат копии

- Версия согласована в pyproject.toml и __init__.py. Добавлен явный режим
  authenticated_account_outgoing для отдельной solo-группы: видимый аккаунт,
  точные чат/имя, принятый DOM, исходящие простые команды. Числовые sender/MID
  не выдумываются. Привязка использует приватный hash телефона; content/minute
  fingerprint и отдельный durable лимит допускают одно пополнение в минуту.
- Реально приняты два набора трёх команд (браузерный тест и сообщения владельца
  с Android): статус, резерв, пополнение до 6. Четыре read-only задачи выполнены,
  два пополнения честно остановлены preflight. Восемь ответов подтверждены
  отдельно после свежих reload, каждый с одним точным совпадением; старый
  первоначальный отчёт сохранён. Эти результаты не означают резерв из 6 статей.
- По следующему поручению добавлен единый чат для отчётов и управления.
  reply_in_control_chat по умолчанию выключен; общий адрес допустим только для
  принятого своего аккаунта, одинаковых точных имён и приватной solo-группы.
  Текстовый group transport имеет свой durable intent, запрет повторного Send
  после unknown и сверку после reload. Собственные отчёты не становятся командами.
  Claim outbox проверяет разрешённые адреса атомарно и блокирует все адреса при
  sending/unknown; перед отправкой повторно проверяются kind, срок и привязка.
- control-once/control-run обслуживают команды без producer/планировщика.
  Новая команда будит задачи/отправителя; готовая очередь ответов больше не ждёт
  полный 30-секундный опрос после каждого сообщения. CAPTCHA, 429, другой
  аккаунт/чат или утрата solo-контекста останавливают приём до проверки владельцем.
- Read-only API/панель показывают последние 10 безопасных итогов команд,
  отдельно queued и выполненную цель; исправлена paired-индикация режима своего
  аккаунта. Содержимое команд, телефоны, адреса чатов и account hashes не раскрываются.
- Пополнение сначала проверяет реальные зависимости; fake не считается успехом.
  Занятый MADLAD означает waiting_services без расхода проходов/claims и без
  повторного inference. Завершение требует ready >= requested; исчерпание проходов
  с недобором остаётся blocked. По новому разрешению профиль моделей прочитан
  с рабочего сервера без записей; отдельные OpenCode/runtime и локальные forwards
  настроены только в копии. MAX publisher fake, все четыре адресата на паузе,
  редакционный sender/server worker не запускались. Рабочие БД/очереди не копировались.
- Проверки до заключительной приёмки нового маршрута: общий выбранный Python-набор
  331 passed, 1 Windows symlink skip, 1 dependency warning; отдельно latency/CLI
  3 passed, API 17 passed и UI/strict TypeScript 77 passed. Наборы пересекаются,
  не суммируются. Ruff, pip check, git diff --check прошли.
- Ограничения этого среза: новая живая доставка в объединённый чат и фактическое
  пополнение до 6 ещё требуют отдельной заключительной записи. Независимая пустая
  история копии не заменяет production-дедупликацию при будущем переносе статей.
  Рабочий сервер не изменён; приватные настройки, ключи, journals и снимки только
  в игнорируемом runtime. Предыдущие записи 0.13.0 сохранены без переписывания.

## 0.13.0 — 2026-11-01: живая приёмка закрытого служебного канала MAX

- По прямому поручению владельца выполнен отдельный вход в профиль мониторинга,
  создан закрытый monitoring_proekts. Штатный интерфейс подтвердил создание
  приватного канала; точное имя проверено после reload. ID и профиль хранятся
  только в игнорируемом runtime/локальной конфигурации.
- Опубликован ровно один служебный отчёт с явной пометкой отдельной тестовой
  копии. Одна запись outbox и один browser Send-intent получили confirmed.
  Независимое повторное открытие канала и две дополнительные перезагрузки
  нашли по одному сообщению с побуквенно совпадающим текстом; новых Send не было.
- После проверки enabled, hourly_enabled, daily_enabled и commands.enabled
  выключены. Фоновая рассылка, команды, preparation worker и редакционные
  адресаты не включались. История и профиль сохранены для следующего подключения.
- Рабочий сервер не опрашивался и не изменялся; редакционные dispatch/leases
  этой копии отсутствуют, все четыре адресата выключены и на паузе. Код пакета
  и версия 0.13.0 не менялись. Прежние записи о локальных проверках и блокировке
  первого входа сохранены как история; это отдельное новое доказательство.
- Ограничения: проверена доставка одного служебного отчёта в MAX, а не статьи
  на четырёх площадках или текущие показатели рабочего сервера. Приём команд
  через реальный управляющий чат, Android, внешний watchdog и серверный
  автозапуск этой проверкой не подтверждаются. Приватные receipts/скриншоты и
  точный текст остаются в data/monitor, вне исходного архива.

## 0.13.0 — 2026-11-01: итоговая локальная проверка мониторинга

- Выбранный общий Python-набор: 337 passed, 2 skipped, 1 warning, 269.27 с.
  После последних правок повторены отправка/команды/setup/server integration:
  55 passed, 1 skipped, 48.35 с; отдельно актуальный API: 14 passed, 1 warning.
  Наборы пересекаются и не суммируются; это не полный прогон всех тестов проекта.
- Node UI/BFF/strict semantic TypeScript: 76 passed. Ruff всего src/tests,
  pip check и git diff --check прошли. Пропуски: Windows symlink privilege и
  ненастроенный TEST_DATABASE_URL; текущая живая PG-интеграция не подтверждена.
- Закрыты найденные при ревью случаи: перепривязка чата игнорирует его историю,
  malformed receipt остаётся unknown, browser-lock берётся до Send-claim,
  старый час истекает, отдельный PG-наблюдатель не конкурирует с preparer,
  ошибка необязательного мониторинга не останавливает обычное пополнение.
  Отдельно отслеживаются сбой/устаревание приёмника команд; configured не выдаётся
  за работающий приём. Создание канала отклоняет отозванное разрешение до Create.
- Локальные init/preview/once/status проверены: fresh SQLite, четыре выключенных
  и приостановленных адресата, нулевые отправки, disabled мониторинг/команды.
  Название monitoring_proekts сохранено только в игнорируемой конфигурации.
- Сборка и проверки комплектности/секретов wheel, sdist и source ZIP фиксируются
  в dist/monitor-release-verification.json и SHA256SUMS-monitor.txt. В исходники
  входят AGENTS.md, история версий, инструкция и service example; runtime исключён.
- Ограничение живой проверки: отдельный login-helper был запущен, но инструмент
  управления браузером остановился из-за недостоверного определения текущего URL.
  Остановлен только собственный helper. Канал не создан, отчёты не отправлялись,
  DOM/Android не приняты; обе функции остаются выключенными. Рабочий сервер и
  исходный checkout не изменялись; установка на Linux и внешний watchdog не выполнялись.

## 0.13.0 — 2026-11-01: Android/MAX-мониторинг в отдельной копии без Bot API

- Отдельные report outbox, browser-профиль и журнал Send/reload; точный адресат,
  версия настроек и текст. Sending/unknown блокируют повтор, сверка только читает.
  Создание закрытого служебного канала имеет собственный журнал намерения.
- Почасовые отчёты и ежедневный снимок, свежесть heartbeat, общий резерв,
  подготовка, отдельные доказательства четырёх площадок, инциденты/исчезновение
  сигналов без повторного шума; просроченные часы не догоняются.
- Ограниченные русские команды, durable inbox, проверка owner/chat/DOM/времени,
  защита от повторов и старой истории после перепривязки. Пополнение использует
  прежние hard gates/preparer; фильтры применяются только по exact подтверждению
  и на паузе; новые источники сохраняются pending_rights без включения.
- Read-only раздел в существующей Postiz-панели; owner-auth API не раскрывает
  приватные отчёты/конфигурацию. Внешний watchdog проверяет instance/freshness и
  состояние доставки, уведомляет только об изменении.
- Добавлены CLI, безопасный пример конфигурации, пример отдельной systemd-службы
  и инструкция. Версия согласована в pyproject.toml и __init__.py.
- Проверки: целевые offline pytest браузера/отчётов/команд/API и Node UI/BFF
  выполнены; итоговые числа общего прогона фиксируются отдельной записью ниже.
- Ограничения: рабочий сервер/его runtime не изменялись; новый канал и приём
  команд на реальном MAX пока не проверены, необходим отдельный вход и живая
  приёмка DOM. Docker/Linux image, Android и внешний watchdog не развёрнуты.
  Ежедневный отчёт — снимок, а не полная статистика за сутки. Callback-кнопки,
  комментарии канала, свободные code/shell-задачи и резервное оповещение не включены.



## 0.12.3 — 2026-11-02: диагностика этапов VK в JUnit

- CI 12f2f70 завершился одним Linux-отказом в сценарии потерянного ответа:
  unknown возник до upload, но прежний counter assertion не сохранил journal phase
  и failure_code. Его точная причина не установлена; runtime, защитные ожидания
  и таймауты по этому результату не меняются. Все пять ранее проверяемых
  VK/native сценариев прошли в Linux; Windows partition 6449 tests без ошибок,
  PostgreSQL integration 264/264 и CodeQL также успешны, но весь CI не принят.
- Диагностическое покрытие JUnit добавлено только в синтетические VK-fixtures:
  разрешённые коды/этапы, тип операции и числовые счётчики без полного сообщения,
  текста snapshot, raw-журнала или credentials. Значение результата, exception,
  точные assertions, авторизация, unknown и время операций сохраняются.
- Целевые проверки: 11 passed, 0 skipped; реальный вложенный JUnit содержит
  четыре успешных сценария с отсутствующими canary-значениями credentials,
  snapshot, private URL и call log. Проверены сохранность identity результата
  и исходного exception при сбое чтения/сериализации диагностики. Независимое
  ревью закрыто без замечаний. Это наблюдаемость, не заявление об исправлении
  неустановленной Linux-причины; worker и все runtime-файлы неизменны.
- Прежний локальный normal завершён только после адресной остановки своего
  pytest process tree: checker фактически вернул 1 и сохранил failed report
  без полного JUnit. Он не считается успешной проверкой; собственная тестовая
  PostgreSQL-БД не изменялась. Следующий CI проверяет диагностический снимок,
  затем полные normal/--postgres выполняются по окончательным source pins.
  Действующая VM и расписание публикаций не менялись.

## 0.12.3 — 2026-11-01: native preview VK сохраняет два представления фото

- Расширенная проверка выявила регрессию нового ожидания: native preview
  содержит два DOM-представления одной согласованной фотографии. Ожидание теперь
  использует точное число 2 для существующего image_contract, как и прежний
  _native_images; generic-маршрут использует число согласованных media.
  Полные проверки общего URL/размеров, asset containment, transform, upload binding,
  derivative SHA, повторного snapshot и свежей авторизации сохранены.
- Неуспешный расширенный VK-прогон 5559ffe остановлен по точному собственному
  process tree; фактический exit 1 и отсутствие полного JUnit сохранены отдельно.
  Его результат не считается полной приёмкой. После исправления 11 целевых
  native/generic тестов passed, включая одну отправку в синтетический
  transport, два reload и отрицательные проверки asset/unknown. Независимое ревью
  исправления числа представлений закрыто без замечаний.
- Дополнительно два native-сценария независимо задерживают decode каждого
  DOM-представления на 400 мс без смены URL/SHA/asset. Оба passed с одной
  синтетической публикацией, двумя reload и запретом повторного запуска;
  прежний native fixture-бюджет 10 секунд сохранён. Контроль с отключением нового
  ожидания дал 2 failed; это отдельный эксперимент, не запуск exact старого source.
- Полный новый VK-набор и обязательные normal/--postgres/CI выполняются по
  следующему фиксируемому снимку. Runtime-политики и действующая VM не меняются;
  native Send, права и качество не ослаблены, новые внешние отправки не разрешены.

## 0.12.3 — 2026-11-01: переносимые PG-тесты и готовность предпросмотра VK

- Выявлена унаследованная из main привязка двух route PostgreSQL-тестов к одному
  порту и имени локальной БД. Корректная отдельная TEST_DATABASE_URL теперь
  проверяется по driver postgresql+psycopg, имени test_* / *_tests и отсутствию
  query. Неверное явное значение вызывает ошибку до подключения без credentials
  и контекста ошибки парсера; отсутствие URL остаётся явным optional skip.
  Случайная схема, cleanup и запрет runtime fallback сохранены.
- CI PostgreSQL использует согласованное имя max_postiz_tests и включает оба
  route-модуля с проверками конкурирующей отправки/reconciliation и guard-регрессии.
  Чужой PostgreSQL на прежнем порту не используется и не изменяется.
- Воспроизведение: четыре допустимых URL дали 4 failed на прежнем guard;
  после исправления 12 guard-тестов passed, включая отказ до create_engine,
  отсутствие секретного маркера и exception context. Независимое ревью закрыто.
  Оба прежде пропущенных real-PG сценария отдельно прошли на собственной новой
  тестовой БД: 2 passed, 0 skipped; временные схемы удалены, public tables и
  посторонние подключения отсутствуют. Это целевая проверка, не полный --postgres.
- Воспроизведена гонка VK: после Next предпросмотр текста уже виден, но новый
  image-узел ещё загружается. Read-only ожидание точного числа декодированных
  изображений использует прежний transition timeout до первого полного snapshot.
  Полные identity/text/media/SHA проверки, повторный snapshot после image GET,
  свежая авторизация и неизвестные исходы сохранены; Send не повторяется.
  На прежнем коде новый набор дал 4 failed / 2 passed; после правки 23 целевых
  теста passed, включая длинный Unicode-текст, два reload, постоянную неготовность,
  смену аккаунта/текста/фото во время ожидания и все прежние readback-mutation controls.
  Бюджет fixture остался 1 секунда. Независимое ревью закрыто без замечаний.
- CI 8fa2807 сохранил две ошибки новых VK browser-сценариев Linux: причинная
  диагностика по JUnit и синтетическим fixtures выявила описанную достижимую гонку.
  Старый CI не сохранил journal, поэтому его точная причина не выдаётся за доказанную;
  assertions дополнены безопасной диагностикой фазы без полного текста snapshot.
  Таймауты и защитные assertions не ослабляются. Его Windows partition (6429 tests, 0 failures/errors),
  PostgreSQL 237/237 и CodeQL успешны, но весь CI не принят как зелёный.
  Локальный normal остановлен только по точному собственному pytest PID/tree;
  parent checker завершился с кодом 1 и сохранил failed report без полного JUnit.
- Полные normal/--postgres и новый CI будут выполнены по следующему фиксированному
  снимку после исправления подтверждённых причин. История доказательств сохранена;
  работающая VM, её файлы, модели, политики и расписание не менялись.

## 0.12.3 — 2026-11-01: PR #2 повторно объединён с новым main

- Пока выполнялись проверки 9c5d8f3, main получил PR #20 и версию 0.12.3.
  Новый merge сохраняет main 4d9149ff, его четыре платформенных маршрута,
  ограничения авторизации/прав, security-изменения, locks и CI. Версия 0.12.3
  согласована в pyproject и пакете; рабочая VM не обновляется.
- Сохранены все 22 CAS controls нового main, исправление namespace в Windows
  shard runner и отдельная загрузка fixture перед отрицательным readiness timeout.
  Чтение receipt MAX без composer и все проверки права отправки PR #2 сохранены.
- Независимое ревью уточнило описание действующего CI и отделило загрузку fixture
  также в отрицательном тесте чтения канала: прежние 150 мс классификации,
  отсутствие повторного GET/Send и сохранность чужого черновика проверяются отдельно.
- Benchmark использует исходный config hash по контракту production
  DedicatedTranslator из main. Независимая регрессия на том же HY model,
  с URL с завершающим slash и без него, воспроизвела четыре несовпадения прежнего
  PR-конструктора. Сохранены лимит 1400, отказ на 1401, точный evidence hash
  и отсутствие HTTP; production-конструктор не изменён.
- Новый целевой benchmark/CAS набор: 61 passed, без failures/errors/skips.
  Все 22 CAS controls выполнены; production-parity regressions прошли после
  воспроизведённых четырёх несовпадений старого конструктора.
- Старые normal/CI результаты 9c5d8f3 сохранены как история старой базы.
  Локальный normal остановлен только по точному собственному pytest process tree;
  checker фактически завершился с кодом 1 и сохранил failed report, полного JUnit нет.
  Успешной приёмкой этот прерванный прогон не считается. CI старого HEAD полностью
  зелёный, но не заменяет проверки объединения с main 0.12.3.
- Полные новые normal/--postgres и CI выполняются по новому фиксируемому source.
  Результаты, независимое ревью и ограничения фиксируются после их завершения.
  История обоих родителей сохранена; VM, модели, её политики и расписание
  публикаций этой интеграцией не менялись.

## 0.11.11 — 2026-11-01: интеграция PR #2 с актуальным main

- Конфликты объединены с main a72398f без удаления прежних записей CHANGELOG.
  Версия 0.11.11 согласована в pyproject и пакете; security bounds, новые locks,
  защита приватных путей и настроек, security-регрессии и CI сохранены из main.
- Ветка сохраняет нормализованный URL и segment_chars=1400 в proof hash стендового
  переводчика, все CAS controls, браузерные readiness/receipt/unknown guards,
  изоляцию TEST_DATABASE_URL и ограниченный бюджет полного pytest 7200 секунд.
- Проверены согласованность 55 CI pins, синтаксис объединённых Python-файлов,
  полный Ruff lint, установленная область formatter и pip check. Node/Postiz
  contracts: 89 passed, ноль skipped, пять наборов без живых сервисов.
  Независимое ревью снятия конфликтов закрыто без замечаний.
- Целевые security/CAS/benchmark/check/shard проверки: 163 passed, четыре
  платформенных skips из-за недоступного создания symlink на этом Windows-ПК.
  Пропущенные сценарии остаются в полном Linux CI; успешное выполнение здесь
  не заявляется. Полный новый normal/--postgres и удалённый CI ещё выполняются.
- Проверки объединённого кода выполняются в отдельном checkout с собственной
  .venv и новой тестовой PostgreSQL-БД; прежние результаты 91318ffe остаются
  историческими и не выдаются за приёмку новых исходников и зависимостей.
- CI из main включает Linux/Windows, PostgreSQL, Node/Postiz contracts, проверку
  полного Windows partition и обязательный CI required. Ограничения main,
  CodeQL и контроль качества не отключаются ради слияния.
- Рабочая VM, её пакет/зависимости, политики, история, claims и расписание
  публикаций не менялись. Этот набор исходников не развёртывается на VM.
  Фактические итоги проверок и ревью фиксируются отдельно после завершения.

## 0.11.11 — 2026-11-01: установка и первая подтверждённая доставка

- На VM установлен проверенный commit `91318ffe`: пакет 0.11.11 и 128 runtime-файлов
  совпали с отдельным точным manifest. Native pause89→90 и start90→91 выполнены
  в безопасном окне, с полной резервной копией и одним фактическим рестартом.
- Сохранены все 258 записей, прежние raw/trace, claims, история, политики и профиль
  моделей. Полная сверка 37 таблиц подтвердила прежний текст, изображение,
  согласование, tokens и дату pending. Отдельно проверены только штатные изменения
  управления, native refence и служебных revision/time при неизменном ledger payload.
- Ранний GET после рестарта получил ошибку соединения. Фактический новый процесс
  и все postconditions затем проверены read-only; исходный claim сохранён,
  отчёт дополнен явным доказательством восстановления. Повторного рестарта не было.
- Первая новая плановая доставка подтверждена 1 ноября 2026 в 21:36:27,226 МСК:
  Postiz PUBLISHED, dispatch verified, одна физическая попытка, два reload MAX с [1,1],
  точный текст, фото перед ним, жирный заголовок и обычное тело. Обе удалённые
  строки отсутствуют; журнал отправок не менялся при независимой проверке.
- Сохранены успешные полные Windows-прогоны: PostgreSQL 5137 passed / 5 skips,
  обычный 5119 passed / 23 skips; отдельная Linux QA — 244 passed без skips.
  Это результаты проверенного кода 91318ffe; последующее дополнение документации
  не меняет deployed baseline. Полный Linux check и проверка типов не заявляются.
- Контроль переведён на точные pins 0.11.11/RUN91, прежний baseline и все proofs
  сохранены. Старые max/max-postiz PAUSED; подготовкой и отправкой владеет VM.
- Слияние PR #2 с main остаётся отдельной интеграцией: пять конфликтов, новые
  security-изменения и locks требуют повторных проверок объединённого кода,
  независимого ревью и зелёных обязательных CI required/CodeQL. PR остаётся Draft.

## 0.11.11 — 2026-11-01: повторная полная проверка после браузерных исправлений

- Оба обязательных прогона выполнены последовательно по новому снимку,
  зафиксированному до запуска. Все восемь этапов каждого завершились успешно;
  после PostgreSQL и после общего прогона повторно совпали 493 файла исходников
  и три инструмента. Проверенный код между прогонами не менялся.
- PostgreSQL: 5137 passed, 0 failures/errors, пять платформенных skips;
  полный pytest занял 6392,218 с при прежнем бюджете 7200 с. Отдельный тестовый
  кластер остановлен, завершение известных процессов проверено отдельно.
- Общий режим: 5119 passed, 0 failures/errors, 23 skips; pytest — 3484,797 с.
  Восемнадцать PostgreSQL-тестов намеренно не получали URL в этом режиме и
  прошли в отдельном прогоне с PostgreSQL. Остальные пять пропусков в обоих
  наборах — три проверки symlink на Windows и две проверки native Linux pipe.
- Все 30 обязательных браузерных и бюджетных регрессий прошли в каждом наборе.
  Нулевой код завершения обоих запусков наблюдён фактически; JUnit, отчёты,
  lifecycle и связь с исходным снимком сверены, прежние отказы сохранены.
- Код отдельно просмотрен; права перед Send, unknown, защита от дублей,
  история и интервал доставки не ослаблены. Проверка типов не настроена.
  Эти результаты не подтверждают живую интеграцию: изолированная Linux QA,
  установка с backup/CAS и первая доставка 0.11.11 проверяются отдельно.
  Действующая VM на момент этих проверок продолжает работу на 0.11.10.

## 0.11.11 — 2026-11-01: бюджеты подготовки и контрольного чтения в браузерных тестах

- Итог общего прогона предыдущего снимка: 5118 passed, один failed и 23 skips;
  остальные семь этапов прошли. Проверка готовности канала завершилась успешно,
  затем контроль неизменности черновика получил TimeoutError при чтении текста
  с унаследованным искусственным лимитом 40 мс. Неуспешный отчёт сохранён.
- Отдельный прогон модуля выявил второй отказ: искусственный бюджет 1000 мс
  ограничивал начальную навигацию до проверки канала. Сохранены 39 passed и
  один failed этого прогона; его успешная приёмка не заявляется.
- Обычная навигация тестов использует штатный бюджет готовности 30 секунд;
  отдельный setup перехода на неверный URL получает тот же явный предел.
  Только контрольное чтение чужого черновика получает бюджет 1000 мс.
  Все assertions, fixture timeout 40 мс, отрицательные проверки на 150 мс и
  их предел две секунды сохранены. Код приложения и его production-бюджеты
  этим изменением не менялись.
- На существующих тестах воспроизведены отказ getter текста длительностью
  200 мс после успешной проверки канала и оба отказа навигации с задержкой
  1200 мс. После правок те же fault-сценарии прошли со всеми assertions.
  Использован отдельный Chrome с синтетическими страницами, без живой отправки.
- Модуль готовности канала целиком: 40 passed, без failures/errors/skips.
  Полный Ruff lint и git diff --check прошли; независимое ревью без замечаний.
- Оба обязательных полных прогона выполняются заново по новому снимку.
  Прежний PostgreSQL-результат 5137 passed / пять платформенных skips относится
  к прежним байтам теста и сохраняется как история. Установка и первая доставка
  0.11.11 ещё не подтверждены; действующая VM продолжает работу на 0.11.10.

## 0.11.11 — 2026-11-01: чтение изображения после таймаута прокрутки

- Изолированная Linux QA выявила отказ чтения уже отправленного поста:
  таймаут штатной прокрутки не перемещал изображение в область загрузки.
  Сохранённый диагностический trace подтвердил TimeoutError; исходный QA-отчёт
  с 219 passed и одним failed сохранён и не считается успешной приёмкой.
- Только после такого таймаута выполняется ограниченная прокрутка прежнего
  DOM-узла: повторно проверяются один точный пост, текст, жирный заголовок,
  обычное тело, допустимый origin и положение изображения перед подписью.
  Проверки и прокрутка выполняются вместе; подмена или удаление узла блокируют
  запасной путь. Финальное подтверждение по-прежнему требует полного image proof.
- Дополнительные регрессии воспроизвели появление дубликата между проверками,
  в том числе при уже загруженном фото. Повторный подсчёт точных сообщений
  перед confirmed сохраняет ожидаемое before_count + 1. Повторного Send нет;
  текущие согласования, история, unknown и публикационные интервалы сохранены.
- До правок воспроизведены таймаут и оба случая позднего дубликата.
  После всех изменений 24 целевые регрессии прошли без skips; отрицательные
  сценарии проверяют чужие URL, изменение текста/формата, дубликат и смену узла.
- Оба браузерных модуля целиком: 217 passed, без failures/errors/skips.
  Ruff lint и git diff --check прошли; независимое ревью без замечаний.
- Полные общая и PostgreSQL-проверки выполняются заново по новому снимку.
  Предыдущие успешные Windows-отчёты относятся к прежним исходникам.
  Изолированная Linux QA, установка и первая доставка новой версии остаются
  отдельными проверками; действующая VM продолжает работу на 0.11.10.

## 0.11.11 — 2026-11-01: итоговые проверки Windows

- Оба обязательных прогона выполнены последовательно на одном предварительно
  зафиксированном наборе исходников. Все восемь этапов каждого прошли;
  после завершения повторно совпали 493 проверяемых файла и три инструмента.
- PostgreSQL: 5113 passed, 0 failures/errors, пять платформенных skips —
  три проверки symlink на Windows и две проверки Linux pipe. Полный pytest
  занял 5055,547 с при ограниченном бюджете 7200 с; тестовая БД остановлена.
- Общий режим: 5095 passed, 0 failures/errors, 23 skips; 18 PostgreSQL-сценариев
  намеренно не получали URL и отдельно прошли в PostgreSQL-прогоне, остальные
  пять имеют те же платформенные причины. Время pytest — 4997,953 с.
  Все шесть новых браузерных и бюджетных регрессий прошли в обоих наборах.
- Независимое ревью итогового diff прошло без замечаний. Прежние failed,
  timeout и controlled-abort отчёты сохранены. Проверка типов не настроена.
  Локальные suite не подтверждают живую интеграцию или доставку MAX;
  изолированная Linux QA, установка и первая доставка проверяются отдельно.

## 0.11.11 — 2026-11-01: положительные браузерные регрессии используют штатное ожидание

- В положительном illustrated-v2 сценарии искусственные 150 мс ограничивали
  также первоначальную навигацию Chrome. Сохранён отказ до Send; неуспешный
  общий прогон остановлен только для собственного pytest, а исполнитель
  завершил остальные этапы и записал FAILED. Полного JUnit и исходного
  traceback этого остановленного прогона нет; его успешная приёмка не заявляется.
- Отдельный синтетический пример с корректным редактором через 400 мс
  воспроизвёл отказ при 150 мс до отправки. Два положительных image-теста
  теперь используют штатный бюджет готовности. При той же задержке сценарий
  проходит с одной имитацией Send и прежней проверкой повторного вызова.
  Все assertions, production-предел 30 секунд, проверки прав до Send и короткие
  отрицательные проверки идентичности канала сохранены.
- Оба браузерных модуля целиком: 193 passed, без failures/errors/skips.
  Полный Ruff lint и git diff --check прошли; отдельное ревью подтвердило
  изменение только четырёх setup statements и сохранность всех 14 assertions.
- Оба обязательных полных прогона после правки стенда выполняются заново
  по новому предварительному снимку исходников. Прежние 5113 passed и пять
  платформенных skips PostgreSQL-прогона относятся к прежним байтам теста
  и сохраняются как история. Linux QA, установка и первая доставка 0.11.11
  ещё не подтверждены; действующая VM остаётся на 0.11.10.

## 0.11.11 — 2026-11-01: ограниченный бюджет полного локального pytest

- Последовательный PostgreSQL-прогон на текущем Windows-ПК дошёл до 99%,
  но завершился TimeoutExpired после 3600 секунд; семь остальных этапов прошли.
  Это незавершённая проверка, её исходный failed-отчёт сохраняется.
- Только этап полного pytest получает ограниченный бюджет 7200 секунд.
  Остальные этапы сохраняют прежние бюджеты; выбранный timeout_seconds теперь
  записывается в JSON каждого выполненного этапа, включая ошибку и таймаут.
  Проверки, состав suite и публикационный интервал 3600 секунд сохранены.
- Виртуальная полная оркестрация воспроизвела прежний отказ для успешной suite
  длительностью 3601 секунду. После правки она проходит; suite на 7201 секунду
  по-прежнему завершается TimeoutExpired с failed-отчётом, а независимый diff
  выполняется после отказа. Все 27 целевых проверок test_check.py прошли.
- Версия 0.11.11 ещё не выпускалась. Успешные полные общая и PostgreSQL-проверки
  после изменения бюджета, развёртывание и доставка MAX остаются отдельными
  незавершёнными проверками; целевые синтетические тесты их не заменяют.

## 0.11.11 — 2026-11-01: сверка MAX после Send не требует редактора публикации

- Подтверждение после reload и native reconciliation проверяют точные URL и
  единственный заголовок канала в прежнем ограниченном бюджете, без требования
  готового редактора. Отсутствие или временная недоступность composer больше
  не прерывает чтение уже опубликованного сообщения до проверки receipt.
- Проверка права отправки остаётся включённой по умолчанию, при входе в канал
  и перед единственным Send. navigate=False сам по себе её не отключает.
  Чужой URL, неправильный/неоднозначный заголовок и отсутствие входа блокируют
  также режим чтения. Exact text/image/bold proof и защита unknown не ослаблены.
- Регрессия на прежнем коде: четыре illustrated-сценария воспроизвели unknown
  после reload и отказ reconciliation без composer. После правки 14 целевых
  проверок прошли, включая два формата изображения и отрицательные проверки
  идентичности канала. Синтетические HTML полностью перехватывают запросы;
  повторных Send, доступа к действующему профилю и живой проверки MAX нет.
- Обязательные общие и PostgreSQL-проверки выполняются отдельно; их итог
  фиксируется после завершения. Причина неготовности composer на живом сайте
  этим исправлением не устанавливается. Код локальный, развёртывание и
  reconciliation действующего неизвестного исхода этим изменением не выполнялись.

## 0.11.10 — 2026-11-01: существующая доставка сверена без повторного Send

- После отправки проверка готовности редактора при reload прервала чтение
  receipt. Два свежих reload MAX подтвердили одно точное сообщение: изображение
  перед текстом, жирный заголовок, обычное тело и отсутствие удалённых строк.
  Причина временной неготовности редактора отдельно не установлена.
- Одноразовая native reconciliation зафиксировала существующую доставку:
  Postiz PUBLISHED/verified, одна физическая попытка. Сохранены исходный unknown,
  raw, backup и история; повторный Send, новый dispatch или DRAFT не создавались.
  03:29:58 МСК — время фиксации сверки; точное физическое время доставки
  не установлено. Полный последующий readback подтвердил неизменный журнал.
- VM самостоятельно пополнила резерв до шести статей с закрытыми claims.
  Read-only сверка проверила сохранённые native bindings источников, прав,
  машинной проверки, traces и изображений; pending учитывается отдельно.
  Полная смысловая приёмка и повторная загрузка HTTP-доказательств не заявляются.
- Рабочий runtime остаётся 0.11.10/RUN89. Правка 0.11.11 пока локальная:
  два параллельных полных прогона завершились по часовому таймауту pytest,
  остальные обязательные этапы прошли; запущен последовательный повтор.
  Установка ожидает успешных обязательных проверок и безопасного окна.

## 0.11.10 — 2026-10-31: первая плановая доставка после обновления подтверждена

- Серверный слот 10:04:23 МСК фактически доставлен в MAX в 10:04:38,833 МСК:
  Postiz PUBLISHED/verified, одна физическая попытка, интервал от прежней
  доставки 3616,172 с. Оператор не вызывал Send и не создавал новый dispatch.
- Независимые два reload MAX дали counts [1,1], точный текст, фото перед ним,
  жирный заголовок и обычное тело; обе исключённые строки отсутствуют,
  publication journal неизменен. SHA readback:
  `b29829b1008f6ee425a438f5d553112948435e20ffcb5cf4c5afc5be10f56d60`.
- Отдельная read-only сверка подтвердила всю source/rights/translation/quality/
  exact approval/Postiz/image/delivery цепочку на runtime 0.11.10; повторной
  отправки нет, изображение совпало с исходными bytes. SHA полного отчёта:
  `84a4faf8f4f27030d024f0ba349c56eb66d89b0bbb079562c96894c390edd624`.
  Машинное одобрение не объявляется ручной гарантией качества перевода.
- После назначения следующего слота на 11:04:39,833 МСК резерв составляет
  пять ready, pending отдельно. Это штатный остаток выше low=3; ручное
  пополнение не запускалось. Ошибок, active claims, owners и mutation leases
  нет. Постоянный контроль остаётся активным; текущий здоровый проход не
  требует правок кода приложения и не ослабляет проверок.

## 0.11.10 — 2026-10-31: постоянный операционный контроль расписания и резерва

- По прямому поручению владельца создан активный контроль текущего чата каждые
  пять минут: worker/API, слоты, доставка Postiz/MAX, reserve/claims/trace и
  leases. Публикациями и подготовкой продолжает владеть VM; прежние max и
  max-postiz оставлены PAUSED. Здоровые неизменные проходы не создают сообщений.
- Инструкция оператора сохранена в prompts/MAX_OPERATIONS_MONITOR.md:
  проверенный новый delivery сверяется один раз, с двумя reload MAX и точным
  текстом/изображением/форматом. 4–5 готовых статей считаются штатным запасом;
  pending учитывается отдельно, refill 6/3/12/3 не запускается вручную.
- Подтверждённый дефект исправляется через воспроизведение, регрессию,
  обязательные checks, независимое ревью, Git и backup/CAS. Обслуживание не
  прерывает active claims/sending/unknown/leases и требует due более 300 секунд.
  Неизвестный исход не снимается повтором Send или сбросом слота.
- Стартовая read-only проверка: 0.11.10/RUN89, шесть ready и один pending,
  активных claims/owners/leases и ошибок нет. Последние четыре доставки
  PUBLISHED/verified, по одной попытке, интервалы больше 3600 секунд.
  Конфигурация контроля проверена штатным инструментом; diff проверен отдельно.
- Это операторская конфигурация Codex и документация, код приложения и
  развёрнутые policy/model pins не менялись. Новый Git source содержит 494
  файла; прежний архив 5e44584/493 и runtime127 остаются exact baseline VM.
  Операторский prompt автоматически на VM не переносится. Приватное состояние
  контроля хранится в ignored data. Локальный контроль требует работающего
  Codex и доступного SSH/VPN; серверный почасовой цикл от него не зависит.

## 0.11.10 — 2026-10-31: независимая сверка истории после возобновления

- Read-only SQL/file verifier подтвердил точные полные payload и порядок всех
  201 записей. Проекция всех 37 таблиц совпала: допустимы только startup,
  native start, refence прежнего pending и его штатное возобновление с четырьмя
  точными Audit. Token/due/snapshot/approval сохранены. Исходная отменённая
  попытка 01:24 остаётся единственной unknown-попыткой, без повтора.
- Совпали SHA всех 26395 файлов исходников статей, raw/trace, изображений,
  model requests и журнала публикаций, а также 46 файлов конфигурации/политик.
  Runtime127, metadata 0.11.10, PID после одного restart и шесть готовых
  статей подтверждены. VM/SQL/HTTP/Send изменений у verifier нет.
  SHA отчёта: `b1654fda6ea52c81857297af31828f9f2247879c41c213195074dd6bced34d15`.
- Первая проверка ожидала побайтного равенства также Chrome-профиля и
  остановилась после разрешённых MAX reload. Исходный failed-отчёт сохранён.
  V2 отдельно фиксирует 49 изменений только настроенного browser profile;
  модели/raw/trace и publication journal остаются строгими. Кэш, IndexedDB,
  browsing history и prefs Chrome не объявляются неизменными. Негативные
  проверки повреждения/исчезновения model raw и изменения журнала блокируют.
- Проверенный код после Windows/Linux QA не менялся; в финальном исходном
  архиве разрешены только пять новых MD поверх QA29, остальные 488 файлов
  сохраняются побайтно. Новая фактическая отправка 0.11.10 на этом срезе
  ещё не выполнена; pending на 10:04:23 МСК остаётся отдельным от резерва.

## 0.11.10 — 2026-10-31: установлена на VM, почасовой цикл RUN89 возобновлён

- После закрытия всех активных claims и пополнения до шести статей выполнены
  native pause87→88, backup/CAS и установка 22 публичных файлов. Package/source
  metadata согласованы: 0.11.10. Все runtime127 соответствуют проверенной QA29.
  Backup: `reserve-1793428131597392153`; SHA его полного индекса:
  `59342a98209c9604f71917f0450c795678cde8aa5c838d3dfe145bef0442dc6d`.
- Установка и один перезапуск сохранили все 201 запись и полный снимок 37
  таблиц, claims, исходные ответы/trace, политики и профиль моделей. Перезапуск
  измерен как 9,469 с, добавлен один ожидаемый startup Audit. SHA установки:
  `ecce279f8a2ba536875491ebb8a39cd9ef7c2786077f8f8c7c1c5e0bd3db3541`;
  SHA restart receipt:
  `6dc5c68aad77bd0c6778d157da17a409e72de09c453143fbe87bf828adc6d51c`.
- Native start88→89 возобновил серверный цикл. Резерв — шесть quality_ready,
  pending отдельно назначен на 10:04:23 МСК. Текст, изображение, snapshot,
  approval и due сохранены; повторный POST, ручной Send, смена политики и
  восстановление старых отменённых/unknown источников не выполнялись.
  SHA native start:
  `4d849f7efa1421cbd87c8f199d343d53249280407092f634e1cc20f106d13bb5`.
- На установленной 0.11.10 выполнены два reload MAX для прежнего поста
  09:04:22: counts [1,1], точный текст, изображение перед ним, жирный заголовок,
  обычное тело и отсутствие удалённых строк; журнал неизменен. SHA readback:
  `35f0a46e8c3b60d0fb446f2c529b4ee849ef078af89fcda9fa5042b708aeca31`.
  Этот пост отправлен до обновления; новая отправка 0.11.10 на данном срезе
  ещё не выполнялась и не объявляется подтверждённой доставкой.
- Проверки: полный Windows/PostgreSQL check — 5089 passed/5 платформенных
  skips; дополнительная Linux/PostgreSQL QA29 — 1155 passed/0 skips в 21
  модуле, включая все пять пропущенных сценариев. Наборы не суммируются.
  Полный Linux unified check, mypy/pyright и удалённый CI не заявляются.
  Правила, промпты и вся история сохраняются в исходном архиве; приватные
  отчёты, credentials, cookies и runtime backup остаются вне Git.

## 0.11.10 — 2026-10-31: Linux QA29 и проверка процедуры обновления

- Изолированная Linux/PostgreSQL QA29 завершена: 1155 passed в 21 модуле,
  0 failed/errors/skipped. Выполнены все пять pipe/symlink-сценариев, которые
  пропускались на Windows. Проверены изменённые модули, browser transport,
  atomic reserve delivery и восстановление доказанного отказа до Send.
  SHA отчёта: `e4965eb9c7e4aa2be31543de6bfed32a5c178a390906bd0ade5042dc3fbba189`.
- Все 493 файла из committed source проверены по manifest/archive; staged
  source и рабочие runtime127, конфигурации, политики и metadata 0.11.9
  сохранили hashes. Тестовая БД удалена. Бизнес-данные production штатно
  менялись серверным циклом; их равенство до/после QA не заявляется.
- Полная Windows/PostgreSQL проверка 5089 passed/5 skips остаётся отдельным
  результатом для тех же 368 файлов кода. Наборы пересекаются и не суммируются.
  Linux QA29 является целевой проверкой, не полным Linux unified check.
- Отдельные приватные регрессии процедуры обновления подтвердили запрет
  неполного JUnit, отсутствующих платформенных тестов и удаления/дублирования
  старого Audit. Backup/CAS сохраняет 17 существующих изменяемых файлов и
  ровно пять отсутствующих. Эти проверки не означают выполненный deploy.
- Перед установкой в MAX два reload подтвердили пост 09:04:22 МСК под 0.11.9:
  один точный текст с изображением и жирным заголовком, журнал неизменен.
  Установка 0.11.10 ожидает завершения текущей подготовки; live Send новой
  версии и изменения production этой записью не заявляются.

## 0.11.10 — 2026-10-31: сохранение релиза и объединение истории

- По прямому поручению владельца исходные 21 изменения 0.11.10 сохранены
  отдельным commit и отправлены в GitHub. История установленной 0.11.9,
  приёмки шести статей и фактической доставки объединена без удаления
  прежних записей; правила AGENTS и промпты сохраняются в исходниках.
- Независимое ревью подтвердило SHA успешного полного отчёта, все 368 SHA
  проверенных файлов и JUnit: 5089 passed, 5 платформенных skipped, 0 ошибок.
  Изменений проверенного кода после QA нет. Дополнительно проверены 493
  публичных файла по семи сигнатурам секретов и запрещённым путям: находок нет.
- Готовится отдельная Linux-проверка пропущенных pipe/symlink сценариев и
  установка с backup/CAS. На этом этапе 0.11.10 ещё не развёрнута; действующий
  серверный цикл и его очередь продолжают работать на прежнем runtime.

## 0.11.10 — 2026-10-31: итоговая проверка процесса прошла

- Повторная `python scripts/check.py --postgres` завершилась кодом 0:
  5089 passed, 5 skipped, без failures/collection errors, один deprecation
  warning закреплённого Starlette/httpx. Pytest: 2721,02 с (45 мин 21 с).
  Package/locks, pip check, весь настроенный lint, scoped format, оба JS,
  изолированный Chrome и git diff --check прошли. Проверены 56 закреплённых
  distributions. Все 368 файлов снимка исходников сохранили SHA в течение QA.
- Пять skips имеют явные причины: три symlink-проверки требуют иных прав
  Windows, две проверки native pipes требуют Linux. PostgreSQL-тесты не
  пропускались. Отдельный локальный кластер PostgreSQL 17.6 штатно остановлен;
  рабочая VM, её БД, конфигурация и публикации не менялись.
- Отдельное ревью проверило возврат ненулевого кода, пустой/повреждённый JUnit,
  отсутствие инструментов/тестов, переход в собственную .venv, timeout,
  изоляцию БД, совместимость CAS mock и инициализацию benchmark adapter.
  Подтверждённые дефекты исправлены, регрессии и неуспешные прогоны сохранены.
  После полного прогона изменены только документация и итоговый отчёт.
- README/AGENTS/docs/CODE_CHECKS.md содержат команды подготовки, запуска одного
  теста и общей проверки, порядок ревью, критерии готовности и измеренную
  длительность полного набора. Версия исходников/metadata — 0.11.10.
  Ограничения: Linux/native symlink-проверки, live MAX/Postiz/модели/LT и
  type checker не подтверждены. CI отсутствует; старый formatter debt явно
  отделён от обязательной области. Deploy, commit и push не выполнялись.

## 0.11.10 — 2026-10-31: полный прогон обнаружил дефект стендового адаптера

- Общая команда с изолированным PostgreSQL завершилась ненулевым кодом:
  5081 passed, 5 failed, 5 платформенных skipped; pytest занял 2821,62 с.
  Остальные обязательные этапы прошли. Неуспешные JSON/JUnit/логи сохранены;
  временный PostgreSQL 17.6 штатно остановлен. Это не зелёная приёмка.
- Все пять падений воспроизведены на маршрутах benchmark: подкласс
  BenchmarkGatewayTranslator не инициализировал новые segment_chars/config_hash
  базового адаптера. Исправлена только инициализация стенда с прежними 1400
  символами; production-список моделей, сегментация и проверки не ослаблены.
  Добавлены три регрессии preflight до любого обращения к модели.
- Целевой набор benchmark/перевод/quality: 277 passed; Ruff для src/tests/scripts
  и отдельно изменённого benchmark_articles.py прошёл. Требуется новый полный
  прогон общей команды, он запускается на том же изолированном принципе.

## 0.11.10 — 2026-10-31: единая воспроизводимая проверка исходников

- Добавлена `python scripts/check.py`: собственная .venv текущего checkout,
  проверка package/lock версий, pip check, полный Ruff lint, явная область
  форматирования, синтаксис обоих JS, изолированный Chrome, полный pytest и
  git diff --check. Этапы имеют статусы, длительности, отдельные логи и JSON/JUnit
  в уникальном игнорируемом каталоге. Ошибка, отсутствие тестов/инструмента,
  timeout или повреждённый отчёт не становятся успешным результатом.
- scripts/test.ps1 делегирует общей команде с сохранением exit code. README,
  AGENTS и docs/CODE_CHECKS.md описывают подготовку по четырём locks, отдельный
  тест, изолированный PostgreSQL, ревью, критерии готовности и границы проверки.
  Версии pyproject/__init__ согласованы. Production не развёртывалась.
- Воспроизведён и исправлен неявный выбор БД двумя PostgreSQL-фикстурами:
  runtime .env.local и прежний приватный fallback больше не читаются. Нужен
  явный TEST_DATABASE_URL; общий check передаёт его только с --postgres,
  требует отдельную test_*/*_tests БД и не выводит URL/credentials.
- Все 55 исходных ошибок Ruff устранены без отключения правил: составные
  инструкции тестов разнесены, fixture imports заменены pytest_plugins,
  zip получил strict=True, неиспользуемое имя переименовано с сохранением
  обязательного чтения payload. Ожидания публикации/прав/unknown не ослаблены.
- Исходный полный pytest обнаружил устаревший positive mock CAS guard:
  ему недоставало обязательных DRAFT/group/integration bindings. Фикстура
  приведена к действующему контракту; добавлены три отрицательных проверки,
  production TypeScript guard не менялся. Модуль: 20 passed.
- Целевые проверки: 555 passed/один Windows symlink skip; отдельно 25 тестов
  общей команды passed. Наборы пересекаются, результаты не суммируются.
  Оба регрессионных теста изоляции БД сначала воспроизвели ошибку, затем прошли.
  Ruff, scoped format, pip check и синтаксис JS успешны.
- Первый итоговый прогон остановлен для подтверждённой правки фикстуры;
  его failed-отчёт сохранён, отдельный локальный PostgreSQL штатно остановлен.
  Новый полный прогон с изолированным PostgreSQL 17.6 выполняется; итог будет
  записан отдельно. Live MAX/Postiz/модели/LanguageTool здесь не проверяются.
  CI отсутствует; type checker не вводился. Старый долг форматирования
  (исходно 213 файлов) вынесен за явно заданную область нового formatter gate.
## 0.12.3 — 2026-11-01: независимые бюджеты загрузки MAX fixture и проверки DOM

- В отрицательных тестах channel readiness подставная страница однократно
  загружается до начала измерения классификации. Загрузка fixture получает
  5000 ms, проверка DOM сохраняет 150 ms и общий assertion меньше 2 секунд.
  По-прежнему требуются ровно один canonical GET, отсутствие отправок и
  неизменность чужого черновика. Production browser_runtime.py не менялся.
- Проверки: все 22 теста модуля прошли в чистом окружении; независимый review
  подтвердил сохранность положительных и HTTP-error navigate=True сценариев.
  Повторный GitHub CI выявил ровно один такой fixture timeout в Windows shard;
  полный Linux-набор, PostgreSQL, quality/Node и три других Windows shard прошли.
  Этот запуск не выдаётся за успешный полный CI. Для merge нужен новый полный прогон.
- Ограничения: исправление стабилизирует синтетическую проверку под нагрузкой,
  не меняет таймауты/права/повторы реальной публикации и не проверяет рабочую VM.
  Внешних действий и живых проверок Пикабу нет.

## 0.12.3 — 2026-11-01: исправления первого CI объединённой ветки

- Windows shard runner добавляет текущий каталог в Python import path перед
  pytest, как обычный python -m pytest. Namespace helpers доступны при прямом
  запуске scripts/ci_pytest_shard.py; алгоритм распределения, hashes, receipts
  и фактический pytest exit code сохранены. Новая регрессия использует только
  синтетический scripts/helper.py, без площадок, БД и runtime-учётных данных.
- В отрицательном UI-тесте fixture-публикации URL разбирается через URL и
  сравнивается по точным origin/pathname вместо substring-проверки. Реальный
  renderer не менялся, CodeQL suppression и ослабление правил не добавлялись.
- Проверки: все 24 shard/receipt теста и 134 Node mock tests прошли. Первый
  GitHub CI выявил ошибку collection на Windows и CodeQL high в тестовой
  проверке; эти результаты сохранены как неуспешные, а не заменены локальным
  passed. PostgreSQL и quality/Node jobs того запуска прошли. Для merge нужен
  свежий обязательный CI и CodeQL точного исправленного commit.
- Ограничения прежние: рабочий сервер не обновлялся; внешних публикаций и
  живых действий на Пикабу нет. Проверки выполняются на временных fixture-данных.

## 0.12.3 — 2026-11-01: объединение четырёх маршрутов с main

- Объединены разработка 0.12.2 и исправления безопасности/CI из main 0.11.10.
  Сохранены маршруты MAX/Telegram/VK, прежний код Пикабу, ограничения согласования,
  unknown/lease, защита путей конфигурации и обновлённые dependency lock-файлы.
- Версии pyproject.toml и __init__.py согласованы на 0.12.3. История обеих веток
  сохранена; отдельная Android-monitor ветка в это слияние не включается.
- Разрешены конфликты benchmark adapter и строгого соответствия image chunks;
  тестовые фикстуры сохраняют актуальные CAS controls и проверки обеих веток.
- Файловый API защищает новые VK browser profiles наравне с MAX/OpenCode:
  добавлены 14 синтетических проверок приватных namespace, symlink и импорта.
- CI Node runner проверяет также production build contract с его замороженной
  fixture; сборка Docker и внешние публикации этими mock-проверками не запускаются.
- Проверки объединённых исходников в новом изолированном venv с pins main:
  761 passed, 10 skipped в security/CI/benchmark/CAS/source-наборе;
  730 passed, 3 skipped, 1 deselected в Telegram/VK/supervisor-наборе.
  Пропуски относятся к Windows symlink/Linux cgroup и отсутствию тестовой
  PostgreSQL. Ruff, 55 dependency pins, pip check и wheel/sdist build прошли;
  все 134 Node mock tests прошли. Полный GitHub CI обязателен перед merge.
- Позднейшая живая приёмка Telegram 1 ноября подтвердила один пост с фото
  1400×1000 и подписью 450 символов после reload. Это отдельное browser evidence;
  API verified=false сохранён. В рамках слияния новых публикаций не выполняли.
- Ограничения: слияние не развёртывает релиз на рабочей VM и не переносит сессии,
  БД или токены. Серверная приёмка/TLS и общий контракт будущих изображений VK
  остаются отдельными задачами. Пикабу не включается, внешних действий нет.

## 0.12.2 — 2026-11-01: native Docker и изображение VK приняты в копии

- Полный Next прошёл webpack, TypeScript, page data, static generation,
  optimization и traces. Финальный образ 0.12.2-full-readiness-20261101-r4
  использует проверенные native Prisma/Nest/Next layers и новый origin guard;
  фактическое повторное использование всех трёх тяжёлых RUN подтверждено.
  Проверены 22 артефакта, восемь overlays и SHA шести изменённых source-файлов.
- Native frontend запускается readonly с сетью только lo, защищённый editorial
  route перенаправляет на auth. Dummy logout с пустым NOT_SECURED выдаёт Secure
  и HttpOnly; штатный SIGTERM проверен. Backend, БД, default CMD и публикации
  Docker-проверками не запускались; после них 0 контейнеров/volumes.
- Существующий пост VK с одной картинкой подтверждён стандартным read-only
  reconcile: два независимых чтения точных текста/автора/asset/Original,
  published и verified=true. Отдельное решение владельца для фактической
  картинки сохранено, original_policy_satisfied=false. Snapshot, первоначальный
  approval и одна попытка отправки неизменны; повторной публикации нет.
- Итоговый frozen-набор: 611 passed, 3 skipped, 2 deselected, 1 warning за
  450.24 с; исходники неизменны. Ruff и pip check прошли, Node contracts —
  89 passed. Native Linux-supervisor ранее принят: восемь сценариев и 29 тестов;
  его source SHA остался прежним. Полного нового прогона Пикабу/проекта нет.
- Версии pyproject/init и активных editable-метаданных согласованы на 0.12.2;
  старый игнорируемый кеш метаданных сохранён локально. Релизный набор формируется
  из чистого commit с AGENTS и полной историей CHANGELOG; отдельный manifest
  связывает source/артефакты с QA, Linux, Docker и live-readback evidence.
- Telegram: один текстовый пост доставлен и независимо прочитан в канале;
  изображения не проверялись, API verified-флаг не подменялся. Пикабу на паузе,
  его unknown сохранён. Все адресаты копии на паузе, send/mutation lease нет;
  финальные БД и журналы сохранены приватным backup.
- Ограничения: image имеет origin только локального стенда; другой public
  адрес требует пересборки. TLS transport, backend login/OAuth и полный стек
  с БД не принимались. VK live-профиль проверен на Windows; авторизация на VM
  не переносилась. Рабочий сервер и исходный checkout не обновлялись.

## 0.12.2 — 2026-11-01: связь экранного превью с точным оригиналом VK

- Read-only проверка нативного Original в профиле обработчика подтвердила
  точные SHA, размер и crop уже согласованного владельцем файла. Просмотрщик
  при этом загружает уменьшенное превью; прежнее дополнительное требование
  одинаковых natural dimensions безопасно останавливало подтверждение.
- Только для отдельно принятой фактической картинки проверяется наблюдённый
  responsive-вариант того же URL: единственное различие — один canonical cs,
  без повторных query keys, смены адреса, crop, увеличения или нарушения
  пропорций. Это проверка экранного представления, не перцептивное сравнение
  и не допуск изменения оригинала: его SHA/размер/crop проверяются точно.
  Обычная медиаполитика, snapshot и исходное согласование не меняются.
- 66 pure/unit проверок, включая 19 новых вариантов, и один browser case с
  двумя readback прошли. Ruff, diff check и независимый review без новых
  блокеров. Сохранённые данные настоящего просмотрщика также прошли helper.
  Новый общий frozen-прогон выполняется отдельно; прежние 591 passed
  сохранены для предыдущей версии исходников.
- Реальный probe сделал один GET Original, не менял журнал/БД и закрыл профиль.
  Новых публикаций нет; сверка существующего поста ещё выполняется. Пикабу
  остановлен, рабочий сервер не изменялся.

## 0.12.2 — 2026-11-01: нативная ссылка VK без собственного layout box

- Read-only probe в штатном профиле подтвердил один native photo anchor
  размером 0×0 с одним видимым IMG. Прежняя проверка безопасно оставляла
  существующую отправку unknown до чтения оригинала; повторного Publish не было.
- Только для post допускается наблюдённая inline-ссылка «Фотография» без
  собственного box: ровно один непосредственный IMG, без дополнительных узлов,
  текста, opacity, transform или clip. Прежние проверки видимости/предков,
  containment, ID, caption, автора и оригинального файла сохраняются.
  Upload и preview этим вариантом не пользуются; исходная медиаполитика,
  options, snapshot, согласование и sealed upload не меняются.
- 14 native положительных/отрицательных проверок прошли; Ruff и diff check
  прошли. Новый общий frozen-прогон выполняется отдельно. Предыдущие 577 passed
  относятся к исходникам до этой правки и сохранены без подмены.
- Probe не менял журнал, БД или содержимое сайта; профиль закрыт. Сверка
  существующего поста ещё не принята; Docker/Next проверяется отдельно.
  Пикабу и рабочий сервер не затронуты.

## 0.12.2 — 2026-11-01: точная сверка VK и исправление native Next recipe

- Image caption VK проверяет наблюдённый native DOM и сокращённые подписи
  ссылок через точный адрес штатного away redirect. Произвольные названия,
  скрытые вставки и другой URL не принимаются; прежний текстовый preset не меняется.
- Отдельное принятие владельцем фактической картинки существующего поста
  хранится как Audit с source/actual SHA, размерами, crop box и точным approval.
  Только read-only reconcile использует это решение; проверяет его до/после
  worker и в финальной locked transaction. Отзыв, замена согласования и
  изменение sealed журнала блокируют подтверждение. Обычная медиаполитика
  без crop, исходные snapshot/upload и запрет повторной отправки сохраняются.
  Special evidence явно содержит original_policy_satisfied=false.
- После полной upstream source-проверки Next получает минимальную настройку
  serverExternalPackages для isomorphic-dompurify: его jsdom читает собственный
  штатный CSS с диска. Библиотека, sanitizer, типизация и минимизация не подменяются.
- Cookie mode production recipe выводится из canonical origin: HTTPS требует
  пустой NOT_SECURED, HTTP допустим только на literal loopback. Противоречивый
  или отсутствующий аргумент прямой Docker-сборки отклоняется. Финальная
  настройка выполняется после Next; адрес конкретного образа остаётся compile-time.
- Проверки: 47 pure/SQLite/protocol и 10 браузерных read-only сценариев VK passed;
  независимый review не оставил новых блокеров. Node build/UI contracts —
  89 passed. Общая frozen-регрессия и полный Docker image ещё проверяются;
  эти локальные результаты не объявляются живой приёмкой.
- Текстовый тест Telegram дополнительно независимо прочитан в Telegram Web:
  точный текст, жирный заголовок и время совпадают с API receipt. Исторический
  API verified-флаг не изменён; изображения Telegram не проверялись. Пикабу
  остановлен, рабочий сервер и исходный checkout не изменялись.

## 0.12.2 — 2026-11-01: итоговая frozen-регрессия VK

- Замороженный текущий набор VK, supervisor и configuration: 476 passed,
  3 skipped, 1 deselected, 1 warning за 464.51 с. Исполняемые файлы не менялись
  во время проверки. Ruff выбранных модулей/тестов и pip check прошли.
- Пропуски: один Windows symlink без привилегии и две native Linux cgroup/setsid
  проверки; Linux-supervisor ранее отдельно принят на настоящем Linux/Chromium.
  Один VK fixture-case с именем профиля Пикабу исключён по текущей паузе.
  Warning — deprecation внешнего Starlette TestClient, не отказ доставки.
- Это профильная регрессия, не новый полный прогон всего проекта. Пикабу не
  проверялся. Подготовлены локальные evidence и конфигурация image preset VK,
  прочие адресаты и прежние отправки сохранены. Реальный image Publish на момент
  этой записи ещё не выполнялся; Docker/Next продолжает отдельную сборку.

## 0.12.2 — 2026-11-01: отдельный контракт одиночного изображения VK

- Добавлен выключенный по умолчанию personal-image-v1 для своей личной стены:
  ровно один PNG/JPEG, явная политика сравнения и локальное evidence пяти
  наблюдённых областей UI с SHA полного DOM-контракта. Прежний personal-text-v1
  по-прежнему не принимает media; изображения не заменяются текстовой отправкой.
- Одни замороженные bytes проходят проверку SHA/формата и декодирование.
  Pixel identity учитывает ориентацию и цветовые metadata; повреждённые PNG,
  неподдержанные EXIF/HDR и некорректные metadata дают безопасный отказ.
  Для штатного пережатия отдельный native-asset-lineage-v1 честно сохраняет
  разные source/derivative SHA, требует исходные размеры и отсутствие crop.
- Durable upload связывает request, owner, source, snapshot и native photo ID.
  Preview проверяется по тому же CDN URL/bytes, двум наблюдённым IMG и transform.
  После одного Publish две загрузки стены проверяют caption, автора, photo ID
  внутри нужного attachment и оригинал из native viewer. Лишние/чужие вложения,
  другой размер и неверная связь asset/image блокируют подтверждение.
- Для неизвестного image-результата без ID добавлен только read-only discovery:
  sealed upload и один click-intent, уникальный exact post и прежние два полных
  readback до записи ID. Ожидание гидратации ограничено; смена sealed журнала
  не перезаписывается при успехе или ошибке. Известный ID не заменяется другим.
- Независимый review закрыл найденные замечания. До последнего дополнения
  новый набор дал 94 passed; после него 6 профильных проверок passed. Общая
  frozen-регрессия текущего кода идёт отдельно; эти числа не суммируются.
  Живой image Publish ещё не выполнялся. Пикабу и рабочий сервер не затронуты.

## 0.12.2 — 2026-11-01: согласованное наблюдение VK image UI

- С согласия владельца проверены нативные загрузка исходной схемы и предпросмотр
  точного текста. Зафиксированы реальный file input, upload photo ID, две
  картинки одного preview attachment, одинаковый CDN URL и native redirect
  ссылок. Photo ID в preview отсутствует; его доказательство не подменяется.
- Редактор закрыт без сохранения, Publish не нажат. Снимки, полный DOM,
  согласование и backup БД/конфигурации копии сохранены только локально в data.
  Все адресаты на паузе, активных send/mutation lease нет; прежний unknown
  и Пикабу не менялись. Рабочий сервер и исходный checkout не обновлялись.
- Это UI-наблюдение для доработки контракта, не живая приёмка обработчика.
  Полная Docker/Next сборка и публикация изображения VK ещё не приняты.

## 0.12.2 — 2026-11-01: локальная проверка доказательств VK media

- Doctor для personal-image-v1 проверяет полный DOM-контракт и целостность
  локального evidence через require_contract. Один media-флаг больше не даёт
  положительную локальную готовность; ошибки evidence выводятся безопасным
  кодом без пути, адресата и приватных деталей. Image preset ограничен своей
  личной стеной, как и прежний текстовый preset.
- Пять VK-only тестов прошли: отсутствующее/невалидное evidence, безопасный
  отчёт об успехе, запрет другого адресата. Browser, сеть, БД и Пикабу в этих
  тестах не использовались. Это проверка локальных предпосылок, не доставки.

## 0.12.2 — 2026-11-01: указатель на актуальную приёмку

- README и инструкция 0.12.1 связывают текущий протокол полной приёмки 0.12.2
  с сохранёнными историческими результатами. Явно отражены принятый native
  Linux-supervisor, незавершённые Docker/Next и VK media, остановка тестов Пикабу.
- Проверены относительные Markdown-ссылки и git diff --check. Это правка
  документации; новых внешних публикаций и изменений рабочего сервера нет.

## 0.12.2 — 2026-11-01: native-приёмка Linux-supervisor

- На отдельном Ubuntu 24.04.5 / WSL2 Linux 6.6.87 выполнены все 8 native
  сценариев: normal orphan/fork/setsid, timeout, blocked stdin, две UTF-8 pipes,
  held pipes, SIGINT, handled SIGTERM и реальный Chromium. Во всех случаях
  private cgroup и записанные PID/starttime очищены, отдельный sentinel сохранён.
- Chromium 153.0.8010.12 запускался non-root с sandbox; отсутствие внешней сети
  проверено фактическими interfaces=lo и network namespace до/после сценариев.
  Отказ без Delegate прошёл до Popen: 0 запусков дочернего процесса.
- Существующий модуль supervisor прошёл на Linux: 29 passed; Windows: 27 passed,
  2 Linux skipped. Исправлена только fixture nonzero-exit: она теперь читает
  stdin перед завершением, как требует протокол worker. Exit=7/stderr assertions
  и runtime-supervisor не изменены. Первоначальные collection/dependency и
  fixture-race отчёты сохранены, результаты не подменялись.
- SHA supervisor остаётся прежним. Изолированные тестовые службы, Chromium и
  их cgroup завершены; аккаунты, БД, публикации и production VM не использовались.
  Docker/Next и изображение VK на этом этапе ещё не приняты; Пикабу остановлен.

## 0.12.2 — 2026-11-01: отдельный Linux-стенд для полной приёмки

- По новому поручению подготовлен отдельный Ubuntu 24.04.5 WSL2 runtime с
  собственным ext4-хранилищем, systemd и non-root пользователем. Официальный
  Ubuntu Base проверен по SHA256 каталога Canonical; исходники перенесены
  из проверенного архива 0.12.1 без credentials, cookies, БД и профилей.
- В новой distro выключены Windows automount и interop. Default distro,
  Docker Desktop и общие настройки WSL сохранены. Рабочий сервер и исходный
  checkout не изменялись; все адресаты прежней копии на паузе, активных send
  или mutation lease нет, прежний unknown сохраняется.
- Подготовлен отдельный native harness с Delegate/PrivateNetwork, PID/starttime
  и sentinel-проверками. Его 14 локальных guard-проверок и Ruff прошли;
  native supervisor и Docker/Next build на этом этапе ещё не приняты.
- Установлены изолированные Python/Playwright/Chromium зависимости. Реальная
  публикация изображения VK ещё требует точного согласования и приёмки.
  По последнему поручению владельца тесты Пикабу остановлены полностью:
  его код, конфигурация, профиль, история и неизвестный исход не менялись.
- Версия согласованно изменена в pyproject.toml и __init__.py. Это начало
  следующего набора изменений, не заявление о завершённой живой интеграции.

## 0.12.1 — 2026-11-01: финальная проверка релиза четырёх маршрутов

- Полный frozen QA: 6018 passed, 1 failed, 8 skipped, 2 warnings, 3562.04 с;
  550 файлов, исполняемый код не менялся. Единственный failed — положительный
  fixture-тест author tracking VK: ACTION_AUTHORIZATION_EXPIRED до открытия
  редактора, Publish-intent=0, без external_id. Секундная защита не ослаблялась.
- Тот же тест и соседние строгие проверки ссылок, автора, паузы и медленной
  авторизации отдельно прошли на неизменном коде: 15 passed, 1 warning, 11.25 с.
  Итог принимается как полный прогон плюс перепроверка, 6019 уникальных passed;
  исходный полный failed-отчёт, журнал и hashes сохранены. Это не один чистый
  полный прогон. Обе реальные PostgreSQL sender/recovery проверки прошли.
- Пропущены четыре Windows symlink и четыре native Linux проверки. Отдельные
  наборы: 259 VK passed; 81 Node UI/build contract passed; Ruff, pip check,
  PowerShell/Bash syntax, установщики и локальный doctor прошли. Partial semantic
  TS/Nest/React дал 0 diagnostics, совпали 13 upstream pins; наборы не суммируются.
- README и эксплуатационная инструкция приведены к итоговой приёмке, содержат
  безопасную установку, отдельные dev/UI зависимости, границы нового supervisor,
  recovery и честные статусы доставки. Версия согласована в pyproject и __init__.
  Source ZIP, wheel и sdist проверяются по этому commit отдельным отчётом
  release-verification.json; приватные runtime, credentials и profiles исключены.
- Живая приёмка: Telegram опубликован и отдельно прочитан в канале; API receipt
  сохраняет verified=false. VK подтверждён штатным read-only reconciler после
  двух reload, attempts=1/Publish=1. Пикабу создал один тест, затем площадка
  удалила его как «Флуд»: current unknown/пауза, повтора не было. MAX сохраняет
  прежний transport без новой живой отправки. Все адресаты копии на паузе.
- Ограничения: новый Docker/Next image не собран и не запущен — daemon недоступен;
  native Linux supervisor на Windows не проверен; media VK/Пикабу остаётся закрытым
  до отдельной живой приёмки. Рабочий сервер и исходный checkout не изменялись.

## 0.12.1 — 2026-11-01: подтверждение существующего поста VK без новой отправки

- Все 259 профильных тестов VK прошли (включая 12 новых), 250.78 с; Ruff,
  diff check и независимый review — без блокирующих замечаний.
- Исправленный штатный read-only reconciler дважды загрузил стену и подтвердил
  существующий пост: published/verified=true, точный текст, автор и отсутствие
  изображения. Сохранены attempts=1 и Publish-intent=1; новых approvals, uploads,
  Publish и внешних изменений при сверке — 0. Остальные отправки не изменены.
  Все четыре адресата после сверки остаются на паузе.
- Пикабу остаётся unknown после удаления площадкой; его не отправляли и не
  сверяли повторно. Telegram отдельно подтверждён в канале, его API receipt
  сохраняет verified=false. MAX сохраняет прежний transport без новой отправки.
- Начат новый полный frozen QA по 550 файлам в отдельной PostgreSQL test DB;
  окончательные counts и архивы этого набора будут записаны отдельно.
  Рабочий сервер, исходный checkout и его runtime этим набором не изменялись.

## 0.12.1 — 2026-11-01: ожидание загрузки опубликованной записи VK

- После чистого frozen QA (6007 passed, 8 skipped, 2 warnings) в согласованное
  время 09:26 МСК выполнен один Publish VK. Запись с точным текстом без изображения
  отдельно видна в браузере; отправка возвращена на паузу. Автоматический readback
  первоначально сохранил unknown, не повторяя Publish.
- Read-only диагностика воспроизвела причину: готовая шапка и CTA появляются при
  нуле постов, примерно через секунду загружается ровно одна точная запись.
  На каждой из двух reload добавлено ограниченное ожидание global exact matches.
  Неверный ID и дубли блокируются сразу; аккаунт проверяется каждый poll.
  Затем заново выполняются прежние полные text/author/link/media и before/after
  проверки. Ожидание не заменяет доказательство и не создаёт новую отправку.
- Новые 12 read-only регрессий прошли за 18.20 с, Ruff и независимый review —
  без блокирующих замечаний. Весь набор VK и новый полный QA этого исправления
  на данном этапе ещё проверяются; прежний clean QA относится к старым hashes.
- Уточнена область нового Job/cgroup и generation fencing: VK/Пикабу; MAX
  сохраняет прежний transport. Инструкция явно фиксирует удаление теста Пикабу
  с причиной «Флуд», текущий unknown/паузу и отсутствие повтора; README отделяет
  историческую приёмку 0.12.0 от текущей 0.12.1. Указаны Browser-флаги установки
  Python-зависимостей, отдельно от самого Chrome.
- В focused test runner добавлена проверка native tags Пикабу; новые VK-тесты
  включаются существующим glob. PowerShell parse и diff check прошли.
  Рабочий сервер не изменялся; финальный архив ещё не собран.

## 0.12.1 — 2026-11-01: исправления по результатам первой живой приёмки

- В согласованном проходе 08:00 МСК Telegram принял единственный пост; точный
  текст и настоящее жирное начертание отдельно прочитаны в канале. Bot API
  receipt сохраняет verified=false, независимое наблюдение не подменяет этот флаг.
- Пикабу создал единственную запись с точным текстом и авторскими тегами,
  автоматически добавив форматный «Текст». Введён отдельный versioned readback
  contract: exact declared tags либо exact declared + один «Текст» только для
  media=[]; остальные лишние/пропущенные/повторные теги запрещены. Declared и
  effective hashes проверяются адаптером отдельно, snapshot/approval/intent не
  переписываются. Официальное описание: https://help.pikabu.ru/article/39889.
  70 проверок, Ruff и независимый review прошли.
- Позднее Пикабу удалил тестовую запись с причиной «Флуд». Исторический HTML
  и первый exact capture сохранены приватно; текущая read-only сверка получила
  недоступную страницу, Dispatch остаётся unknown и на паузе. Новый пост,
  повтор Publish, изменение текста и обход модерации не выполнялись.
- VK остановился до Publish: фоновая CTA обновляется при открытой форме.
  Теперь только эта неактивная кнопка исключается из mutation stamp при одной
  видимой форме. Аккаунт, URL, сама форма, текст, настройки и deadline сохраняют
  проверки; до открытия формы CTA по-прежнему обязательна. Три регрессии прошли,
  реальная защищённая подготовка exact текста/Story=false прошла без Publish.
- Новые VK journals сохраняют startup worker SHA и последний action intent.
  Для одного старого запроса отдельно проверены frozen 548 файлов, валидный
  монотонный Publish-intent=0, закрытый worker, точные SQL fences и две загрузки
  стены. Он завершён как failed/NOT_SENT_PROVEN с аудитом и сохранением старого
  approval/journal/request; это не автоматическое разрешение повторной отправки.
  35 частных unit/SQLite проверок и независимый review прошли.
- Прежний полный QA: 5972 passed, 1 ресурсный timeout до первого Create-click,
  8 skipped, 2 warnings. На тех же 548 файлах отдельно прошли 9 исходных
  negative/deadline/pause проверок; полный запуск не переименован в чистый.
  После описанных исправлений требуется новый полный QA текущих исходников.
  Все адресаты на паузе, рабочий сервер и исходный checkout не менялись.

## 0.12.1 — 2026-11-01: ожидание native SPA и поиск кандидата VK

- Исправлены переходы после открытия редактора, перед обязательным «Далее»
  личной стены и после Story toggle: ограниченные read-only ожидания готовности
  не заменяют последующие context/metadata/DOM проверки и fresh authorization.
- На настоящей стене обнаружены старые записи с другим rich HTML, которые
  блокировали поиск нового поста. Для известных renderer v1/v2 добавлен только
  консервативный предварительный отбор по подпоследовательности символов raw DOM.
  Скрытые/вставленные данные не исключают возможное совпадение: такой кандидат
  по-прежнему проходит строгий renderer, exact text и независимый readback.
  Другие контракты не меняются; слишком большой raw текст блокируется.
- 9 regression случаев SPA/pause и 14 history/filter случаев прошли локально.
  Настоящая форма VK прошла защищённую подготовку с точным текстом, Story=false,
  0 images/Publish/upload и 7 read-only SELECT 1 callbacks. Это fixture authority,
  а не проверка реального Dispatch/lease или доставки; возможен native autosave.
- Первый общий QA остановлен до завершения ради этих исправлений и не считается
  успешным frozen-прогоном. Полный актуальный VK и новый общий QA выполняются
  после фиксации исходников. Рабочий сервер и внешние публикации не затронуты.

## 0.12.1 — 2026-11-01: подготовка согласованной живой приёмки в копии

- После проверки отсутствия variants/approvals/dispatches и mutation owners
  сохранена приватная копия конфигурации. Только в изолированном runtime
  включены три согласованных текстовых адресата; MAX и фоновые задачи выключены.
  DOM-флаги VK/Пикабу основаны на проверке реальных форм; media остаётся закрыто.
- Подготовлены ровно три авторских варианта с согласованными текстом и posting
  metadata. Все три прошли локальный preflight, адресаты остаются на паузе.
  Создано 0 approvals, внешних публикаций и uploads не было.
- Владелец отдельно разрешил агенту выбрать точную будущую дату 1 ноября до
  10:00 МСК после успешной проверки кода. Сама дата и доставка на этом этапе
  ещё не назначены; общий frozen QA продолжается. Токены, профили и полный
  протокол остаются только в игнорируемых файлах копии. Рабочий сервер не менялся.

## 0.12.1 — 2026-11-01: recovery, управление версиями и эксплуатационные команды

- Expired sending переходит в unknown с очищенным owner и аудитом; read-only
  reconcile работает при общей паузе. Поздний ответ сохраняет только evidence/ID,
  не повышая unknown до published. Живые leases и чужие owners не сбрасываются.
- Добавлен последний cheap SQL fence в новой session после quality. Проверяются
  текущие config/app/binding/approval/пауза/lease; медиа и модели повторно не
  вызываются. Старый core при config/credential rotation останавливается до
  claim/cancel, сохраняя новые approvals. Browser parent/child связывает закрытое
  runtime generation; child держит один authority core и свежие SQL sessions.
- Панель больше не загружает сотню полных длинных bodies одним ответом:
  fixed страницы по 50, фильтр unresolved, точный read-only details с полным
  текстом/media/sources/metadata. До загрузки exact версии approval недоступен.
  CLI получил details и локальный doctor без БД/сети/автосогласования.
- Добавлены безопасные установщики новой копии; legacy compose-команды
  блокируются isolation marker до обращения к Docker. Новый fresh recipe
  использует pinned official OCI, текущие MAX/guard/editorial sources и полные
  backend/orchestrator/frontend builds. Docker/Buildx remote overrides и
  перезапись существующего tag запрещены; runtime deployment не выполняется.
- Проверки отдельных наборов: recovery/Hub/routes 85 passed, authority/recovery
  35 passed; route details/API/evidence 42 passed; doctor 14 passed; installers
  4 passed; UI/strict TS 72 passed и production-build contracts 9 passed.
  Наборы пересекаются и не суммируются. Отдельно реальные TS/Nest/React types
  дали 0 diagnostics; совпали 13 upstream source pins.
- Ограничения: Docker daemon недоступен, полный Next/image build и новый
  стек не запускались. Живая отправка и общий frozen QA фиксируются отдельно.
  Рабочий сервер, исходный checkout, БД и история не менялись.

## 0.12.1 — 2026-11-01: браузерные действия и совместимость текущего VK

- В Пикабу воспроизведён старый разрыв: pause во время последнего DOM guard
  позволяла один fixture Publish. Теперь после длительных проверок выполняются
  fresh SQL, атомарный DOM/context/focus snapshot и native действие в общем
  окне не более секунды. VK использует тот же принцип, без дополнительных
  locator RPC после расчёта remaining timeout. Snapshot охватывает context
  между одинаковыми atomic captures; нестроковые error codes санитизируются.
- VK ждёт hydrated account href и поздний SPA editor. Собственный login,
  перенаправленный площадкой в feed, один раз возвращается на проверенную
  личную стену; чужой аккаунт не перенаправляется и не принимается.
- Добавлен opt-in personal-text-v1 с полным DOM-контрактом. Новый renderer
  vk-native-text-v2 поддерживает наблюдённые native inline/flow-root wrappers
  и spacing BR, сохраняя прежний v1. Сверяется полный логический текст DOM,
  а не обещание отсутствия стандартного сворачивания preview.
- Реальная форма VK показала pointer-events:none у input Story: отправитель
  нажимает проверенную native label без force/JS click и подтверждает false.
  Пресет явно текстовый, media-флаг не разрешает изображения. Пикабу inspect
  сообщает безопасные blank/structure/pending-tag признаки; неизвестный исход
  сохраняет только разрешённый failure_code, без private exception text.
- Проверки на этапах: VK 198 passed перед последними native-адаптациями;
  Pikabu 188 passed плюс отдельные 71 protocol/runtime cases; реальные owner
  DOM и точный согласованный Pika текст/три тега прошли offline final guard
  с 0 Publish/upload. VK online preparation после адаптации прошла exact text,
  Story=false и final guard с 0 Publish/upload; тестовый черновик закрыт.
  Окончательный общий QA текущих исходников выполняется отдельно.
- Ограничения: окно SQL→browser ограничено, но не атомарно между системами.
  Ни fixture, ни подготовленный черновик не являются доказательством доставки;
  media и завершающий перенос Пикабу сохраняют прежние ограничения.

## 0.12.1 — 2026-11-01: завершение собственного дерева browser worker

- Новый supervisor назначает Windows Job до передачи stdin и закрывает только
  собственное дерево при timeout/interrupt/выходе. Ошибки не содержат stdin,
  stdout или credentials. Browser callers VK/Пикабу используют его; прежний
  MAX transport не переносился на этот helper.
- На Linux простого killpg недостаточно: Playwright запускает Chromium с
  detached process group. Используется отдельная приватная делегированная
  cgroup v2 и cgroup.kill, fail-closed до stdin при отсутствии предпосылок.
  В копии source service unit содержит Delegate=yes; VM не изменяется.
- Проверки: Windows actual nested Job/потомок/посторонний sentinel/stdio и
  cgroup fixtures — 27 passed, 2 честных Linux skips; Ruff passed. Зависший
  приватный clone-probe отдельно отмечен aborted, не выдан за PASS. Его
  подтверждённое собственное дерево завершено без действий над чужими процессами.
- Ограничения: реальный Linux kernel cleanup здесь не исполнялся; doctor
  сообщает read-only hints, не факт создания cgroup. Нужны Linux 5.14+, cgroup
  v2 и delegation. Новый supervisor не разрешает повтор неизвестной отправки.

## 0.12.1 — 2026-11-01: подготовка эксплуатационных исправлений в копии

- На реальном профиле VK воспроизведена гонка готовности: account header
  доступен через 2,94 с, кнопка создания поста появляется через 5,73 с.
  Добавлено ограниченное ожидание редактора и раздельная диагностика доступного
  редактора и незавершённого DOM-контракта; inspect ничего не заполняет.
- Перед native действиями VK дорогостоящие проверки завершаются до свежей
  проверки полномочий. Затем состояние формы захватывается одним read-only
  DOM вызовом и сравнивается с проверенным снимком; callback и native action
  ограничены общим секундным окном. Утрата полномочий или изменение снимка
  блокируют действие. Это не атомарная транзакция между БД и сайтом.
- Проверки на этом этапе: независимый аудит и реальная read-only диагностика
  VK без внешних публикаций; regression, полная приёмка и сборка новой версии
  выполняются отдельно. Версия согласована в pyproject и package __init__.
- Ограничения: рабочий сервер не меняется, адресаты копии остаются выключены
  и на паузе. Прежние разрешённые даты внешних тестов истекли; live delivery
  этой записью не заявляется. Остальные эксплуатационные исправления и их
  проверки будут зафиксированы отдельными новыми записями.

## 0.12.0 — 2026-10-31: итоговая приёмка четырёх маршрутов в отдельной копии

- Реализованы Telegram Bot API, личная стена VK через собственный browser
  transport и полностью автоматический браузерный маршрут Пикабу. Сохранены
  прежний MAX, exact согласование версии/даты/адресата, durable claims,
  mutation lease, история и запрет повторения неизвестного результата.
  Добавлены изолированный CLI, авторский ввод, owner API и управление в Postiz.
- Финальный frozen Python QA: **5781 passed, 6 skipped, 4 warnings**,
  2458,82 с, собственная PostgreSQL test DB и четыре pytest worker. Все 530
  файлов сопоставлены до/после; исполняемых изменений во время прогона нет.
  Четыре skip вызваны недоступной Windows symlink privilege, два — Linux pipe
  selector; warnings относятся к deprecation Starlette/httpx TestClient.
- После UI-only исправления inspect отдельно прошли **67 Node/UI проверок**
  со strict semantic TypeScript. Python и конфигурация побайтно совпадают с
  полным QA. Ruff, pip check и diff check прошли; прежние неуспешные прогоны
  сохранены и не названы успешными. Дополнительные наборы не суммируются.
- В процессе общей проверки исправлены прежний benchmark-конструктор
  GatewayTranslator (segment/config identity), устаревшая CAS fixture и
  механические замечания Ruff. Production quality/approval guards не ослаблены;
  Windows rename исправлен отдельно с проверкой отсутствия повторных запросов.
- Default PowerShell startup проверен: только состояние, без worker. В собственной
  runtime БД четыре выключенных адресата на паузе, ноль Variant/Approval/Dispatch
  и mutation leases. Чистая распаковка smoke source ZIP прошла init/status без
  сети, обращений к исходному checkout и записей вне новой копии. В runbook
  добавлена установка окружения Python 3.12 после распаковки.
- Smoke-упаковка текущего Python-кода сохранила все 530 исходных файлов в sdist
  и 89 runtime-модулей в wheel; CRC, manifest hashes и проверка фактических
  локальных секретов/DSN прошли без совпадений. Финальные dist собираются из
  зафиксированных исходников после этой записи с отдельными SHA256 и проверкой.
- Живые границы: созданы бот и закрытый Telegram-канал, проверено право доступа;
  VK наблюдался только во встроенном браузере, вход отдельного отправителя не
  подтверждён; Пикабу прошёл реальный offline editor-контракт. Внешних текстовых
  постов не было: требуется согласование владельца. Media новых браузерных
  маршрутов остаётся закрытым; Docker daemon недоступен, живой стек Postiz
  копии не запускался. Рабочий сервер и исходный checkout не изменялись.

## 0.12.0 — 2026-10-31: доступ к площадке отделён от подтверждения доставки в UI

- Панель принимает явный `access_only` с `fixture=false` как результат
  настоящей проверки доступа, даже когда `live_verified=false` обозначает
  отсутствие проверки доставки. Telegram inspect больше не показывает ложное
  сообщение о неподтверждённом сетевом соединении при подтверждённом праве.
- Отдельные подписи показывают доступ и право публикации либо отсутствие
  подтверждённого права; каждая сохраняет указание, что доставка не проверена.
  Fixture имеет приоритетную локальную подпись. Неизвестные типы, неполные
  данные и противоречивые publish/can_publish остаются нейтральным результатом.
- Проверки: полный Node/UI набор со strict semantic TypeScript — 67 passed,
  без ошибок и пропусков. Добавлены четыре проверки access-only/legacy/fixture
  и некорректных данных, включая реальный обработчик `EditorialPanel` с
  fetch-stub и только inspect плюс read-only refresh. Лог сохранён локально
  в игнорируемом `.test-state/inspect-ui-node.log`.
- Ограничения: это UI-only изменение с локальными ответами fixtures;
  внешних отправок нет. Python, config и MANIFEST этим набором не менялись;
  результат отдельного frozen Python QA не подменяется результатом Node.

## 0.12.0 — 2026-10-31: точность описания отдельных профилей и offline-проверки

- В runbook личный VK через `vk_browser` указан основным выбранным маршрутом,
  а API сообщества — отдельным вариантом. Авторизация встроенного браузера
  явно отделена от ещё не подтверждённого входа в профиль отправителя копии.
- Уточнена offline-проверка Пикабу: storage state собственного persistent
  профиля передавался ephemeral context только в памяти; его страницы оставались
  `about:blank`. Файлов cookies/storage и вывода их содержимого не было.
- Проверки: независимый read-only аудит документов и сопоставление с сохранёнными
  приватными доказательствами; `git diff --check`. Исполняемые файлы не менялись,
  полный frozen QA продолжается; эти уточнения не подтверждают живую доставку.

## 0.12.0 — 2026-10-31: пример VK для выбранной личной страницы

- В основном примере четырёх платформ `vk_test` использует `vk_browser`,
  согласно выбору владельца. API сообщества остаётся отдельным вариантом,
  а не выдаётся за поддержку личной страницы новым VK ID token.
- Пример остаётся выключенным; текстовый DOM-контракт и media-контракт false,
  профиль браузера принадлежит копии. При инициализации ничего не отправляется.
- Проверки: publishing schema и выключенное состояние всех четырёх примеров
  подтверждены. Полный QA и повторная проверка переносимого запуска текущего
  архива выполняются отдельно. Живая доставка этой правкой не подтверждается;
  локальная конфигурация уже подготовленной копии и рабочий сервер не менялись.

## 0.12.0 — 2026-10-31: ограниченный повтор Windows-коммита локальных отчётов

- После трёх WinError 5 при атомарной записи в двух прежних benchmark-модулях
  добавлен общий `report_files.replace_report_file`. Он повторяет только
  замену уже записанного и fsync-нутого временного файла на Windows для
  WinError 5/32/33: бюджет 1 секунда, интервал до 50 мс, не более 21 попытки.
  Deadline повторно проверяется после ожидания до следующей замены.
- JSON, timestamp, исходники и результаты проверок не формируются заново при
  повторе rename. Helper не меняет ACL, не удаляет файлы и не повторяет модели,
  HTTP или LanguageTool. Остальные ошибки и ОС завершаются сразу; при исчерпании
  бюджета прежний отчёт и временный файл сохраняются с исходной ошибкой.
- Интеграция ограничена двумя вызовами после существующего fsync:
  `benchmark_cascade.Report.save` и `check_article_benchmark.save_report`.
  Права, input-history gates, hashes, durable claims и unknown/no-replay
  поведение не ослаблены; маршруты публикации этим исправлением не менялись.
- Проверки: helper и оба benchmark-набора с complex cascade — 90 passed;
  Ruff и `git diff --check` — passed. Проверены два временных отказа с одним
  атомарным коммитом тех же bytes, ограниченный постоянный отказ, просроченное
  пробуждение, fail-fast и один LT/model вызов после post-response rename
  retries. Неизвестный LT при незаписанном результате не вызывается заново.
- Ограничения: это локальная I/O-устойчивость, не исправление прав каталога и
  не подтверждение причины блокировки. Исходные неуспешные full-run результаты
  сохраняются; новый полный frozen-прогон фиксируется отдельно. Сохранённые
  benchmark-отчёты остаются привязанными к исходным code/input hashes, без
  автоматической миграции или переписывания прежней истории.

## 0.12.0 — 2026-10-31: полнота исходного sdist после проверки упаковки

- Свежая сборка выявила семь файлов, сохранённых в прямом source ZIP,
  но отсутствовавших в sdist. В `MANIFEST.in` явно добавлены `.gitignore`,
  Rust-исходник и patch Candle с `deployment/translation/requirements.txt`,
  JSON fixtures тестов и PowerShell mocks рабочей станции. Runtime, бинарные
  файлы, node_modules и локальные env-файлы дополнительно не включались.
- В игнорируемом `data/final-packaging-smoke/fixed` собраны wheel, sdist и
  прямой source ZIP. Все 528 выбранных исходных файлов ZIP присутствуют
  побайтно в sdist; пропуски и расхождения — ноль. Wheel содержит все 88
  текущих Python-модулей, версию 0.12.0 и `platform-agent` entrypoint.
  CSS, схемы, примеры config, новые документы, `AGENTS.md` и история сохранены.
- Проверены CRC/manifest hashes, совпадение исходников, отсутствие приватных
  runtime-путей и совпадений с фактическими секретами собственной `.env.local`,
  включая разобранные/декодированные пароли DSN: все соответствующие счётчики
  равны нулю. Значения секретов не выводились и не записывались в отчёт.
- Ограничения: это smoke упаковки, а не финальные dist или успешный общий QA.
  Изменились метаданные `MANIFEST.in` и документация; полного совпадения всех
  source hashes с предыдущим frozen-срезом этим набором не заявляется.
  Исполняемая Python/TypeScript логика не менялась; финальная приёмка и архивы
  оформляются после отдельного итогового прогона и документирования результатов.

## 0.12.0 — 2026-10-31: фактический offline-контракт редактора Пикабу

- Read-only/offline приёмка обнаружила реальную структуру абзацев: точные
  `node-wrapper-view__host[data-node-view-wrapper]` → content → paragraph.
  Она принимается атомарным mapper вместе с проверенным пустым не редактируемым
  drag handle SVG/use; произвольные DIV/SVG, дополнительные текст и изображения
  не допускаются. Пробелы, внутренние пустые абзацы и обычные BR сохраняются.
- Ровно один конечный pristine wrapped P с ProseMirror-trailingBreak учитывается
  как editor affordance, остальные пустые абзацы сохраняются. Неоднозначный
  перевод строки в конце утверждённого тела отклоняется заранее кодом
  `PIKABU_TRAILING_LINEBREAK_UNSUPPORTED`; текст не обрезается и не подменяется.
  Публичная сверка не получает послабления для этого editor affordance.
- Ввод использует native focus без Range/framework API. Перед каждой клавишей
  снова проверяются разрешение, точный `/add`, аккаунт, active editor и
  collapsed selection внутри него. Чужая страница, потеря фокуса/selection,
  скрытый/обрезанный блок или изображение, неизвестный порядок блоков и
  несогласованный href блокируют действие. Пикабу redirect допускается только
  в точной известной форме и с разрешённым конечным URL.
- Проверки: полный текущий набор четырёх Pikabu-модулей — 161 passed за
  339,04 с; Ruff passed. Независимые 24 DOM probes passed; повторный code review
  новых ветвей не выявил P1. Проверены zero-key/zero-Publish при смене URL,
  account/focus/selection, неизвестные wrappers/chrome, clipping/opacity,
  поддельные BR, точные ссылки, hidden image и запрет повторной отправки.
- Две приёмки на настоящем клиентском JS в одноразовых offline-контекстах
  выполнили фактические `_fill_editor`, `_check_editor`, `_final_editor`: один
  абзац и несколько абзацев с внутренним переносом строки прошли без Range
  подмены. В обоих случаях точные title/body/три тега совпали, внешних мутаций,
  загрузок и Publish — ноль; оба контекста закрыты, storage state не сохранялся,
  source/config не менялись. Terminal-newline case сохранён как отрицательная
  проверка, не выдан за PASS. Приватные helpers/DOM proofs находятся только
  в игнорируемых data и в исходный архив не входят.
- Ограничения: offline клиент не доказывает фактическую доставку. Реальный
  Publish и независимый публичный readback требуют согласованного сценария;
  media gate остаётся отдельным и этим тестом не подтверждается. Рабочий сервер,
  его БД, задания и профили этой разработкой не использовались.

## 0.12.0 — 2026-10-31: граница заголовка Telegram

- Совпадение заголовка только с началом более длинного слова в body больше
  не считается уже включённым заголовком: `Марс` перед `Марсианская…`
  формирует отдельную жирную строку, а не выделяет часть слова. Точное
  совпадение и заголовок перед переводом строки не дублируются.
- Проверки: весь Telegram-модуль — 151 passed, включая четыре точных
  HTTP-регрессии границы заголовка и прежние UTF-16 лимиты 1024/4096.
- Ограничения: это локальные API fixtures, не живая доставка. Внешний текст
  тестового поста ещё ожидает согласования владельца; сервер не менялся.

## 0.12.0 — 2026-10-31: уточнение разрешений и границ документальной проверки

- Исправлены ошибочные формулировки о согласованном VK-тексте: наблюдался
  подготовленный частный черновик без отправки; согласование владельцем точного
  текста, адресата и даты ещё ожидается. Агент сохраняет решение владельца,
  но не заменяет его автоматической политикой MAX для новых маршрутов.
- Документы отделяют Telegram access-only inspect, вход и пустой редактор
  Пикабу, offline DOM/preview и локальные fixtures от живой доставки.
  Прежнее fixture-доказательство не названо подтверждением прошлой публикации.
  Более ранний desktop/mobile layout fixture не выдаётся за свежую визуальную
  приёмку последних подписей истории; текущие 63 Node-проверки указаны отдельно.
- В `vk_browser.py` исправлен только исторический комментарий. Проверка AST
  до/после подтверждает отсутствие изменений исполняемой логики; AST SHA-256:
  `3aefec3632729290b759129d60518210dfaf65fc10b6cae4eb91993c9385d474`.
- Ограничения: внешних постов этим уточнением не создавалось. Общий полный QA
  остановлен для исправления отдельного обнаруженного DOM-блокера Пикабу;
  его итог и финальные артефакты будут записаны после следующего frozen-прогона.
  Прежние исторические записи и результаты проверок не переписаны.

## 0.12.0 — 2026-10-31: точные доказательства и адресаты в истории маршрутов

- Панель получает короткий `evidence_kind` только из сохранённого audit,
  связанного с неизменным snapshot и датой. Fixture явно остаётся локальной
  проверкой; API-квитанция, независимое чтение и исторический флаг различаются.
  Смена текущего режима адаптера не превращает прежний тест в живую доставку.
- Исторические платформа, маршрут и адресат берутся из frozen dispatch,
  а не из текущих настроек Target. Приватное содержимое audit не добавляется
  в инвентарь панели. При отсутствии доказательств подтверждение не выводится.
- Оба браузерных reconcile получают frozen snapshot БД: receipt должен
  соответствовать согласованному содержимому, а не только локальному журналу.
  Сверка не выполняет отправку или загрузку.
- Проверки: 16 Python-регрессий классификации, подмены snapshot, отсутствующих
  proof, изменения настроек и передачи frozen snapshot прошли; 63 Node/strict
  TypeScript проверки панели, включая неизменный исторический адресат, прошли.
- В отдельной копии по поручению владельца созданы бот и закрытый канал
  Telegram. Живой Bot API inspect подтвердил право публикации через явно
  выбранный уже работающий локальный proxy. Это доступ, не доставка: текстовые
  посты не отправлялись. У Пикабу подтверждён вход и структура пустого редактора;
  текстовый и media-контракты остаются выключены до отдельной приёмки.
  Рабочий сервер и исходный checkout не изменялись. Итоговый полный QA
  обновлённого frozen-кода фиксируется отдельной записью после завершения.

## 0.12.0 — 2026-10-31: проверка квитанции Пикабу в родительском адаптере

- `PikabuBrowser` повторно читает sealed журнал после worker и требует точного
  совпадения stdout с ним, root/options/DOM, request ID, адресатом, аккаунтом
  и frozen snapshot. `verified=true` без этой привязки даёт unknown/false;
  fixture и неполное доказательство не становятся живой доставкой.
- `pikabu-public-story-dom-v2` связывает заголовок, тело, точные теги, URL
  источников и изображения с hashes snapshot. Адаптер отдельно проверяет
  canonical ID/URL, фактический readback URL, DOM/HTML hashes и ограниченное
  конечное время наблюдения. Огромное число вместо timestamp не вызывает
  необработанный OverflowError. Reconcile принимает snapshot из БД и требует
  новое наблюдение после начала сверки, сохраняя запрет повторной отправки.
- Проверки: 37 mocked protocol cases passed за 6,53 с; пять настоящих Chrome
  fixture-путей полного автопостинга, text-only, known preparation, fixture
  binding и свежего reconcile passed за 30,32 с. Ruff прошёл; независимый
  повторный просмотр не выявил дополнительных P1 в проверенной границе.
- Ограничения: mocked receipts и локальный Chrome не доказывают живую
  публикацию. Внешних публикаций/загрузок этим изменением нет; текущий сервер
  и его профили не использовались. Live text-only тест и отдельный media gate
  по-прежнему требуют согласованного сценария и фактической независимой сверки.

## 0.12.0 — 2026-10-31: исторический адресат и будущая дата CLI-согласования

- История новых маршрутов показывает platform/destination только из точного
  dispatch snapshot. Смена текущего адресата не переписывает подпись прежней
  отправки; при отсутствии сохранённого назначения выводится «Адресат не
  сохранён», без подстановки текущих настроек.
- `routes_cli approve` отклоняет дату без часового пояса и дату в прошлом
  либо точно равную текущему времени до вызова `approve_variant`. Проверка
  будущего времени совпадает с UI/API; автоматическое догоняющее согласование
  через CLI не создаётся.
- Default focused script дополнен проверками сохранённого evidence kind и
  CLI-границами расписания. Проверки: полный Node/strict TypeScript набор —
  63 passed; быстрые CLI boundary tests — 4 passed; Ruff — passed. Регрессия
  подтверждает неизменность исторической строки после смены текущего target.
- Ограничения: проверки локальные, с fixtures/FakeCore, без согласований в
  рабочей БД или внешних публикаций; общий Python full-run имеет отдельный
  итог и этой записью не объявляется успешным.

## 0.12.0 — 2026-10-31: история Postiz различает локальные проверки и доставку

- В карточках отправок используется сохранённый `evidence_kind`, а не текущий
  режим адресата. Fixture всегда обозначается «Локальная проверка; живая
  доставка не проверена», включая ошибочный исторический `verified=true`;
  фиктивная ссылка не показывается как ссылка на публикацию.
- Подпись API-квитанции требует явного `api_receipt`, независимое подтверждение
  — `independent_readback` вместе с `verified=true`. Без доказательств UI
  сообщает отсутствие подтверждения; прежний verified-флаг отдельно обозначен
  как историческая запись без новых доказательств. Неизвестные и невалидные
  виды доказательств не повышаются до API-квитанции или живой сверки.
- Проверки: полный Node TypeScript/UI contract-набор — 62 passed, включая
  strict semantic TypeScript, неизвестные evidence kinds, fixture с изменённым
  на live адресатом и отсутствие ссылки на фиктивную публикацию.
- Ограничения: это локальная проверка UI/BFF; Docker-стек и внешние публикации
  не запускались. Backend передаёт только безопасную классификацию, без
  приватных объектов аудита или учётных данных.

## 0.12.0 — 2026-10-31: точная сверка и изоляция браузерного Пикабу

- Журнал `pikabu-browser-single-click-v2` сохраняет origin и признак fixture.
  Чужой, отсутствующий или тестовый контекст не принимается за живую доставку;
  loopback URL проверяется до навигации и хранится как фактический readback URL.
  Даже confirmed при `reconcile` заново читает известную страницу без отправки.
- После проверки удалённых bytes изображения выполняется новый атомарный
  снимок URL, аккаунта, автора, заголовка, текста, источников, тегов, изображений
  и видимости. Дополнительный текст вне абзацев, скрытые части текста и лишние
  изображения блокируются до отправки и при независимой сверке результата.
- После durable intent снова проверяются exact approval, общая пауза,
  исходный владелец и срок lease. Перед единственным Publish после последней
  авторизации повторно сверяются редактор, аккаунт, режимы, ошибки и кнопка.
  Изменение состояния даёт unknown и не разрешает повторную отправку.
- Добавлен отдельный `media_contract_verified=false`: проверка текстового
  маршрута не включает загрузку изображений. Gate действует в адаптере и worker
  до изменения редактора; конфигурационный статус показывает оба флага отдельно.
- Проверки: независимый просмотр закрыл найденные P1; Ruff изменённых модулей
  прошёл. Расширенный прогон Пикабу, offline probe, Hub и API: 123 passed и один
  таймаут первого GET локального стенда до любых мутаций. Этот тест отдельно
  прошёл без изменения production-кода; после увеличения только навигационного
  бюджета fixture повторные 20 race/authority cases прошли за 137,23 с.
  Отдельные 10 проверок общей паузы, durable intent и последней авторизации
  также прошли. Локальный text-only стенд выполнил одну отправку и reload.
- Ограничения: это код, локальные fixtures и read-only/offline доказательства,
  а не живая доставка Пикабу. Текст и время внешнего теста ожидают согласования
  владельца; live upload и сохранность SHA изображения не проверены. Проверенные
  приватные адресаты оставались выключены, DOM/media flags — false; рабочий
  сервер и его профили не изменялись. Итоговый общий QA фиксируется отдельно.

## 0.12.0 — 2026-10-31: локальный ввод авторских постов для новых маршрутов

- `routes_cli create-original TARGET --file JSON --attest-original` сохраняет
  точные title/body/media/metadata в отдельные Story, Draft, ревизии и Variant.
  Подтверждение собственных прав оператором привязано к hashes и отдельному
  аудиту `owner-original-v1`; создание не выполняет model/API/browser запросы,
  не создаёт approval/dispatch и не меняет включение или паузу адресата.
- Допущены только новые прямые Telegram/VK/Пикабу маршруты. До создания
  требуются включённые адресат и профиль в режиме кроме `dry_run`; адресат
  может оставаться paused. Это предотвращает создание версии с заведомо
  устаревающим binding при последующем включении. MAX и прежний bridge/hourly
  не получают права источника через этот ввод; общие quality/media gates
  сохранены. Изменение title/body/media, канонической истории или аудита
  блокирует использование исходного подтверждения.
- Документированы ограниченный UTF-8 JSON внутри копии, точные Пикабу metadata
  и отдельное последующее согласование версии/адресата/даты. Default focused
  test-script включает owner-original, credentials и copy-local PostgreSQL
  dispatch tests; без собственного TEST DSN последний модуль сохраняет skip.
- Проверки: focused `test_owner_original.py` + `test_route_isolation.py` —
  48 passed; Ruff для изменённых Python-файлов, PowerShell Parser и AST
  встроенного Python test-script, `pip check` — passed. Проверены поддельные
  metadata без канонической цепочки, отсутствие/подмена audit, изменённые
  тексты/media/история, hourly origin, MAX и disabled/dry-run lifecycle.
- Ограничения: проверки выполнены на локальных fixtures. Реальные тексты,
  согласования и внешние публикации этим набором не создавались; UI пока
  принимает в работу уже созданный CLI-вариант. Подтверждение прав оператором
  не является машинной верификацией фактов или разрешением отправки.

## 0.12.0 — 2026-10-31: CSS панели Postiz в исходном пакете

- В `MANIFEST.in` явно включены `deployment/**/*.css`: source distribution
  сохраняет `editorial.module.css` вместе с TypeScript-компонентами панели.
- В собственном `.venv` копии установлены build-зависимости из официального
  PyPI; `setuptools==84.0.0` и `wheel==0.48.0` соответствуют `pyproject.toml`.
  `pip check` завершился без нарушенных зависимостей.
- Проверки: `python -m build --sdist --wheel --no-isolation` создал временные
  проверочные пакеты в игнорируемом `data/packaging-qa-manifest`. В sdist
  проверены наличие CSS и точное совпадение его SHA с исходником; в wheel —
  CRC и наличие новых route-модулей. В обоих пакетах отсутствуют пути приватных
  runtime-каталогов и локальные env-файлы.
- Ограничения: это проверка упаковки, а не подтверждение полного запуска
  Postiz, готовности аккаунтов или живой доставки. Финальные архивы создаются
  после завершения общей проверки и согласования итогового состояния кода;
  временные QA-пакеты не выдаются за финальный релиз.

## 0.12.0 — 2026-10-31: четыре маршрута в отдельной тестовой копии

- По поручению владельца работа ведётся только в отдельном worktree
  `codex/four-platform-isolated`, от исходного commit c68a463. Рабочий сервер,
  его БД, очереди, cookies и сервисы не изменяются и в копию не переносятся.
- Добавляется отдельная маршрутизация Telegram Bot API, VK API и браузерного
  Пикабу с прежними exact approval, датой, адресатом, паузой и durable dispatch.
  MAX сохраняет прежний browser transport. Новые маршруты не наследуют
  автоматическое согласование почасовой политики MAX.
- Квитанция API, локальная fixture и независимая сверка различаются явно;
  доказательства новых адаптеров сохраняются отдельно от статуса доступа.
- Проверки и оставшиеся ограничения будут зафиксированы новой записью после
  завершения тестов. Эта запись не утверждает живую интеграцию новых маршрутов.

## 0.11.11 — 2026-11-01: повторная полная проверка после браузерных исправлений

- Оба обязательных прогона выполнены последовательно по новому снимку,
  зафиксированному до запуска. Все восемь этапов каждого завершились успешно;
  после PostgreSQL и после общего прогона повторно совпали 493 файла исходников
  и три инструмента. Проверенный код между прогонами не менялся.
- PostgreSQL: 5137 passed, 0 failures/errors, пять платформенных skips;
  полный pytest занял 6392,218 с при прежнем бюджете 7200 с. Отдельный тестовый
  кластер остановлен, завершение известных процессов проверено отдельно.
- Общий режим: 5119 passed, 0 failures/errors, 23 skips; pytest — 3484,797 с.
  Восемнадцать PostgreSQL-тестов намеренно не получали URL в этом режиме и
  прошли в отдельном прогоне с PostgreSQL. Остальные пять пропусков в обоих
  наборах — три проверки symlink на Windows и две проверки native Linux pipe.
- Все 30 обязательных браузерных и бюджетных регрессий прошли в каждом наборе.
  Нулевой код завершения обоих запусков наблюдён фактически; JUnit, отчёты,
  lifecycle и связь с исходным снимком сверены, прежние отказы сохранены.
- Код отдельно просмотрен; права перед Send, unknown, защита от дублей,
  история и интервал доставки не ослаблены. Проверка типов не настроена.
  Эти результаты не подтверждают живую интеграцию: изолированная Linux QA,
  установка с backup/CAS и первая доставка 0.11.11 проверяются отдельно.
  Действующая VM на момент этих проверок продолжает работу на 0.11.10.

## 0.11.11 — 2026-11-01: бюджеты подготовки и контрольного чтения в браузерных тестах

- Итог общего прогона предыдущего снимка: 5118 passed, один failed и 23 skips;
  остальные семь этапов прошли. Проверка готовности канала завершилась успешно,
  затем контроль неизменности черновика получил TimeoutError при чтении текста
  с унаследованным искусственным лимитом 40 мс. Неуспешный отчёт сохранён.
- Отдельный прогон модуля выявил второй отказ: искусственный бюджет 1000 мс
  ограничивал начальную навигацию до проверки канала. Сохранены 39 passed и
  один failed этого прогона; его успешная приёмка не заявляется.
- Обычная навигация тестов использует штатный бюджет готовности 30 секунд;
  отдельный setup перехода на неверный URL получает тот же явный предел.
  Только контрольное чтение чужого черновика получает бюджет 1000 мс.
  Все assertions, fixture timeout 40 мс, отрицательные проверки на 150 мс и
  их предел две секунды сохранены. Код приложения и его production-бюджеты
  этим изменением не менялись.
- На существующих тестах воспроизведены отказ getter текста длительностью
  200 мс после успешной проверки канала и оба отказа навигации с задержкой
  1200 мс. После правок те же fault-сценарии прошли со всеми assertions.
  Использован отдельный Chrome с синтетическими страницами, без живой отправки.
- Модуль готовности канала целиком: 40 passed, без failures/errors/skips.
  Полный Ruff lint и git diff --check прошли; независимое ревью без замечаний.
- Оба обязательных полных прогона выполняются заново по новому снимку.
  Прежний PostgreSQL-результат 5137 passed / пять платформенных skips относится
  к прежним байтам теста и сохраняется как история. Установка и первая доставка
  0.11.11 ещё не подтверждены; действующая VM продолжает работу на 0.11.10.

## 0.11.11 — 2026-11-01: чтение изображения после таймаута прокрутки

- Изолированная Linux QA выявила отказ чтения уже отправленного поста:
  таймаут штатной прокрутки не перемещал изображение в область загрузки.
  Сохранённый диагностический trace подтвердил TimeoutError; исходный QA-отчёт
  с 219 passed и одним failed сохранён и не считается успешной приёмкой.
- Только после такого таймаута выполняется ограниченная прокрутка прежнего
  DOM-узла: повторно проверяются один точный пост, текст, жирный заголовок,
  обычное тело, допустимый origin и положение изображения перед подписью.
  Проверки и прокрутка выполняются вместе; подмена или удаление узла блокируют
  запасной путь. Финальное подтверждение по-прежнему требует полного image proof.
- Дополнительные регрессии воспроизвели появление дубликата между проверками,
  в том числе при уже загруженном фото. Повторный подсчёт точных сообщений
  перед confirmed сохраняет ожидаемое before_count + 1. Повторного Send нет;
  текущие согласования, история, unknown и публикационные интервалы сохранены.
- До правок воспроизведены таймаут и оба случая позднего дубликата.
  После всех изменений 24 целевые регрессии прошли без skips; отрицательные
  сценарии проверяют чужие URL, изменение текста/формата, дубликат и смену узла.
- Оба браузерных модуля целиком: 217 passed, без failures/errors/skips.
  Ruff lint и git diff --check прошли; независимое ревью без замечаний.
- Полные общая и PostgreSQL-проверки выполняются заново по новому снимку.
  Предыдущие успешные Windows-отчёты относятся к прежним исходникам.
  Изолированная Linux QA, установка и первая доставка новой версии остаются
  отдельными проверками; действующая VM продолжает работу на 0.11.10.

## 0.11.11 — 2026-11-01: итоговые проверки Windows

- Оба обязательных прогона выполнены последовательно на одном предварительно
  зафиксированном наборе исходников. Все восемь этапов каждого прошли;
  после завершения повторно совпали 493 проверяемых файла и три инструмента.
- PostgreSQL: 5113 passed, 0 failures/errors, пять платформенных skips —
  три проверки symlink на Windows и две проверки Linux pipe. Полный pytest
  занял 5055,547 с при ограниченном бюджете 7200 с; тестовая БД остановлена.
- Общий режим: 5095 passed, 0 failures/errors, 23 skips; 18 PostgreSQL-сценариев
  намеренно не получали URL и отдельно прошли в PostgreSQL-прогоне, остальные
  пять имеют те же платформенные причины. Время pytest — 4997,953 с.
  Все шесть новых браузерных и бюджетных регрессий прошли в обоих наборах.
- Независимое ревью итогового diff прошло без замечаний. Прежние failed,
  timeout и controlled-abort отчёты сохранены. Проверка типов не настроена.
  Локальные suite не подтверждают живую интеграцию или доставку MAX;
  изолированная Linux QA, установка и первая доставка проверяются отдельно.

## 0.11.11 — 2026-11-01: положительные браузерные регрессии используют штатное ожидание

- В положительном illustrated-v2 сценарии искусственные 150 мс ограничивали
  также первоначальную навигацию Chrome. Сохранён отказ до Send; неуспешный
  общий прогон остановлен только для собственного pytest, а исполнитель
  завершил остальные этапы и записал FAILED. Полного JUnit и исходного
  traceback этого остановленного прогона нет; его успешная приёмка не заявляется.
- Отдельный синтетический пример с корректным редактором через 400 мс
  воспроизвёл отказ при 150 мс до отправки. Два положительных image-теста
  теперь используют штатный бюджет готовности. При той же задержке сценарий
  проходит с одной имитацией Send и прежней проверкой повторного вызова.
  Все assertions, production-предел 30 секунд, проверки прав до Send и короткие
  отрицательные проверки идентичности канала сохранены.
- Оба браузерных модуля целиком: 193 passed, без failures/errors/skips.
  Полный Ruff lint и git diff --check прошли; отдельное ревью подтвердило
  изменение только четырёх setup statements и сохранность всех 14 assertions.
- Оба обязательных полных прогона после правки стенда выполняются заново
  по новому предварительному снимку исходников. Прежние 5113 passed и пять
  платформенных skips PostgreSQL-прогона относятся к прежним байтам теста
  и сохраняются как история. Linux QA, установка и первая доставка 0.11.11
  ещё не подтверждены; действующая VM остаётся на 0.11.10.

## 0.11.11 — 2026-11-01: ограниченный бюджет полного локального pytest

- Последовательный PostgreSQL-прогон на текущем Windows-ПК дошёл до 99%,
  но завершился TimeoutExpired после 3600 секунд; семь остальных этапов прошли.
  Это незавершённая проверка, её исходный failed-отчёт сохраняется.
- Только этап полного pytest получает ограниченный бюджет 7200 секунд.
  Остальные этапы сохраняют прежние бюджеты; выбранный timeout_seconds теперь
  записывается в JSON каждого выполненного этапа, включая ошибку и таймаут.
  Проверки, состав suite и публикационный интервал 3600 секунд сохранены.
- Виртуальная полная оркестрация воспроизвела прежний отказ для успешной suite
  длительностью 3601 секунду. После правки она проходит; suite на 7201 секунду
  по-прежнему завершается TimeoutExpired с failed-отчётом, а независимый diff
  выполняется после отказа. Все 27 целевых проверок test_check.py прошли.
- Версия 0.11.11 ещё не выпускалась. Успешные полные общая и PostgreSQL-проверки
  после изменения бюджета, развёртывание и доставка MAX остаются отдельными
  незавершёнными проверками; целевые синтетические тесты их не заменяют.

## 0.11.11 — 2026-11-01: сверка MAX после Send не требует редактора публикации

- Подтверждение после reload и native reconciliation проверяют точные URL и
  единственный заголовок канала в прежнем ограниченном бюджете, без требования
  готового редактора. Отсутствие или временная недоступность composer больше
  не прерывает чтение уже опубликованного сообщения до проверки receipt.
- Проверка права отправки остаётся включённой по умолчанию, при входе в канал
  и перед единственным Send. navigate=False сам по себе её не отключает.
  Чужой URL, неправильный/неоднозначный заголовок и отсутствие входа блокируют
  также режим чтения. Exact text/image/bold proof и защита unknown не ослаблены.
- Регрессия на прежнем коде: четыре illustrated-сценария воспроизвели unknown
  после reload и отказ reconciliation без composer. После правки 14 целевых
  проверок прошли, включая два формата изображения и отрицательные проверки
  идентичности канала. Синтетические HTML полностью перехватывают запросы;
  повторных Send, доступа к действующему профилю и живой проверки MAX нет.
- Обязательные общие и PostgreSQL-проверки выполняются отдельно; их итог
  фиксируется после завершения. Причина неготовности composer на живом сайте
  этим исправлением не устанавливается. Код локальный, развёртывание и
  reconciliation действующего неизвестного исхода этим изменением не выполнялись.

## 0.11.10 — 2026-11-01: существующая доставка сверена без повторного Send

- После отправки проверка готовности редактора при reload прервала чтение
  receipt. Два свежих reload MAX подтвердили одно точное сообщение: изображение
  перед текстом, жирный заголовок, обычное тело и отсутствие удалённых строк.
  Причина временной неготовности редактора отдельно не установлена.
- Одноразовая native reconciliation зафиксировала существующую доставку:
  Postiz PUBLISHED/verified, одна физическая попытка. Сохранены исходный unknown,
  raw, backup и история; повторный Send, новый dispatch или DRAFT не создавались.
  03:29:58 МСК — время фиксации сверки; точное физическое время доставки
  не установлено. Полный последующий readback подтвердил неизменный журнал.
- VM самостоятельно пополнила резерв до шести статей с закрытыми claims.
  Read-only сверка проверила сохранённые native bindings источников, прав,
  машинной проверки, traces и изображений; pending учитывается отдельно.
  Полная смысловая приёмка и повторная загрузка HTTP-доказательств не заявляются.
- Рабочий runtime остаётся 0.11.10/RUN89. Правка 0.11.11 пока локальная:
  два параллельных полных прогона завершились по часовому таймауту pytest,
  остальные обязательные этапы прошли; запущен последовательный повтор.
  Установка ожидает успешных обязательных проверок и безопасного окна.

## 0.11.10 — 2026-10-31: первая плановая доставка после обновления подтверждена

- Серверный слот 10:04:23 МСК фактически доставлен в MAX в 10:04:38,833 МСК:
  Postiz PUBLISHED/verified, одна физическая попытка, интервал от прежней
  доставки 3616,172 с. Оператор не вызывал Send и не создавал новый dispatch.
- Независимые два reload MAX дали counts [1,1], точный текст, фото перед ним,
  жирный заголовок и обычное тело; обе исключённые строки отсутствуют,
  publication journal неизменен. SHA readback:
  `b29829b1008f6ee425a438f5d553112948435e20ffcb5cf4c5afc5be10f56d60`.
- Отдельная read-only сверка подтвердила всю source/rights/translation/quality/
  exact approval/Postiz/image/delivery цепочку на runtime 0.11.10; повторной
  отправки нет, изображение совпало с исходными bytes. SHA полного отчёта:
  `84a4faf8f4f27030d024f0ba349c56eb66d89b0bbb079562c96894c390edd624`.
  Машинное одобрение не объявляется ручной гарантией качества перевода.
- После назначения следующего слота на 11:04:39,833 МСК резерв составляет
  пять ready, pending отдельно. Это штатный остаток выше low=3; ручное
  пополнение не запускалось. Ошибок, active claims, owners и mutation leases
  нет. Постоянный контроль остаётся активным; текущий здоровый проход не
  требует правок кода приложения и не ослабляет проверок.

## 0.11.10 — 2026-10-31: постоянный операционный контроль расписания и резерва

- По прямому поручению владельца создан активный контроль текущего чата каждые
  пять минут: worker/API, слоты, доставка Postiz/MAX, reserve/claims/trace и
  leases. Публикациями и подготовкой продолжает владеть VM; прежние max и
  max-postiz оставлены PAUSED. Здоровые неизменные проходы не создают сообщений.
- Инструкция оператора сохранена в prompts/MAX_OPERATIONS_MONITOR.md:
  проверенный новый delivery сверяется один раз, с двумя reload MAX и точным
  текстом/изображением/форматом. 4–5 готовых статей считаются штатным запасом;
  pending учитывается отдельно, refill 6/3/12/3 не запускается вручную.
- Подтверждённый дефект исправляется через воспроизведение, регрессию,
  обязательные checks, независимое ревью, Git и backup/CAS. Обслуживание не
  прерывает active claims/sending/unknown/leases и требует due более 300 секунд.
  Неизвестный исход не снимается повтором Send или сбросом слота.
- Стартовая read-only проверка: 0.11.10/RUN89, шесть ready и один pending,
  активных claims/owners/leases и ошибок нет. Последние четыре доставки
  PUBLISHED/verified, по одной попытке, интервалы больше 3600 секунд.
  Конфигурация контроля проверена штатным инструментом; diff проверен отдельно.
- Это операторская конфигурация Codex и документация, код приложения и
  развёрнутые policy/model pins не менялись. Новый Git source содержит 494
  файла; прежний архив 5e44584/493 и runtime127 остаются exact baseline VM.
  Операторский prompt автоматически на VM не переносится. Приватное состояние
  контроля хранится в ignored data. Локальный контроль требует работающего
  Codex и доступного SSH/VPN; серверный почасовой цикл от него не зависит.

## 0.11.10 — 2026-10-31: независимая сверка истории после возобновления

- Read-only SQL/file verifier подтвердил точные полные payload и порядок всех
  201 записей. Проекция всех 37 таблиц совпала: допустимы только startup,
  native start, refence прежнего pending и его штатное возобновление с четырьмя
  точными Audit. Token/due/snapshot/approval сохранены. Исходная отменённая
  попытка 01:24 остаётся единственной unknown-попыткой, без повтора.
- Совпали SHA всех 26395 файлов исходников статей, raw/trace, изображений,
  model requests и журнала публикаций, а также 46 файлов конфигурации/политик.
  Runtime127, metadata 0.11.10, PID после одного restart и шесть готовых
  статей подтверждены. VM/SQL/HTTP/Send изменений у verifier нет.
  SHA отчёта: `b1654fda6ea52c81857297af31828f9f2247879c41c213195074dd6bced34d15`.
- Первая проверка ожидала побайтного равенства также Chrome-профиля и
  остановилась после разрешённых MAX reload. Исходный failed-отчёт сохранён.
  V2 отдельно фиксирует 49 изменений только настроенного browser profile;
  модели/raw/trace и publication journal остаются строгими. Кэш, IndexedDB,
  browsing history и prefs Chrome не объявляются неизменными. Негативные
  проверки повреждения/исчезновения model raw и изменения журнала блокируют.
- Проверенный код после Windows/Linux QA не менялся; в финальном исходном
  архиве разрешены только пять новых MD поверх QA29, остальные 488 файлов
  сохраняются побайтно. Новая фактическая отправка 0.11.10 на этом срезе
  ещё не выполнена; pending на 10:04:23 МСК остаётся отдельным от резерва.

## 0.11.10 — 2026-10-31: установлена на VM, почасовой цикл RUN89 возобновлён

- После закрытия всех активных claims и пополнения до шести статей выполнены
  native pause87→88, backup/CAS и установка 22 публичных файлов. Package/source
  metadata согласованы: 0.11.10. Все runtime127 соответствуют проверенной QA29.
  Backup: `reserve-1793428131597392153`; SHA его полного индекса:
  `59342a98209c9604f71917f0450c795678cde8aa5c838d3dfe145bef0442dc6d`.
- Установка и один перезапуск сохранили все 201 запись и полный снимок 37
  таблиц, claims, исходные ответы/trace, политики и профиль моделей. Перезапуск
  измерен как 9,469 с, добавлен один ожидаемый startup Audit. SHA установки:
  `ecce279f8a2ba536875491ebb8a39cd9ef7c2786077f8f8c7c1c5e0bd3db3541`;
  SHA restart receipt:
  `6dc5c68aad77bd0c6778d157da17a409e72de09c453143fbe87bf828adc6d51c`.
- Native start88→89 возобновил серверный цикл. Резерв — шесть quality_ready,
  pending отдельно назначен на 10:04:23 МСК. Текст, изображение, snapshot,
  approval и due сохранены; повторный POST, ручной Send, смена политики и
  восстановление старых отменённых/unknown источников не выполнялись.
  SHA native start:
  `4d849f7efa1421cbd87c8f199d343d53249280407092f634e1cc20f106d13bb5`.
- На установленной 0.11.10 выполнены два reload MAX для прежнего поста
  09:04:22: counts [1,1], точный текст, изображение перед ним, жирный заголовок,
  обычное тело и отсутствие удалённых строк; журнал неизменен. SHA readback:
  `35f0a46e8c3b60d0fb446f2c529b4ee849ef078af89fcda9fa5042b708aeca31`.
  Этот пост отправлен до обновления; новая отправка 0.11.10 на данном срезе
  ещё не выполнялась и не объявляется подтверждённой доставкой.
- Проверки: полный Windows/PostgreSQL check — 5089 passed/5 платформенных
  skips; дополнительная Linux/PostgreSQL QA29 — 1155 passed/0 skips в 21
  модуле, включая все пять пропущенных сценариев. Наборы не суммируются.
  Полный Linux unified check, mypy/pyright и удалённый CI не заявляются.
  Правила, промпты и вся история сохраняются в исходном архиве; приватные
  отчёты, credentials, cookies и runtime backup остаются вне Git.

## 0.11.10 — 2026-10-31: Linux QA29 и проверка процедуры обновления

- Изолированная Linux/PostgreSQL QA29 завершена: 1155 passed в 21 модуле,
  0 failed/errors/skipped. Выполнены все пять pipe/symlink-сценариев, которые
  пропускались на Windows. Проверены изменённые модули, browser transport,
  atomic reserve delivery и восстановление доказанного отказа до Send.
  SHA отчёта: `e4965eb9c7e4aa2be31543de6bfed32a5c178a390906bd0ade5042dc3fbba189`.
- Все 493 файла из committed source проверены по manifest/archive; staged
  source и рабочие runtime127, конфигурации, политики и metadata 0.11.9
  сохранили hashes. Тестовая БД удалена. Бизнес-данные production штатно
  менялись серверным циклом; их равенство до/после QA не заявляется.
- Полная Windows/PostgreSQL проверка 5089 passed/5 skips остаётся отдельным
  результатом для тех же 368 файлов кода. Наборы пересекаются и не суммируются.
  Linux QA29 является целевой проверкой, не полным Linux unified check.
- Отдельные приватные регрессии процедуры обновления подтвердили запрет
  неполного JUnit, отсутствующих платформенных тестов и удаления/дублирования
  старого Audit. Backup/CAS сохраняет 17 существующих изменяемых файлов и
  ровно пять отсутствующих. Эти проверки не означают выполненный deploy.
- Перед установкой в MAX два reload подтвердили пост 09:04:22 МСК под 0.11.9:
  один точный текст с изображением и жирным заголовком, журнал неизменен.
  Установка 0.11.10 ожидает завершения текущей подготовки; live Send новой
  версии и изменения production этой записью не заявляются.

## 0.11.10 — 2026-10-31: сохранение релиза и объединение истории

- По прямому поручению владельца исходные 21 изменения 0.11.10 сохранены
  отдельным commit и отправлены в GitHub. История установленной 0.11.9,
  приёмки шести статей и фактической доставки объединена без удаления
  прежних записей; правила AGENTS и промпты сохраняются в исходниках.
- Независимое ревью подтвердило SHA успешного полного отчёта, все 368 SHA
  проверенных файлов и JUnit: 5089 passed, 5 платформенных skipped, 0 ошибок.
  Изменений проверенного кода после QA нет. Дополнительно проверены 493
  публичных файла по семи сигнатурам секретов и запрещённым путям: находок нет.
- Готовится отдельная Linux-проверка пропущенных pipe/symlink сценариев и
  установка с backup/CAS. На этом этапе 0.11.10 ещё не развёрнута; действующий
  серверный цикл и его очередь продолжают работать на прежнем runtime.

## 0.11.10 — 2026-10-31: итоговая проверка процесса прошла

- Повторная `python scripts/check.py --postgres` завершилась кодом 0:
  5089 passed, 5 skipped, без failures/collection errors, один deprecation
  warning закреплённого Starlette/httpx. Pytest: 2721,02 с (45 мин 21 с).
  Package/locks, pip check, весь настроенный lint, scoped format, оба JS,
  изолированный Chrome и git diff --check прошли. Проверены 56 закреплённых
  distributions. Все 368 файлов снимка исходников сохранили SHA в течение QA.
- Пять skips имеют явные причины: три symlink-проверки требуют иных прав
  Windows, две проверки native pipes требуют Linux. PostgreSQL-тесты не
  пропускались. Отдельный локальный кластер PostgreSQL 17.6 штатно остановлен;
  рабочая VM, её БД, конфигурация и публикации не менялись.
- Отдельное ревью проверило возврат ненулевого кода, пустой/повреждённый JUnit,
  отсутствие инструментов/тестов, переход в собственную .venv, timeout,
  изоляцию БД, совместимость CAS mock и инициализацию benchmark adapter.
  Подтверждённые дефекты исправлены, регрессии и неуспешные прогоны сохранены.
  После полного прогона изменены только документация и итоговый отчёт.
- README/AGENTS/docs/CODE_CHECKS.md содержат команды подготовки, запуска одного
  теста и общей проверки, порядок ревью, критерии готовности и измеренную
  длительность полного набора. Версия исходников/metadata — 0.11.10.
  Ограничения: Linux/native symlink-проверки, live MAX/Postiz/модели/LT и
  type checker не подтверждены. CI отсутствует; старый formatter debt явно
  отделён от обязательной области. Deploy, commit и push не выполнялись.

## 0.11.10 — 2026-10-31: полный прогон обнаружил дефект стендового адаптера

- Общая команда с изолированным PostgreSQL завершилась ненулевым кодом:
  5081 passed, 5 failed, 5 платформенных skipped; pytest занял 2821,62 с.
  Остальные обязательные этапы прошли. Неуспешные JSON/JUnit/логи сохранены;
  временный PostgreSQL 17.6 штатно остановлен. Это не зелёная приёмка.
- Все пять падений воспроизведены на маршрутах benchmark: подкласс
  BenchmarkGatewayTranslator не инициализировал новые segment_chars/config_hash
  базового адаптера. Исправлена только инициализация стенда с прежними 1400
  символами; production-список моделей, сегментация и проверки не ослаблены.
  Добавлены три регрессии preflight до любого обращения к модели.
- Целевой набор benchmark/перевод/quality: 277 passed; Ruff для src/tests/scripts
  и отдельно изменённого benchmark_articles.py прошёл. Требуется новый полный
  прогон общей команды, он запускается на том же изолированном принципе.

## 0.11.10 — 2026-10-31: единая воспроизводимая проверка исходников

- Добавлена `python scripts/check.py`: собственная .venv текущего checkout,
  проверка package/lock версий, pip check, полный Ruff lint, явная область
  форматирования, синтаксис обоих JS, изолированный Chrome, полный pytest и
  git diff --check. Этапы имеют статусы, длительности, отдельные логи и JSON/JUnit
  в уникальном игнорируемом каталоге. Ошибка, отсутствие тестов/инструмента,
  timeout или повреждённый отчёт не становятся успешным результатом.
- scripts/test.ps1 делегирует общей команде с сохранением exit code. README,
  AGENTS и docs/CODE_CHECKS.md описывают подготовку по четырём locks, отдельный
  тест, изолированный PostgreSQL, ревью, критерии готовности и границы проверки.
  Версии pyproject/__init__ согласованы. Production не развёртывалась.
- Воспроизведён и исправлен неявный выбор БД двумя PostgreSQL-фикстурами:
  runtime .env.local и прежний приватный fallback больше не читаются. Нужен
  явный TEST_DATABASE_URL; общий check передаёт его только с --postgres,
  требует отдельную test_*/*_tests БД и не выводит URL/credentials.
- Все 55 исходных ошибок Ruff устранены без отключения правил: составные
  инструкции тестов разнесены, fixture imports заменены pytest_plugins,
  zip получил strict=True, неиспользуемое имя переименовано с сохранением
  обязательного чтения payload. Ожидания публикации/прав/unknown не ослаблены.
- Исходный полный pytest обнаружил устаревший positive mock CAS guard:
  ему недоставало обязательных DRAFT/group/integration bindings. Фикстура
  приведена к действующему контракту; добавлены три отрицательных проверки,
  production TypeScript guard не менялся. Модуль: 20 passed.
- Целевые проверки: 555 passed/один Windows symlink skip; отдельно 25 тестов
  общей команды passed. Наборы пересекаются, результаты не суммируются.
  Оба регрессионных теста изоляции БД сначала воспроизвели ошибку, затем прошли.
  Ruff, scoped format, pip check и синтаксис JS успешны.
- Первый итоговый прогон остановлен для подтверждённой правки фикстуры;
  его failed-отчёт сохранён, отдельный локальный PostgreSQL штатно остановлен.
  Новый полный прогон с изолированным PostgreSQL 17.6 выполняется; итог будет
  записан отдельно. Live MAX/Postiz/модели/LanguageTool здесь не проверяются.
  CI отсутствует; type checker не вводился. Старый долг форматирования
  (исходно 213 файлов) вынесен за явно заданную область нового formatter gate.


## 0.11.9 — 2026-10-31: приёмка шести готовых статей и завершение контроля

- На срезе 00:43 МСК получены ACCEPTED_SIX и accepted_closed_usable_count=6.
  Все шесть quality_ready имеют закрытые claims, права/качество/изображение,
  exact snapshot и проверку всей истории дублей. Pending на 01:01:47 МСК
  учтён отдельно. Source/model/HTTP/Send вызовов и записей в БД у проверки нет;
  native lock reads выполнены под SQL guard с rollback.
  SHA приёмки: `1073615e343f2bb8750fd1fe077a8678f290d06d3be39679f14cf1109ab7d0e7`.
- Сравнение с исходным paused86 снимком: все шесть ID и закрытых claims новые,
  среди прежних 170 записей их нет; исходный запас после обновления был нулевым.
  Пополнение выполнено сервером после native start87. Подготовка вручную,
  восстановление отменённых источников и согласование pending вместо резерва
  не выполнялись. Почасовой серверный цикл остаётся активным.
- Первый проход приёмки остановился с OperationalError и не был засчитан.
  Диагностический reader сохранил исходные SQL/quality/rights/duplicate gates;
  следующий проход подтвердил все шесть без изменения логики проверки.
- Сохраняется консервативный Latin-mask guard: он также отклоняет часть
  замен русских терминов/кредитов английским оригиналом. Такие кандидаты не
  объявляются семантически неверными без независимой проверки; отказ и raw
  сохраняются, сервер выбирает другой источник. Машинная приёмка не является
  ручной оценкой безошибочности каждого полного перевода.
- Финальные исходники 0.11.9 оформляются из отдельного чистого checkout:
  параллельная незавершённая 0.11.10 сохранена отдельно. В архиве 0.11.9 только
  четыре обновлённых MD; остальные 486 файлов, включая runtime127, побайтно
  соответствуют исходному архиву успешной QA28. Проверки и живая доставка
  00:01:46 МСК подтверждены отдельными доказательствами ниже.

## 0.11.9 — 2026-10-31: фактическая доставка нового цикла и живой автокарантин

- В 00:01:46,509 МСК статья, подготовленная уже 0.11.9, фактически доставлена
  в MAX. Одна физическая попытка, Postiz PUBLISHED; интервал от предыдущей
  доставки 3616,073 с. Два независимых reload MAX дали counts [1,1], точный
  текст, изображение перед ним, жирный заголовок и обычное тело. Обе удалённые
  строки отсутствуют, журнал не изменён. SHA readback:
  `9b014d7d8903885f6d5bd298a8a2f18fd3e2f4d739a23e9f0f4fbbce77c4c98a`.
- Отдельная SELECT-only проверка подтвердила всю цепочку: исходник и права,
  MADLAD, одну адресную правку Qwen, свежие Min/LT, точный approval/snapshot,
  дату, получателя и совпадающие bytes изображения. SHA полного отчёта:
  `3a547e3f428d4d11252c4f5f305583ad38ecbd34168c5d578a8ebcd108024021`.
  Приватные readers выбирают prompt по сохранённому review_protocol_contract;
  старые readers и первый неуспешный отчёт REVIEW_PROMPT_CHANGED сохранены.
  Изменения readers не ослабляли native binding или проверки доставки.
- В 00:11 сервер сам изолировал новый полностью завершённый INVALID_EDITOR_RESULT:
  cached proof, guard и Audit hashes совпали, проверены все семь файлов ответа.
  Запись cancelled, свой claim rejected и освобождён; raw/trace/error сохранены,
  DRAFT/approval/Send не создавались. Следующая статья прошла Min/LT и пополнила
  резерв. SHA независимой read-only сверки живого автокарантина:
  `80b729925debe4b320989a7001ed5e1c784d3a59336ad2e8d83c17c11b9766a6`.
- На срезе 00:20 МСК пять закрытых quality_ready, pending на 01:01 учтён отдельно;
  ещё одна статья готовится автоматически. Приёмка шести usable продолжается.

## 0.11.9 — 2026-10-30: QA28, установка и возобновление серверного цикла

- Исправленный код прошёл изолированную Linux/PostgreSQL QA28: 1286 passed
  в 33 модулях, без ошибок и пропусков. Production оставалась активной;
  QA не меняла её данные. Config/public source hashes сохранены; нормальная
  работа production отражена отдельными снимками без заявления их равенства.
  Receipt SHA: `d7c0abbe276ac32f2b8663d2eec3e05dc78b076af454c6d2918fd3fb5651e725`.
  Неуспешная QA27 и её исходники/лог сохранены.
- Доставка 23:01:30 МСК под прежней 0.11.8 независимо подтверждена в MAX:
  Postiz PUBLISHED, одна физическая попытка, два reload с counts [1,1], exact
  текст, фото перед ним, жирный заголовок и обычное тело; журнал не изменён.
- Повторный завершённый pre-DRAFT отказ изолирован отдельной операцией:
  четыре отрицательных замечания, исходные error/raw/attempts и claim identity
  сохранены. Атомарная композиция закрыла только эту статью/claim с Audit,
  затем выполнила native maintenance pause86 с отдельным Audit. Новых моделей,
  DRAFT/approval/Send для этой статьи нет; остальные 169 записей сохранены.
- С backup установлены 19 проверенных файлов 0.11.9. Установка сохранила
  все 170 записей, claims, config и raw; один перезапуск 6,217 с добавил только
  штатный startup Audit. Runtime/package/health согласованы, native start
  возобновил RUN87. Профиль моделей, права, рубрика, 6/3/12/3 и интервал
  3600 с не менялись. Новая подготовка началась автоматически.
- На срезе 23:13 МСК шесть закрытых usable статей и доставка после установки
  ещё не подтверждены; контроль продолжается. Факт доставки до установки не
  объявляется живой проверкой нового runtime.

## 0.11.9 — 2026-10-30: результат QA27 и безопасный снимок глубоко вложенного ответа

- Изолированная Linux/PostgreSQL QA27: 1284 passed, один failed, без пропусков
  и collection errors. Production оставалась активной; в ней QA ничего не
  меняла, config/public source hashes сохранены. Эта проверка не разрешила
  установку версии. Исходные receipt и лог сохраняются отдельно.
- Сбой оказался в построении quarantine proof для чрезмерно вложенного JSON:
  Linux разобрал его, но deepcopy превысил предел рекурсии вне защитной ветки.
  Перенесено только копирование недоверенного объекта внутрь той же защиты;
  при таком отказе structured untrusted остаётся null, полный raw и его SHA
  сохраняются. Действительный отрицательный verdict и неизвестные запросы
  не меняются. Версия ещё не устанавливалась; требуется новый серверный прогон.

## 0.11.9 — 2026-10-30: завершённый отказ проверки не занимает весь резерв

- Для новых hourly-practical-v2 записей отдельно закрепляется
  article-review-bounds-v2: до 16 замечаний и 16 предупреждений, до 1024
  символов на пункт, общий размер findings до 32768 байт, ответ до 65536 байт.
  Все замечания и отрицательное решение сохраняются; превышение границ и
  невалидная схема не становятся одобрением. Исторические rubric/parser/prompt
  и их hashes не меняются. Новый prompt хранится в prompts/.
- Полный невалидный ответ допускает только карантин своей статьи: exact
  input/candidate/prompt/trace/HTTP-200/validated и семь durable-файлов OpenCode
  связываются с attempt и Audit. Закрытие записи, собственного claim и один
  Audit атомарны; источник остаётся использованным. После рестарта разрешено
  завершение того же доказанного исхода без нового запроса модели.
- Timeout, неполный ответ, повреждённый proof, потерянный token/fence, sending,
  unknown и mutation lease сохраняют блокировку. Невалидный ответ после Qwen
  имеет отдельный terminal outcome; два repair claim и прежние raw сохраняются.
  Отправитель использует готовую очередь независимо от пополнения резерва.
- Текущая запись с четырьмя сохранёнными замечаниями закрыта отдельной
  exact-операцией по прямому поручению владельца, без модели, approval или Send.
  Это не миграция старых отказов и не разрешение их автоматического повтора.
- Локально: интегрированный набор 172 passed, один PostgreSQL skip без
  TEST_DATABASE_URL; после усиления binding отдельно 70 parser/binding и
  53 completed-proof проверки прошли. Наборы пересекаются и не суммируются.
  Ruff и diff-check прошли. Живая сверка MAX выше относится к 0.11.8.
- Проверки новой версии продолжаются; установка 0.11.9 и запас из шести
  закрытых usable статей пока не подтверждены. Ни локальные тесты, ни машинный
  verdict не объявляются доказательством безошибочности полного перевода.

## 0.11.8 — 2026-10-30: результаты установки и независимой сверки MAX

- Изолированная Linux/PostgreSQL QA26: 812 passed в 25 модулях без ошибок
  и пропусков. Перед установкой сохранены исходники, raw и 37 таблиц;
  один перезапуск занял 3,647 с. Runtime/package/health согласованы, RUN85.
  Историческая запись ниже описывает более ранний этап и сохранена.
- Штатный обработчик закрыл failed pre-click слот 14:42 и оставил его
  исходный Attempt/журнал без изменений; повторной отправки этой статьи нет.
  Другая статья доставлена 15:59:28 МСК одной попыткой, Postiz PUBLISHED.
- Независимая сверка публикации 22:01:11 МСК: два reload MAX, counts [1,1],
  exact текст, изображение перед текстом, жирный заголовок и обычное тело,
  обе удалённые строки отсутствуют; журнал неизменен. Фактические интервалы
  последних четырёх публикаций превышают 3600 с, догоняющих пачек нет.
- Пополнение позднее остановилось на завершённом reviewer protocol refusal
  из-за четырёх findings при прежнем лимите три. Отправитель продолжал работать;
  запас шести статей на этом этапе не подтверждён. Исправление — 0.11.9 выше.

## 0.11.8 — 2026-10-30: точность даты Postiz и готовность канала до отправки

- Подтверждён сбой 14:42: браузер остановился на проверке канала до Send;
  новый durable-журнал сохранил BROWSER_LOGIN_OR_CHANNEL_REQUIRED и
  send_started=false. Свежая отдельная проверка открыла канал и после двух
  reload не нашла точного сообщения. Исходная failed-попытка и журнал сохранены.
- Освобождение доказанного pre-click failed-слота блокировало точное сравнение
  metadata-даты Postiz с подписанной датой: миллисекундное значение отличалось
  на 0,235 мс. Исправление допускает только конечную корректную metadata-дату
  с разницей строго менее 1 мс; подписанные slot/record/dispatch/approval-даты
  и hashes сохраняют точные проверки. Unknown, leases и неполный proof не снимаются.
- Проверка канала до отправки получает ограниченное ожидание готовности с
  сохранением точного URL, названия канала и прав на публикацию. Изменение
  не разрешает повтор Send, восстановление старого источника или замену unknown.
- При явном MAX HTTP 429 автоматическая замена статьи запрещена: failed-слот,
  исходный журнал и попытка сохраняются до сверки, повторного запроса нет.
- Локально: 107 native passed и один PostgreSQL skip без TEST_DATABASE_URL;
  после добавления защиты 429 отдельно прошли 23 целевые проверки. Реальный
  DOM-набор готовности канала: 22 passed; прежняя browser_publishing: 19 passed.
  Наборы пересекаются и не суммируются. Ruff и diff-check прошли. Изолированная
  PostgreSQL QA и установка ещё не выполнены; исторические результаты ниже
  сохранены, локальные проверки не объявляются живой доставкой.


## 0.11.10 — 2026-11-01: разбор CodeQL и исправления зависимостей

- Выполнен разбор 45 CodeQL alerts по сохранённому SARIF основного Python analysis.
  Подтверждены 7 отмеченных мест в двух связанных цепочках (3 прямых sink и
  4 prerequisite/validation locations), остальные 38 — доказанные false positives.
  Это не семь отдельных дефектов. Полное сопоставление всех 45 locations,
  регрессионных тестов и 82 dependency alerts: docs/security/2026-11-01-v0.11.10.json.
  После полной индексации манифестов Dependabot показал 82 alerts: 50 уникальных
  advisory для шести пакетов; дубли разных манифестов учитываются отдельно.
- Уязвимые объявленные версии заменены: Pillow 13.0.0, cryptography 50.0.2,
  certifi 2026.10.23, setuptools 84.2.1, opentelemetry-api 1.45.2,
  Transformers 5.17.0 и совместимый huggingface-hub 1.33.0. Минимальные/верхние
  границы и runtime/dev/images locks согласованы с requirements-ci.txt.
  Для advisory Transformers без first_patched_version проверено включение
  конкретного upstream fix commit в release, а не только уязвимый диапазон.
- Исходные CodeQL flows для env names, подписанной cookie и фиксированного argv
  проверены отдельными synthetic controls. Значения ключей не помещаются в отчёт;
  подтверждённые ошибки путей и ложные срабатывания фиксируются раздельно.
- Исправлены пять функциональных дефектов: обход topic/sources namespace,
  workflow collision с проверенными файлами, direct private JSON selection,
  открытый неявный/перенесённый стандартный профиль и workflow=None readback.
  Одна privacy policy защищает известные default и настроенные browser/OpenCode
  roots, включая roots под config/. Schema poisoning не проходит предварительную
  проверку; все targets перепроверяются до записи. Live guards sending/unknown,
  mutation locks, восстановление прежних файлов и публикационные проверки сохранены.
- В отдельных окружениях прошли pip check, аудит 55 core pins и 37 resolved
  translation dependencies без Torch: 0 известных advisory в проверенных наборах.
  Pillow formats, Fernet и CA bundle проверены smoke; synthetic Fernet ciphertext
  совместим в обе стороны между cryptography 49.0.0 и 50.0.2. Wheel/sdist собраны.
  Итоговый локальный набор на обновлённых зависимостях: 104 passed, 4 ожидаемых
  Windows symlink skipped, 1 прежний Starlette/httpx warning; Ruff/pins прошли.
  Полные GitHub pytest/CodeQL и Linux symlink-контроли обязательны перед merge.
- Установки и проверки выполняются в отдельных локальных окружениях/временных
  каталогах. Модели, рабочая VM, её БД, очереди, история, публикации и исходный
  checkout не изменяются. Новая версия относится к репозиторию и ещё не установлена
  на работающий сервер. Финальные проверки и disposition записываются в этом наборе.

## 0.11.7 — 2026-11-01: полный Windows pytest четырьмя обязательными частями

- Последовательный Windows job дошёл до 88% и был остановлен по лимиту 45 минут;
  `CI required` завершился ошибкой и сохранил запрет merge. Linux проверен полностью:
  4863 passed, 18 ожидаемых PostgreSQL skipped, 1 warning; отдельная PostgreSQL 17:
  199 passed, 1 warning, без пропусков. Таймаут не выдан за успешный тест.
- Windows collection разделена на четыре обязательных job по SHA256 nodeid.
  Ни один тест не удаляется: `CI required` проверяет успех всех matrix jobs и
  receipts четырёх частей — одинаковый полный collection/hash, точные counts,
  однократное назначение каждого nodeid, отсутствие пересечений/потерь и exit 0.
  Отфильтрованный набор и повреждённые/неполные receipts блокируют merge.
- JUnit и receipts имеют разные имена artifacts; агрегатор получает только чтение
  Actions. Download action закреплён по SHA. Linux сохраняет полный pytest/coverage,
  PostgreSQL и Node Windows/Linux остаются обязательными. Hard gates не ослаблены,
  runtime-пакет, production-сервер, модели, очередь и публикации не менялись.
- До применения проверено на 28 синтетических тестах: четыре непересекающиеся
  части полностью покрыли collection; перестановка порядка не меняет назначения;
  pytest failure/usage/no-tests и неверные параметры сохраняют ненулевые exit codes.
  Постоянный набор проверки wrapper/receipts: 23 passed; checker работает без
  site-packages (`python -S`); Ruff, pins и actionlint прошли. Полный GitHub CI
  повторяется на новой конфигурации перед штатным merge.

## 0.11.7 — 2026-11-01: исправления по первому полному GitHub CI

- Первый полный Ubuntu pytest обнаружил шесть ошибок: 4857 passed, 6 failed,
  18 PostgreSQL skipped, 1 warning. Существующий benchmark subclass обходил
  родительский конструктор и не имел `segment_chars`/`config_hash`; три маршрута
  завершались AttributeError до HTTP. Добавлена инициализация прежнего лимита
  1400 и hash фактической конфигурации маршрута. Критерии, raw, запрет повтора
  неизвестного исхода и production adapter не ослаблялись.
- Native CAS test теперь использует полный актуальный DRAFT/group/integration
  snapshot. Добавлены три проверки отказа для изменённых state/group/integration;
  рабочий TypeScript guard и его pinned hashes не менялись. Локально весь CAS
  модуль: 20 passed, 1 deprecation warning; benchmark: 34 passed, без пропусков.
- В PostgreSQL job добавлены test_article_reserve_store и test_reserve_preclick_recovery:
  это ещё четыре проверки гонок/истории, которые в общем наборе требуют отдельной
  БД. Ruff дополнительно охватывает исправленный benchmark. Полный CI повторяется
  на исправленном commit; защита main остаётся активной, failing PR не объединён.
- Полный pytest остаётся обязательным на обеих ОС; coverage собирается на Linux,
  чтобы Windows не повторяла ту же инструментированную проверку. Каждый новый
  commit PR проверяется через pull_request/synchronize, main — через push;
  устранены два одинаковых CI запуска на каждое изменение PR.
- Исправления выполнялись в отдельном worktree. Работающий сервер, модели,
  production-БД, публикации и исходный checkout не затронуты.

## 0.11.7 — 2026-11-01: проверка pins Dependabot и фактическое включение защиты

- Ruleset `Protect main` №26308277 включён и прочитан обратно через GitHub API:
  PR, обязательный актуальный `CI required` от app 15368, CodeQL errors/high,
  запрет удаления/force push и пустой bypass. Пока проверки PR не завершены,
  GitHub показывает `BLOCKED`. Настройки безопасности прочитаны обратно:
  Dependabot security updates, Secret scanning и Push protection включены;
  открытых Secret scanning alerts на момент проверки нет.
- Dependabot не разбирает нестандартные `requirements*.lock`. Добавлен поддерживаемый
  `requirements-ci.txt` с 55 точными pins из runtime/dev/browser locks и обязательный
  stdlib-checker их равенства. PR обновления требует синхронной правки соответствующих
  locks; изменение одного лишь манифеста не пройдёт CI. Старые locks не мигрировали.
  TypeScript/tslib исключены из обычных автообновлений: их версии закреплены CAS
  contract и меняются вместе с доказательствами компиляции.
- Явно разрешена выгрузка только выбранных скрытых JUnit/coverage-файлов;
  для `.yml` закреплён LF. Дополнительные проверки: actionlint 1.7.12 прошёл,
  checker pins прошёл positive-case и семь независимых negative-cases,
  локальный целевой pytest — 671 passed, 5 expected skipped, 1 warning.
  Первые GitHub Ruff/build, PostgreSQL, Node Windows/Linux и CodeQL jobs прошли;
  итог полного CI проверяется на последней версии PR перед объединением.
- CodeQL baseline и шесть прежних Dependabot alerts (4 high, 1 medium, 1 low)
  сохранены открытыми для отдельного разбора. Ничего не dismissed/подавлено,
  защита не ослаблялась ради старых alerts. Работающий сервер не затронут.

## 0.11.7 — 2026-11-01: GitHub CI и защита main

- Добавлены обязательные проверки GitHub Actions: Ruff, совместимость зависимостей,
  сборка wheel/sdist, полный pytest с Chrome на Windows/Linux, отдельная одноразовая
  PostgreSQL 17 и пять Node suites Postiz на обеих ОС. Агрегатор `CI required`
  отклоняет failed/cancelled/skipped jobs; Actions закреплены по точным commit SHA,
  token имеет только чтение, JUnit/coverage сохраняются на семь дней.
- Node tooling отделён от OpenCode runtime и закреплён lock-файлом. CAS guard
  компилируется только во временной копии с проверкой source/compiled SHA;
  все suites обязательны, пропуски запрещены. Нормализация LF воспроизводит Git
  blobs на Windows, не переписывая исходники и не обращаясь к Docker/серверу.
- Добавлены Dependabot для Python, npm и Actions, а также декларативный ruleset:
  PR, успешный `CI required` именно от GitHub Actions, актуальная база, CodeQL,
  запрет force push/удаления main и обязательное закрытие обсуждений. Для одного
  владельца не требуется чужой approving review; bypass не предусмотрен.
- В GitHub включены Dependabot alerts/security updates, Secret scanning,
  Push protection и CodeQL Python/JavaScript/TypeScript. Первый CodeQL baseline
  завершился успешно и сохранил 45 открытых alerts (2 critical, 42 high, 1 medium).
  Они не подавлялись и требуют отдельного разбора; успешный scan не означает
  отсутствие уязвимостей. CodeQL merge protection оценивает находки PR diff.
- Исправлены 55 нарушений существующего Ruff без ослабления правил: многооператорные
  строки тестов, регистрация pytest fixtures, явная строгость zip и имя неиспользуемого
  значения при сохранении обязательного чтения `payload["chat_id"]`.
- Локально: зависимости установлены в отдельную .venv; pip check и wheel/sdist
  прошли; Node 89 passed, 0 failed/skipped; CAS 81 mocked checks; npm audit tooling
  не нашёл уязвимостей. Полные GitHub jobs и применение ruleset проверяются отдельно.
  Версия runtime-пакета не менялась; работающая VM, её БД, очереди, публикации,
  модели и существующие локальные checkout не изменялись.

## 0.11.7 — 2026-10-30: установка, закрытие старого unknown и фактическая доставка

- Итоговый код прошёл изолированную Linux/PostgreSQL qa-v25: 745 passed в 23
  модулях, без ошибок и пропусков. Все 37 production-таблиц и публичные исходники
  сохранены. SHA receipt:
  `cc6b98ce3e76c1953360452c7ca5409e13f26a8489bd60d663baecd038c8607c`.
  Это focused-набор; пересекающиеся Windows-наборы 209/41/10/70/91 не суммируются.
- Установлена 0.11.7 на maintenance-паузе revision 82 с backup, сохранением 158
  записей, raw и шести готовых статей. Один перезапуск 4,751 с;
  import/metadata/health согласованы. Политика, профиль моделей и Node-образ
  не менялись. История прежних результатов ниже не переписана.
- По уже полученному прямому поручению владельца отдельно закрыт dispatch
  `265e75d3-54c7-47d3-bd24-fcd358cecea3` слота 01:24. Оригинальный UNKNOWN
  Attempt и attempts=1 сохранены. Dispatch/record cancelled, approval revoked,
  слот missed, источник навсегда использован. Нового Send/Attempt/Postiz-мутации
  этой статьи нет. Это операционное решение не доказывает отсутствие старой
  отправки и не расширяет pre-click-протокол на исторические unknown.
- Штатный start возобновил revision 83. Другая статья, NOIRLab iotw2528b,
  фактически доставлена 30 октября в 11:41:52 МСК одной попыткой: dispatch
  `ae82100d-8f3f-4018-85ee-2d23a15ca915` verified, Postiz PUBLISHED.
  Два read-only reload MAX подтвердили ровно одно сообщение с точным текстом,
  оригинальным изображением выше нативного жирного заголовка и обычным телом;
  обе исключённые строки отсутствуют. Журнал не изменён. SHA readback:
  `8a95f16209643ac5c4a787b09dc3f0d355d38199d2768458c6f146ec09a4fe32`.
- Следующий точный слот 12:41:53 МСК, actual+3601 с; догоняющих отправок нет.
  Долгая подготовка продолжалась независимо от отправителя. На срезе 11:50:
  161 запись, quality_ready 5, отдельно pending 1/preparing 1, dispatch unknown 0,
  владельцев/leases 0. Шесть — запас до расходования, а не текущие шесть готовых.
- README, навигатор и инструкция VM обновлены с сохранением датированной истории.
  Итоговый архив 484 публичных файлов и локальный manifest исключают runtime,
  cookies, секреты, БД, медиа и модели; bounded privacy-проверка не является
  абсолютной гарантией обнаружения любого формата секрета. Будущие сетевые сбои
  и неизвестные исходы сохраняют необходимость сверки, а не повтора статьи.

## 0.11.7 — 2026-10-30: окончательная авторизация после ожидания интерфейса

- Все ограниченные ожидания редактора и загрузки теперь завершаются до последней
  авторизации. После неё выполняются только немедленные проверки точного текста,
  жирного заголовка, обычного тела и вложения, затем durable-запись и клик. Пауза
  или отказ авторизации не пережидаются и не превращаются в разрешение отправки.
- Добавлены проверки отказа, изменения текста и форматирования из callback
  авторизации. Вместе с прежними проверками гонки загрузки и единственного Send
  целевой набор прошёл 10 тестов за 28,95 с. Серверный QA и установка ещё не
  выполнены; старая попытка 01:24 остаётся unknown без повторной отправки.

## 0.11.7 — 2026-10-30: готовность редактора MAX и доказанные отказы до отправки

- Перед выделением заголовка браузер дожидается завершения загрузки изображения,
  доступности кнопки и точного текста в редакторе на последовательных проходах
  отрисовки. После одного Ctrl+B отдельно ожидает настоящее выделение заголовка
  и обычное тело; повторного переключения форматирования нет. Перед Send точный
  текст, оформление и готовность проверяются ещё раз.
- Подготовка и завершённый отказ до клика получают отдельный durable-журнал с
  привязкой request/config/payload/изображения и исходной причиной. Запись и rename
  синхронизируются до Send. При любой неопределённости после начала отправки
  сохраняется unknown; существующие журналы прежнего протокола не переписываются.
- BrowserMax признаёт BROWSER_PRECLICK_FAILED только после проверки полного
  нового доказательства и его SHA. Для будущих таких отказов отдельная native-
  проверка сопоставляет единственную failed-попытку, согласование, источник,
  snapshot, свежий Postiz ERROR и bytes изображения. Она атомарно закрывает слот,
  отзывает согласование и сохраняет источник использованным. Исходные QA, raw,
  failed-попытка и история остаются; неизвестные результаты этим путём не снимаются.
- Импорт точного перехода QUEUE → ERROR при таком доказательстве сохраняет
  содержимое и проверку качества Variant. Иные правки и журналы проходят прежние
  консервативные проверки. Отправитель продолжает с другим источником в допустимом
  следующем слоте, без повторения старой статьи или догоняющих отправок.
- Локально прошли 209 браузерных проверок, отдельно 41 проверка доказательств и
  4 проверки ожидания/единственного клика. Новый native-модуль: 70 passed и один
  пропуск без TEST_DATABASE_URL; прежние controller/bridge/cancelled-проверки:
  91 passed и один такой же пропуск. Это пересекающиеся focused-наборы, а не
  суммарное число уникальных тестов. Изолированная Linux/PostgreSQL QA итогового
  набора ещё не выполнена; на VM пока установлена 0.11.6.
- В MAX без Send воспроизведён BROWSER_ARTICLE_TITLE_FORMAT_UNAVAILABLE при
  немедленном форматировании после загрузки фото; тот же текст после ожидания
  формируется корректно. Отдельный guarded NoSend-тест нового worker на том же
  тексте и изображении завершился без ошибки, с одним вызовом выделения заголовка,
  сохранённым старым журналом и очищенным тестовым черновиком. Эти проверки не
  являются живой доставкой. Исторический
  код ошибки попытки 01:24 не сохранился: её unknown, слот и история не изменены,
  повторная отправка не разрешена этим исправлением.

## 0.11.6 — 2026-10-30: установка, резерв и границы живой проверки

- На VM установлена 0.11.6 после изолированной Linux/PostgreSQL qa-v24: 456 тестов
  в 10 модулях за 57,21 с, без ошибок и пропусков, с одним известным предупреждением
  Starlette. При установке сохранены 154 записи, 37 таблиц, исходные файлы моделей
  и конфигурация. Один перезапуск занял 3,384 с; штатный цикл возобновлён.
- Доставки 29 октября в 22:24:06 на 0.11.4, в 23:24:20 на 0.11.5 и 30 октября
  в 00:24:33 на 0.11.5 подтверждены отдельно через Postiz и два reload MAX:
  изображение первым, настоящий жирный заголовок, обычное тело и подпись v2.
  Уточнение прежней сводки: первый пост содержит 2740 символов, а не 2700.
- Из пригодного резерва отдельно исключены найденные существенные смысловые
  ошибки с сохранением исходного машинного одобрения, отказов и всех источников.
  Одна новая статья прошла штатную адресную правку и повторные Min/LT. В итоге
  подготовлены шесть закрытых quality_ready с проверенными исходниками,
  изображениями и историей дублей; машинный стиль и допустимые языковые замечания
  сохранены по practical-критериям.
- Отправка в слоте 01:24 получила unknown и не подтверждена. Точный текст после
  двух свежих загрузок MAX не найден; журнал начала отправки отсутствует, JPEG
  сохранён. Историческая причина не записана адаптером. Попытка, согласование,
  занятый слот и источник сохраняются; новые доставки после 00:24 не заявляются.

## 0.11.6 — 2026-10-30: пополнение резерва после исключения известной ошибки

- Исключение непригодной quality_ready включает текущий цикл пополнения до
  настроенной цели6, даже когда оставшиеся четыре статьи ещё выше обычного
  порога3. Конфигурация, политика и нормальный hysteresis не меняются; отменённая
  статья не возвращается в работу и не получает нового запроса модели.
  Добавлена проверка старта с четырьмя пригодными статьями и одной ошибочной:
  после её отмены подготавливаются только два новых источника до запаса6.
  Локально33 focused-проверки прошли за18,60с; две PostgreSQL-проверки пропущены
  без TEST_DATABASE_URL. Ruff E4/E9/F и diff-check прошли. Linux QA22b и
  развёртывание этого набора ещё не выполнены.

## 0.11.6 — 2026-10-30: порядок проверки ещё занятого кандидата резерва

- Отправитель сначала пропускает quality_ready, которым ещё владеет активный
  подготовитель, и только затем проверяет закрытый резерв на доказанную подмену
  сравнения массы. Подготовитель сам атомарно отменяет такой кандидат до
  освобождения claim. Это исключает промежуточный CLAIM_LOST без выдачи слота,
  новой модели или снятия неизвестного исхода. Проверка добавлена в существующий
  модуль подготовки. Локально прошли 32 focused-проверки за 17,16 с, две проверки
  PostgreSQL пропущены без TEST_DATABASE_URL; Ruff E4/E9/F и diff-check прошли.
  Развёртывание и Linux QA22b остаются отдельным этапом.

## 0.11.6 — 2026-10-30: отдельный запрет публикации доказанной подмены сравнения массы

- Для будущей отправки из резерва добавлен узкий детерминированный guard:
  единственное утверждение «Despite being only N–M% as massive as the Milky Way
  galaxy» при 0 < N ≤ M < 100 не может получить смысл «только на N–M% массивнее
  Млечного Пути». Сопоставляются точный диапазон и объект сравнения; сохраняются
  hashes полного EN/RU, исходные цитаты и их позиции в кодовых точках Unicode.
  Корректный перевод, другой диапазон/объект, отрицание, цитата и неоднозначная
  составная фраза этим правилом не классифицируются.
- Это дополнительное решение о пригодности к будущей публикации, а не новая
  рубрика или подмена положительного Ministral. Только полностью проверенный
  локальный quality_ready без Postiz/approval/dispatch/слота отменяется отдельным
  Audit. Исходник остаётся использованным; Variant, машинный verdict, raw,
  доказательства и закрытый quality_ready claim сохраняются. Свежий ещё занятый
  claim переводится в rejected атомарно с отменой до освобождения резерва.
  Неизвестный исход, незавершённый ремонт и повреждённая привязка блокируют действие.
- Проверка выполняется перед подсчётом ёмкости резерва и перед назначением слота;
  другая pending-публикация, её дата и готовые корректные статьи не изменяются.
  Сохранённый реальный EN/RU воспроизвёл точный дефект без модели или изменения
  записи. В изолированной копии прошла 31 focused-проверка за 15,84 с; две проверки
  PostgreSQL локально пропущены из-за отсутствия TEST_DATABASE_URL. Ruff E4/E9/F
  и diff-check прошли. Linux QA22b и развёртывание этого guard ещё не выполнены;
  закрытый шаблон не является общей гарантией семантической точности перевода.

## 0.11.6 — 2026-10-29: десятичная типографика проверенных единиц длины

- Устранён ложный отказ числового guard для `2.4-metre` → `2,4-метровым`
  и аналогичных десятичных измерений. Только закрытые английские формы
  meter/metre и foot/feet сопоставляются с конечным списком русских падежных
  и прилагательных форм метра и фута. Значение Decimal, категория знака,
  единица и количество каждого измерения обязаны совпасть в полном сегменте.
  Лишь после этого соответствующий unit-adjective перестаёт считаться ID;
  остальные защищённые идентификаторы по-прежнему сравниваются буквально.
- Меняются только символы числовой записи; слова, единицы, порядок, знаки и
  ссылки остаются прежними. Преобразования единиц нет. Иное, пропавшее или
  лишнее значение/измерение, неоднозначная пунктуация, даты, диапазоны,
  группировка, частичный ID/URL, явная ID-метка и неопознанный математический
  знак запрещают все замены сегмента. Прежний hard gate и независимые
  Ministral/LanguageTool сохранены; старые отказы и исходы не переоцениваются.
- В изолированной рабочей копии прошли 275 проверок четырёх translation-модулей
  за 31,20 с, Ruff E4/E9/F и diff-check. Покрыты оба живых типа десятичных
  прилагательных, повторы и перестановки единиц, пропуски, подмена знака,
  ID/date/range/URL и Unicode-операторы. Чистое чтение двух сохранённых полных
  EN/RU дало 1 и 4 замены и равенство прежней числовой проверки без модели
  или изменения записи. Linux QA22 и развёртывание 0.11.6 ещё не выполнены;
  отменённые кандидаты не отправлялись заново.

## 0.11.5 — 2026-10-29: проверка документации и описания GitHub

- Обновлены 44 документа, включая два новых: навигатор и описание выпуска.
  Прочитаны все 55 Markdown-файлов; 260 локальных ссылок ведут к существующим
  файлам, все руководства docs включены в навигатор. Diff-check успешен;
  pyproject и __init__ согласованы на 0.11.5, прежнее тело CHANGELOG сохранено.
- Проверены 482 публичных файла по запрещённым runtime-путям и строгим
  сигнатурам ключей: совпадений нет. Это ограниченный скан, не гарантия
  отсутствия любых возможных секретов во всей истории. Runtime-отчёты,
  модели, фотографии, cookies и секреты не включены в публикуемый состав.
- Описание существующего GitHub-репозитория обновлено под научную редакцию
  с переводом, проверками качества, резервом и публикациями через Postiz.
  Этот набор не меняет код, политики, конфигурацию или работающие сервисы;
  не запускает новые модели, публикации или повтор исходов. Исходники 0.11.5
  публикуются с отдельно описанными границами тестов и живой приёмки.

## 0.11.5 — 2026-10-29: актуализация всей документации и подготовка GitHub

- README и инструкция VM приведены к установленной 0.11.5: отдельно указаны
  полная qa-v19 (2759) и focused qa-v20/qa-v21 (1077/941), без суммирования
  уникальных тестов и без подмены живой доставки тестами с имитациями.
- Добавлены описание выпуска и навигатор всех руководств. Обновлены описание
  проекта, архитектура, критерии приёмки, editorial workflow, API/конфигурационные
  контракты, модель данных, интеграции, MADLAD/preflight и резерв. Старые инструкции
  и исследования помечены по области применения; прежние записи CHANGELOG,
  правила AGENTS, машиночитаемый TOPIC и версионированные промпты сохранены.
- Зафиксирован проверенный срез: 0.11.5 включена, 0/6 quality_ready, одна новая
  подготовка. Последняя независимо проверенная доставка в 22:24:06 МСК выполнена
  на 0.11.4 до установки; первая новая доставка 0.11.5 и заполненный запас ещё
  не подтверждены. Эти ограничения не скрываются публикацией исходников.
- Обновление затрагивает только публичную документацию. Runtime, код, модели,
  настройки, сервисы, обе БД и отправки этим набором не меняются. Перед загрузкой
  выполняются проверки локальных ссылок, покрытия навигатора, согласованности
  версий, сохранности истории и исходящих файлов; результаты оформляются
  дополнительной записью. Приватные runtime-отчёты и секреты в Git не входят.

## 0.11.5 — 2026-10-29: безопасное разбиение длинного предложения до запроса перевода

- Исправлена ложная ошибка разбиения: если найденная граница короткого
  предложения занимает меньше трети лимита, используется последний безопасный
  пробел внутри лимита. Слова, URL, числа, абзацы и хвост исходника сохраняются;
  жёсткого разрезания длинных токенов и увеличения лимита нет. Прежние подходящие
  границы предложений и контракт сохранённых метрик не меняются.
- Оба поля статьи проверяются чистой `preflight_segments` до первого HTTP
  заголовка или тела. Доказанный локальный отказ разбиения получает отдельный
  `TRANSLATION_PREFLIGHT_UNSPLITTABLE_INPUT`; прежний generic
  `TRANSLATION_UNSPLITTABLE_INPUT` у прямых `segments`/`translate` сохранён.
  Новая ошибка не означает успешный перевод и не переоценивает прежние
  заблокированные записи или неизвестный исход.
- Native `TranslationPreflightError` создаётся только до первого запроса
  выделенного переводчика. Его `dedicated-translation-preflight-v1` связывает
  точную модель, лимит, полный исходник со ссылкой, полный проверенный config,
  поле отказа и исходную локальную ошибку. Обычный pure replay, прямой
  `translate`, сетевой отказ и generic exception не получают эту provenance.
- Только новый native preflight-отказ допускает отдельное аудируемое завершение
  локального источника: exact durable claim/reserve binding, полный исходник и
  config, двухсобытийный trace, replay того же MADLAD/600 и проверенное изображение
  должны совпасть с сохранённой provenance. В одной транзакции запись отменяется,
  claim освобождается и сохраняется Audit; исходная ошибка, попытка, изображение,
  raw и навсегда использованный источник остаются. Прежний generic отказ, сеть,
  неизвестная модель, чужой переводчик и изменённое доказательство не снимаются.
- Завершающий локальный прогон четырёх translation-модулей вместе с новыми
  сценариями прошёл: 184 проверки за 12,83 с. Проверены короткая первая фраза при лимитах
  400/600/1400, целый URL и числа, неразрывные поля, поздний плохой абзац и
  отсутствие любого HTTP при preflight-отказе; прежние cache, numeric,
  alias/artifact gates и метрики сохраняются. Ruff E4/E9/F и diff-check успешны.
  Дополнительные локальные reserve-модули ещё проверяются; проверки PostgreSQL
  в локальной среде без TEST_DATABASE_URL пропускаются и не считаются Linux QA.
  Модели, источники и production этой правкой не вызывались; Linux QA и
  развёртывание 0.11.5 ещё не выполнены.

## 0.11.4 — 2026-10-29: точное сравнение Cloudflare-контакта без изменения прав

- Устранена причина остановки до изображения и моделей: два сохранённых source
  proof одной статьи имели одинаковую страницу и условия, но разные случайные
  XOR-ключи Cloudflare для одного контактного адреса. Только при сравнении
  эквивалентности учитывается точный декодированный адрес в
  `/cdn-cgi/l/email-protection#HEX` и `span[data-cfemail]`. HTMLParser определяет
  смещения исходных атрибутов; все остальные символы HTML остаются значимыми.
  Некорректный hex, UTF-8, управляющие символы, неполный адрес и превышение
  ограниченного размера блокируют проверку.
- Полные raw proofs и их SHA, индивидуальные проверки прав и текста лицензии,
  страницы, изображения и credit сохранены. Другой адрес, ссылка, текст условий,
  HTML, страница или изображение по-прежнему блокируют материал. Порядок выбора
  сохранённого proof и точные привязки прежних изображений не меняются.
- Добавлен отдельный типизированный путь только для корпуса дублей после
  аудируемого завершения доказанного локального отказа до изображения/моделей.
  Он требует точной записи PostgreSQL, исходного raw/native hash, reserve binding,
  terminal claim и двух scoped Audit, отсутствия моделей, правок, GET-артефактов,
  trace и внешнего состояния. Возвращается только полный английский исходник
  для dedup. Прежняя ветка отменённых изображений побайтно сохранена;
  `ARTICLE_IMAGE_PROOF_INVALID` не добавлен в общий список безопасных отказов.
  Исходная ошибка и failed-история не становятся quality passed и не разрешают
  повтор статьи или неизвестного исхода.
- Локально прошли 594 проверки: 456 сценариев изображений за 28,85 с и 138
  сценариев корпуса дублей за 237,54 с, без ошибок и пропусков. Добавлены 68
  проверок CF/raw/лицензии/страницы/изображения и 78 проверок типизированного
  завершения и подмен доказательств. Проверены оба формата v1/v2, один GET,
  неизменность raw proofs и чтение корпуса исключительно через SELECT.
  Critical Ruff E4/E9/F и diff-check успешны. Модели и production не вызывались;
  отдельная Linux/PostgreSQL QA20 и развёртывание этой версии ещё не выполнены.

## 0.11.3 — 2026-10-29: ограниченный вход редактора без потери исходных замечаний

- Установлена причина прежнего HTTP 400 редактора: вход 19016 токенов превысил
  фактический контекст Qwen 16384 при одном слоте. Серверный guard отклонил
  слишком длинный вход до генерации текста; OpenCode сохранил её как HTTP 503.
  Большую часть входа составляли повторённые данные LanguageTool. Старый запрос
  не повторялся, его исходный отказ и доказательства сохранены.
- Только будущая `hourly-practical-v2` использует отдельный
  `article-model-feedback-v1`. Полные raw events остаются в неизменяемом
  snapshot и trace. Модель получает каждый Min issue/warning и все блокирующие
  LT findings, включая точный UTF-16 span, сообщение и полный список замен;
  убраны повторы и транспортные поля. Оригинал, перевод, числа и literal URL
  не обрезаются. Raw feedback, контекст и модельный вход связаны hashes в claim,
  Audit, trace и evidence; обе ограниченные попытки сохраняют свои входы.
- Новый короткий промпт редактора занимает 1027 байт. Прежний v8-промпт,
  контекст и обработка ответов старых записей не меняются. На сохранённом
  проблемном snapshot чистая локальная реконструкция дала 11014 вместо 75848
  байт; генерации для этой статьи не было.
- До создания editor claim и запроса модели действует консервативная граница
  11263 UTF-8 байта: из 16384 оставлены 4096 на ответ, 1024 на chat context и
  один защитный токен. Превышение возвращает MODEL_INPUT_BUDGET. Полный известный
  исходный отказ сохраняется с отдельным snapshot/Audit и штатно отменяется;
  качество не становится passed. Это не карантин инфраструктурной ошибки.
  Неизвестный, failed, потерянный или незавершённый claim проверяется раньше
  бюджета и по-прежнему запрещает повтор.
- Исправлена только тестовая постановка ленивой загрузки фото: прямой receipt
  может вернуть None до появления IMG, затем общий bounded wait обязан увидеть
  настоящее изображение. На Linux прошли все 138 браузерных сценариев за 82,54 с;
  runtime worker не менялся. Исходный QA18 с 2731 passed и одним отказом fixture
  сохранён; полный QA19 нового набора ожидает запуска.
- Локально прошли 323 проверки редактора и резерва в четырёх модулях за 81,34 с,
  без ошибок и пропусков, включая 27 новых сценариев. Проверены полный цикл с кратким входом,
  свежими Min/LT, вторую правку по последнему кандидату, подмену budget proof,
  сохранность всех замечаний, старый replay и отсутствие model request при
  известном превышении. Ruff и diff-check успешны.
  Эта версия ещё не развёрнута; новая политика на VM не активирована.

## 0.11.3 — 2026-10-29: отдельная практическая рубрика для фактических ответов LanguageTool

- Добавлена явно выбираемая `hourly-practical-v2` для будущих записей.
  LanguageTool возвращает `issueType=uncategorized` в том числе для обычного
  согласования, запятой и стиля. В v1 такие ответы блокировались раньше
  применения практического языкового бюджета. V2 распознаёт только известные
  категории GRAMMAR, STYLE и PUNCTUATION при прежних проверках структуры,
  типа и границ UTF-16. Грамматика расходует прежний ограниченный бюджет;
  стиль и необязательная пунктуация остаются советами. Неизвестные категории,
  типы и повреждённые ответы не становятся советами.
- Исходные LT matches, замечания и raw моделей сохраняются в evidence.
  Версия рубрики закрепляется до первого запроса в записи, попытке, Audit,
  варианте и согласовании; смешение v1/v2 отклоняется. Старые правила v1,
  meaning-first и legacy сохраняют прежнее поведение и прежние промпты.
  Исторические отказы не пересматриваются и не возвращаются в резерв.
- Новый отдельный промпт просит явно сверять содержательные предложения
  и отрицания not/no/without: аудит обнаружил, что положительный вердикт
  Ministral иногда пропускает такие ошибки. Это уточнение не гарантирует
  смысловую безошибочность. Проверки основного объекта, фактов, чисел, literal
  URL, языка, прав, инфраструктуры, дедупликации и интервала сохранены.
- На Windows прошли 295 регрессионных проверок в семи модулях; одна проверка
  symlink пропущена из-за недоступной привилегии хоста. После обезличивания
  fixtures повторно прошли 32 проверки новой классификации. Использованы четыре
  наблюдавшиеся структуры LT с синтетическими русскими фразами; исходные
  статьи и runtime-идентификаторы в публичные fixtures не включены.
  Независимый replay восьми сохранённых v1 LT samples совпал с прежним
  кодом; три прежних промпта побайтово сохранены. Ruff и diff-check успешны.
- Версия 0.11.3 согласована в пакете. Развёртывание и активация новой рубрики
  на VM пока не выполнены; действующая политика этой правкой не меняется.

## 0.11.2 — 2026-10-29: завершена сверка метаданных первой доставки

- Отдельной аудируемой сверкой удалена только устаревшая служебная ошибка
  Bridge первой иллюстрированной статьи. Фактическая доставка уже была
  подтверждена; новых submit, upload или Send нет. Исходная unknown observation,
  её ошибка и история сохранены.
- Сквозная проверка первой записи теперь согласована: Postiz PUBLISHED,
  подтверждённый dispatch, доставленный слот, прежние текст и исходное
  изображение. Два свежих чтения MAX подтвердили ровно одно сообщение,
  изображение перед жирным заголовком и обычный основной текст.
- 19 локальных проверок guard прошли. Сверка business data всех 37 таблиц
  подтвердила только ожидаемое исправление метаданных и один Audit; все
  113 записей до установки 0.11.2 сохранены. Это разовая сверка доказанного
  результата, не разрешение повторять отправку с неизвестным исходом.

## 0.11.2 — 2026-10-29: обычная доставка иллюстрированной статьи и сверка Postiz

- ESA/Hubble POTW 2535a прошла обычный серверный цикл до PUBLISHED
  в 13:38:12 МСК, без восстановительного helper. Сквозная проверка подтвердила
  точные согласование, изображение, dispatch, доставленный слот и Postiz.
  Два свежих reload MAX нашли ровно один полный текст 2748 символов,
  фотографию выше настоящего жирного заголовка и обычный основной текст;
  обе исключённые строки подписи отсутствуют. Проверены 2033210 байт исходного
  изображения Postiz; все 113 записей до установки 0.11.2 остались неизменными.
- Для первой уже подтверждённой доставки выполнена одна ограниченная сверка
  Temporal: completed submit события 24 сохранён, продолжение после границы
  27 выполнило новый checkPostStatus 31/33 и штатный updatePost 37/39.
  Тот же пост стал PUBLISHED, новый run завершился с 67 событиями; новых
  submit, upload, Send, finalize, plug-отправок и дочерних задач нет.
  Унаследованные getPost/getPostsList/submit совпали с исходной историей;
  прежние 1030 событий, unknown observation и ошибка сохранены.
- Закрытые guards этой разовой операции прошли 48 локальных проверок.
  Финальный worker ранее прошёл 138 фокусных браузерных проверок, отдельно
  успешны 38 прежних сценариев. Полная qa-v17 из 2642 тестов остаётся проверкой
  предшествующего baseline и не выдаётся за повторный прогон финального worker.
- README, текущие разделы VM и адаптера обновлены по фактической доставке.
  Остаточная ошибка Bridge первой записи ещё сверяется отдельно; её цепочка
  пока не объявляется полностью согласованной. Обычную полную цепочку
  подтверждает вторая статья. Готовый резерв на проверенном срезе равен нулю
  при цели шесть, NOIRLab 2541 готовится; горизонт источников 43800 часов включён.
  Заполненный резерв и бесконечная почасовая готовность не утверждаются.
- Проверены 74 локальные ссылки в восьми связанных документах, повреждённых
  ссылок нет; diff-check успешен. Версия согласована как 0.11.2; runtime-код
  в этом обновлении документации не менялся.

## 0.11.2 — 2026-10-29: доставка изображения сверена без повторной отправки

- Существующий пост ESA/Hubble POTW 2536a дважды найден после свежего reload
  ровно в одном экземпляре. Подтверждены полный текст, фотография 1920x1440
  перед заголовком, настоящий жирный заголовок и обычное тело; обе исключённые
  строки подписи отсутствуют. Исходный upload SHA сохранён отдельно от
  обработанных MAX пикселей.
- Одноразовая аудируемая native reconcile зафиксировала published/verified
  без publish/upload/Send. Исходная unknown observation и её ошибка сохранены;
  счётчик физической отправки остался равен одному. Слот получил факт доставки,
  адресат возвращён на паузу между отправками; остальные записи не изменялись.
- Дополнительно исправлено ожидание истории после reload: подпись может
  появиться позже редактора, поэтому её отсутствие ждёт общий предел 15 секунд,
  а лишнее совпадение сразу блокирует подтверждение. Это ожидание только чтения.
  Финальные 138 локальных браузерных проверок прошли; прежние 38 проверок
  транспорта также прошли. Полная qa-v17 из 2642 проверок предшествует этой
  узкой правке worker и не выдаётся за повторный прогон финального файла.
- Обе правки worker установлены атомарно без перезапуска API. При каждой
  установке business data 37 таблиц и журнал доставки сохранились. Версия
  Python остаётся 0.11.2 в том же наборе изменений.
- Postiz после длительного pending завершил собственную задачу со статусом
  ERROR, хотя MAX уже подтвердил сообщение. В журнале редакции честно
  сохранён awaiting_postiz; полная цепочка и освобождение очереди ожидают
  отдельной сверки статуса Postiz без новой отправки. Готовый резерв на этом
  этапе содержит одну следующую статью, целевой размер остаётся шесть.

## 0.11.2 — 2026-10-29: установлен worker чтения ленивого фото MAX

- Установлена узкая поправка `browser_worker`: прокрутка крупного placeholder
  или изображения выше точной подписи ограничена одним найденным постом.
  После ленивой загрузки повторно проверяются полный текст, положение,
  размер, пропорции, жирный заголовок и обычный основной текст. Заглушка сама
  по себе подтверждением не является.
- Добавлен точный HTTPS-host `i.oneme.ru` без wildcard и userinfo. Объявленный
  src и фактический currentSrc проверяются независимо, включая отказ при
  автоматическом HTTP→HTTPS upgrade браузера. Подписанный query не попадает
  в receipt proof; SHA исходного upload сохранён, SHA преобразованных MAX
  пикселей не утверждается. Старые origin и текстовые маршруты сохранены.
- Локально прошли 132 проверки receipt/изображения за 86,68 с и 38 прежних
  браузерных сценариев за 105,34 с, без ошибок и пропусков. В headless Chrome
  проверены длинная подпись, offscreen placeholder, фото 1920×1440, два reload,
  native bold, обычное тело и ровно один Send; опасные host/схемы и ложные
  изображения отклонены. Ruff и diff-check успешны.
- При установке одного worker API оставался онлайн, business data всех
  37 таблиц сохранены. Полная qa-v17 из 2642 тестов остаётся проверкой прежнего
  baseline; новый worker имеет отдельные 170 фокусных проверок. Инструкция
  адаптера дополнена контрактом receipt и no-Send reconcile.
- Нативная сверка уже отправленного сообщения ещё ожидается. Исходная попытка,
  её unknown и причина сохраняются до успешного штатного reconcile;
  повторного Send, upload или нового dispatch этот патч не разрешает.

## 0.11.2 — 2026-10-29: подтверждение лениво загружаемой фотографии MAX

- Первое иллюстрированное сообщение фактически появилось в MAX с жирным
  заголовком и новой подписью. Полный текст найден после двух reload ровно
  в одном экземпляре; прежние строки подписи отсутствуют. Повторного Send нет.
- Старое подтверждение ошибочно считало сообщение исчезнувшим: длинная подпись
  оставляла фото за пределами экрана в image-placeholder, а после прокрутки
  MAX показывал JPEG с точного HTTPS-host i.oneme.ru, не учтённого селектором.
  RO-сверка увидела 1920x1440 над заголовком; исходный upload SHA сохранён,
  пиксели после обработки MAX не называются побайтово исходными.
- Готовится узкая поправка чтения: прокрутка внутри точного сообщения для
  загрузки фото и разрешение наблюдаемого точного CDN-host. Заглушка сама
  подтверждением не является; размер, положение, пропорции, точный текст,
  жирный заголовок и обычное тело остаются обязательными. Unknown отправки
  будет сверяться существующим no-Send reconcile, без повтора публикации.
- До завершения сверки служебный dispatch остаётся unknown, Postiz QUEUE;
  следующий текст уже прошёл качество и сохранён как quality_ready.

## 0.11.2 — 2026-10-29: CAS развёрнут, тот же пост назначен с изображением

- Установлен Postiz `0.11.2-illustrated-cas` на закреплённой базе
  `0.10.8-reliability`. При замене образа все 114 записей и business data
  37 таблиц сохранились, тот же DRAFT остался неизменным. API агента не
  останавливался; обе compiled-копии browser provider сохранены.
- CAS разрешает только точные draft/schedule для одного DRAFT. Назначение
  сохраняет content, settings, group, integration и одно изображение;
  проверяются будущая дата UTC, expected hash и mutation lease. Шесть знаков
  дробной секунды Python поддерживаются без замены или округления даты.
  Опасные типы, изменённый snapshot, потоки и повторная публикация блокируются.
- На фактическом compiled guard прошла 81 проверка; отдельно успешны 8
  симуляций сборки и проверка реального snapshot Prisma с payload Python.
  Новые исходник и compiled hash закреплены в manifest сборки, как и старый
  образ с его preimage. Это проверки назначения, а не доказательство MAX Send.
- Доказанный HTTP 409 до контроллера разобран одной аудируемой сверкой:
  старый отказ сохранён, перевод и изображение не создавались повторно,
  новый Postiz post не создавался. После штатного start73 обычный обработчик
  успешно назначил тот же ESA/Hubble POTW 2536a.
- Свежий GET в 11:40 МСК подтвердил QUEUE на 11:43:52 МСК: прежние 3045
  символов, одно JPEG, точное согласование и pending dispatch, ноль attempts.
  Следующая ESA/Hubble POTW 2535a готовится независимо; известный отказ
  изображения другого источника сохранил отмену и не заблокировал подбор.
  Фактическая доставка нового формата в MAX пока ожидает отдельной сверки.
- Python остаётся 0.11.2 с прежними результатами qa-v17, без правок runtime.
  README и текущий статус VM обновлены. Восемь документов прошли проверку
  локальных ссылок; ограниченный аудит 474 публичных файлов не выявил
  приватных путей или строгих сигнатур ключей. Всю Git-историю заново не
  проверяли; подтверждение расписания не подменяет подтверждение доставки.

## 0.11.2 — 2026-10-29: исправление CAS при назначении поста с изображением

- Живой проход впервые довёл новый формат до DRAFT с одним изображением:
  перевод, независимый Ministral и LanguageTool завершились успешно.
  Назначение расписания получило HTTP 409 до изменения Postiz.
- Проверка установленного compiled guard показала причину: CAS принимал
  только type=draft и отвергал type=schedule до mutation/start. Изолированное
  выполнение именно установленного модуля подтвердило ноль bridge calls/leases;
  свежий GET сохранил тот же DRAFT, текст и изображение, без dispatch/approval.
- Готовится узкое разрешение CAS для schedule с сохранением одного точного
  поста, контента, изображения, адресата, версии и mutation lease. Штатный
  Python-код и его 124 runtime-файла остаются прежними qa-v17; проверка Node
  и фактическая доставка описываются отдельно после завершения.
- Существующий черновик не пересоздаётся, модели не пересэмплируются.
  Отдельная сверка доказанного отказа до контроллера сохраняет прежний отказ
  и позволяет только новое точное назначение того же черновика, без снятия
  настоящих sending/unknown. На время исправления общая пауза 72.

## 0.11.2 — 2026-10-29: установка пятилетнего корпуса с сохранением истории

- Полная qa-v17 прошла 2642 теста в 52 модулях за 826,73 с на изолированной
  PostgreSQL, без ошибок и пропусков. Это проверка исходников и fakes;
  фактическая доставка нового формата подтверждается отдельно.
- Python 0.11.2 установлен на VM, ready/autonomous после короткого перезапуска
  за 1,672 с. Все 113 записей сохранены; сверка business data 37 таблиц выявила
  только один ожидаемый startup Audit. Схема, модели, приватная конфигурация
  и прежний образ Postiz не менялись.
- По точному ответу владельца «До 5 лет» на паузе 70 изменён только возраст
  будущего источника: 8760 → 43800 часов. Прежние owner instruction сохранены;
  девять полных политик трёх источников для текстового формата, v1 и v2
  архивированы по hashes до изменения. Добавлен один Audit активации политики.
- Для 26 записей подтверждён exact policy replay. Остальные 87 исторических
  записей не реконструировались из этой текущей политики и сохранены без
  переоценки, повторной подготовки или изменения hashes. Отмены и источники
  остаются в дедупликации; лимит 350 слов и интервал 3600 секунд сохранены.
- Отдельный штатный start установил control 71 в 10:57 МСК. Началась подготовка
  ESA/Hubble POTW 2536a, изображение получено; перевод и дальнейшая проверка
  продолжаются. Фактическая доставка v2 в MAX и наполненный резерв пока не
  объявляются подтверждёнными.
- README и текущий статус VM обновлены. Восемь документов прошли проверку
  локальных ссылок, 472 публичных файла — ограниченный поиск приватных путей
  и строгих сигнатур ключей, без находок. Все 124 локальных runtime-файла
  совпали с замороженным manifest qa-v17; этот статический аудит не является
  полной проверкой всех возможных секретов в Git-истории.

## 0.11.2 — 2026-10-29: корпус пяти лет и устойчивый отказ изображения

- По прямому ответу владельца «До 5 лет» подготовлено расширение возраста
  источника до 43800 часов в прежних разделах NOIRLab/ESA/Hubble. Срок не
  меняет права, полные тексты, качество, интервалы или историю источников.
  Точные прежние политики сохраняются по hash для воспроизведения старых записей.
- Реальная подготовка v2 выявила расхождение: полная загрузка допускала 10 MiB,
  а 512 диапазонов по 8192 байта ограничивали Range четырьмя MiB. Бюджет теперь
  вычисляется из общего лимита (1280 диапазонов); ETag, два соединения, таймаут,
  однократность каждого диапазона и общий предел 10 MiB сохранены.
- Источник с известным отказом изображения до первой модели остаётся отменённым
  и использованным. Для проверки смысловых дублей нужен его полный сохранённый
  input, а не несуществующий журнал перевода; повреждённые доказательства и
  неизвестные исходы сохраняют блокировку. Проверки этого изменения продолжаются.
- Установленный 0.11.1 прошёл qa-v16: 2555 тестов в 50 модулях за 753.08 с,
  без ошибок и пропусков, на отдельной PostgreSQL. При установке 112 записей
  и business data 37 таблиц сохранились; ready после перезапуска за 1.60 с.
  v2 активирован отдельным Audit, затем start69. Новая фотография отклонена до
  моделей; опубликованным новый формат не объявляется. Сейчас общая пауза 70.
- Фокусные проверки NASA/science/смешанного подбора и нового pre-model dedup
  прошли 462 теста без ошибок/пропусков. На VM строгий RO replay прочитал все
  113 полных исходников, после чего отдельный аудит закрыл только незавязанный
  claim известной локальной ошибки корпуса. Все 113 записей и старый код ошибки
  сохранены; модели, source GET, Postiz и MAX при сверке не вызывались.

## 0.11.1 — 2026-10-29: подпись без архивной и машинной строк

- По новому прямому поручению владельца article-illustrated-v2 убирает обе
  указанные строки из будущих публикаций. Источник, CC BY 4.0, полный credit
  изображения, нейтральное обозначение адаптации и жирный заголовок сохраняются.
  Прежний v1 и текстовые подписи воспроизводятся без переписывания истории.
- Перед изменением сервер поставлен на паузу 68: новой внешней очереди нет.
  Первая подготовка v1 и её полный trace сохранены; опубликованным новый формат
  ещё не объявляется. Изменение версии согласовано в pyproject и __init__.
- Для установленного 0.11.0 qa-v15 прошёл 2247 тестов в 50 модулях за 639.59 с
  на отдельной PostgreSQL с fakes. Установка сохранила 111 записей и business
  data 37 таблиц; перезапуск добавил один startup Audit, активация один Audit.
  До нового поручения выполнялся start67. Это не доказательство доставки v2.
- Проверки, установка 0.11.1 и фактическая публикация нового формата продолжаются.
- Фокусная регрессия v1/v2 прошла 617 тестов: 512 проверок изображения/браузера
  и 105 проверок атрибуции, policy replay и полной цепочки на mocks. Подтверждены
  реальные native bold и reload в headless Chrome; это не доставка в MAX.
- Первую локальную запись v1 отменили отдельным точным аудитом после известного
  завершения моделей и нуля внешних объектов. Сохранены raw/trace и занятый
  источник; все 111 прежних записей неизменны. Освобождён только её старый claim.
- README и инструкция VM актуализированы; ещё шесть инструкций помечены как
  исторические. Проверены локальные ссылки, 470 публичных файлов и ограниченный
  набор из 64 неопубликованных коммитов/668 версий файлов: приватных путей и
  строгих сигнатур ключей не найдено, 20 URL-срабатываний — тестовые fixtures.
  Это не абсолютная гарантия отсутствия любых секретов во всей Git-истории.

## 0.11.0 — 2026-10-28: жирный заголовок в новом формате

- По дополнительному поручению владельца иллюстрированный пост использует
  настоящее жирное начертание заголовка в редакторе MAX, без буквальных `**`.
  Текст snapshot остаётся прежним; признаки берутся из подписанного заголовка
  и presentation contract. Основной текст должен остаться обычным.
- Точный qa-v14 без этой последней правки форматирования прошёл 2237 тестов
  в 50 модулях на отдельной PostgreSQL с fakes, без ошибок и пропусков.
  Production API оставался онлайн на паузе 66, все 111 записей и business data
  37 таблиц сохранились; исключались только runtime/ledger counters.
- В настоящем пустом редакторе MAX выполнена обратимая проверка: выделенный
  заголовок жирный, тело обычное, полный текст не изменён; затем composer
  восстановлен пустым после reload. Ноль загрузок вложений и Send, это ещё
  не доказательство публикации нового формата.
- Полная регрессия с новым форматированием и живая установка продолжаются.
- README и инструкция VM обновлены под кандидат 0.11.0 с явным отделением
  текущей VM 0.10.8 от ещё не установленного кандидата. В 470 публичных файлах
  не найдены приватные пути/строгие сигнатуры ключей, локальные ссылки проверены;
  это не объявляется полным аудитом всей Git-истории.

## 0.11.0 — 2026-10-28: иллюстрации научных статей и новая подпись

- По прямому поручению владельца подготовлен article-illustrated-v1: изображение
  перед текстом и нейтральное «Перевод и адаптация.» вместо машинной подписи.
  Полные credits, источник и лицензия сохранены; старый формат воспроизводится
  по прежним marker/policy hashes без изменения истории.
- Изображение выбирается по точному ID статьи из официального JPEG 2048x1536
  (при отсутствии такого файла — из объявленного Publication JPEG)
  NOIRLab/ESA/Hubble. Сохраняются полные права, HTML, credits и hash файла.
  JPEG проверяется до моделей: до 10 MiB/40 MP, длинная сторона от 1600 пикселей,
  короткая от 800; повтор неизвестной загрузки запрещён durable claim.
- Картинка связывается с reserve/variant/approval и сохраняется при Postiz schedule.
  Браузер сверяет крупное изображение над подписью после reload, без повторного
  Send. Доставленные пиксели после преобразования MAX не называются исходным SHA.
- Для сети VM добавлена ограниченная передача диапазонами HTTP с If-Match/ETag:
  до 512 диапазонов по 8192 байта, два соединения, без повторов диапазонов.
  Полный JPEG 1920x1200/1600376 байт реально получен на VM за 46 секунд;
  это проверка загрузки, не публикация. HTTP 401/403/429 останавливают источник.
- Только новый формат сохраняет две буквальные mailto-ссылки официального
  блока NOIRLab; письма не отправляются. Абзац источника не обрезается,
  прежний parser/replay остаётся строгим. Старый текстовый резерв не учитывается
  как готовый резерв формата с обязательным изображением.
- Пауза во время чистого подбора источника теперь закрывает только selecting
  claim без записи и моделей; новая ревизия управления, token, отсутствие
  bind/model history проверяются в одной транзакции. Bound/unknown не снимаются.
- Версия пакета согласована в двух файлах, новое поручение сохранено в AGENTS.
  Изолированные проверки и живая установка этого изменения ещё выполняются.

## 0.10.8 — 2026-10-28: установка постоянного исправления без остановки API на время QA

- Точный qa-v12 прошёл 1936 тестов в 43 модулях за 573.46 секунды на отдельной
  PostgreSQL с fakes: без failed/errors/skipped, все 462 staged файла неизменны.
  Production worker оставался онлайн. Сверка всех 37 таблиц сохранила business
  data; из равенства исключены только editorial_control.runtime и счётчики
  editorial_article_ledger.revision/updated_at. Это не исключение для records,
  approvals, dispatches, slots или истории.
- Пройдены 87 локальных Node-тестов, по 28 проверок настоящего compiled JS
  backend и orchestrator в образе. В изолированной сети обе копии провайдера
  прошли реальный отказ TCP → pending, затем только status локального HTTP
  fixture → completed. Ни модели, ни MAX этими тестами не вызывались;
  успешный fixture не объявляется живой доставкой или сбоем production.
- На VM установлен закреплённый образ 0.10.8-reliability и ровно 15 публичных
  diff-файлов Python/провайдера/тестов/документов/compose. Сначала обновлён
  провайдер при доступном API; затем на паузе 64 сохранены обе БД и runtime,
  исходники установлены с работающим API, выполнен короткий перезапуск Python.
  Source/import/distribution равны 0.10.8, схема 0008 без миграции. Все 108
  records и 37 business-таблиц при установке неизменны; перезапуск добавил
  только ожидаемый startup Audit. Приватная конфигурация сохранена.
- Установлены постоянные защиты: только типизированный отказ fetch сохраняет
  прежний pending провайдера; HTTP/auth/config/JSON и terminal отказы остаются
  ошибками. Отправитель закрывает просроченный отменённый слот в missed лишь
  после свежего точного Postiz ERROR, нуля attempts/receipt, отозванного approval
  и проверки всех привязок/leases под текущим fence. Dispatch sending/unknown
  или возможная доставка требуют сверки. Отменённая статья не восстанавливается,
  её источник остаётся использованным; занятые календарные часы пропускаются.
- Штатный start возобновил control 65: один worker, waiting_dispatch без ошибок.
  Сохранён тот же будущий QUEUE на 16:12:43 МСК, исходные текст/дата/approval,
  ноль attempts и отсутствие нового POST. Все 122 runtime-файла совпали с qa-v12.
  Публикация 15:12:42 МСК остаётся PUBLISHED; после перезапуска полный текст
  снова независимо прочитан в MAX дважды, по одному точному экземпляру.
- Обе новые compiled-копии дополнительно вызвали настоящий API status агента
  для существующей опубликованной и будущей записи: четыре проверки дали
  соответственно completed и pending. Выполнено ноль submit/Send; это живая
  проверка чтения статуса, а не новая публикация или искусственный сбой.
- После установки 0.10.8 сервер фактически доставил следующую новую статью
  в 16:12:55 МСК: Postiz PUBLISHED, один published attempt, без автоправок,
  шесть замечаний LT сохранены как advice. Интервал от прежней доставки —
  3612.324343 секунды. Полная сверка цепочки подтвердила все 122 runtime-файла
  0.10.8 и точную сохранность 87 прежних записей. Полный текст независимо
  прочитан в MAX дважды: по одному точному экземпляру. Это двенадцатая новая
  доставка после активации практичной рубрики; отдельное сравнение полного
  EN/RU не выявило существенного изменения смысла.
- Следующий точный pending назначен на 17:12:56 МСК. В снимке 16:31 МСК:
  110 records, 46 published, 57 cancelled, три quality_ready, один pending,
  одна новая подготовка, прежние human и failed. Пополнение резерва автоматически
  продолжается через MADLAD/Ministral без текущей ошибки; sending/unknown
  отправок и leases нет. Старые отказы не восстановлены. Production-сбой
  намеренно не создавался; недоступность сети/MAX всё ещё может задержать
  доставку, догоняющих пачек нет.

## 0.10.8 — 2026-10-28: устойчивость почасовой очереди при недоступности агента

- Провайдер MAX сохраняет pending при временном транспортном отказе агента.
  Повтор submit или отправки не выполняется; после восстановления продолжается
  проверка результата. Ошибки доступа, протокола и известные terminal отказы
  не подменяются успешной доставкой.
- Для точной отмены после Postiz ERROR подготовлено автоматическое закрытие
  слота только с доказанными нулём попыток, отозванным approval и отсутствием
  внешнего результата/leases. Исходный текст, отмена и дедупликация сохраняются.
  sending/unknown остаются заблокированы. Занятый час missed не используется повторно.
- Добавлена сборка трёх файлов провайдера поверх закреплённого готового образа
  без пересборки интерфейса. Версия Python согласована в обоих файлах.
  Адресные тесты, полная регрессия и установка этой версии ещё выполняются.

## 0.10.7 — 2026-10-28: проверка установки и освобождение несостоявшегося слота

- Полная регрессия qa-v11: 1885 passed в 42 модулях на изолированной
  PostgreSQL, без failed/errors/skipped. Все 455 staged файлов неизменны;
  37 production-таблиц с 108 записями и 254 core/runtime файла совпали до/после.
  Известный pending проверялся только чтением. Тесты использовали fakes и сами
  по себе не подтверждают работу моделей или доставку MAX.
- После закрытого backup обеих БД установлены шесть файлов 0.10.7.
  Source/import/distribution согласованы, схема 0008 без миграции. Все модели
  БД, 108 записей, pending/slot и приватная конфигурация сохранены точно;
  установка не запускала worker и не выполняла отправку.
- Установлена причина остановки после фактической доставки 10:10:50 МСК:
  пока агент был остановлен для исправлений, Postiz получил
  MAX_BROWSER_AGENT_UNAVAILABLE в 11:37–11:40 МСК. При запуске в 11:51:13 МСК
  sync впервые импортировал ERROR, отозвал approval и отменил dispatch без
  единой попытки отправки. Его pending-слот со старой control revision остался
  занят и блокировал следующие статьи. Время возникновения ERROR у Postiz
  отдельно не установлено; 11:51:13 — время наблюдаемого импорта.
- На общей паузе 62 и остановленном сервисе выполнена одна адресная сверка
  точного отменённого задания: свежий Postiz GET ERROR, неизменные текст/дата,
  отозванное согласование, ноль attempts и отсутствие leases/receipt доставки.
  Только просроченный слот переведён в missed со снятием active_target и новой
  Audit-записью; все 108 records и остальные 35 таблиц неизменны. Исходник,
  отмена, dispatch, approval и история сохранены; повторной отправки не было.
- Зафиксировано ограничение обслуживания: общая пауза агента не останавливает
  уже назначенную очередь Postiz. API агента нельзя останавливать при будущем
  QUEUE без завершения/сверки отправки либо согласованной остановки отдельного
  provider worker с проверкой внешнего исхода. Автоматический перевод
  sending/unknown в missed не добавлен; освобождение было одноразовым.
- Единственный серверный worker возобновлён штатным start 62→63. Следующая
  новая статья фактически доставлена в 15:12:42 МСК: один published attempt,
  Postiz PUBLISHED, точные approval/text/date/quality bindings. Кандидат без
  автоправок, Ministral и hard gates пройдены, пять замечаний LT сохранены
  как advice практичной рубрики. Полный текст независимо прочитан в MAX
  дважды: точное совпадение и ровно один экземпляр в каждой проверке.
  Это одиннадцатая новая доставка после активации рубрики; перерыв между
  10:10 и 15:12 не объявляется непрерывной почасовой работой.
- Следующий точный pending назначен на 16:12:43 МСК, ещё четыре записи
  quality_ready. Внешних мутаций при независимой сверке не было; отменённая
  статья не восстановлена, отправок догоняющей пачкой нет. Автоматическое
  восстановление подобного известного сбоя ещё не реализовано в 0.10.7.

## 0.10.7 — 2026-10-28: точность даты при возобновлении прежнего QUEUE

- После активации практичной рубрики сервер самостоятельно подтвердил десять
  новых доставок. SELECT-сверка показала ровно по одному slot/approval/dispatch
  и одному published attempt; интервалы 3610.994–3616.588 секунд. Старые 87
  записей сохранены точно. Резерв достигал шести дважды: 02:15:39 и 05:38:38 МСК.
  Первая и десятая публикации независимо прочитаны в MAX два раза каждая:
  точный полный текст, по одному экземпляру, при строгой сверке ожидающего QUEUE.
- Для пополнения найдены ещё семь допустимых полных подписей 250–350 слов.
  Штатными pause/settings/start изменён только мягкий предел 250→350,
  control 56→59. Старые тексты и утверждённая дата ожидающей публикации
  сохранены. При возобновлении обнаружено точное расхождение точности:
  native due 1793175051.701083 и Postiz GET 1793175051.701; попыток отправки нет.
- Исправлен только resume известного pending: binding, cadence и аудит
  используют исходную подписанную дату dispatch; наблюдаемая дата GET
  допускает разницу менее 1 мс и обязательно конечна. Approval, slot token,
  текст, даты в БД, rights/quality и неизвестные исходы не меняются. Нового POST,
  schedule или согласования этот путь не создаёт. Версия согласована в двух файлах.
- Первые 14 адресных regression-проверок прошли; добавлены ещё две проверки
  фактического интервала. Полная регрессия, установка и живое возобновление
  этого исправления на момент записи ещё не подтверждены.

## 0.10.6 — 2026-10-28: установка и явная активация практичной рубрики на VM

- Точный qa-v10 прошёл 1869 тестов в 41 модуле на отдельной PostgreSQL,
  без ошибок и пропусков. Все 454 staged hashes сохранились. Дополнительная
  сверка подтвердила неизменность 252 production core/config/env/metadata
  файлов и всех 37 таблиц целиком; 87 записей, пауза 55 и пустые operational
  counters сохранены. Это проверки с fakes, не реальные модели или MAX.
- После полного backup на VM установлены ровно 14 diff-файлов. Source и
  metadata равны 0.10.6; схема 0008 без миграции, образ Postiz прежний.
  Все таблицы, claims и приватная конфигурация совпали до/после установки.
  Установщик допускает замену только двух известных публичных .example.json;
  рабочие файлы config и policy установкой не заменялись.
- Отдельная одноразовая активация изменила только review_rubric и добавила
  точное новое поручение в owner_instruction базовой политики. Зарегистрированные
  NASA/NOIRLab/ESA политики получили hourly-practical-v1 только для будущих
  записей. Политика, приватный before image и hashes закреплены в Audit.
  Штатный ServerState.control возобновил control 55→56, затем запущен
  единственный серверный worker. Добавлены ровно два Audit; все 87 записей,
  старые отказы/unknown и адресаты сохранены точно. Replay старых критериев
  и повторная публикация старых источников не выполнялись.
- На момент запуска новый резерв ещё пуст и новая фактическая доставка MAX
  не подтверждена. Установка и успешные тесты не объявляются наполнением канала.

## 0.10.6 — 2026-10-28: практичные критерии почасового машинного перевода

- По новому прямому поручению владельца подготовлена отдельная
  hourly-practical-v1. Стиль, буквальные обороты, варианты имён и отдельные
  языковые ошибки становятся сохраняемыми советами LT. Для типизированных
  грамматических/орфографических замечаний действует бюджет
  max(8, ceil(N русских слов / 10)); URL, ранее доказанные термины и необязательные
  стиль/типографика его не расходуют. Невалидный LT и массовые ошибки блокируют.
- Новый более короткий промпт независимого Ministral сначала проверяет
  идентичность основного объекта и существенный смысл; числа, отрицания,
  центральные факты, причинность и направление остаются блокирующими.
  Противоречивое approved=true с непустыми issues не превращается в одобрение.
  Raw моделей и LT сохраняются; права, язык, ссылки, hard gates, два durable
  repair claims, exact approval/слоты/интервал 3600 секунд и дедупликация прежние.
- Рубрика выбирается явно в политике только для будущих записей, с привязкой
  record/attempt/Audit, prompt hash, evidence и повторной независимой проверкой
  изменённого текста. Прежние markerless и meaning-first-v1 записи сохраняют
  старые критерии. Пример политики выключен; массового восстановления нет.
  Версия согласована в pyproject и __init__, поручение добавлено в AGENTS.
- Старый кандидат 87 завершился not_repairable и отменён без внешнего состояния.
  Read-only сверка выявила ложный semantic approved: телескоп Gemini South
  превратился в созвездие, а предупреждение модели не стало отказом. Это
  ограничение рецензента сохранено в истории; новый промпт отдельно проверяет
  основной объект. По переходному watch control поставлен на паузу 54→55
  только после завершения моделей; 87 записей, все отказы и старый unknown
  сохранены, активных отправок, slots, моделей и leases нет.
- Проверки модуля качества: 316 passed; после уточнения бюджета и точного
  replay свежего scientific evidence отдельно 29 практических тестов passed.
  Дополнительно прошли 95 проверок автоматической политики/коррекции и 8
  итоговых новых сценариев, включая полную fake-цепочку до однократной доставки
  и паузы. Ruff и diff-check успешны. Полная изолированная
  QA, установка, активация новой политики, наполненный резерв и новая
  фактическая доставка MAX на момент этой записи ещё не подтверждены.

## 0.10.5 — 2026-10-28: короткие полные исходники для наполнения резерва

- Ограниченная подготовка NOIRLab iotw2619a завершена известным
  not_repairable после семантического approved и трёх словарных замечаний LT.
  Запись отменена штатно без DRAFT/approval/отправки; полный исходник, перевод,
  проверки, ответ Qwen и занятый источник сохранены. Повтор не выполняется.
- Read-only аудит третьей страницы NOIRLab выполнил десять различных GET
  без повторов и нашёл семь новых полных подписей 182–244 слова. Повторно
  проверены parser, terms/rights hashes, текущие политики, все 86 записей,
  исключения и пятиграммы; между новыми кандидатами overlap не выше 0.251.
  Семь исходников не объявляются семью проверенными переводами.
- Для следующего подбора штатными ServerState.control/settings под единственным
  worker lock изменён только мягкий max_words 500→250: pause 51→52,
  settings 52→53, start 53→54. Полный снимок БД после каждого этапа подтвердил
  сохранность истории и адресатов; добавлены ровно три записи аудита.
  Резерв 6/3/12/batch 3, права, полный текст, независимые Min/LT и все hard
  gates прежние. Прошли AST и проверки допустимого diff/защиты истории.
- Временный API-only обработчик остановлен, запущен единственный обычный
  серверный worker. Исторические отмены не восстанавливаются. Подтверждённого
  нового перевода в резерве и новой фактической доставки MAX пока нет.

## 0.10.5 — 2026-10-28: проверка полного ответа редактора и возобновление подготовки

- На границе кандидатов сохранены пауза 50, все 85 записей, отказы и прежний
  неизвестный исход. Для будущих запросов Qwen настроены max_tokens 4096,
  response_format=json_object и reasoning_effort=none; thinking=false и
  прежний sampling сохранены. Таймаут клиента поднят с 600 до 1200 секунд.
  Независимый Ministral, MADLAD/его contract и SHA, сегменты 600 символов,
  права, hard gates и два durable claims не менялись. Приватные before images
  и точные CAS receipts сохранены.
- Синтетическая живая цепочка ArticleRepair V8 → независимый Ministral →
  LanguageTool/hard gates прошла. Настоящий wire подтвердил параметры запроса,
  полные SSE-ответы stop без reasoning: Qwen 93 токена/73 секунды, Ministral
  138 токенов/94 секунды. Выполнено ровно по одному запросу каждой модели;
  статей, DRAFT, согласований и публикаций этот тест не создавал.
- Первое диагностическое обращение отвергнуто локальным посредником до
  upstream POST: посредник не поддерживал stream=true. Это подтверждено
  точными config/process/FD/inode/timestamps, неизменным пустым каталогом
  обязательных claims и журналом OpenCode. Отдельная аудируемая сверка закрыла
  только этот синтетический claim как diagnostic_no_upstream. Исходные raw и
  completed=false сохранены; неизвестные исходы статей этим не снимаются.
  Для SSE-посредника прошли Ruff и 10 локальных fixture-проверок.
- Диагностические службы остановлены после завершения запросов, рабочий
  общий API восстановлен, перезапущен только проектный OpenCode. Control
  возобновлён 50→51 штатным ServerState.control под worker lock; служба
  отправки ещё не запущена. Все таблицы, кроме control и одной новой записи
  аудита, сохранены точно; все адресаты остаются на паузе.
- Начата подготовка одного полного ранее не использованного текста NOIRLab
  из сохранённых raw: 248 слов, 2257 символов, права/условия и вся история
  проверены заново, максимальное совпадение пятиграмм 0.2091 < 0.40.
  Используются штатные reserve claims, MADLAD и независимые проверки; сетевых
  повторов источника и возврата отменённых записей нет. На момент записи
  резерв ещё пуст и новая фактическая доставка MAX не подтверждена.

## 0.10.5 — 2026-10-27: проверенная установка V8 на VM

- Точная qa-v9 прошла 1832 теста на отдельной PostgreSQL, без ошибок и пропусков.
  Все 451 staged hashes и рабочие данные production до/после QA совпали.
  Выполнялись 39 модулей с fakes, реальные модели и MAX тестами не вызывались.
- Для строгого baseline 0.10.4 собран отдельный ops-архив из qa-v8 с заменой
  только двух уже установленных документов; код и тесты остались байтово
  прежними. На VM подтверждены 120 core hashes и оба документа. Исторические
  отклонения вспомогательных файлов перечислены отдельно, массовой синхронизации
  не было. Тело прежнего CHANGELOG дополнительно сверено без изменений.
- После backup установлены ровно 12 diff-файлов .5. Source и metadata равны
  0.10.5, схема 0008 без миграции; все 37 таблиц, 82 записи и приватная
  конфигурация сохранены по hashes. Образ Postiz не менялся.
- Отдельная аудируемая активация перевела control 48→49 и запустила единственный
  серверный обработчик. Для новых исправлений действует article-repair-v8;
  старые raw, отказы, claims и неизвестный источник сохранены без переоценки.
  Профиль Qwen, сегменты 600, фильтр 500 слов и резерв 6/3/12/batch 3 не менялись.
- На момент установки quality_ready = 0 и новых подтверждённых доставок нет.
  Следующий этап — фактическая проверка нового кандидата и доставки в MAX;
  успешная QA и установка не объявляются наполнением канала.

## 0.10.5 — 2026-10-27: реальные ошибки и ошибочные предупреждения редактора

- В новом завершённом отказе Qwen назвал неверную форму научного термина
  допустимой стилистикой и отказался исправлять весь кандидат из-за спорных
  предупреждений Ministral. Исходный отказ, LanguageTool и текст сохранены;
  отменённая запись не возобновляется и заново модели не передаётся.
- Для следующих новых кандидатов подготовлен article-repair-v8: ошибочные
  или необязательные замечания одного проверяющего не отменяют реальные
  грамматические и лексические ошибки другого. Допускается точная по смыслу,
  грамотная русская перефразировка без удаления фактов, имён, чисел и ссылок.
  Корректный редкий термин не превращается в ошибку из-за словаря; невозможность
  безопасного исправления по-прежнему требует явного not_repairable.
- Протокол ответа, два durable claims, строгая защита от Latin-маскировки,
  исходники, независимые Min/LT, hard gates и exact approval остаются прежними.
  Контракты и промпты V3–V7 сохраняются для точного воспроизведения истории.
  Версия исходников согласована в pyproject и __init__.
- Локально: 42 проверки V8/Unicode прошли; историческая и автоматическая
  регрессия дала 370 passed и один сбой test fixture, который менял V7-файл
  вместо проверяемого нового V8. После исправления fixture все 7 адресных
  проверок прошли, включая сохранение старых отказов при изменении V7/V8.
  Ruff и diff-check успешны. Это проверки с fakes, без новых запросов моделей.
- Полная изолированная VM QA и установка этой версии ещё не выполнены.
  Рабочая 0.10.4 поставлена на паузу 48 между кандидатами; все 82 записи и
  отказы сохранены, активных моделей, отправок и leases нет.
  Новая фактическая доставка и наполненный резерв пока
  не подтверждены. Этот раздел не объявляет подготовку успешной публикацией.

## 0.10.4 — 2026-10-27: расширение подбора и профиль корректора

- Проверены сохранённые исходники и дополнительные официальные каталоги.
  При прежнем фильтре 350 слов новый короткий контент оказался ограничен:
  часть NASA совпадает с историей, два неиспользованных полных текста ESA/Hubble
  содержат 398/404 слова, но всего 2694/2913 символов EN. Фильтр подбора повышен
  до 500 слов через штатный API. Полный исходник сохраняется; окончательный
  предел 3500 символов, права, ссылки, числа, независимые Min/LT и дедупликация
  не менялись. При изменении сохранены все 79 записей по общему hash.
- Один новый Qwen-ответ завершился HTTP 200 за 490 секунд, но повторял отказ
  и оборвался внутри JSON-строки reason. Полный raw и новый request journal
  сохранены; чистый V7 replay подтвердил протокольный отказ до schema/QA.
  Источник отменён без повторного запроса и без внешнего состояния. Следующий
  кандидат завершился явным not_repairable и также остался в истории.
- На границе между кандидатами общий control поставлен на паузу 45 при нулевых
  моделях, отправках и leases. Для будущих Qwen-запросов применены temperature
  0.7, top_p 0.8, top_k 20, min_p 0 и presence_penalty 1.5 по
  [официальной карточке Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B/blob/3770784d47152821ff8b46455b3abc794854235b/README.md).
  max_tokens 2048, thinking=false, timeout 600 секунд и независимый рецензент
  сохранены. Приватный before image и точный diff подтверждают сохранность
  остальных настроек. Перезапущен только OpenCode данного проекта; проверен
  ответ /api/info версии 2.0.17, без inference. Настройки подбора записаны
  revision 46, цикл возобновлён revision 47. Прежние отказы не пересматривались.
- Дополнительно восстановлены два отсутствующих публичных benchmark-промпта
  из точного qa-v8 архива с exclusive create; все 120 проверяемых файлов
  runtime/prompts совпали с манифестом. Секреты и прочие файлы не заменялись.
- После запуска выбран новый полный текст ESA/Hubble. На момент записи резерв
  ещё пуст; новая фактическая доставка MAX не подтверждена. Эффект нового
  профиля оценивается обычными проверками, успешной публикацией не объявлен.

## 0.10.4 — 2026-10-27: снятие одной блокировки и запуск на VM

- По прямому поручению владельца неудачная подготовка с неизвестным исходом
  завершена как failed с model_result_unknown, её claim — abandoned_unknown.
  Источник остаётся использованным; повтор перевода или публикация этой записи
  запрещены. Исходная ошибка OpenCode, raw, единственная попытка коррекции,
  Model Audit и все остальные 76 записей сохранены. Отдельный Audit содержит
  точное поручение, границы исключения и hashes полного приватного before image.
  Первый контроль транзакции выявил ошибку сортировки снимка и полностью
  откатился; после исправления канонической сортировки применение подтверждено.
- Точная qa-v8: 1773 passed, без ошибок и пропусков в отдельной PostgreSQL VM;
  все 449 файлов исходников неизменны, production до/после тестов совпадает.
  Перед установкой сохранён и восстановлен один устаревший test-file baseline;
  это не меняло runtime или БД. Другие исторические отклонения вспомогательных
  файлов зафиксированы отдельно и массово не переписывались.
- После проверенного backup установлены 14 файлов .4. Source и metadata равны
  0.10.4, схема 0008 без миграции. Hashes всех 37 таблиц, 77 записей и приватных
  настроек совпали до/после установки. Образ Postiz не менялся. Отдельная
  аудируемая активация перевела общий control 43→44 и запустила единственный
  серверный обработчик; reserve target 6, low 3, maximum 12, batch 3 сохранены.
  Перевод идёт полными текстами сегментами до 600 символов, без overlap;
  права, независимые Min/LT, два repair claims, дедупликация и интервал доставки
  не менее 3600 секунд сохраняются. Прочие маршруты и desktop heartbeat выключены.
- На момент запуска quality_ready = 0; новая фактическая доставка MAX ещё не
  подтверждена. Установка и тесты не объявляются наполнением канала.

## 0.10.4 — 2026-10-27: смешанная латиница и сохранение сессий OpenCode

- В живом исправлении 0.10.3 обнаружен пробел: старый guard запрещал полную
  замену русского слова латиницей, но пропускал «Хорáлек» с латинской á внутри.
  Этот кандидат не опубликован; исходный отказ и обе коррекции сохранены.
  Новый article-repair-v7 блокирует полные и смешанные Latin-подмены до Min/LT.
  V3–V6 воспроизводят прежние raw/prompt/hash для аудита, V6 prompt неизменён;
  невалидный полный V7 response сохраняется как protocol_guard без повторов.
- Исправлен разрушительный finally session-адаптера: после timeout он прежде
  делал interrupt/DELETE и терял возможность получить результат. Новый приватный
  журнал сохраняет intent/prompt SHA до сети, создание/SID до generate, полный
  response/status до разбора и только затем разрешает обычную очистку успеха.
  Любой timeout/HTTP/JSON/empty/disk failure удерживает сессию без повторного
  запроса. Duplicate keys/NaN не считаются успехом. Symlink/junction не может
  направить приватный журнал в папку исходников; tuple/usage и stateless прежние.
- Локально: 17 новых Unicode tests, 332 смежных regression, 315 проверок
  исторических/current repair контрактов; journal и смежные модули — 418 passed,
  включая 35 новых cases. Ruff/diff-check успешны. Окончательная VM QA ещё нужна;
  0.10.4 не установлена и новой живой доставкой не объявляется.
- Рабочая 0.10.3 дошла до 77 записей: 41 cancelled, 34 прежних published,
  один исторический human draft и одна needs_attention. Qwen получил timeout;
  точный prompt восстановлен, но payload→SID связь и полный ответ утрачены.
  GET сессии даёт 404, список/SQLite пусты, логовый InterruptError связан только
  временем. Это не completed result. Сохранены forensic snapshots и исходные
  ошибки; claim остаётся blocked_unknown, повтор и протокольный карантин не делались.
  Общая пауза revision 43, DRAFT/approval/dispatch/leases новых публикаций нет,
  quality_ready резерв пуст. До разрешённой сверки/нового прямого решения
  владельца автоматический цикл не возобновляется.

## 0.10.3 — 2026-10-27: установка проверенного словаря на VM

- Точная qa-v7 прошла 1373 теста в отдельной PostgreSQL БД VM без ошибок/skips;
  все 445 staged hashes совпали, production revision 41 и 68 записей/claims
  сохранились. Пакет qa-v6 с двумя сбоями не устанавливался.
- После private backup установлены 13 diff-файлов относительно qa-v5.
  Source/installed metadata равны 0.10.3, схема 0008 без миграции. Все 37
  таблиц совпали по row-multiset hashes, runtime и прежняя история сохранены;
  проверена добавочность CHANGELOG. Образ Postiz не менялся.
- При запуске сверены три сохранённых первичных снимка. Отдельный Audit
  зафиксировал release activation source-bound-terms-v1 для новых кандидатов;
  все 68 старых записей остались без нового маркера и совпали по общему hash.
  Настройки и translation config с бюджетом 600 не менялись; semantic override
  не применялся. Общий control возобновлён revision 42, единственный server
  owner запущен. Прочие маршруты и desktop heartbeat не включались.
- Начался новый подбор. Резерв пока пуст, новая фактическая доставка ещё не
  подтверждена; успешная установка не считается проверкой публикации.

## 0.10.3 — 2026-10-27: исправление legacy-конструктора после VM QA

- Точная qa-v6 на отдельной PostgreSQL VM дала 1371 passed / 2 failed без skips;
  production revision 41, все 68 записей/claims и 445 source hashes сохранены.
  Оба сбоя обнаружили одну регрессию: конструктор проверки без record ошибочно
  открывал DB session, хотя новый словарь в этом legacy-пути выключен.
  Неуспешная сборка не установлена; failed receipt и log сохранены локально.
- DB/Audit binding теперь вызывается только для существующего record. Для новой
  durable-записи все проверки источника, marker и Audit остаются обязательными.
  Повтор локально: два прежних wiring-теста и 37 integration/tamper tests прошли;
  Ruff и diff-check успешны. Независимый read-only review не выявил ослабления.
  Для установки требуется новая точная VM qa-v7; живая доставка ещё не заявлена.

## 0.10.3 — 2026-10-27: проверенные научные написания без переоценки истории

- Живой новый ESA/Hubble-кандидат 0.10.2 сохранил ошибку Dove → «Голубые»;
  Ministral её не выявил, LanguageTool заблокировал текст, Qwen вернул
  not_repairable. Запись отменена без DRAFT/approval/dispatch. Сохранены
  начальный текст, исходное одобрение с warnings, LT и отказ; это не доставка.
  Цикл остановлен между кандидатами revision 41: 68 записей, ноль model claims,
  активных слотов, отправок и leases. Ошибочная версия не восстанавливается.
- Устранена причина части ложных орфографических блокировок: только три
  source-bound области Хаббл, Годдарда и конечные формы «звездообразующий».
  Первичные снимки ИКИ РАН/САО РАН получены HTTP 200, hashes закреплены;
  [границы и ссылки](docs/SCIENTIFIC_SPELLING.md). Это не новые источники статей.
  Требуются точные EN narrative/RU context, UTF-16 позиции и конкретный LT
  UnknownWord rule; для повторного Хаббла сохраняется именованный anchor.
  Raw LT остаётся полным; grammar, опечатки, смысл, прочие слова и права
  этим доказательством не снимаются.
- Отдельный source-bound-terms-v1 включён только для новых production automatic
  records и закрепляется в record/attempt/Audit до моделей. Маркер, source/input/
  policy/candidate hashes, record_id, полный EN/RU, review raw и LT raw связаны
  с trace/evidence и проверяются перед approval. Исторические human/recovery
  и записи без маркера сохраняют старый валидатор. Старые отказы не переоцениваются.
  Смешанный LT отказ по-прежнему требует адресной Qwen-правки и новых Min/LT;
  остаются два durable claim, запрет unchanged/unknown повтора и весь старый аудит.
- Локально: 123 теста узкого словаря и 37 финальных integration/tamper tests
  прошли; предыдущий combined regression 116 passed. Ruff чистый. Изолированная
  VM QA окончательной сборки, установка 0.10.3 и новая доставка ещё не заявлены.

## 0.10.2 — 2026-10-27: установка и запуск нового подбора

- Точная qa-v5 прошла 772 теста в отдельной PostgreSQL БД VM без ошибок/skips.
  Все 441 staged hashes совпали; production pause revision 39, 67 записей
  и reserve-claim history сохранены. В набор вошли 11 новых тестов ротации
  и два прежних теста независимого fallback; уточняется ранний счёт десяти.
- На безопасной паузе и остановленном server owner сохранён новый private backup,
  установлены 13 diff-файлов относительно qa-v3. Схема осталась 0008, все 37
  таблиц совпали по row-multiset hashes; рабочие config, cookies, данные и история
  не переписывались установкой. Source/installed metadata равны 0.10.2.
  Прежний текст CHANGELOG сохранён точным суффиксом новых записей.
- Отдельным Audit прямого поручения владельца начата проба `segment_chars=600`
  только для новых записей закреплённого MADLAD; прежний private config сохранён
  для отката. Общие настройки 6/3/12/3, фильтры, интервал и политики прежние,
  control revision 40. Benchmark A01 остаётся отклонённым изолированным опытом,
  старые статьи не восстанавливаются. Единственный server owner запущен;
  desktop heartbeat и прочие маршруты не включались.
- Новая фактическая доставка на момент этой записи ещё не подтверждена;
  резерв quality_ready пуст. Установка и успешные тесты не являются доставкой.

## 0.10.2 — 2026-10-27: смена источника между кандидатами резерва

- Прежний open_science вращал приоритет только по часу. После нескольких
  известных отказов резерв продолжал выбирать тот же источник до конца часа.
  В режиме article-reserve-v1 следующий подбор теперь начинается со следующей
  зарегистрированной политики после последней записи резерва. Исторические
  записи не меняются; начальный и прежний почасовой режимы сохраняют ротацию
  по часу. Права, фильтры, cursors, claim fencing и общая дедупликация прежние.
- Новые десять тестов проверили смену после отменённой/готовой/опубликованной
  записи, исторический хвост и неизвестный источник без изменения аргументов
  selectors. Ruff и diff-check успешны. Предыдущая точная qa-v4 прошла
  761 тест на отдельной PostgreSQL VM без skips; все 440 hashes и production
  pause revision 39/67 записей/claim history сохранены. Изменение ротации
  требует отдельного qa-v5 до установки.
- Единственная независимая Ministral/LT-проверка полного пробного A01 с
  бюджетом 600 завершилась отказом: изменено название конкретного кратера,
  потеряно отношение наложения высотной модели на снимок; сохранены также
  прочие исходные замечания. Полный raw не заменён одобрением. Это отдельный
  эксперимент без ledger mutation/approval/dispatch и без права публикации.

## 0.10.2 — 2026-10-27: явный размер фрагмента и проверка перекрытия

- MADLAD получил opt-in `segment_chars` 400–1400; отсутствие поля сохраняет
  прежние 1400. Другие переводчики не принимают уменьшенный бюджет. Остатки
  исходника не обрезаются; перекрытия и служебной разметки в рабочем маршруте нет.
  Новый режим сохраняет размеры, порядок и SHA запросов/ответов сегментов,
  pinned worker evidence и контракт в прежнем событии translation_returned.
  Согласования, Qwen claims, Ministral, LanguageTool, числа, ссылки и права
  не ослаблены; старые отклонённые записи не пересэмплируются.
- Отдельный экспериментальный planner/parser/stitcher проверяет центральные
  spans с соседним контекстом и точными маркерами, без fuzzy удаления текста.
  Production factory к нему не подключена. Одна живая ASCII-проба завершилась
  повторением маркеров до 1024 tokens: complete=false, известный length,
  248,91 секунды. Полные input/raw и hashes сохранены только в ignored runtime;
  новая разметка не подбиралась повторными запросами.
- Проверка замороженных двадцати статей без моделей: 19 подходят под строгие
  окна 400–600, одно предложение содержит 665 символов. На общих 19 текстах
  перекрытие требует 1,72 объёма входа и 80 запросов вместо прежних 36.
  Отдельный полный A01 переведён новым адаптером 600 без изменения production
  ledger: два body-запроса 474/157 символов, суммарно 41,73 секунды. Перевод
  содержит смысловые ошибки; завершение запросов не объявляется quality PASS.
- Целевые локальные проверки: 324 passed, два ожидаемых Windows skips для
  Linux pipe selector; Ruff и diff-check успешны. Production безопасно
  поставлен на паузу revision 39 между кандидатами, с нулём model claims,
  отправок и leases; сохранены 67 записей. Установка 0.10.2 и новая доставка
  пока не заявляются. Предыдущие тесты 0.10.1 не являются ими.

## 0.10.1 — 2026-10-27: установка и настройка полного научного исходника

- Точная qa-v3 прошла 435 тестов на отдельной PostgreSQL БД VM без ошибок/skips;
  437 staged hashes, production revision 36, 62 записи и reserve-claim history
  совпали до/после. Выполнена установка только шести diff-файлов относительно
  фактически установленного qa-v2; source/installed metadata равны 0.10.1.
- Сохранены повторные private backups. Все 37 таблиц editorial DB совпали по
  row-multiset hashes до/после установки; схема осталась 0008, миграции/старые
  согласования/конфигурация не переписывались. Образ Postiz 0.10.0 сохранён:
  UI этим патчем не менялся. Полный source regression: 350 passed.
- Read-only аудит шести неиспользованных полных подписей обнаружил четыре
  структурно корректных текста длиной 305/324/324/404 слова; два текста остались
  заблокированы прежней проверкой ссылок. При 180 и 300 словах кандидатов в этой
  выборке не было. На общей паузе штатно изменён только nasa.max_words 180→350
  с отдельным Audit доказательства; авторы/ключевые слова/рейтинг сохранены.
  Исходник не обрезается; hard limit русского поста 3500 символов, права,
  числа/ссылки, независимые Ministral/LT и все проверки качества неизменны.
- Цикл возобновлён revision 38 с прежним резервом 6/3/12/3. Первый новый подбор
  выполняется; живая доставка этой версии на момент записи ещё не подтверждена.

## 0.10.1 — 2026-10-27: безопасный обход неподдержанных карточек каталога

- Первый живой подбор 0.10.0 не создал статьи: NASA не нашёл подходящего текста,
  а сохранённые страницы ESA и NOIRLab содержали по одной карточке вне точного
  разрешённого URL-контракта и блокировали остальные карточки того же каталога.
  Цикл остановлен на общей паузе revision 36, 62 записи сохранены; активных
  подготовок, слотов, отправок и leases нет.
- Реализована узкая фильтрация чистых same-origin image-namespace карточек
  вне разрешённого маршрута с сохранением исходного HTML, hashes и отказа.
  Такие ссылки не запрашиваются. URL-контракт статьи, проверка прав, credits,
  исходного текста и машинного качества не расширяются; небезопасный URL,
  повреждённые каталог, пагинация и checkpoint по-прежнему блокируют источник.
- На сохранённых реальных каталогах осталось по 19 разрешённых карточек и
  одному сохранённому отказу; пагинация продолжается без сброса checkpoint.
  Целевые проверки: 34 passed; полный source regression и точный VM QA выполняются
  отдельно до установки. Новая живая доставка пока не заявляется.

## 0.10.0 — 2026-10-26: установка на VM и запуск резерва

- Точная сборка qa-v2 прошла 417 тестов в отдельной PostgreSQL БД VM без skips;
  hashes всех 437 staged-файлов до/после совпали. Production оставался на общей
  паузе: 62 записи, настройки и весь ledger совпали по hashes, внешних операций нет.
- Сохранены private PostgreSQL backups обеих БД, runtime и исходное состояние.
  На общей паузе установлены 32 изменённых публичных файла и миграция 0008;
  source/installed metadata согласованы на 0.10.0. Все прежние 62 записи, ledger,
  private config и TOPIC сохранены; прежняя история CHANGELOG осталась точным
  суффиксом новой, код прошёл строгую сверку baseline.
- Собран образ max-postiz-linux:0.10.0-editorial, SHA256
  782787e5cf1559d76d3c89e7f0804af68acd2e60e70bac96bec75ed963dc4587.
  Пересоздан только контейнер Postiz, БД/volumes/очереди сохранены. Новые UI и
  BFF прошли 44 проверки и сборку Next/TypeScript; все 29 stack-checks успешны.
- Отдельным Audit прямого поручения владельца включён article-reserve-v1 с
  настройками 6/3/12/3 только для новых задач open_science/max_test. Прочие фильтры
  сохранены, global control revision 35; человеческое согласование не использовано.
  Единственный server owner запущен, desktop heartbeat не включался.
- Новая живая доставка на момент этой записи ещё не подтверждена: проверяется
  первая подготовка/отправка. История и неизвестные исходы не переотправлялись.

## 0.10.0 — 2026-10-26: резерв проверенных статей и независимая отправка

- По прямому поручению владельца добавлен article-reserve-v1: настраиваемый запас
  quality_ready 6/3/12 и до трёх новых источников за проход. После двух известных
  неудачных Qwen-правок сохраняются отказ, raw и claims, выбирается другой источник.
  Отменённые и исторические записи не восстанавливаются и не переоцениваются.
- Подготовка моделей отделена от короткого прохода отправителя в одном серверном
  владельце; разные экземпляры ledger сливают только независимые records.
  PostgreSQL claims и publication_slots (миграция 0008) защищают подготовку,
  exact назначение и адресата от дублей, конкуренции, рестарта и lost claim.
- Готовый резерв локален до назначения: нет Postiz DRAFT, approval и dispatch.
  Перед DRAFT и approval повторяются права, source/input/trace/evidence/prompt
  hashes, независимые Ministral/LanguageTool и прежние hard gates. MADLAD и
  максимум двух адресных правок сохранены. Неизвестный внешний исход запрещает
  замену; проверенный MAX delivered_at сохраняется до сверки Postiz.
- В Postiz добавлены настройки запаса, его остаток, ближайшая отправка и отдельный
  статус подготовки. По умолчанию новая политика выключена; старые настройки
  читаются без изменения сохранённых исторических согласований.
- На действующей 0.9.5 найден известный отказ каталога: ESA включил comparisons
  URL, NOIRLab — ID без разрешённого суффикса. Они остаются запрещены. На границе
  парсинга каталога отказ теперь SCIENCE_SOURCE_CATALOG_INVALID с сохранённой
  причиной; блокируется весь такой каталог/источник, а независимые разрешённые
  источники доступны прежнему подбору. Повреждённый checkpoint не маскируется.
- Полный локальный прогон с изолированным PostgreSQL: 3091 passed, 4 платформенных
  skip Windows. После финальных проверок границ отдельно: science 332 passed,
  подготовка 19 passed и контроллер/атомарная доставка 25 passed с PostgreSQL.
  В отдельной PostgreSQL БД VM исходная сборка резерва прошла 85 тестов без skips,
  все 62 исторические записи и настройки до/после совпали. UI: 44 passed, strict
  TypeScript; Ruff и diff-check чистые. Изменённая обработка каталога требует
  повторного точного VM прогона до установки.
- Развёртывание и новая живая доставка 0.10.0 пока не подтверждены. Прежние живые
  проверки 0.9.5 и офлайн-проверки не считаются ими; результат установки будет
  добавлен новой записью. Сеть VPN восстановлена штатным перезапуском туннеля.

## 0.9.5 — 2026-10-26: проект резерва для почасовой доставки

- По новому запросу владельца описан переход от одного кандидата в час к
  отдельным подготовке, резерву quality_ready и слотам доставки. Предложены
  настраиваемые уровни 6/3/12 и немедленный выбор другого источника после
  двух неудачных адресных правок без сброса claims и прежних отказов.
- Разделены известный отказ до отправки и неизвестный внешний исход:
  замена не разрешена при sending/unknown/lease или возможной доставке.
  Сохранены независимые Ministral/LT, права, exact hashes, вся дедупликация,
  фактический интервал 3600 секунд и запрет догоняющих пачек.
- Проверены текущие guards server_cycle/article_cycle/automatic_policy:
  одна подготовка занимает час, долгий prepare синхронно занимает проход
  сервиса. В плане предусмотрены независимый короткий dispatch и проверки
  конкурентных claims. Это проект, runtime и его политика не изменены.
  Проверка документации: git diff --check; живые тесты новой схемы не заявляются.

## 0.9.5 — 2026-10-25: первая живая доставка MADLAD и сверка MAX

- Единственный серверный цикл самостоятельно выбрал NOIRLab Image of the Week
  `iotw2602a`, выполнил перевод `madlad400-3b-q4k-causal-v2`, hard gates,
  независимый Ministral и LanguageTool. Получены approved=true, issues=[],
  пять сохранённых advisory warnings и ноль нерешённых LT-замечаний.
  Эта статья прошла сразу: живой Qwen repair не требовался; ограниченная
  автокоррекция проверена тестами, но не выдаётся за выполненную на этой статье.
- Policy approval, DRAFT и отправка относятся к одной точной версии. Postiz
  post `cmvoax5an0000s29nyquzvrhk`, dispatch
  `14591ab4-c2f6-4c04-924f-dbd91b2730f1`: одна попытка published/verified
  25 октября в 23:52:08 Europe/Moscow, Postiz PUBLISHED с точными текстом и датой.
- Независимое чтение MAX в 23:54:16–19, включая перезагрузку, подтвердило один
  пост и посимвольное совпадение всех 1640 символов. SHA256 текста:
  `7a944f7020497f9fa9c635e192e3b5cd8ad5c798d057ffd9884c71365fd123e5`.
  Приватный проверочный скрипт первоначально считал вложенную DOM-обёртку
  отдельным текстовым узлом. Проверка ограничена единственным постом и его
  видимым literal-узлом; нормализация не заменяет сравнение. Первые неуспешные
  отчёты сохранены; production-публикация не повторялась и не редактировалась.
- Сохранены все 59 прежних записей; кроме отдельно аудированного карантина
  старого отказа остальные 58 совпадают по hash. Теперь 60 записей, 34 published.
  После доставки все пять адресатов paused, активных отправок, mutation leases
  и legacy intents нет. Серверный цикл остаётся включён; следующий подбор в
  00:00, следующая отправка не ранее 26 октября 00:52:09 МСК и только после QA.
- Исходные raw, traces, backup БД и доказательства остаются в ignored data.
  Калман и сглаживание вердиктов не добавлены. Итоговая проверка документации:
  git diff --check; ранее исправленные критические ветки — 414 passed на VM.
  Успех одной доставки не является гарантией ежечасного прохождения качества.

## 0.9.5 — 2026-10-25: проверка и установка на VM

- Первый полный VM-прогон: 2979 passed, 13 failed, один platform skip.
  Один отказ выявил описанный ниже private-path дефект; двенадцать —
  устаревшую recovery-фикстуру, смешивавшую V2 записи и V1 trace/context.
  Исправлена только фикстура, строгий runtime guard сохранён. Теперь
  отдельно проверяются согласованные V1/V2 и несовпадение их маркеров.
- Перепроверка исправленных веток и критической цепочки на изолированной
  PostgreSQL БД VM: 414 passed. Исходный полный отчёт и новый отчёт сохранены
  отдельно. Все остальные файлы сравнивались по SHA; Ruff и diff-check чистые.
- На общей паузе установлены только 14 изменённых публичных файлов; рабочие
  TOPIC, приватные настройки и история не заменялись исходными примерами.
  Source и installed package metadata согласованы: 0.9.5.
- Единственный старый полный Latin-mask отказ проверен без модели/HTTP,
  помещён в адресный карантин и отменён с сохранением failed claim/raw/часа.
  Все 59 записей сохранены, остальные 58 записи побуквенно совпали по
  каноническому hash. Новый переводчик выбран в конфигурации с Audit.
  Основной обработчик установлен на прежней общей паузе; публикация ещё
  не подтверждена и не считается результатом этих проверок.

## 0.9.5 — 2026-10-25: MADLAD и ограниченный цикл адресных исправлений

- По новому поручению владельца для automatic max_test добавлен точный
  `madlad400-3b-q4k-causal-v2`. Адаптер требует contract и SHA исправленного
  worker, весов, tokenizer и config каждого ответа; дефектный прежний alias
  не допускается. Исторический Hy и отдельные human-пути сохраняются.
- Отдельный production gateway использует постоянные артефакты и Python,
  запускает один собственный CPU worker на сегмент и освобождает его память
  до ответа. Нет fallback, повторов inference и согласования по health.
  Raw, claims и неизвестные исходы сохраняются в закрытом runtime.
- Связь VM с loopback gateway ограничена отдельным SSH forced command.
  Глобальный запрет port forwarding OpenMediaVault сохранён. Исходный
  сравнительный стенд, его отказы и исправленный Candle patch не переписаны.
  Для unified diff явно сохранены LF и значимые пробелы строк контекста.
- Сохранены независимые Ministral и LanguageTool, hard gates, два durable
  claim на адресное исправление Qwen, exact version approval, дедупликация
  всей истории и запрет повторения неизвестного/неизменённого отказа.
  Принятие кандидата и причины остановки отделены от предложений редактора;
  Калман, сглаживание оценок и повторные опросы ради passed не добавлены.
- Перед runtime-правками проверены 59 записей, отсутствие отправок/leases
  и точное сохранение исходных таблиц/настроек; сохранён закрытый PostgreSQL
  dump и архив runtime. Пакет согласованно обновлён до 0.9.5.
- Для старого полного отказа `ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN` добавлен
  отдельный адресный карантин по прежнему exact trace/input/prompt/claim.
  Чистый replay обязан воспроизвести тот же отказ без модели и HTTP; старый
  failed сохраняется вместе с одноразовыми Audit. Таймаут, повреждённое
  доказательство, lost claim и внешнее состояние не снимаются. Текст не passed.
- Проверки адаптера/QA: 124 passed; исторических human/policy/repair путей:
  123 passed. Gateway на Linux: 28 passed, включая pipes и cleanup.
  Карантин и отрицательные проверки: 96 passed. Один технический EN→RU smoke
  через production-соединение завершился с EOS за 6,18 с; собственный worker
  выгружен до ответа. Это не статья и не доставка. Живая публикация и проверка
  автокоррекции на момент этой записи ещё не завершены; результат будет
  зафиксирован отдельной записью, без подмены тестов доставкой.

## 0.9.5 — 2026-10-25: проверка private-path в окружении с вложенными data

- Полный VM-прогон выявил обход проверки `state_dir` через `..`, когда
  родительский путь уже содержал `data`. Теперь компоненты `..` запрещены
  до разрешения пути; проверка разрешённого частного каталога сохранена.
- Расширенный Linux-прогон gateway: 30 passed, включая внешние каталоги
  `data`, корректный вложенный путь, pipes, неизвестный исход и cleanup.
  Исправлен только уже установленный новый gateway в простое; завершённый
  технический raw и claim сохранены, запрос модели не повторялся.
  Рабочая редакция всё ещё на паузе, отправок и leases нет.

## 0.9.4 — 2026-10-25: сравнение четырёх переводчиков завершено

- Завершены 80 выбранных попыток на двадцати одинаковых полных статьях:
  Hy 20/20, Hy с глоссарием 20/20, исправленный MADLAD 20/20, TranslateGemma
  16/20. Четыре ReadTimeout Gemma оставлены неподтверждёнными, без повторного
  перевода или фиктивной смысловой оценки. Семь старых MADLAD-попыток из
  дефектного backend сохранены отдельно и не подменены новым экспериментом.
- По сохранению фактов, PASS и скорости выбран MADLAD-400-3B Q4 на исправленном
  Candle как основа черновика: 10/20 без существенных искажений/пропусков,
  9/20 смысловых PASS со ссылками, медиана 4:26. У Hy — 3/20 и 8:47,
  у Hy с глоссарием — 6/20 и 12:04, у Gemma — 6/16 и 4:40.
  На общих шестнадцати статьях MADLAD также первый: 8 без существенных ошибок,
  7 PASS, медиана 4:12. Ограничения одного ИИ-оценщика и малой выборки описаны
  в `docs/TRANSLATION_20_ARTICLES.md`; это не универсальная точность модели.
- Все 76 полных кандидатов получили свежие LT-доказательства по exact RU
  при прежних hard gates и source-bound правилах. Всего один кандидат прошёл
  совокупность критериев стенда; это не новый Ministral verdict, policy approval
  или разрешение публикации. Русский текст MADLAD требует редактуры.
- Оценки сохранены до раскрытия owner-map. Сводка сверяет 80 уникальных
  task/source/candidate/terminal hashes, две исходные цепочки контрактов и два
  отчёта качества, без переписывания заголовков/истории. Raw, отказы, замечания
  и закрытый backup остаются в ignored data.
- Уточнён фактический startup: неизменённый native gateway делает один
  служебный перевод вне корпуса; все 20 MADLAD-статей тёплые по backend.
  Неточное описание отсутствия smoke сохранено в замороженном профиле,
  исправление оформлено отдельным доказательством. Замеры и ответы не менялись.
- Проверки: три Rust-теста, прямой/обратный patch check, четыре регрессии
  безопасного продолжения на MockTransport и фактические model/LT-прогоны.
  Финальный read-only контроль подтвердил прежние 59 записей, хеши шести
  таблиц и трёх настроек, ноль активных отправок/leases. Контроллеры завершены,
  частные порты свободны, общие inference-службы не перезапускались.
- Пакет и рабочий переводчик остаются 0.9.4/Hy. Управление сохранено в найденной
  паузе revision 31 с прежним `ARTICLE_RECONCILIATION_REQUIRED`; benchmark не
  устраняет этот отдельный производственный отказ и не подтверждает доставку MAX.

## 0.9.4 — 2026-10-25: исправлены causal-позиции лабораторного MADLAD

- В отдельной копии pinned Candle исправлены relative-position buckets
  quantized T5: encoder остаётся bidirectional, decoder использует causal
  формулу эталонного Transformers. Добавлены воспроизводимый patch и инструкция;
  рабочий Hy и публичный ArticleQuality gate не менялись.
- Три Rust-теста прошли: 1026 фиксированных эталонных значений и контрпример
  causal distance 16. Отдельный release binary собран с прежними locked/offline
  зависимостями; SHA-256 `7b3fd9c77e130992a9b233c30b26a0245b9bb4386a5ecc7250f244d90d4e97cd`.
  Прямой и обратный `git apply --check` прошли без изменения обеих копий.
- Исходные module/binary/Cargo.lock сохранены в закрытом backup. Повторная
  проверка SHA подтвердила прежние веса, tokenizer, config и worker. Старые
  MADLAD-измерения из дефектного backend сохранены отдельно и исключаются
  из сравнения качества; неопределённая седьмая попытка не переименована в passed.
- Для исправленной сборки заморожен отдельный профиль на тех же двадцати
  статьях, с прежними greedy/seed/лимитом, segmenter и числовым formatter.
  Три остальных профиля не переоценивались: проверена их точная неизменность
  при объединении двух контрактов без переписывания raw заголовков и цепочек.
- Живой новый прогон завершил первые четыре статьи без ошибок; полная оценка
  точности и скорости ещё не закончена. Это не интеграционная проверка MAX:
  публикации остаются на прежней паузе; согласования, история и защита от дублей
  не изменены.

## 0.9.4 — 2026-10-25: выявлен дефект изолированного MADLAD backend

- После трёх полных MADLAD-ответов и трёх отказов `length` проверены raw:
  одинаковые восьмисловные фрагменты повторялись до 71 раза. Это не основание
  снижать требования качества или увеличивать лимит ради принятия ответа.
- При сверке pinned Candle quantized T5 с эталонным Transformers T5 найдено
  неверное применение двунаправленных relative-position buckets к декодеру.
  Например, расстояние 16 получает bucket 10 вместо causal bucket 16.
  Эти измерения не позволяют сделать вывод о качестве корректного MADLAD.
- Остановлен только собственный контроллер стенда; его семь начатых MADLAD
  задач, raw, отказы и незавершённая седьмая попытка сохранены без перезаписи.
  Неизвестный исход не превращён в успешный перевод. Частные порты освобождены,
  общие inference-службы и рабочий переводчик не перезапускались.
- Перед остановкой повторная read-only сверка подтвердила прежние 59 записей,
  контрольные хеши шести таблиц и трёх настроек; активных отправок и leases нет.
  Публикации остаются на найденной общей паузе. Исправленный MADLAD-профиль
  ещё требует отдельного проверенного прогона; победитель пока не определён.

## 0.9.4 — 2026-10-25: продолжение остановленного сравнения переводчиков

- Проверена фактическая остановка стенда 24 октября в 05:13 МСК: сохранены
  20 полных переводов Hy, 20 Hy с глоссарием, пять TranslateGemma и один
  неопределённый исход TranslateGemma A06 после ReadTimeout 600 секунд.
  MADLAD до остановки не запускался; выбора победителя и полной оценки не было.
- По новому поручению владельца запущено отдельное закрытое продолжение
  только 34 ещё не начатых задач: TranslateGemma A07–A20 и MADLAD A01–A20.
  Исходный журнал и его ошибка сохранены побайтно. Любая прежняя начатая
  задача, включая timeout, исключена из повторного перевода.
- Продолжение использует неизменные корпус, prompt, веса, параметры и adapter.
  Для каждой новой статьи отдельный append-only журнал; неопределённый исход
  остаётся непроверенным и не превращается в model FAIL или passed. Перед
  переходом к другой статье завершаются только собственные процессы стенда.
- Проверки: совпали hashes замороженных входов, исходного журнала и бинарника
  MADLAD; частные порты свободны, общие модели спят. Четыре локальные регрессии
  на MockTransport проверили запрет повтора прежнего unknown, единственную новую
  статью, неизменность истории и отказ на повреждённую цепочку. Это проверки
  продолжения, не оценка точности моделей. Живой запуск достиг TranslateGemma A07.
- Рабочая конфигурация и пакет 0.9.4, проверки качества, дедупликация и общая
  пауза публикаций не изменены. Итоги точности/скорости остаются незавершёнными;
  неопределённый исход A06 должен учитываться в окончательном сравнении.

## 0.9.4 — 2026-10-23: изолированный стенд четырёх переводчиков на двадцати статьях

- По поручению владельца подготовлено сравнение текущего Hy, того же Hy
  с заранее заданным глоссарием, MADLAD-400-3B Q4 и TranslateGemma 12B Q4.
  До запуска заморожены двадцать полных текстов NASA/NOIRLab/Hubble,
  187 фактов и 62 термина; исходники и ответы остаются в ignored data.
- Добавлены отдельные runner, контроллер собственных процессов и loopback
  адаптеры с сохранением raw, хешей, ошибок, времени и завершённости.
  Рабочий Hy prompt/Translator и одинаковое числовое форматирование сохранены.
  Известный обрыв не пересэмплируется; неизвестный HTTP блокирует продолжение.
- Отдельный checker выполняет прежние hard gates, article-links-v2 и свежий
  LanguageTool; семантическое решение в нём явно pending. Подготовлены
  анонимные пакеты для независимой проверки полного EN/RU.
- Исправлены два подготовительных дефекта стенда: финальный управляющий EOS
  MADLAD больше не попадает в текст (полные token IDs сохранены); путь Python
  внутри venv не раскрывается в системный interpreter через symlink.
  Старые smoke, freeze и неудачный запуск до первого модельного HTTP сохранены.
- Проверки: 95 passed, один Windows skip из-за отсутствия права создания
  symlink; тот же сценарий на Linux входит в 19 passed controller tests.
  MADLAD собран с pinned Candle/locked dependencies и выполнил отдельный
  EOS-complete smoke; это не результат двадцати статей. Ruff и diff-check
  проходят. Полные живые замеры и выбор победителя ещё не завершены.
- Рабочие пакет/конфигурация остаются 0.9.4. Подготовка временно поставлена
  на общую паузу, контрольные хеши истории/таблиц/настроек сохранены.
  Ни публикации, ни approvals/dispatch, ни восстановление прежних отказов
  стенд не выполняет; после измерений обязательна повторная сверка хешей.

## 0.9.4 — 2026-10-23: результат проверки короткой статьи в 15:00 МСК

- Штатный VM-цикл выбрал новый полный текст NOIRLab в пределах 180 слов.
  Hy заменил десятичную точку запятой, потерял ссылку CTIO и допустил
  смысловые ошибки в названиях Магеллановых Облаков и положении Канопуса.
- Автоматический редактор восстановил только ссылку. Остальные ошибки
  сохранились; числовой валидатор изолировал завершённый ответ как известный
  отказ. Это `protocol_guard`, а не отрицательное решение модели. Новые
  Ministral/LanguageTool, DRAFT, approval и dispatch не создавались.
- Проверка показала: одного ограничения длины недостаточно для выхода на
  почасовую публикацию. Новые исключения, повторы и ослабления проверок
  не добавлены. Источник, raw, отказ и занятый час сохранены; активных
  отправок и leases нет. Фактическая публикация нового источника не достигнута.

## 0.9.4 — 2026-10-23: начальный режим коротких полных статей

- По поручению владельца приоритетом стала регулярная публикация без новых
  усложнений алгоритма. На рабочей VM предел отбора уменьшен с 400 до 180
  английских слов. Переводится полный подходящий исходник; обрезания текста нет.
- В текущей длинной статье ESA/Hubble редактор вернул `not_repairable` вместо
  исправленного текста. Исходный отказ, raw, источник и занятый час сохранены;
  статья не согласована и не отправлена. Новые recovery-пути не добавлялись.
- Настройка изменена через штатный API на общей паузе после закрытого backup.
  Readback подтвердил единственное изменение `nasa.max_words`; остальные
  фильтры, источники и Reddit-настройки совпали. VM-цикл возобновлён,
  следующий свободный календарный час — 15:00 МСК. Новых публикаций пока нет.
- Проверки смысла, чисел, ссылок, LanguageTool, права, история и дедупликация
  сохранены. Изменение только настройки не требует нового запуска моделей
  для прежнего отказа; его повторная оценка не выполняется.

## 0.9.4 — 2026-10-23: полные literal URL со скобками перед публикацией

- При сверке штатного цикла 10:00 МСК обнаружен дефект прежнего извлечения
  ссылок: закрывающая скобка внутри URL отбрасывалась, поэтому полный адрес,
  усечённый адрес и изменённый суффикс могли считаться одинаковыми.
  Отправка остановлена общей паузой до исправления. Начатый Hy завершился
  известным отказом качества; запись отменена с сохранением raw, исходника,
  занятого часа и остальных 55 записей. Повторных запросов модели нет.
- Новый `article-links-v2` сохраняет целые скобки, literal query, fragment
  и percent encoding. Повреждённый URL остаётся исходным повреждённым токеном
  для диагностики, а не исправляется кодом. Исторический v1 не переписывается.
- Будущие QA, автокоррекция `article-repair-v6`, доказательства и допуск к
  публикации используют точную новую версию. Связь записывается до Hy в
  журнале, attempt и Audit. Старые v3/v4/v5 промпты и replay сохраняются.
  Перед будущей отправкой дополнительно проверяются полные ссылки старого DRAFT.
- Сохранены проверки чисел, языка, смысла, Ministral/LanguageTool, история,
  дедупликация и интервал доставки. Перед правкой создан закрытый backup.
- Первый широкий прогон выявил пять сбоев совместимости восстановления RONI/MUR
  с новым контекстом ссылок; исправлена привязка staged/recovery trace к его
  исторической или новой версии, без нового решения Ministral. Старые failed
  claims, raw, исходники и запрет повторной remote mutation сохраняются.
- Повторный полный прогон текущего снимка на отдельной PostgreSQL БД VM:
  1231 passed, одна прежняя deprecation warning. Включены новые URL/QA/repair
  guards, политики, отправка, исторические human-пути и staged RONI/MUR recovery.
  Ruff и diff-check проходят; старые промпты побайтно сохранены. Фактическая
  публикация нового источника пока не подтверждена.

## 0.9.3 — 2026-10-23: точная диагностика потерянных ссылок для редактора

- В цикле 08:00 МСК Hy сохранил научные числовые значения, но потерял три
  narrative-ссылки. Цифры в URL вызвали числовой отказ. Редактор вернул точный
  прежний перевод; `ARTICLE_REPAIR_UNCHANGED` сохранил отказ без DRAFT,
  согласования или отправки. Этот исходник и занятый час остаются в истории.
- Новый `article-repair-v5` передаёт редактору точные исходные, пропущенные
  и посторонние URL. Отдельный промпт объясняет восстановление целого URL,
  включая числа внутри адреса. Приложение не дописывает потерянные ссылки.
  Старые v3/v4 промпты, контекст и доказательства сохраняются для точного replay;
  новые правила не переоценивают прежние отказы.
- Числа, ссылки, смысл, свежие Ministral/LanguageTool, запрет неизменённого
  повтора, дедупликация и интервал доставки сохраняются. Перед исправлением
  сверены отправки и leases, включена общая пауза и создан закрытый backup.
- Проверки: на отдельной PostgreSQL БД VM прошли 458 тестов парсера, карантина,
  автокоррекции, серверного цикла, политик, дедупликации и dispatch-only обработчика.
  Локально — 153 теста парсера, 86 тестов карантина/автокоррекции и дополнительный
  regression; Ruff и diff-check проходят. Четыре реальных старых raw воспроизведены
  чисто, с прежними отказами, без моделей и HTTP. Фактическая публикация нового
  источника пока не подтверждена и будет проверяться в следующем штатном цикле.

## 0.9.2 — 2026-10-23: продолжение после полного числового отказа редактора

- При сверке живого цикла обнаружена остановка после полного ответа редактора
  с `TRANSLATION_NUMBER_CHANGED`: из предложенного текста исчезло исходное
  эквивалентное значение в милях. Проверка обоснованно отклонила кандидат,
  но завершённый известный отказ не позволял продолжить следующие часы.
- Такой полный ответ теперь допускает только отказный карантин с повторной
  чистой проверкой сохранённого raw. Новая адресная одноразовая операция для
  прежнего `failed` требует общей паузы, точных claims, входа, промптов, источника,
  политики и отсутствия DRAFT, approval, dispatch и leases. Сохраняются старый
  отказ, raw, время, исходник и занятый час; повторного запроса модели нет.
- Числа, ссылки, смысл, LanguageTool и условия публикации не ослаблены.
  Таймаут, неизвестный исход и потерянный claim этим путём не снимаются.
- При разборе отдельного отказа Hubble найден корректно восстановленный URL,
  отклонённый из-за Markdown-обёртки. Новый `article-repair-v4` приводит только
  безопасные HTTP(S)-ссылки к прежнему обычному тексту перед diff, replay guard
  и всеми hard gates. Косметический повтор остаётся `UNCHANGED`; потерянный URL
  код не дописывает. Исторический v3 сохраняет прежнюю строгую интерпретацию;
  старые отказы и отменённые статьи повторно не оцениваются и не отправляются.
- Проверки: 415 тестов на отдельной PostgreSQL БД VM, включая парсер редактора,
  старый v3 replay, карантин, автокоррекцию, политики, дедупликацию и dispatch-only
  обработчик. Локально прошли 118 тестов парсера и 53 теста карантина; Ruff и
  diff-check проходят. Это отдельно от ожидаемой живой публикации нового текста.

## 0.9.1 — 2026-10-23: проверка живого подбора и продолжения после отказа

- На VM установлена версия 0.9.1; проверены hashes всех 400 публичных файлов,
  import/distribution версии и 29 эксплуатационных проверок. Postiz остаётся
  единственной панелью, прежний Hub выключен, desktop-автоматизация на паузе.
- Включён режим «Открытая наука». После адресного карантина первого полного
  отказа NOIRLab следующий штатный час в 01:00 МСК самостоятельно закрыл
  отклонённую запись и выбрал другую статью. Исходный URL и час не освобождены.
- Во втором переводе числовой gate обнаружил дроби с изменённой нотацией;
  автоматически вызван редактор. Его полный ответ также потерял исходную ссылку:
  новый протокол сам сохранил отказный карантин без вмешательства оператора.
  Ни один из двух кандидатов не получил DRAFT, approval или dispatch.
- Подтверждены все прежние 50 записей без изменений, неизменный старый JSON-журнал,
  базовая NASA-политика и прежние пять источников. В журнале теперь 52 записи;
  прежние 34 PUBLISHED и два DRAFT сохранены, отправок и leases нет, все адресаты
  paused между публикациями. VM-цикл включён, следующий штатный час — 02:00 МСК.
- Ограничение проверки: фактическая доставка материала нового источника пока
  не подтверждена. Отклонённые переводы не выданы за успешную живую публикацию;
  все исходники, raw и доказательства сохранены только в ignored data.

## 0.9.1 — 2026-10-23: завершённый отказ редактора при потере ссылки

- На первом живом тексте NOIRLab автоматический редактор потерял исходную
  ссылку. Строгая проверка остановила вариант до создания DRAFT; положительный
  ответ Ministral не заменяет проверку ссылок и LanguageTool.
- Полный известный ответ редактора с `TRANSLATION_REFERENCE_CHANGED` теперь
  допускает проверяемый карантин. Исходные raw, отказ, исходник и занятый час
  сохраняются; новых моделей, согласования или отправки карантин не вызывает.
  Следующий штатный час может продолжить подбор без догоняющей публикации.
- Для уже завершённого отказа добавлена отдельная одноразовая операция только
  на общей паузе: повторно проверяются сохранённый raw, все привязки и отсутствие
  DRAFT, согласований, отправок и leases. Неизвестный исход остаётся блокирующим.
- Проверки: 51 локальный тест карантина и автокоррекции; на отдельной БД VM —
  150 тестов связанных политик, подбора и dispatch-only обработчика. После
  сохранения всех прежних тестов карантина прошли ещё 128 проверок карантина
  и серверного цикла. Ruff и diff-check проходят. Живая доставка этой
  отклонённой статьи не заявляется.

## 0.9.0 — 2026-10-23: дополнительные научные источники и отдельные политики прав

- По поручению владельца добавлены полные научные тексты NOIRLab Image of the
  Week и ESA/Hubble Picture of the Week. Проверены официальные условия CC BY
  4.0, разрешающие переводы и повторную публикацию. Медиа не загружаются.
  Attribution, источник, лицензия и машинное редактирование включены в точный
  согласуемый текст; их утрата или изменение блокируют согласование.
- Для новых источников политики выводятся из неизменной NASA-политики с точными
  source ID, разделами URL и доказательствами прав. Общие target, рубрика,
  час и лимиты доставки прежние. Новые права требуют полного разрешения
  хранения, перевода и публикации; неизвестный источник не получает NASA-политику.
- Сборщик сохраняет полный narrative, все HTML-ссылки, credits, исходные HTML
  и контрольные суммы в ignored data. Условия сравниваются с проверенным
  смысловым текстом; их изменение останавливает источник. Прежние исключения,
  отменённые записи и межисточниковые повторы участвуют в общей дедупликации.
- В Postiz добавлен режим «Открытая наука» с общими фильтрами авторов и слов.
  Приоритет источников вращается по часу. Известный read-only отказ одного
  источника позволяет проверить другой; ошибки целостности, пауза и потеря
  лидерства останавливают цикл. В час переводится не более одной новой статьи.
  Неизменённые отказы не пересэмплируются; автокоррекция, Ministral и LT прежние.
- Закрыты индивидуальные исключения прав в credits, narrative и служебных
  блоках, включая NC, ND и personal use. Сохранённое доказательство повторно
  принимается только при точном совпадении полного неизменяемого содержимого.
  HTTP 401/403/429 или challenge сохраняет запрет источника без повторов.
- Конфигурация новых HTML-источников использует встроенный профиль
  `server_science`; общий сборщик отклоняет его до HTTP. Проверяется установка
  полного примера по действующей схеме и закрыт обход строгого импортера.
- На VM обнаружено зависание повторно используемого HTTP-соединения после
  успешного каталога. Для этого сборщика GET используют отдельные соединения
  с чтением до 60 секунд, без повторов, смены UA/proxy или обхода ограничений.
  Проверены шесть настоящих GET: оба каталога, полные статьи и условия прав
  вернули HTTP 200; старые 50 записей не изменены, модели не вызывались.
- Проверки: 1204 регрессионных теста в отдельной PostgreSQL БД на VM;
  после окончательных исправлений — 406 целевых тестов сборщика, политик,
  атрибуции, подбора и общего импорта. В сборке Postiz прошли 43 проверки формы/SSR/TypeScript.
  Ruff и diff-check проходят. Это отдельно от проверки живой публикации.

## 0.8.1 — 2026-10-22: живая автокоррекция и повторный подбор будущих источников

- Подтверждён полностью автоматический цикл NASA SVS 5682: исходный перевод и
  первая содержательная правка отклонены LanguageTool, вторая правка прошла
  новые независимые Ministral и LT. Все исходные raw, отказы, замечания и два
  durable repair claim сохранены; повторной оценки неизменённого текста нет.
- Фактическая доставка — 22 октября в 13:20 МСК: одно policy-согласование,
  один dispatch и одна попытка, verified и Postiz PUBLISHED. Все 1355 символов
  сообщения в MAX точно совпадают с замороженным snapshot и текстом Postiz.
  После доставки все адресаты paused; история содержит 50 записей, в Postiz
  34 PUBLISHED и два прежних DRAFT. Приватные доказательства сохранены в data.
- Штатные последующие циклы продолжают проверять источник. В наблюдавшиеся
  17:00 и 23:00 новой статьи нет: разрешённый корпус использован или отсеян
  прежними ограничениями. Пустой час не считается доставкой и не вызывает
  восстановления отменённых записей, ослабления фильтров или догоняющей пачки.
- Исправлена отдельная ошибка checkpoint: будущая дата Search/Page API больше
  не заносит материал в список окончательно просмотренных. После наступления
  даты или исправления ошибочной даты источник проверяется обычным подбором;
  права, ссылки, тип, возраст, длина и дедупликация остаются обязательными.
- Добавлены изолированные проверки полного пути одной и двух автокоррекций
  через policy, тот же Postiz post, единственную отправку и последующую паузу.
  Отдельные регрессии проверяют обе будущие даты и сохранение отказов прав,
  небезопасной ссылки и неподдерживаемого типа. Фикстуры не считаются MAX.
- До обновления сверены нулевые отправки/leases, пауза адресатов и сохранена
  закрытая копия двух БД, исходников, настроек и evidence. На VM в отдельной
  PostgreSQL БД прошли 494 проверки; после уточнения порядка чтения даты —
  40 проверок окончательной версии. Локально прошли 38 проверок подбора и две
  проверки доставки; Ruff, diff-check и независимый review без замечаний.
  Исторические seen не сбрасываются: прежние отказы не восстанавливаются.
- На VM развёрнута согласованная версия 0.8.1, единственный серверный цикл
  возобновлён. Эксплуатационная проверка — 29 checks passed. Все 50 записей
  и исходный JSON побуквенно сохранены, отправок/leases — 0, все адресаты paused.
  Hub disabled/inactive, desktop-автоматизация не включалась. Следующая новая
  доставка зависит от появления подходящего разрешённого источника; пустые
  часы не обходятся повторным переводом или отправкой старого материала.

## 0.8.0 — 2026-10-22: ограниченное автоматическое исправление перевода

- По новому поручению владельца добавлен отдельный Qwen35-9B редактор после
  известного pre-DRAFT отказа Hy. Редактор предлагает содержательную правку по
  полному EN/RU и исходным замечаниям; каждый изменённый кандидат заново проходит
  независимые Ministral и LanguageTool. Hard gates, права, числа, ссылки,
  грамматика, дедупликация и фактический интервал доставки не ослаблены.
- Максимум две durable-попытки на запись в исходном текущем часу. Уникальный
  PostgreSQL Audit claim фиксируется до запроса модели; неизвестный исход,
  потеря claim, невалидный ответ и инфраструктурный сбой не разрешают повтор.
  Безопасный отказ редактора и прежний кандидат сохраняют отказ качества;
  занятый час и источник остаются в истории, следующий час не догоняет пропуски.
- Полные входы, точные замены, raw и traces сохраняются отдельно в ignored data;
  проверяются hashes источника, политики, промптов, кандидатов и доказательств.
  Форматные изменения и подмена русских слов латиницей не обходят отказ.
  Создаётся один DRAFT той же записи, согласование по-прежнему выполняет policy.
  Незавершённый repair claim блокирует publish и policy approval.
- В живой изолированной проверке редактор исправил числовую нотацию, но не
  перечислил её в собственном списке правок; v1 честно остановился до Ministral
  и DRAFT. В контракте article-repair-v2 весь фактический diff вычисляется и
  проверяется кодом отдельно от сохранённых model_changes. Полнота аудита больше
  не зависит от второго пересказа модели; каждое заявленное изменение и цитата
  по-прежнему проверяются, новые hard gates/Ministral/LT обязательны.
- Для production выбран article-repair-v3 с простым плоским ответом title/body
  и вычисляемым кодом полным diff. Второй изолированный запрос v2 вернул сломанный
  JSON и также заблокирован без повторного запроса. Его raw сохранён; такой ответ
  не исправляется догадками и не считается одобрением. Новый плоский формат
  убирает ненужное дублирование текста; проверки смысла и грамматики прежние.
- Полностью сохранённый protocol-invalid ответ редактора получает отдельный
  аудируемый карантин без повторного запроса и без DRAFT/approval. Статья сохраняет
  исходный отказ и закрывается только после истечения часа; новый источник
  выбирается обычным путём. Повреждённый proof, таймаут, отсутствие ответа,
  неизвестный исход и потерянный claim сохраняют общую блокировку.
- Живая независимая проверка сохранённого изменённого Qwen-кандидата: новый
  Ministral approved=true, свежий LT оставил unknown-word «микрограммам».
  Результат ARTICLE_QUALITY_REJECTED сохранён, словарь не расширен, отправки нет.
  Это проверка реальных моделей/LT без DRAFT, не новая доставка MAX.
- Перед изменениями VM сверены нулевые active dispatches/leases и пауза всех
  адресатов; сохранены закрытые копии двух БД, настроек, исходников и evidence.
  На VM прошли 458 проверок, включая настоящую отдельную PostgreSQL БД,
  одноразовые claims, конкурентных вызывающих, одну DRAFT и отсутствие approval
  в корректоре. После финальной проверки exact context локальный набор 31 passed;
  независимый review, Ruff и diff-check прошли. Фикстуры не считаются доставкой.
- Развёрнуты согласованные source/import/distribution 0.8.0; финальный VM-набор
  exact-context/карантина — 31 passed, эксплуатационная проверка — 29 checks.
  Единственный VM worker возобновлён; неполный час обновления пропущен, следующая
  подготовка назначена обычным расписанием на 13:00 МСК. Desktop остаётся PAUSED,
  старый Hub disabled/inactive. Общий inference не перезапускался.
- Финальная сверка: 49 уникальных записей, все 42 прежние записи полностью
  совпадают, прежний JSON неизменён; 33 PUBLISHED и 2 старых DRAFT в Postiz.
  Все адресаты paused, active dispatches/leases — 0. Просроченный известный отказ
  5665 закрыт с сохранением источника, часа, raw и причины; восстановления нет.
  Новую доставку MAX этот запуск не подтверждает. Положительный путь коррекции
  проверен изолированными тестами; живой проверенный кандидат остался отклонён LT.

## 0.7.2 — 2026-10-22: подтверждённая доставка и актуальная ошибка панели

- Через единственный VM worker выполнена новая доставка NASA SVS 5666:
  22 октября в 07:56 МСК, один Postiz ID, одно policy-согласование и одна
  попытка dispatch. Подтверждены verified, Postiz PUBLISHED и точное совпадение
  всех 2593 символов фактического сообщения MAX, включая источник и подпись.
  Все адресаты после доставки вернулись на паузу; история содержит 47 записей.
- Первый Hy-кандидат сохранён с отказом. Перед DRAFT исправлены проверенные
  по EN замена миль километрами, пропуск причинности, неточная отсылка к
  воздействию пожаров и замечание LT. Изменённый кандидат прошёл новые Ministral
  и LanguageTool; прежний raw, предупреждения и отказ не заменялись одобрением.
- Успешная проверка готовой версии и подтверждённые healthy pending/verified
  PUBLISHED очищают прежнюю текущую ошибку панели. Unknown, rejected, failed,
  неполная сверка и инфраструктурные ошибки сохраняют блокировку и диагностику.
  Исторические ошибки записей, raw и доказательства не меняются.
- До переключения кода повторно проверены нулевые активные отправки/leases,
  общая пауза и создана закрытая копия обеих БД, исходников и доказательств.
  На VM прошли 98 проверок контроллера, общей паузы, гонок и dispatch-only worker
  в отдельной БД; локальный набор контроллера — 71 passed, без суммирования
  пересечений. Независимый review, Ruff и diff-check прошли.
- Эксплуатационная read-only проверка VM: 29 checks passed. Новый сервис включён,
  прежний Hub выключен; desktop automation остаётся PAUSED. Postiz сохраняет
  прежние 2 DRAFT и 32 PUBLISHED. Новых вызовов inference эта проверка не делала.
- Возвращён широкий подбор NASA без фильтра одной тестовой статьи; настраиваемый
  предел исходника — 400 слов, прежний предел поста — 3500 символов. Сервер
  самостоятельно начал следующую уникальную статью 5669 в текущем часу.
- Продолжение подтверждено второй фактической доставкой: NASA SVS 5669,
  08:56 МСК, одна попытка, verified и Postiz PUBLISHED. Все 3046 символов
  сообщения MAX точно совпали с согласованным snapshot. Фактический интервал
  двух новых доставок — 3610,51 секунды. Содержательно исправленный кандидат
  повторно прошёл Ministral и LT, исходные отказы сохранены.
- Финальная сверка: 49 уникальных записей, все 42 прежние записи совпадают
  полностью, исходный JSON неизменён. В Postiz 33 PUBLISHED и 2 прежних DRAFT;
  активных отправок и mutation leases нет, все адресаты paused. Глобальный
  VM-цикл включён. Следующая статья 5665 остановлена известным pre-draft отказом
  качества без отправки; её отказ и занятый час сохранены, следующий час — 10:00.

## 0.7.1 — 2026-10-22: честный отказ Hy и окно исправления текущей статьи

- Живая сверка обнаружила четыре отказа после переноса: Hy не принимает
  repair feedback и кэширует exact RU. Удалены две фиктивные попытки того же
  неизменяемого перевода; содержательный отказ, raw и замечания сохраняются.
  Переводчики с настоящим изменением кандидата сохраняют прежние три попытки.
- Известный pre-draft отказ текущего часа сохраняется для разрешённой адресной
  правки через `prepare --retry-record --candidate`. Автоматическое закрытие
  происходит после истечения часа. Уже отменённые записи не восстанавливаются;
  invalid review, инфраструктура, unknown и начатые мутации не снимаются.
- Закрыт обход отказа первичного Hy через операторский кандидат: канонический
  полный trace связывает отклонённый текст с записью, исходником, входом и
  промптом. Неизменённый текст, пробелы/переносы и нормализация Markdown-ссылок
  не разрешают новый опрос Ministral. Неполное доказательство блокирует повтор;
  содержательная правка требует новых Ministral и LanguageTool.
- Такой же запрет форматного повтора распространяется на последующие
  отклонённые операторские кандидаты. Для новых попыток сохраняется отдельный
  replay hash; прежние exact/raw hashes не меняются. Старые попытки без этого
  поля требуют связанного канонического trace и при его отсутствии блокируются.
- До runtime-изменений проверены отсутствие активных отправок/leases и общая
  пауза; сохранены закрытые копии обеих БД, исходников, настроек и доказательств.
- Проверки quality/dedicated/recovery: 384 passed, включая чувствительные
  регрессии числа вызовов Hy и запрета повторной оценки неизменного отказа.
  Контроллер: 58 passed. Новый набор защиты отказов: 35 passed, включая
  23 повреждения доказательств и повторы последующего кандидата. На VM проверены
  268 различных регрессий в отдельной БД: после исправления тестового переноса
  строки все 35 replay-тестов повторно прошли. Ruff и diff-check прошли.
  Это изолированные проверки без живых вызовов моделей и внешних публикаций.
  Полная новая доставка в MAX этой записью ещё не подтверждается.

## 0.7.0 — 2026-10-22: ширина редакции в узкой панели Postiz

- Панель редакции ограничена шириной родителя: `width: 100%`, `min-width: 0`
  и `box-sizing: border-box`. Минимальная ширина сброшена только у локальных
  карточек, сетки, полей и обёртки таблицы; широкая таблица прокручивается
  отдельно. Глобальные стили и JavaScript не изменялись.
- Локально прошли сохранённые 40 контрактных проверок. Уже запущенная сборка
  выполнила 41 проверку с временным CSS-тестом, который затем удалён из
  исходников; основной набор остаётся из 40 проверок. Полные webpack,
  TypeScript и Next routes прошли, новый образ собран за 307 секунд.
- Работающие контейнеры и сервисы не перезапускались. Геометрия окна 555 px
  этой локальной проверкой не подтверждается: её проверяют в браузере после
  переключения образа. История публикаций и правила отправки не изменялись.
- После адресного переключения только Postiz проведена живая браузерная
  проверка: ширина панели и родителя совпадает (539,2 px в окне 555 px),
  правая граница не обрезана, кнопки и дата следующего цикла полностью видны.
  Серверный worker и его сохранённое состояние не перезапускались.

## 0.7.0 — 2026-10-22: переключение VM, сохранение истории и эксплуатационные проверки

- На VM включён единственный `max-postiz-editorial.service`; прежний Hub
  остановлен и отключён от автозапуска. Postiz использует новый внутренний
  bridge и нативную «Редакцию». Nginx и firewall закрыли 8090/8091; 8092 доступен
  только Docker, пользовательская панель остаётся на 4007. БД и volumes сохранены.
- В PostgreSQL точно перенесены 42 прежние записи. Исходный JSON, IDs, hashes,
  отказы, отмены и история 31 доставки сохранены; закрытые резервные копии обеих
  БД и всех приватных доказательств созданы до переключения. 29 read-only
  проверок работающей VM прошли, внешних мутаций при этих проверках не было.
- В браузере проверены сохранение настроек после reload, start/pause и новый
  owner-browser через Postiz с подключённым WebSocket. С Windows подтверждены
  закрытые 8090/8091/8092 и доступный Postiz 4007; чужой/анонимный доступ блокируется.
- Из сетевой и startup-диагностики удалена обязательность старого Hub. Старые
  setup-команды прекращаются до изменений при обнаружении миграции, чтобы
  не восстановить прежние nginx/auth/worker. Browser installer не включает
  остановленный обработчик. Основная инструкция — `docs/VM_EDITORIAL.md`.
- Статус показывает следующую границу часа или уже назначенную дату отправки;
  активные этапы очищают устаревшее время ожидания. Это изменение отображения,
  а не перенос даты или повтор запуска. Прошли 45 адресных controller tests.
- После контролируемого restart сохранились общая пауза и точная история;
  повторные 29 migration checks и 29 stack checks прошли. Цикл включён из
  Postiz: использованный час не повторился, следующий подбор показан на
  22 октября 02:00 МСК. Все адресаты остаются paused до разрешённой доставки.
- Изолированный прогон на VM: 373 passed; эксплуатационные регрессии:
  64 passed, 1 Windows symlink skipped; нативная панель: 40 контрактных
  проверок и полная сборка Next/TypeScript. Эти наборы частично пересекаются.
- Первый автономный живой подбор выбрал NASA SVS 5656. Hy вернул реальные
  ошибки перевода/орфографии; положительный Ministral не отменил отказ LT.
  Неизменный кандидат не оценивался повторно, raw/trace и использованный час
  сохранены; запись отменена без DRAFT/approval/dispatch. Это подтверждает
  защитную ветку, но не подтверждает новую доставку версии 0.7.0 в MAX.
- Сборщик Reddit подготовлен и проверен на фикстурах. Разрешённого живого API,
  OAuth и прав на внешние переводы пока нет; публикация Reddit не включена.

## 0.7.0 — 2026-10-22: явный WebSocket-путь входа в MAX

- Исправлена ссылка «Вход в MAX» в Postiz: установленный noVNC строит
  WebSocket URL от корня origin, поэтому ссылка явно передаёт
  `path=owner-browser/websockify`. Проверяется точный URL и использование
  общей константы в интерфейсе; маршрут без закрытого префикса не подставляется.
- Прошли 40 контрактных проверок локально и внутри нового образа, строгая
  проверка TypeScript, production webpack и Next routes. Изолированный
  контейнер без сети подтвердил исправленный путь в клиентском bundle,
  owner-browser 204 и сохранность трёх прежних provider/CAS модулей.
- Образ `0.7.0-editorial` пересобран за 267 секунд. Работающий контейнер
  Postiz, БД и сервисы не перезапускались; фактическое WebSocket-соединение
  после переключения образа этой проверкой ещё не подтверждено.

## 0.7.0 — 2026-10-22: внутренний сетевой маршрут новой редакции

- После точного переноса 42 записей новая API-only проверка обнаружила UFW-блок
  Docker→8092: прежнее разрешение относилось только к 8091. Добавлен отдельный
  prepare/retire-hub сценарий firewall: новый API допускается только из того же
  Docker CIDR; после проверки удаляются старые разрешения 8090/8091. SSH/VPN и
  пользовательский Postiz 4007 не изменяются. Проверены bash -n, API/owner
  отрицательные сценарии и реальный status MAX из существующего контейнера
  через новый bridge; 42 записи и их hashes совпадают, внешних мутаций нет.

## 0.7.0 — 2026-10-22: автономная редакция на VM и единая панель Postiz

- По прямому поручению владельца подготовлен серверный цикл без зависимости
  от desktop Codex: один PostgreSQL leader, ограниченный подбор NASA, прежние
  Hy → независимый Ministral → LanguageTool, exact policy approval и dispatch.
  Общая пауза проверяется также при прямом вызове прежних CLI.
- Управление и полный журнал статей перенесены в новые таблицы PostgreSQL с
  сохранением старых ID, hashes, trace, отмен, approvals и dispatches. CAS и
  authoritative marker запрещают потерю истории и возврат к старому JSON.
  Закрытая копия обеих БД и приватных данных сохранена до runtime-изменений.
- В Postiz добавлены нативные «Редакция», настройки, общая пауза/запуск,
  история/ошибки и защищённый вход в MAX. BFF проверяет cookie/owner/org/Origin;
  прежние CAS/provider guards сохранены. Образ 0.7.0-editorial успешно собран:
  39 контрактных проверок, webpack, полные TypeScript и Next routes.
- OAuth-сборщик Reddit поддерживает автора, ключевые слова, рейтинг/комментарии,
  давность, subreddit, bounded pagination, оригиналы crosspost и scoped права.
  144 адресных/совместимых теста прошли; живого одобрения и OAuth пока нет.
  Публикация Reddit остаётся закрытой до отдельной source policy и прав.
- Независимый review выявил гонку pause/approval, потерю fencing после stop
  и crash-gap marker; исправлены транзакционная проверка паузы, отозванный
  fence и восстановление marker. Локально прошли 147 chain checks и118 новых
  state/source checks; 4 PostgreSQL checks на Windows упёрлись в недоступную
  локальную БД и переносятся на изолированную БД VM. Это ещё не живая проверка
  миграции и доставки: результаты переключения фиксируются отдельной записью.

## 0.7.0 — 2026-10-22: проверка перед переключением VM

- На отдельной PostgreSQL БД VM прошли 373 проверки, включая новую миграцию,
  единственного владельца worker, CAS журнала и восстановление резервной копии;
  локально — 365 checks, отдельно финальный controller/NASA suite — 63.
  Один upstream Starlette/httpx deprecation warning сохранён как ограничение.
- Живой read-only probe восстановил полный корпус42/42, включая11 отменённых
  источников, и выявил отсутствие page_type в search API. Исправление сохраняет
  строгий type guard полной страницы; последующий probe нашёл полный новый текст
  за6 GET без claim/моделей/публикации. Bounded pagination использует отдельный
  PostgreSQL cursor и не застревает на первых30 результатах; новые arrivals
  проверяются первыми, количество запросов ограничено.
- Финальный Postiz image 0.7.0-editorial собран и проверен: 39 контрактных tests,
  webpack/TypeScript/Next routes, owner-browser204, строгие revisions. Прежние
  provider/CAS модули совпали с базой. Фазы и ошибки отображаются по-русски.
- Перед переключением сохранены14331 приватный файл доказательств и исходников,
  обе БД, конфигурация и nginx. Устаревшая Windows TEST_DATABASE_URL отключена;
  актуальная VM credential отличается, её менять не потребовалось. Переключение
  и фактическая новая доставка ещё не подтверждены этой записью.

## 0.6.25 — 2026-10-21, 08:06 МСК: проверка развёртывания и сохранённой паузы

- Commit ec10cd2 опубликован на GitHub и развёрнут на VM: 351 исходный файл
  совпал с manifest, source/import/distribution — 0.6.25. Перед заменой сохранён
  отдельный source backup, quiet guard подтвердил отсутствие jobs/отправок/leases
  и все пять paused. Образ Postiz 0.5.1 и runtime-настройки не менялись.
- На VM прошли 326 адресных quality/policy/protocol/RONI/link тестов за 20,08
  секунды; один upstream Starlette/httpx deprecation warning. Health — 28/28.
  Это изолированные проверки и эксплуатационная готовность, а не живая оценка
  исправленного перевода: новый Ministral/LT для 5586 не запускался.
- Heartbeat ACTIVE с точным сохранённым промптом 0.6.25, прежними расписанием и
  чатом. Отменённая 5586 не восстановлена: исходник, час и первоначальный отказ
  остаются использованными, повторной подготовки/публикации нет. Следующий запуск
  сначала сверяет dispatches и выбирает другой источник в свободном текущем часу.
  Подготовка по-прежнему зависит от работающего desktop Codex.

## 0.6.25 — 2026-10-21: точная проверка научного «пространство-время»

- В новой NASA SVS 5586 самостоятельно выявлены смысловые ошибки первого Hy:
  сжатие вместо спирального сближения, потеря ожидаемого обнаружения и роли ESA,
  неверная привязка размера орбиты, а также грамматика первой фразы. Первое
  одобрение Ministral этих ошибок не снимает; LT остановил кандидат, DRAFT,
  approval и dispatch не создавались. Все три внутренних cached кандидата,
  единственный raw/parsed Ministral и исходный отказ сохранены приватно.
- Независимо проверенный изменённый кандидат сохраняет полный EN и числа;
  исправляет фактические ошибки и две запятые. Вместо ложного LT «солнечные массы»
  используется эквивалентное отношение 0.4 массы Солнца. Буквальный numeric gate
  не менялся; неподанный вариант с десятичной запятой сохранён отдельно.
- Правильное «пространство-время» подтверждено первичной публикацией ТМФ на
  MathNet (tmf6729). Добавлена узкая квалификация exact source/RU context,
  MORFOLOGIK/misspelling/UnknownWord/TYPOS и UTF-16 позиции. Полный match и
  источник доказательства сохраняются; прочие замечания блокируют. Старые
  recovery, grammar/числовые/правовые guards и рубрика не расширяются. Независимый
  review выявил и закрыл попадание нового правила в общий saved-verdict путь:
  теперь default False, только два fresh meaning-first callsite ставят отдельный
  контракт; automatic evidence требует его точного значения и запрещает сочетание
  с protocol_recovery. Legacy и прежние научные recovery остаются без нового допуска.
- В 07:58 МСК запись 5586 закрыта только через cancel-preparation: времени
  занятого подготовительного часа недостаточно для исправления runtime и новой
  проверки Min/LT. Полные EN/RU, финальный исправленный кандидат и raw сохранены;
  источник, час, первоначальный отказ и попытки не сброшены. Независимый RO guard
  подтвердил отсутствие Variant/approval/dispatch/lease. Поздний retry и публикация
  не выполнялись; следующему свободному запуску нужен другой источник.
- Перед runtime сохранён закрытый backup 0.6.24 с обеими БД, журналом и
  настройками: jobs/активных отправок/leases нет, все пять адресатов paused.
  Версии source/package и сохранённый промпт обновляются согласованно.
- Windows: 273 quality-проверки, пять lifecycle/contract случаев policy и
  86 correction/recovery проверок прошли. В широком наборе прошли 216 случаев;
  единственная прежняя protocol fixture явно отделена от fresh marker, после
  чего все 15 protocol evidence тестов прошли. Два test doubles принимают новый
  keyword argument. Ruff, pip check, согласованные версии 0.6.25 и diff-check
  успешны; независимый review не оставил блокеров. Развёртывание проверяется
  отдельно: локальные тесты и исправленный кандидат не подтверждают живой перевод
  или доставку статьи 5586, которая остаётся отменённой.

## 0.6.24 — 2026-10-21, 07:34 МСК: проверка развёртывания загрузчика

- Commit 7a6c173 опубликован на GitHub и развёрнут на VM: все 351 исходный файл
  совпали с manifest, source/import/distribution — 0.6.24. После закрытого backup
  и штатного старта прежнего сервиса работает один dispatch-only worker; образ
  Postiz 0.5.1 не менялся. На VM прошли 164 адресных теста, затем 28/28 health checks.
  Короткий HTTP warm-up после старта завершился без дополнительного перезапуска.
- Новый штатный live fetch NASA SVS 5366 сохранил полный narrative и DOI ровно
  один раз: все четыре поля совпали с независимо разобранным DOM-эталоном.
  Старый input и доказательство потери ссылки сохранены приватно. Это подтверждает
  живую загрузку текста, но не его перевод или отправку.
- Последующий prepare правильно вернул STALE_SOURCE до claim и моделей:
  дата 19 сентября 2024 старше текущих 8760 часов. Дата и политика не менялись;
  ledger остался из 39 записей, новая статья и час не созданы. Следующий материал
  выбирается в действующем диапазоне дат с прежней дедупликацией.
- Read-only аудит подтвердил Brunei verified/PUBLISHED, все пять адресатов paused,
  отсутствие активных отправок и leases, один worker без рестартов и ошибок.
  Heartbeat ACTIVE, сохранённый промпт 0.6.24, прежние расписание и чат совпадают.
  Продолжение подготовки по-прежнему зависит от работающего desktop Codex.

## 0.6.24 — 2026-10-21: сохранение HTML-ссылок научного источника

- Brunei (NASA SVS 15084) опубликована в 07:09:11 МСК: один verified dispatch,
  Postiz PUBLISHED и полный фактический текст MAX сверены. Интервал после Salton —
  3661,07 секунды; все пять адресатов снова paused, активных отправок и leases нет.
- При проверке NASA SVS 5366 обнаружено, что общий HTML-очиститель оставлял
  подпись ссылки на научную публикацию, но удалял DOI href. Добавлен отдельный
  обработчик ссылок narrative в загрузчике NASA: видимый текст и HTTP(S) адреса
  сохраняются как Markdown для прежнего article-links-v1. Относительные ссылки
  разрешаются от канонической страницы; дополнительные запросы и медиа не нужны.
- Небезопасные схемы, userinfo, обфусцированные адреса, неоднозначные подписи,
  вложенные/дублированные/несбалансированные anchors блокируют загрузку явно.
  Общий clean_html, критерии качества и guards типа страницы, даты, прав и размера
  не менялись. Найденный независимым review пробел баланса тегов устранён до выпуска.
- Старый входной файл без DOI и доказательство дефекта сохранены приватно.
  Независимое DOM-представление исходника восстанавливает один exact DOI и все
  прежние видимые слова: 1363 → 1407 символов с Markdown. Это проверка сохранённого
  API, не живая загрузка новой версии и не подтверждение перевода или отправки.
- На Windows прошёл широкий набор из 253 проверок загрузки, форматирования,
  качества и цепочки статей. После финального исправления баланса anchors прошли
  76 адресных проверок, включая все 52 теста загрузчика. Ruff, pip check,
  source/import/distribution 0.6.24 и diff-check успешны; независимый review — без
  оставшихся блокеров. Перед runtime сохранён закрытый backup исходников,
  настроек, журнала и обеих БД 0.6.23; jobs/отправок/leases нет, все адресаты paused.
  Развёртывание и живой результат будут зафиксированы отдельно после проверки.

## 0.6.23 — 2026-10-21, 03:43 МСК: живая проверка RONI recovery

- Commit 2690c8e сохранён на GitHub и развёрнут на VM: 351 файл сверён,
  source/import/distribution 0.6.23. Postiz 0.5.1 не пересобирался; работает
  один прежний dispatch-only worker. На VM прошли 185 адресных тестов за
  27,19 секунды, один upstream warning; эксплуатационная проверка — 28/28.
- Read-only проверка фактического failed trace подтвердила точные источник,
  кандидата, промпт, два UnknownWord и положительный raw Ministral с пустыми
  issues. Затем один отдельный RONI recovery выполнил свежие hard gates и LT,
  квалифицировав только два точных научных названия. Исходные отказ, failed
  claim и raw/warnings сохранены; нового опроса модели не было.
- Один native CAS обновил тот же Postiz DRAFT. Полный EN/RU, ревизия 6,
  новый content/evidence/trace hash и точный remote текст проверены; состояние
  automatic_ready. Согласований и dispatches на этой стадии нет, все пять
  адресатов paused, активных отправок и leases нет. Это живая коррекция
  черновика, а не подтверждение его доставки.
- Будущие 04:06 МСК прошли preflight: интервал после фактической NISAR —
  3647,24 секунды. Назначение выполняется отдельно для нового exact hash.
  Heartbeat ACTIVE с точным промптом 0.6.23, прежними расписанием и чатом;
  подготовка по-прежнему зависит от работающего desktop Codex.

## 0.6.23 — 2026-10-21: название RONI в научной проверке орфографии

- NISAR (NASA SVS 5615) доставлена в 03:05:12 МСК: один verified dispatch,
  Postiz PUBLISHED и полный фактический текст MAX сверены. Интервал после
  предыдущей статьи — 3659,44 секунды; все пять адресатов снова paused.
- В новой статье NASA SVS 5685 исправлено реальное искажение: трёхмесячное
  среднее должно относиться к центральному месяцу, а не рассчитываться по
  одному месяцу. Исправленный текст получил положительный Ministral без issues,
  но LT остановил его на двух правильных «Ниньо», предлагая «Нино». Русская
  страница ВМО в разделе «Измерения» подтверждает точное название RONI.
- Добавлена отдельная узкая source-bound квалификация названия и отдельный
  одноразовый RONI recovery по полному trace и exact pending snapshot. Прежний
  MUR-путь не расширяется. Старые failed claim/raw/отказ сохраняются; нового
  Ministral нет, свежие hard gates и LT обязательны перед same-post native CAS.
  Отрицательные verdict, иные LT-ошибки, unknown и начатая remote mutation
  остаются блокирующими. Восстановление не назначает дату и не согласует отправку.
- До изменений runtime сохранены закрытый backup исходников, настроек, журнала
  и обеих БД 0.6.22; отправок, jobs и leases нет, все адресаты paused.
  Результаты проверок, развёртывания и живой коррекции будут записаны отдельно
  после завершения. Новый черновик ещё не является доставленной публикацией.
- До развёртывания прошли 313 quality/rubric/protocol проверок, 463 проверки
  цепочки публикации и 26 новых проверок RONI recovery. Ruff, pip check,
  version consistency и diff-check успешны; независимый code review блокеров
  не обнаружил. Это offline/mock проверки, не доказательство живой коррекции.

## 0.6.22 — 2026-10-21, 00:46 МСК: живая проверка исправленного черновика

- Код a93a9d3 сохранён на GitHub и развёрнут на VM: 349 файлов сверены,
  source/import/distribution 0.6.22. Postiz 0.5.1 сохранён без новой сборки;
  служебная проверка после старта прошла 28/28. На VM завершены 865 тестов
  за 259,82 секунды, один upstream warning; тесты не выданы за публикацию.
- Для NASA SVS 5600 полная trace-привязка проверена на фактическом pending
  snapshot. Одно отдельное recover-staged-spelling повторило hard gates и LT,
  квалифицировав только научный термин; исходный raw Ministral и два warnings
  сохранены побайтно, нового inference не было. Прежний failed correction claim
  и его отказ остались неизменными. Новый recovery claim завершён однократно.
- Один native CAS обновил тот же Postiz DRAFT; полный исправленный текст,
  evidence/trace hash и revision 6 сверены. Возвращён automatic_ready с hash
  29dc5337a8d2913b81b266fc65c5e19806002455ce7b9c65670e45c9bd4d5248.
  Ни approval, ни dispatch на этом этапе нет, все пять адресатов paused,
  активных отправок и leases нет. Источник, исходный час и дедупликация сохранены.
- Read-only preflight подтвердил будущие 01:03 МСК и интервал 3650,11 секунды
  после фактической PACE; scheduling и approval выполняются отдельно после этого
  результата. Это подтверждение подготовки, не доставки следующей статьи.
- Heartbeat ACTIVE с точным промптом 0.6.22 и прежними расписанием/чатом.
  Подготовка зависит от работающего desktop Codex; отправляет прежний единственный
  dispatch-only worker. Новые маршруты и Reddit не включались.

## 0.6.22 — 2026-10-21: научный термин в проверке орфографии

- Первая живая коррекция 5600 получила положительный Ministral для исправленного
  текста, но LT остановил её на «многомасштабного», предложив «много масштабного».
  Это unknown-word словаря: форма «многомасштабного анализа» подтверждена первичной
  статьёй Копысова и Сагдеевой в «Вычислительных методах и программировании»,
  2005, 6(1), 1–8 (Math-Net vmp623). До отказа внешних изменений Postiz,
  approvals и dispatches не было; прежний текст и failed claim сохранены.
- Добавлена узкая source-bound орфографическая квалификация только точного
  термина, правила MORFOLOGIK UnknownWord и UTF-16 позиции; остальные опечатки
  и грамматические замечания не снимаются. Отдельное одноразовое восстановление
  требует полного неизменённого trace, точного pending snapshot и кандидата,
  исходного positive Ministral с пустыми issues и доказанного отказа только LT
  до любой записи Postiz. Модели не опрашиваются; обязательны новые hard gates
  и LT, прежние отказ/claim/raw остаются в истории. Same-post native CAS,
  пауза, дедупликация и отдельный publish сохраняются.
- До новой правки сохранены исходники, настройки, журнал и обе БД версии 0.6.21.
  Результаты тестов, развёртывания и живого восстановления будут записаны после
  завершения. Это правило не превращает содержательный отказ модели в одобрение.
- До развёртывания прошли 203 проверки quality/rubric/protocol, 60 проверок
  correction/recovery и четыре ключевых повтора после последней защиты хеша
  сохранённого промпта. Ruff и diff-check успешны; это offline/mock результат.

## 0.6.21 — 2026-10-21, 00:33 МСК: результат развёртывания

- Commit be2e044 сохранён на GitHub, 348 файлов сверены на VM; source/import/
  distribution 0.6.21. Образ Postiz 0.5.1 проверен по image ID, source/compiled
  guard hash и 10 исполняемым контролям; frontend/backend/orchestrator online
  без перезапусков. После окончания запуска API эксплуатация прошла 28/28.
- На VM прошли 809 адресных тестов за 238,33 секунды, один upstream warning.
  Это offline/mock проверки, не доказательство отправки новой статьи.
- Штатный correct-staged для 5600 выполнил один новый Ministral: positive с
  двумя advisory warnings. LT дал единственный unknown-word на верном термине;
  операция завершилась needs_attention до native POST, сохранив fail-closed
  pending metadata и durable claim. Ни approval, ни dispatch не созданы,
  исходный DRAFT не перезаписан. Повтор операции или модели не выполнялся.
- Heartbeat ACTIVE с точным промптом 0.6.21, прежними расписанием и чатом.
  Все пять адресатов paused; для подготовки по-прежнему нужен desktop Codex.

## 0.6.21 — 2026-10-21: исправление автоматического Postiz DRAFT

- PACE (NASA SVS 5599) фактически доставлена 21 октября в 00:02:09 МСК:
  verified dispatch, Postiz PUBLISHED, одна попытка. В MAX сверены точная проза,
  оба полных URL, служебная подпись и один экземпляр заголовка. Интервал после
  предыдущей доставки — 3656,37 секунды; после результата все пять адресатов
  paused, активных отправок и mutation leases нет.
- Следующий Hy-перевод NASA SVS 5600 получил положительный Ministral и LT,
  но независимая сверка выявила реальные ошибки: JPL названа проектом,
  пропущено точное описание MUR, форматы визуализации приписаны данным.
  Такой passed результат не принят за смысловую гарантию и не отправлен.
- Добавлен отдельный correct-staged для изменённого кандидата того же
  автоматического DRAFT: exact old hash, текущий час, проверка источника/политики,
  отсутствие approvals/dispatches/leases, private backup и durable claim.
  Новый Ministral и LanguageTool предшествуют единственному native обновлению
  того же Postiz ID без назначения даты. Старые текст, evidence/raw/trace и
  история сохраняются; неизвестный исход блокируется без повтора. Человеческий
  режим и approval API не используются. Native rotation group аудируется.
- Native guard Postiz и Hub получили compare-and-swap: ожидаемый remote hash
  проверяется свежим GET после получения единственной штатной mutation lease.
  Конкурентная правка блокируется до controller, подтверждение CAS проверяет
  клиент. Ошибка GET до записи освобождает только собственный lease; unknown
  результата записи не разрешает повтор. Образ 0.5.1 собирает только guard
  поверх проверенного 0.5.0, сохраняя frontend и прежние compiled repairs.
- До изменений сохранены исходники/конфигурация/журнал и обе БД VM; прежние
  исходники 0.6.20 находятся на GitHub. Проверки и результат развёртывания
  фиксируются отдельной записью после завершения; локальные тесты не являются
  доказательством живого исправления или доставки следующей статьи.
- До развёртывания прошли 41 проверка correct-staged, 18 проверок Postiz-клиента,
  адресные native CAS/bridge-регрессии, Ruff и diff-check. В Docker-сборке
  выполнены 10 контролей фактического compiled guard с локальными зависимостями;
  живой bridge и отправка этими тестами не вызывались.

## 0.6.20 — 2026-10-20, 23:01 МСК: подтверждение развёртывания и доставки

- Исходники 5797838 сохранены на GitHub и развёрнуты на VM: 340 файлов сверены,
  source/import/distribution 0.6.20. Приватный source-only rollback не перезаписывает
  БД, текущий журнал, настройки или секреты. Перезапущен тот же Hub service с
  --dispatch-only; один процесс, NRestarts=0. На VM прошли 29 адресных проверок
  dispatch/cadence (один upstream deprecation warning), эксплуатация — 28/28.
- Почасовая automation ACTIVE, точный промпт 0.6.20 и прежние расписание/чат
  сохранены. Новые источники и остальные маршруты не включались.
- Тому же NASA SVS 5632 DRAFT назначены 23:01 МСК; отдельная hourly_article_policy
  создала одно согласование и один dispatch. В 23:01:13 получены verified и
  Postiz PUBLISHED с одной попытки. В фактическом MAX сверены точная проза,
  оба буквальных URL в отдельных link nodes, attribution, время 23:01 и один
  экземпляр заголовка. AX отделяет ссылки от прозы, поэтому проверка не подменяет
  полное строковое сравнение результатом с пропущенным URL.
- Интервал после предыдущей доставки — 54303,62 секунды; пропущенные часы не
  догонялись. После результата все пять адресатов paused, активных dispatch и
  mutation leases нет. Прежний отказ, новый verdict, три advisory warnings и LT
  сохранены вместе с точными EN/RU/hash; ручное согласование не использовалось.
- Повторный Status подтверждает AC 0/0/0 и неизменность DC. В 22:34–22:57 новых
  power/sleep/time-change событий нет, но была задержка исполнения около 21 минуты.
  Её причина этим аудитом не установлена; исправление питания не объявляется
  доказательством непрерывного desktop heartbeat или полной автономности VM.
  Для подготовки по-прежнему нужны работающий Codex, сеть и модели.

## 0.6.20 — 2026-10-20: защита почасовой подготовки от сна рабочего места

- Журналы Windows подтвердили сон при закрытии крышки с последующей гибернацией
  через 60 секунд при питании от сети. VM, модели и единственный dispatch-only
  worker оставались доступны; desktop-подготовка зависит от работающего Codex.
  Периоды гибернации 08:19–15:40 и 17:42–22:04 МСК объясняют часть перерыва;
  промежуток бодрствования 15:40–17:40 не объяснён этими событиями.
- Добавлена обратимая настройка только AC lid/sleep/hibernate с приватной
  резервной копией исходных значений, проверкой неизменности DC и отдельным
  восстановлением. Первое реальное применение остановилось до backup/мутаций:
  два Git в PATH дали неоднозначный массив executables. Исправлен выбор первого
  конкретного Application для Git и powercfg; добавлена регрессия этого случая.
- Пройдены 10 mock-контролей настоящего PowerShell-кода (один pytest), Ruff и
  diff-check. Независимо проверены Status и исходные девять mock-контролей
  в Windows PowerShell 5.1. Моки не выданы за изменение питания.
- 22:34:52 МСК: реальный Apply завершён verified. AC lid/sleep/hibernate стали
  0/0/0; DC остались 1/1200/60, активная схема прежняя. Уникальные backup, intent
  и receipt сохранены приватно. Restore проверен моками; живой сон/откат не
  запускались. Работа от батареи, выключение, закрытие Codex и потеря сети по-прежнему
  могут прерывать подготовку; защита не является гарантией круглосуточной работы.
- До изменений сохранены приватные исходники/конфигурация/журнал и обе БД VM;
  исходная 0.6.19 уже сохранена на GitHub. Перед резервированием активных отправок,
  leases и jobs нет, все пять адресатов paused. Версии пакета и актуальный
  промпт согласованно переведены на 0.6.20.
- Последняя фактическая доставка NASA SVS 5595 подтверждена в 07:56:09 МСК:
  один verified dispatch, одна попытка, Postiz PUBLISHED и точный текст MAX.
  Просроченная подготовка 5594 получила ARTICLE_SLOT_EXPIRED до нового inference;
  исходник, прежний отказ, исправленный кандидат и использованный час сохранены.
  Защита не обходилась; пропущенные часы не догоняются.
- После отдельного приватного экспорта EN/RU и trace старую pre-draft 5594 закрыли
  штатным cancel-preparation. Отсутствие Variant/DRAFT/approval/dispatch/lease и
  remote post проверено; источник, час, отказ, кандидат и история не сброшены.
- Для текущего часа выбран один новый полный текст NASA SVS 5632 с проверенными
  правами и дедупликацией всех 30 прежних источников. Реальные ошибки Hy (будущее
  время, потерянные эфемериды, несогласованное название) исправлены в изменённом
  кандидате того же record. Свежие Ministral и LanguageTool успешны; один Postiz
  DRAFT, exact hash, три advisory warnings сохранены. На этом этапе approval и
  dispatch отсутствуют; DRAFT не принят за публикацию.

Даты указаны по Europe/Moscow. Новые записи добавляются сверху при каждом
законченном изменении проекта; секреты и содержимое публикаций сюда не попадают.

## 0.6.19 — 2026-10-20: ограниченный advisory-контракт и адресное восстановление

- Полный живой цикл новой статьи выявил протокольный сбой: Ministral вернул
  положительное типизированное решение без issues, но четыре advisory warnings
  превысили общий прежний лимит трёх. Система корректно остановилась до DRAFT.
- Advisory warnings отделены от содержательных issues: до 8 строк, до 1024
  символов каждая и 8192 UTF-8 байт суммарно. Типы, exact keys, отрицательные
  решения, существенные ошибки и все hard gates сохраняют блокировку; raw и
  замечания не усекаются. Семантический промпт/рубрика не меняются.
- Добавлен отдельный одноразовый recover-review-protocol только для доказанного
  warning-count-only сбоя последнего pre-draft trace. Exact hashes, текущая
  политика и отсутствие staging/approval/dispatch/lease обязательны. Сохранённый
  положительный verdict не пересэмплируется; новые hard gates и LanguageTool
  формируют новую evidence. Исторические review_invalid/rejected не переписаны.
- Перед runtime-изменениями сохранены исходники/настройки/журнал и обе БД;
  исходная 0.6.18 с тегом уже находится на GitHub. Проверки, развёртывание и
  фактический результат следующей доставки фиксируются после завершения;
  незавершённая подготовка не считается публикацией.
- Локально: 93 targeted protocol/meaning-first/evidence и 176 legacy/automatic/
  migration проверок прошли; наборы частично пересекаются. Отдельно подтверждены
  запрет отрицательного/невалидного verdict, повреждённой схемы и повторного
  семплирования. Ruff/diff-check успешны. Это ещё не доказательство новой доставки.
- Для cycle/CLI прошли 38 адресных проверок восстановления, включая дедупликацию,
  backlog 24 и чужие unknown/staging до и после нового LanguageTool. Интеграционный
  cycle/automatic набор дал 160 PASS до последнего малого дополнения backlog;
  оно отдельно покрыто свежим адресным набором. На рабочей VM набор повторяется
  после развёртывания; локальные результаты не подменяют его.
- Развёрнута 0.6.19 в source/import/distribution; сверены 336 файлов. На VM
  завершены 730 тестов (один upstream warning), 28/28 эксплуатационных проверок
  успешны. Независимый итоговый аудит исходников блокеров не нашёл.
- 20 октября, 01:39 МСК: выполнено одно адресное protocol recovery новой статьи.
  Прежние два неуспешных preparation attempts и старый trace сохранились точно;
  positive raw Ministral и все четыре предупреждения не менялись. Повторных
  inference нет, свежий LanguageTool и hard gates успешны. Создан один DRAFT.
- Для того же поста назначены 01:50 МСК; обычная hourly_article_policy создала
  одно согласование и одно pending-задание с неизменным проверенным текстом.
  Heartbeat ACTIVE с точным промптом 0.6.19 и прежним почасовым расписанием;
  назначенную отправку выполняет прежний единственный dispatch-only worker.
  На этом этапе публикация ещё ожидается, pending не принят за доставку.
- 20 октября, 01:50:14 МСК: полный цикл новой NASA SVS 5670 завершён живой
  публикацией. Один dispatch verified, Postiz PUBLISHED, одна попытка. Точный
  основной текст, обе буквальные ссылки, attribution/footer и отметка 01:50
  независимо сверены в фактическом MAX; заголовок встречается один раз.
  Интервал после SSHA — 3660,42 секунды. Все пять адресатов снова paused,
  активных отправок и mutation leases нет. История/trace сохранены; новый
  протокол не потребовал повторного семплирования положительного verdict.

## 0.6.18 — 2026-10-20: согласованные лимиты почасовой политики

- Второй живой цикл выявил конфликт: max_test использует профиль test с прежним
  интервалом 120 минут и лимитом 16 в сутки, тогда как связанная автоматическая
  политика требует минимум 3600 секунд и максимум 24. Postiz подтвердил тот же
  QUEUE на будущую дату, но Hub отказал до создания approval/dispatch.
- Только для уже проверенного automatic hourly планирование использует лимиты
  связанной политики и Europe/Moscow. Обычные маршруты и human сохраняют профиль.
  Общая проверка выполняется до изменения Postiz и повторяется под блокировкой
  адресата при approval; actual-delivery cadence, дедупликация и lease сохранены.
- Добавлено адресное одноразовое reconcile-approval подтверждённого QUEUE только
  после SCHEDULE_GAP_TOO_SHORT: exact hash/date/target/evidence, отсутствие любых
  прежних approvals/dispatches/lease, durable claim и audit. Нет повторного POST,
  переноса даты, новой оценки моделей или человеческой роли. Unknown не снимается.
- Default status после завершения отправки старого мигрированного черновика
  финализирует именно его незавершённый ledger, даже если последняя по порядку
  запись уже опубликована. Приоритет sending/unknown и активных dispatch сохранён;
  следующий свободный час не блокируется устаревшим pending в журнале.
- Перед исправлением сохранены закрытые исходники/настройки/ledger и обе БД;
  исходная 0.6.17 уже сохранена на GitHub. Проверки и runtime-результат фиксируются
  ниже после завершения; назначенная публикация ещё не считается доставленной.
- Локально прошли 82 integrated automatic/reconcile/migration теста и 15 тестов
  лимитов/истории; дополнительно проверены гонка после claim и финализация старого
  published record. Ruff/diff-check успешны, независимый статический review без
  блокеров. Эти результаты не являются подтверждением новой живой доставки.
- На рабочей VM source/import/distribution 0.6.18: сверены 333 исходных файла,
  завершены 632 теста (один upstream warning), 28/28 health checks успешны.
  Перед изменением runtime подтверждены нулевые отправки/lease; прежний
  единственный dispatch-only worker перезапущен без запуска других маршрутов.
- 20 октября, 00:24 МСК: после свежей сверки отсутствия статьи в MAX выполнен
  один reconcile-approval для того же SSHA QUEUE. Созданы одно согласование
  hourly_article_policy и одно pending-задание на прежние 00:49:00 МСК;
  текст, revision и Postiz post сохранены. Повторных scheduling POST, inference
  и отправки нет. Исходный отказ и причина восстановления сохранены в аудите.
- Почасовой heartbeat снова ACTIVE с точным промптом 0.6.18 и прежним расписанием
  на пятой минуте; конфигурация сверена после записи. До наступления даты работает
  существующий VM worker. Новая доставка пока не подтверждена: следующий heartbeat
  должен сверить dispatch, Postiz PUBLISHED и фактический текст MAX, затем паузу.

## 0.6.17 — 2026-10-20: ссылки для текстового транспорта MAX

- Живая публикация выявила отсутствие Markdown-рендеринга в браузерном MAX.
  Inline-ссылки нормализуются до gates/Ministral/LanguageTool: подпись остаётся
  в предложении, каждый literal URL вынесен на отдельную строку. Оригинал не
  изменяется; доказательства и хеши относятся к конечному тексту для отправки.
- Canonical candidate hash учитывает нормализацию, raw input сохраняется
  отдельно: эквивалентный Markdown/plaintext отказ не пересэмплируется.
  Старые traces сохраняют прежний контракт; staged-пост не переписывается
  молча. Ранее доставленная 5654 остаётся в истории без повторной отправки.
- Точный служебный заголовок/footer отделяется от review-сегмента и при одном,
  и при двух переводах строки после Postiz. Прежний точный footer-only с двумя
  LF поддержан для совместимости. Чужие ссылки, неполная атрибуция с одним LF,
  содержательный текст и научные числа не удаляются. Старый неуспешный preflight
  миграции 5591 не сбрасывается: новая отправка или повтор модели не выполнялись.
- Локально: 145 quality-проверок, 73 replay/trace-проверки, 34 stage-rejected
  и 77 boundary/human/migration проверок прошли; наборы частично пересекаются.
  Ruff и diff-check успешны. Состояние развёртывания фиксируется отдельно ниже.
- Развёрнута рабочая 0.6.17 (import и distribution), сверены 331 исходный файл.
  На VM завершены 590 тестов (один upstream deprecation warning), 28/28 health
  checks успешны. Existing automatic_ready 5663 остаётся допустимым с тем же
  точным hash, без новой оценки, согласования или отправки при развёртывании.
  Есть закрытая предшествующая копия исходников, настроек, журнала и обеих БД.
- 20 октября, 00:01 МСК: heartbeat max-postiz возобновлён в ACTIVE с промптом
  0.6.17 и прежней почасовой периодичностью на пятой минуте. Политика automatic
  включена; единственный worker dispatch-only, все адресаты между публикациями
  paused. Нулевая активная отправка и lease подтверждены. Почасовая подготовка
  по-прежнему требует работающего Codex на компьютере владельца; уже назначенную
  отправку выполняет VM. Новый plaintext-формат ссылок проверен тестами, отдельная
  живая публикация новой версии ещё не выполнялась из-за ограничения интервала.
- 20 октября, 00:02 МСК повторно обновлены Gmail-переписка и поиск писем по
  Reddit №18605497: нового ответа/одобрения нет. Свежий read-only status VM
  подтверждает отсутствие OAuth-параметров и выключенный Reddit. Дубликат заявки
  и внешние сообщения не отправлялись.

## 0.6.16 — 2026-10-19: автоматический почасовой цикл и критерии по смыслу

- По новому прямому решению владельца отменён обязательный human gate для
  почасового max_test. Добавлен явный automatic-режим с отдельным actor и аудитом
  точной версии/даты/адресата/доказательств. Старый human-режим и история сохранены;
  человеческие checkbox, cookie и роль editor автоматизацией не имитируются.
- Новая рубрика meaning-first-v1 отделяет стилистические предупреждения от
  существенных искажений, пропусков и выдуманных фактов. Числа, URL, язык, права,
  орфография/грамматика и инфраструктура сохраняют блокировку. Raw/parsed ответы,
  warnings, LT и хеш промпта сохраняются; negative не превращается в positive.
- Актуализированы правила, пример политики, актуальный промпт, инструкция и
  паспорт компонентов. Перед изменением VM сохранены закрытые копии исходников,
  настроек, журнала и обеих БД; исходный 0.6.15 подтверждён на GitHub.
- Reddit: браузерная Gmail-проверка 19 октября не нашла нового ответа после
  уточнения от 17 октября. VM OAuth status: одобрения, client ID и refresh token
  нет, Reddit остаётся выключенным. Connector требует авторизации, браузерный
  просмотр с действующей сессией выполнен; письма не отправлялись.
- На этапе подготовки пройдены 188 локальных quality/legacy тестов и Ruff.
  Эти проверки не доказывают живую доставку или процент прохождения переводов;
  развёртывание и итоговые проверки фиксируются отдельно ниже.
- Контракт automatic проверен 36 тестами; отдельно прошли переход границы часа
  и canonical hash промпта при CRLF/LF. Адресная миграция — 18 тестов, readonly
  автоматическая карточка/API — 7 тестов; прежний human API — 41/41. При review
  найден и устранён конфликт вложенных mutation leases: единственный владелец
  блокировки — установленный Postiz guard, завершение ожидается только чтением.
  Unknown/timeout сохраняет needs_attention без повторного POST или unlock.
  Локальные staged-правки automatic явно блокируются до отдельного пути новой
  проверки; до DRAFT доступна аудируемая правка candidate. Живая доставка ещё
  не подтверждена этими offline проверками.
- Общий локальный интеграционный набор automatic/migration/human/exception/cycle/
  dispatch-only/bridge завершён: 249/249 passed. Последние регрессии границы часа,
  CRLF/LF и выбора старого активного dispatch прошли отдельно; Ruff и diff-check
  успешны. Эти результаты относятся к локальному стенду, до запуска рабочей VM.
- На рабочей VM установлена 0.6.16: import/distribution совпадают, сверены
  329 файлов, прошли 537 тестов и 28/28 эксплуатационных проверок. В 23:37 МСК
  включена automatic-политика. Живой отрицательный контроль новой рубрики
  отклонил намеренную потерю отрицания за один запрос; выдачи его за согласование
  или публикацию не было. Начат новый полный Hy-перевод NASA SVS 5654;
  результат доставки фиксируется отдельно после фактической сверки.
- Живой Hy-кандидат 5654 принят новой смысловой рубрикой со стилистическими
  warnings, но остановлен LanguageTool на настоящей грамматической ошибке.
  Через аудируемый candidate той же записи исправлены грамматика и заголовок;
  новая точная версия прошла независимый Ministral и LT. Первая попытка и отказ
  сохранены, создан один DRAFT. Это подтверждает работу разделения рекомендаций
  и блокирующих ошибок, но не даёт статистики прохождения большинства текстов.
- Полный живой цикл 5654 завершён: назначено 23:48:45 МСК, verified dispatch
  подтверждён в 23:48:59, Postiz PUBLISHED, фактический текст независимо проверен
  в MAX. Один исходный Postiz ID, одна отправка, один экземпляр сообщения;
  max_test автоматически вернулся на паузу, active dispatches/lease равны нулю.
  Прежний ICESat-2 после тайм-аута ожидания согласования восстановлен штатным
  ERROR→DRAFT без смены ID/текста/даты и без потери истории; его отправок нет.
- Прежний SSHA-черновик 5663 адресно проверен новой рубрикой и LT один раз:
  automatic_ready, тот же Postiz DRAFT, без согласования и отправки в занятый час.
  Старые доказательства и metadata сохранены отдельным миграционным аудитом.

## 0.6.15 — 2026-10-19: передача отказа Ministral человеку

- По прямому решению владельца валидный содержательный отказ с завершёнными
  hard gates и LanguageTool создаёт один Postiz DRAFT. Исходный raw/parsed verdict
  и все замечания сохраняются со статусом rejected; отказ не становится passed.
  Невалидный ответ, недоступная инфраструктура, числа/язык/права и неуспешный LT
  не снимаются человеческим исключением.
- Hub показывает полные доказательства и требует отдельный checkbox разбора,
  содержательное пояснение и прежнее подтверждение человека. Решение связывается
  с approval, точным текстом/исходником, хешем доказательств, адресатом и датой;
  worker проверяет эту связь перед отправкой. Bearer и CLI не согласуют статью.
- Добавлен stage-rejected: восстанавливает только последний законченный отказ
  из полного канонического trace, повторяет локальные gates и LT adjudication,
  не вызывает модели, сохраняет отмену/попытки/использованный час. Existing
  Variant/RemoteDraft, unknown outcome, активные отправки и lease блокируют повтор.
- Актуализированы правила, инструкция и промпт; версии пакета синхронизированы.
  Проверки: 214 прежних quality/cycle/human тестов, 102 проверки нового quality,
  parser и approval gate, 7 сквозных offline проверок с настоящим валидатором
  доказательств, 41 API-тест; Ruff и синтетический UI без ошибок. Живые согласования
  и публикации этими тестами не выполнялись. Развёртывание проверяется отдельно.
- Дополнительные проверки: 34 теста stage-rejected и 135 тестов согласования,
  отправки и API прошли локально. На рабочей VM — 412 passed, 28/28 health checks,
  import/distribution 0.6.15. Сохранены закрытые копии исходников/настроек и обеих БД.
  Реальный прежний отказ восстановлен из канонического trace в один Postiz DRAFT:
  rejected, три исходных замечания, согласований/dispatch/lease 0. История отмены
  и использованный час сохранены, inference не повторялся. Счётчики Postiz:
  12 PUBLISHED, 3 DRAFT, 0 QUEUE; все адресаты paused. Возобновлён heartbeat с новым
  правилом человеческого исключения. Код, документация и актуальный промпт сохранены
  на GitHub; доставка этой правкой не выполнялась и не заявляется.

## 0.6.14 — 2026-10-19: прямой Hy и обязательный редактор-человек

- По новому поручению владельца почасовой сценарий готовит перевод Hy-MT2-7B,
  независимую сверку Ministral по английскому оригиналу и LanguageTool. Результат
  — Postiz DRAFT, ожидающий человека; прежний CLI publish запрещён. В Hub показаны
  оригинал и перевод, дата того же поста из календаря, отдельное подтверждение
  человека. Bearer не согласует такую статью, правки сбрасывают машинное доказательство.
- Проверка привязана к точному тексту/исходнику. Повтор неизменённого отклонённого
  кандидата не пересэмплируется; ошибка инфраструктуры не становится одобрением.
  Добавлены защита смены ревизии во время staging, запрет медиа, сверка активного
  dispatch после переноса даты, интервал фактической доставки 3600 секунд и пауза
  после verified. Другие маршруты и Reddit этим поручением не включаются.
- Добавлен отдельный режим существующего Hub worker `serve --dispatch-only`:
  он не запускает общий tick, сборщики, recurring, видео или Reddit и выбирает
  только почасовые отправки с человеческим согласованием. Linux unit закрепляет
  этот ограниченный режим; второй worker не создаётся.
- Для Hy и Ministral применён native idle-sleep 15 секунд с закрытым backup
  прежних unit/argv. Gateway пассивно ждёт выгрузки рецензента до Hy и выгрузки Hy
  до проверки; не повторяет inference и не использует POST sleep. Профили моделей
  и лимиты памяти сохранены. Ограничение: чужая нагрузка на хост не блокируется,
  cold start может дать безопасный timeout.
- До правок исходный вариант 0.6.13 сохранён на GitHub отдельным commit и tag
  v0.6.13-pre-human-review-20261019. Обновлены AGENTS, актуальный промпт и инструкции.
  API/UI проверки: 53 теста и 20 финальных API-тестов; gateway/quality: 254 теста,
  Ruff и JS syntax без ошибок. Synthetic UI не вызывал живые согласования.
  Рабочее развёртывание и живой перевод проверяются отдельно; эти локальные тесты
  не являются доказательством доставки в MAX.
- На рабочей VM выполнены 133 проверки новых human/gateway/quality путей.
  Непубликующие живые контроли: Hy-перевод принят (164,36 с), намеренная потеря
  отрицания отклонена Ministral (79,98 с). В реальном NASA-тексте Hy ошибся с
  термином land ice и записью числа; отказ числового gate сохранён. После явной
  правки реальных ошибок Ministral вернул три замечания, противоречащие полному
  кандидату/исходнику; LanguageTool не нашёл ошибок. Отказ не пересэмплировался
  и не заменён одобрением. EN/RU и замечания экспортированы приватно, pre-draft
  запись закрыта после сверки отсутствия внешних операций; источник и час сохранены.
  Нового успешного Postiz DRAFT или живой доставки этот прогон не доказывает.
- В 21:37 МСК возобновлены human-политика, существующий dispatch-only worker и
  heartbeat на пятую минуту часа. Postiz разрешает только main/max, 34 очереди
  исключены, RUN_CRON пуст; image ID сохранён. Все пять адресатов paused,
  новых согласований/отправок 0, счётчики Postiz остались 12 PUBLISHED/1 DRAFT.
  При первом старте frontend ответил раньше API: автоматический безопасный откат
  сохранил паузу; после проверки authenticated GET /posts включение завершено.
  Заключительные Linux-проверки human/dispatch-only/API: 69 passed; проверка
  статусов и запрета локального редактирования: 3 passed. Повтор ранее отклонённой
  статьи не разрешён; следующий запуск выбирает новый источник в новом часу.
- RO-скрипт check-stack расширен до 28 проверок: включены unit/gateway Hy и
  настоящий авторизованный GET /posts Postiz без запуска Hub/БД или inference.
  Шесть тестов проверяют готовность, 401/503, отсутствие конфигурации и защиту
  секрета в сообщении об ошибке. Предыдущий frontend-only сигнал больше не
  считается достаточным для API.
- Заключительная живая сверка 21:40 МСК: 28/28 проверок инфраструктуры и API,
  ровно один dispatch-only Hub процесс; активных dispatch/leases/jobs/recurring
  и OpenCode sessions нет, обе модели спят, bridge без ошибок. Ключи, исходники,
  ответы моделей, экспорт перевода и эксплуатационные доказательства — только
  в ignored data. Эти результаты не заменяют согласование и новую живую доставку.

## 0.6.13 — 2026-10-19: тест Argos → Hy 7B с независимой проверкой

- На втором физическом узле создана отдельная CPU-среда Argos 1.11.0,
  EN→RU 1.9, CTranslate2 INT8 и MiniSBD. Архив и английский ONNX закреплены
  SHA-256; инференс выполняется без сетевых загрузок, два потока, без GPU.
  Сохранены две ошибки совместимости старого Stanza до инференса; исходные
  веса перевода не изменялись. Рабочий прогон завершил 10/10 переводов,
  включая два повтора без response cache и полный NASA-текст.
- Argos независимо оценён по прежнему корпусу: 3/7 PASS, 38/56, полный текст
  NASA — FAIL. Добавлены тестовый каскад с отдельным Ministral, контрольными
  правильным/ложным переводами, ограниченным fallback к Hy и отдельный
  исследовательский вариант маршрутизации сложных входов. Неизвестный исход,
  некорректная проверка и повторное отклонение требуют редактора; согласие
  двух переводчиков не используется как доказательство.
- Пройдены 337 регрессионных тестов и 22 дополнительных теста второго
  эксперимента, Ruff без ошибок. Детальные живые результаты и ограничения
  фиксируются в docs/ARGOS_CASCADE_EVALUATION.md. Это отдельный тестовый стенд:
  основной маршрут Hy и рабочий пакет 0.6.12 на VM112 не переключались,
  пауза Hub/адресатов и отсутствие публикаций сохранены.
- В исходном каскаде завершены восемь входов: по семи коротким последнее
  полученное содержание прошло 5/7, но среди пяти одобренных переводов был
  один ложноположительный результат — потеря двух аппаратов ESCAPADE.
  После дополнительного правила сложности результат последних кандидатов —
  6/7, 49/56; выбраны пять переводов, все пять проходят прежнюю шкалу.
  Это исследование после первого прогона, не слепая проверка; один Hy PASS
  остаётся пограничным из-за синтаксической кальки. Полный NASA и идиома
  не прошли; хорошая версия заголовка блокирована протоколом проверки.
- На 12 ГиБ RAM выявлены давление памяти и дисковое чтение при одновременном
  размещении Hy Q8 и Ministral. Временно проверен idle-sleep рецензента;
  исходный unit восстановлен с совпадением SHA, таймер отката отключён,
  реальный API smoke вернул OK. Исходные timings имеют смешанный профиль,
  общего ускорения E2E они не доказывают. Два сбоя доступности при контролируемом
  перезапуске отделены от смысловых ошибок. Дополнительная первая смысловая
  проверка идиомы после известного 503 распознала ошибку, но превысила лимит
  длины замечаний; исходный отказ не заменён согласием. Автоприёмка и
  автопубликация этим тестом не включаются.

## 0.6.12 — 2026-10-18: выделенный перевод EN → RU и сравнение четырёх моделей

- Подготовлены закреплённые GGUF Hy-MT2-1.8B Q8, Hy-MT2-7B Q8,
  TranslateGemma 12B Q4_K_M и отдельная среда OPUS-MT CTranslate2 INT8.
  Корпус MAX/Postiz содержит семь случаев и два повторных замера на модель.
  Исходные ответы и эксплуатационные отчёты находятся в игнорируемой data/;
  методика и ограничения — в docs/TRANSLATION_MODEL_COMPARISON.md.
- Добавлен отдельный loopback gateway перевода и выбираемый адаптер в подготовке
  статей. Независимая модель сравнивает исходник с переводом; числовой фильтр,
  LanguageTool и отказ при ошибке проверки сохранены. Метаданные называют реальные
  модели. Повтор того же отклонённого перевода использует кеш только внутри задачи
  и не получает новое согласование повторным опросом рецензента.
- Реализованы проверки неверной конфигурации, обрезки ответа, занятости gateway,
  отдельного рецензента и непубликующий live smoke с ложным отрицательным контролем.
  Выполнены 299 локальных регрессионных тестов, Ruff без ошибок; пакет 0.6.12
  установлен на VM112, import/distribution версии совпали. Фактическая связка
  DedicatedTranslator Hy 7B → отдельный Ministral → LanguageTool подтверждена.
  Основной стенд: Hy 7B 6/7, TranslateGemma 5/7, Hy 1.8B 4/7, OPUS 2/7.
  Рабочий Hy 7B: 9/9 запросов без ошибок, четыре CPU-потока, медиана 32,865 с
  по семи входам, пик cgroup 8,214 ГиБ, OOM и перезапусков — 0.
  Пробуждение после простоя проверено.
  Формат чисел восстанавливается только при однозначном совпадении Decimal-
  значений, знаков и количества; прежний gate не ослаблен.
- Независимая проверка приняла отдельный буквальный положительный контроль
  и отклонила заведомую ложь. Полная статья переведена за 284,152 с с пробуждением
  и отклонена за 122,954 с: есть неточности перевода и ошибочные замечания рецензента.
  Контрольный машинный перевод тоже отклонён; альтернативный Qwen 9B не дал
  основания менять рецензента. Автономная приёмка и публикация не подтверждены;
  нужна редактура. Исходные отказы сохранены, согласование не пересэмплировалось.
  Подробные живые замеры и ограничения — в сравнительном отчёте.
  Выбор переводчика не снимает паузу Hub/адресатов и не включает публикацию;
  маршруты Reddit и legacy editor этим изменением не переключаются.

## 0.6.11 — 2026-10-18, 21:10: подтверждена полная пауза Postiz

- Пересоздан только существующий контейнер Postiz, image ID сохранён.
  Фактические RUN_CRON пустой и EXCLUDE_QUEUE из 36 очередей проверены;
  точный runtime compiled module с этими значениями даёт 0 worker definitions.
  PM2 frontend/backend/orchestrator и веб-панели работают, API вернул HTTP 200.
- SQL READ ONLY после пересоздания: 0 QUEUE и будущих публикаций, сохранены
  12 PUBLISHED и 1 DRAFT. В Temporal сохранены 3 служебных workflow.
  В main осталась только старая cached-запись poller с последним доступом
  до запуска нового контейнера; два замера подтвердили неизменность времени.
  Свежих pollers main/max нет. Workflow и очереди не удалялись.
- После применения пройдены 25/25 проверок стека. Политики выключены,
  Hub без worker, автоматизация на паузе. Документация возобновления требует
  нового поручения и разрешения только нужных очередей; массовое включение
  всех processors не предписывается. Это проверка паузы, не новой публикации,
  исправления качества модели или физической перезагрузки серверов.

## 0.6.11 — 2026-10-18, 18:44: проверка развёрнутой паузы

- На VM установлен пакет 0.6.11; import и distribution metadata совпали.
  Перед изменением проверены нулевые активные dispatch/leases, сохранены
  приватный архив исходников/конфигурации, прежний unit и дампы обеих БД.
  Существующий Hub остановлен и запущен с постоянным `90-owner-pause.conf`;
  второй worker не создавался. `systemd-analyze verify` прошёл, ExecStart
  содержит `--no-worker`, панель вернула HTTP 200.
- Политика выключена на VM и Windows, все пять адресатов на паузе.
  Автоматизация Codex `max-postiz` подтверждена как `PAUSED`; остальные
  найденные автоматизации также уже на паузе. В Windows не найдено
  отдельного MAX–Postiz worker или задания Task Scheduler.
- После запуска без worker: 25/25 проверок служб/контейнеров/API, ноль
  активных dispatch, mutation leases и legacy intents. Новая статья не
  публиковалась; очереди и черновики сохранены. Это проверка остановки
  фоновых заданий и доступности панели, не сквозной публикации или reboot-тест.
- Ограничения: ошибочные/некорректные ответы рецензента не устранены заменой
  проверки на согласование; исторический cgroup OOM и неустановленная причина
  перезапуска GPU требуют отдельного продолжения после нового поручения.
  Для повышения лимита памяти нужны дополнительная RAM либо проверенный
  профиль с меньшим потреблением; настройки shared LLM не менялись.

- Дополнение полной паузы Postiz: в его БД сверены 0 QUEUE, 12 PUBLISHED
  и 1 DRAFT. Три служебных Temporal workflow сохранены. Linux compose задаёт
  пустой RUN_CRON и исключение всех 36 фактических очередей, включая max.
  На точном compiled module из установленного custom image проверка
  конфигурации дала 36 → 0 worker definitions без нативных worker/соединений.
  Пауза рассчитана на этот image; при смене версии список сверяют повторно.

## 0.6.11 — 2026-10-18: пауза всех заданий и диагностика GPU143

- По прямому указанию владельца почасовая автоматизация поставлена на паузу,
  политика выключена, адресаты оставлены на паузе. Добавлен постоянный
  systemd drop-in `max-postiz-owner-pause.conf`: Hub сохраняет веб-панель,
  но запускается с `--no-worker`. Актуальный промпт запрещает возобновление
  по запоздалому heartbeat без нового поручения владельца.
- Паспорт компонентов дополнен результатами административного SSH-аудита
  GPU143: kernel, RAM, GPU, driver, служба и параметры модели. Подтверждён
  cgroup OOM в 12:09:29 МСК при лимите 8 GiB. Лимит не повышен: доступно
  лишь 11,60 GiB физической RAM; 10 GiB оставили бы 1,60 GiB другим нагрузкам.
  Настройки shared LLM сохранены. Причина перезапуска в 17:20 не доказана;
  близость загрузки модели к HTTP 503 не выдана за установленную причину.
- Текущая статья остановлена штатно после некорректного JSON рецензента с
  неразрешёнными замечаниями. Draft, Variant, Postiz post, approval и dispatch
  не созданы; отказы, исходник и использованный час сохранены. Проверки
  качества не ослаблялись. Изолированный опыт другого представления входа
  также отклонён; процедура не развёрнута и отменённый текст не пересэмплирован.
- Перед изменением сохранён GitHub tag
  `v0.6.10-pre-gpu-audit-20261018`. Секреты, резервные копии и диагностические
  отчёты хранятся только в ignored data. Версия согласована в pyproject и
  импорте пакета. Проверки развёртывания и оставшиеся ограничения приведены
  следующей датированной записью после сверки VM.


## 0.6.10 — 2026-10-18: проверка доказательств в рецензии перевода

- Инструкция Ministral требует сверять замечания с полными исходником и
  переводом, учитывать другие абзацы и скобки, приводить короткие точные
  фрагменты для конкретного расхождения. Дополнительное упоминание источника
  в заголовке не требуется, если оно отсутствует в исходном заголовке,
  а перевод сохраняет факты и грамматику. Реальные пропуски, ошибки смысла,
  отрицания, языка и темы по-прежнему требуют отказа.
- Код gates, схема ответа, LanguageTool, числовая проверка и защита от
  повторной рецензии отклонённого кандидата не изменены. Повторные замечания,
  противоречившие точному тексту, не были заменены согласованием: текущая
  pre-draft запись отменена штатно после сверки нулевых внешних операций.
  Исходник, использованный час и все отказы сохранены; публикации в этом
  запуске нет. Процедура и актуальный промпт описывают такую остановку,
  а также сверку очереди LLM после клиентского тайм-аута.
- Проверки Windows: 172 tests по article quality/cycle, Ruff и diff-check.
  Новый prompt отдельно проверен через реальные OpenCode/Ministral/LanguageTool
  на двух новых синтетических текстах без БД, Postiz или публикации:
  верный перевод принят, утрата отрицания отклонена, LT — 0 в обоих случаях.
  Каждый пример отправлен один раз; отменённая статья не пересэмплировалась.
- На VM развёрнута 0.6.10: перед заменой сверены пустая очередь Postiz,
  отсутствие активных dispatch и leases, сохранены приватные копии исходников,
  конфигурации, журнала и обеих БД. Перезапущена существующая служба Hub.
  Импорт пакета и distribution metadata совпали; на Linux прошли те же
  172 targeted tests, затем 25 проверок состояния служб, контейнеров и API.
  Глубокие генерации в health-check не запускались. После обновления max_test
  на паузе, активных dispatch и mutation leases нет.
- Ограничение: это узкая калибровка двух решений, а не сквозная публикация
  или доказательство устранения всех ложных замечаний. В отрицательном
  контрольном ответе наряду с реальной потерей отрицания осталось лишнее
  терминологическое замечание; оно сохранено в приватном trace. Следующая
  новая статья требует всех прежних проверок и точного reviewed-hash.
- Версия согласована в pyproject и импорте пакета. Исходная версия сохранена
  в GitHub tag `v0.6.9-pre-review-evidence-20261018`; секреты и runtime
  доказательства остаются в игнорируемых локальных файлах.

## 0.6.9 — 2026-10-18: развёртывание и подтверждённая публикация по расписанию

- На Linux VM развёрнута 0.6.9 после проверки отсутствия активных отправок
  и mutation leases. Сохранены приватные копии исходников, конфигурации,
  журнала и обеих БД; перезапущена только существующая служба Hub.
  Версия импорта и установленная distribution metadata теперь совпадают.
- Проверки развёрнутой VM: 221 targeted tests, восемь контейнеров healthy,
  восемь необходимых служб active/enabled; Hub, Postiz, OpenCode,
  LanguageTool и оба маршрута моделей отвечают. Локально ранее прошли
  237 targeted tests. Для Linux-проверки установлен pytest 9.1.1 из
  закреплённых wheels; требования runtime не изменены.
- Новая текстовая статья NASA прошла Qwen, независимый Ministral,
  LanguageTool (0 замечаний) и сравнение с полным оригиналом. Точная версия
  согласована политикой и поставлена в Postiz на 18 октября 06:22:23 МСК.
  В 06:22:32 подтверждены одна успешная попытка, dispatch verified,
  Postiz PUBLISHED и совпадающий текст в MAX без повторного URL.
- После сверки max_test возвращён на паузу; активных dispatch и mutation
  leases нет. Почасовая автоматизация Codex возобновлена на пятой минуте
  часа; журнал сохраняет интервал не менее 3600 секунд и запрет догоняющих
  пачек. Приватные отчёты и снимок доставки сохранены только в ignored data.
- Актуализированы паспорт компонентов, README и процедура серии.
  Ограничения: подтверждён один новый сквозной запуск после исправления,
  а не многочасовая стабильность. Выбор и редакторская проверка следующих
  статей требуют работающей среды Codex; Reddit остаётся выключен.

## 0.6.9 — 2026-10-18: надёжность подготовки и отложенной публикации статей

- Сохранены все замечания и отрицательный вердикт Ministral при однозначной
  форме `issues: [{"issue":"текст"}]`: нормализация меняет только оболочку,
  сохраняет исходный ответ в приватном trace и позволяет выполнить LanguageTool
  и передать конкретную обратную связь переводчику. Неизвестная форма блокирует.
- Qwen получает точный набор числовых токенов и различия при исправлении,
  включая исходную десятичную точку. Проверка чисел не ослаблена.
- Неизменённый отклонённый текст не получает новую рецензию в пределах
  подготовки. Для явных candidate/revise-путей хеш отказа сохраняется в ledger,
  сверяется между запусками и фиксируется в Audit; после отказа качества
  обычный retry требует явной редакции. Инфраструктурные сбои остаются повторяемыми.
- NASA helper принимает оба выделенных narrative widget: `Basic text` и
  `Basic text with HTML`. Описания медиа и поисковая выдержка по-прежнему
  не заменяют статью; ограничения URL, прав, даты и размера сохранены.
- Почасовое пробуждение Codex перенесено на пятую минуту часа для подготовки
  заранее; интервал по фактическим публикациям остаётся не менее 3600 секунд.
  Процедура уточняет допустимую будущую дату и Published-фильтр списка Postiz.
- Исходная версия сохранена в GitHub tag
  `v0.6.8-pre-chain-reliability-20261018`. Версия согласована в pyproject,
  импорте пакета и текущей документации. Секреты и runtime state в Git не входят.
- Проверки Windows: 237 targeted tests по quality, cycle, NASA fetch, bridge,
  конфигурации и профилю моделей; Ruff и git diff --check прошли. Две реальные
  отрицательные рецензии воспроизведены офлайн с сохранением отказа и достижением
  проверки орфографии. Это не подтверждение новой живой доставки.
- Ограничения: исторический raw-хеш с неизвестными исходными пробелами требует
  сверки приватного trace. Выбор нового источника зависит от работающего Codex;
  существующий worker VM исполняет уже согласованную очередь. Развёртывание
  и новая живая публикация фиксируются отдельной записью после проверки.

## 0.6.8 — 2026-10-18: отдельный предел давности научных материалов NASA

- Почасовой NASA SVS сценарий теперь использует явный `max_source_age_hours`
  (72–8760) из своей локальной политики при подготовке и публикации. Значение
  не меняет общую свежесть других источников и каналов.
- Пример политики и инструкция согласованы: для научного объяснения допустимы
  материалы до года, но исходная дата сохраняется и не обозначается свежей
  новостью. Прежний порог 72 часа дал `STALE_SOURCE` до создания записи.
- Проверки: `tests/test_article_cycle.py` и `tests/test_configuration.py` —
  87 passed; `git diff --check` прошёл. Добавлены проверки принятия статьи за
  30 дней для отдельной политики, отказа сверх её предела и цепочки prepare/publish.
- Ограничение: права и пригодность каждого URL по-прежнему проверяются отдельно;
  максимум один год не разрешает повтор исходника, перевод или сторонний материал.

## 0.6.7 — 2026-10-18: паспорт компонентов и подключений (документация)

- Добавлен docs/COMPONENTS_AND_CONNECTIONS.md: схема обработки и публикации,
  размещение PVE/VM/GPU, версии ПО, Docker-компоненты, модели, LAN/loopback/
  Docker подключения, конфигурационные пути, автозапуск и границы backup.
  Проверенные версии отделены от деклараций deployment и неизвестных полей.
- Read-only аудит 18 октября подтвердил PVE/VM, версии редакционного runtime,
  девять running-контейнеров (восемь healthy, admin-tools без healthcheck),
  backend/build моделей и общий API. Для паспорта не выполнялись inference,
  публикации, загрузки моделей, установка или перезапуск служб.
- Зафиксированы реальные различия: исходники агента 0.6.7 против installed
  metadata 0.6.0; custom Postiz image max-postiz-linux:0.5.0 против upstream
  provenance 2.25.1 и package metadata 1.0.0. Внутренний Qwen endpoint явно
  отделён от неизменного клиентского API 10.20.30.95:8083/v1.
- README обновлён: версия 0.6.7, паспорт как точка входа, текущая почасовая
  NASA/MAX цепочка, Linux как рабочая установка, Windows как отдельный стенд,
  исторические отчёты и выключенные/неподтверждённые направления разделены.
- Проверки: ссылки и обязательные поля документов, отсутствие placeholders
  и секретов в новых текстах, согласованность версии, сохранность прежнего
  CHANGELOG и git diff --check; независимая сверка с приватными live-снимками.
  Пакет и runtime не изменялись, новые тесты кода не добавлялись.
- Ограничения: паспорт — датированный текстовый снимок, не полный образ
  установки. Секреты, действующие настройки, БД, cookies, Docker images,
  модели и desktop scheduler остаются вне Git. Полная пересборка без seed
  image и восстановление всей системы этим аудитом не проверялись. Версия
  пакета остаётся 0.6.7; опубликованная release-tag не переносится.

## 0.6.7 — 2026-10-18: восстановление сети Proxmox и ограниченная мера для e1000e

- После восстановления доступа журнал предыдущей загрузки подтвердил
  e1000e Detected Hardware Unit Hang физического eno1 с 17 октября
  18:43:36 МСК. Хост продолжал работать и исполнял cron; 18 октября
  в 03:18:11 зафиксированы короткое нажатие Power и начало штатного выключения,
  следующая загрузка — 03:19:18. VM 109 и 112 запустились автоматически.
  Точный аппаратный/драйверный триггер зависания пока не установлен.
- Проверен предоставленный доступ к Keenetic, сохранены приватные журналы,
  DHCP/ARP и router ping. По четырём историческим наблюдениям проверена
  физическая MAC; выполнены один прямой WOL и один router WOL. Подтверждена
  отправка пакетов, но не причинная связь WOL с восстановлением. VPN,
  firewall и постоянная конфигурация маршрутизатора не менялись.
- Добавлены deployment/proxmox/guard-eno1-tso.sh и systemd unit. Guard
  закрепляет hostname/PVE/PCI/vendor/device/MAC/driver и меняет только TSO
  физического eno1, проверяя неизменность GSO/GRO/checksums. По умолчанию
  read-only, применение root-only, повторный off идемпотентен; нет reset,
  ifdown, изменения EEE или автоматического включения TSO при остановке.
  Unit ограничивает время ожидания NIC и запускается до pve-guests.
- На Proxmox сохранена закрытая резервная копия исходного состояния и
  заменяемых файлов. Мера применена: unit enabled/active/success, TSO off,
  защищённые features прежние, линк 1 Gbps Full. Сетевая конфигурация,
  работающие VM и службы приложений не перезапускались. Проверены по 16 МиБ
  TCP в обе стороны с одинаковой SHA-256; новых Hardware Unit Hang в окне
  проверки нет. Первичный сбор journal после применения отклонил формат
  времени; отдельная read-only сверка подтвердила результат без повторной
  мутации. Документы NETWORK_RECOVERY и E1000E_RECOVERY описывают факты,
  первичные источники, установку, ограничения и откат.
- Перед изменениями проверены отсутствие sending/unknown и mutation leases,
  сделаны pg_dump и копия ledger. Независимая живая сверка подтвердила
  две прежние статьи: Postiz PUBLISHED, dispatch verified, каждый точный
  текст виден в MAX один раз, в том числе после reload. Истёкшая третья
  prepared закрыта штатным abandon-prepared без внешних действий; ledger,
  исходник и использованный час сохранены. Все адресаты на паузе, активных
  отправок и leases нет. Новых публикаций и согласований этого прогона нет.
- Проверки: 47 targeted tests прошли, 1 Windows symlink-тест пропущен из-за
  отсутствия разрешения; Bash syntax, Ruff, diff и согласованность версии
  проверены. После применения пройдены все 27 глубоких live-checks стека,
  включая реальные ответы Qwen и Ministral. Сетевой helper завершился
  кодом 0 через обычный и явно выбранный Wi-Fi пути. Версия согласована
  в pyproject.toml и __init__.py; исходники и история сохранены на GitHub.
- Ограничения: TSO off — ограниченная мера против известных e1000e TX hangs,
  не доказательство окончательного исправления; возможен рост нагрузки CPU.
  Повторная загрузка ради проверки persistence не выполнялась, длительная
  устойчивость ещё не проверена. Приватные доказательства и backup остаются
  в ignored data/ и закрытом каталоге хоста; пропущенные часы не догоняются.

## 0.6.6 — 2026-10-18, 02:46: локализация сетевого отказа и автономная диагностика

- Повторная проверка установила: отдельный GPU-узел доступен по SSH как через
  WireGuard, так и напрямую через Wi-Fi; существующий ограниченный ключ и
  закреплённый host key прошли проверку, штатный read-only worker status
  успешен, health Ministral вернул HTTP 200 и status=ok. Недоступны физический
  Proxmox, редакционная VM и VM с общим API. Смена доступного адреса Proxmox
  однократной проверкой SSH/8006 серверной подсети не обнаружена.
- Конкуренция LAN/VPN-маршрутов подтверждена, но не является доказанной
  первопричиной: GPU отвечает через оба пути, Proxmox и его гости не отвечают
  через оба. Отказ локализован на Proxmox или его сетевом подключении;
  питание, зависание, NIC/кабель/порт остаются непроверенными гипотезами.
  Системные маршруты, VPN, firewall и службы не изменялись.
- Добавлен scripts/check-network.py: ограниченные параллельные TCP-проверки
  работают без VM и прав администратора, поддерживают отдельный проход через
  заданный интерфейс Windows и сохраняют JSON только внутри ignored data.
  Код 0 означает доступность основных TCP-портов, а не работу приложений или
  живую доставку. Скрипт не читает секреты, не выполняет SSH/публикации/WOL.
- Добавлена инструкция docs/NETWORK_RECOVERY.md с фактами, границами выводов
  и продолжением восстановления; дополнены инструкция автозапуска и сохранённый
  почасовой промпт. Версия пакета согласована в pyproject.toml и __init__.py.
- Проверки: 27 тестов диагностического helper прошли, 1 реальный symlink-тест
  пропущен из-за отсутствия соответствующего разрешения Windows. Ruff чистый.
  Живой запуск helper с обычным и прямым Wi-Fi путями завершился кодом 1,
  корректно отразив недоступность Proxmox/гостей при доступном GPU; это
  подтверждение диагностики отказа, а не восстановления. Проверены diff и
  согласованность версии. Приватные доказательства остаются только в data.
- До изменений сохранён GitHub tag v0.6.5-pre-network-recovery-20261018.
  Ограничения: административной сессии Keenetic и подтверждённого физического
  MAC Proxmox нет; удалённое включение не выполнено. Требуется доступ к
  маршрутизатору либо проверка питания/кабеля на месте. Точная причина требует
  журналов хоста. VM остаётся на последней известной версии 0.6.5; новые
  исходники и история пока не синхронизированы с ней. Публикаций/согласований
  этого прогона нет, состояние прерванной подготовки требует сверки после
  возврата связи. Пропущенные часы не догоняются.

## 0.6.5 — 2026-10-17, 18:51: сетевой сбой при подготовке третьего материала

- После копирования нового NASA-текста и получения перевода пропало
  подключение рабочего компьютера к VM, Proxmox и общему API моделей.
  Диагностика обнаружила конкурирующие маршруты LAN/VPN; подключения через
  явно выбранный Wi-Fi также завершились тайм-аутом. Шлюз локальной сети
  доступен. Для восстановления нужен доступ к сети/серверам; текущий сеанс
  Windows не имеет прав администратора для изменения системных маршрутов.
- Сохранены исходник, исправленный редакторский кандидат, последняя известная
  запись подготовки и сетевой отчёт в ignored data. Локально проверены схема,
  URL, русский язык, числовые токены и длина кандидата. Независимая модель,
  LanguageTool и живая доставка исправленного текста остаются непроверенными.
- Publish для нового материала не вызывался, новой Postiz QUEUE и
  согласования от этого прогона нет. После восстановления сначала требуется
  сверка сохранённой записи; неизвестную операцию не повторять, истёкший
  час не догонять. Последние две доставки до сбоя подтверждены в MAX/Postiz,
  каждое сообщение видно один раз, адресат был на паузе.
- Ограничения: итог подготовки на VM после обрыва соединения неизвестен.
  Подробности и содержимое текста остаются приватными; код версии 0.6.5
  не изменён. Новая история и исходный архив сохранены локально/GitHub;
  синхронизация этой записи на VM ожидает восстановления подключения.

## 0.6.5 — 2026-10-17, 18:41: подготовка заранее с сохранением интервала отправки

- Обнаружено противоречие в почасовом промпте: ранний heartbeat предлагалось
  пропускать целиком, хотя код допускает подготовку заранее и вычисляет дату
  отправки как max(now + 120 секунд, последняя публикация + 3601 секунда).
  Уточнены промпт и инструкция; интервал фактических отправок, блокировки
  неизвестной доставки и запрет догоняющих публикаций сохраняются.
- Проверки: 3 существующих теста расписания прошли за 4.31 секунды — следующий
  пост до истечения интервала, отказ истёкшего слота и полный staging window.
  Перед работой сверены предыдущая verified доставка, Postiz PUBLISHED и MAX:
  оба опубликованных текста видны по одному разу, отправок и leases нет,
  max_test на паузе. Код пакета остаётся версии 0.6.5.
- Ограничения на момент записи: подтверждены две реальные публикации;
  новый материал этого часа ещё готовится. Почасовой выбор и контроль
  зависят от доступности Codex; приватные доказательства остаются ignored.

## 0.6.5 — 2026-10-17, 16:35: первая живая доставка и почасовой контроль

- Подтверждена одна реальная публикация NASA-текста в MAX в 16:32 Moscow:
  одна Postiz QUEUE, один verified dispatch, Postiz PUBLISHED. Браузерная
  квитанция проверила появление точного текста, сохранённый снимок просмотрен.
  Адресат max_test возвращён на паузу; другие маршруты не публиковались.
- Перевод Qwen прошёл явную редактуру; независимый Ministral одобрил точный
  кандидат, LanguageTool вернул ноль замечаний. В provenance и аудит записана
  редакторская правка, а не нетронутый автоматический перевод.
- Включена heartbeat-автоматизация Codex «MAX–Postiz — статьи каждый час»:
  каждый час в 40 минут выбирает новый разрешённый текст, проверяет,
  согласует точную версию и контролирует доставку. Использованный час
  пропускается; следующий новый выбор с 17:40 Moscow. Прежний четырёхчасовой
  сценарий остаётся на паузе. Промпт сохранён в prompts/.
- Проверки: 219 targeted tests passed, Ruff и diff --check прошли;
  подтверждены фактическая доставка, орфография, Postiz и возврат паузы.
  Код 0.6.5 сохранён в GitHub commit 246bd89. Исправлен документированный
  путь browser evidence: data/browser-publications; сами отчёты остаются ignored.
- Ограничения: пока подтверждена одна живая доставка, а не многочасовая
  устойчивость серии. Выбор и контроль требуют доступности Codex и его
  исполнительного компьютера; VM самостоятельно доставляет уже согласованное.
  Reddit не одобрен и не включён. Прямую ссылку на сообщение транспорт не
  возвращает; подтверждение опирается на точный текст, снимок и квитанцию.

## 0.6.5 — 2026-10-17: проверяемая редактура до создания черновика

- В живом NASA переводе модель меняла 11:59 p.m. на семантически верное 23:59,
  что не соответствует строгому сравнению numeric tokens. Gate не ослаблен:
  feedback теперь содержит оба набора чисел и указание сохранять запись,
  переводя пояснения без конвертации времени и единиц.
- Явный prepare --retry-record --candidate принимает редакторский title/body
  только для той же неудачной подготовки без вариантов и внешних действий.
  Новый перевод не запускается, точный кандидат проверяет независимая модель
  и LanguageTool. Сохраняются источник, слот, история и hash кандидата;
  provenance честно помечает редакторскую правку. Версия согласована до 0.6.5.
- Проверки: общий предыдущий набор 212 passed, новые 6 candidate-retry tests
  и 70 quality tests прошли; Ruff и diff --check прошли. Повтор полного
  целевого набора выполняется перед живым согласованием.
- Ограничения на момент записи: отправок этой серии нет; исходный перевод
  второго материала ещё проверяется. Изменения не подменяют одобрение Reddit
  и не снимают проверку точной версии, даты, адресата и фактической доставки.

## 0.6.4 — 2026-10-17: редакторская повторная проверка и защита границ цикла

- Живой reviewer обрывал длинный JSON по лимиту ответа. Промпт теперь требует
  до трёх коротких конкретных замечаний на русском и полный JSON; исправления
  должны опираться на исходник, без добавления научных предположений.
- Добавлен check_candidate без повторного перевода и revise-prepared: точный
  редакторский кандидат проходит прежние смысловые, числовые и языковые gates,
  независимую модель и LanguageTool. Версия Draft/Variant и hash обновляются
  согласованно; отклонённая правка сохраняет текст и блокирует публикацию.
- Добавлен abandon-prepared только для локального неподтверждённого варианта
  без внешних операций и mutation leases; сохраняются исходник, история и
  дедупликация. Исправлен тупик подготовленного текста после истечения часа.
- При синхронизации перед approval сравниваются все содержательные поля,
  включая media, sources, адресата и конфигурацию. Явный due-at тоже требует
  минимум 120 секунд. Версия согласованно повышена до 0.6.4.
- Проверки: предыдущий общий набор 178 passed; новые candidate tests входят
  в 69 quality passed, ещё 21 guard/correction test прошёл отдельно; Ruff и
  diff --check прошли. Все 25 startup checks зелёные. Штатный inspect подтвердил
  текущую авторизованную сессию и правильный канал MAX без ввода и отправки.
- Живая неудачная подготовка закрыта штатно после проверки отсутствия
  Draft/Variant, dispatch и Postiz. Публикаций этой серии пока нет; общий
  повторный набор и новый исходник проверяются. Reddit остаётся выключен.

## 0.6.3 — 2026-10-17: диагностика и исправление живого отказа качества

- Приватный журнал сохраняет фазы, ответы моделей и замечания LanguageTool;
  повторная редактура получает точный предыдущий кандидат и замечания.
  Усилена проверка русского заголовка, управления глаголов и смысловых связей.
- Реальный словарь ошибочно отклонил корректное «Магелланово». Добавлено
  узкое исключение с научным свидетельством для полного названия галактики,
  соответствующего английского исходника, точного правила и UTF-16 позиции.
  Сырые замечания и их разбор сохраняются; остальные ошибки блокируют текст.
- Добавлена безопасная отмена только подготовки без Draft/Variant и внешних
  операций; исходник, час и история сохраняются. Истечение часа до завершения
  подготовки откатывает создаваемые сущности и не оставляет зависший вариант.
- Добавлен воспроизводимый helper native-профиля OpenCode 2.0.17 с приватной
  резервной копией, ограничением длины ответа и отключением thinking Qwen.
  Адрес API и имена моделей сохранены. Версия согласованно повышена до 0.6.3.
- Проверки: 55 quality tests, 16 model-profile tests, 19 trace/cancel/retry
  tests и 3 expiry tests прошли отдельно; Ruff и diff --check прошли.
- Ограничения на момент записи: реальная генерация завершалась отказом
  качества до внешних операций. Первая доставка после исправления и почасовая
  автоматизация ещё проверяются; runtime evidence остаётся ignored.

## 0.6.2 — 2026-10-17: восстановление подготовки после живого тайм-аута OpenCode

- Первый живой prepare остановился с OPENCODE_GENERATION_FAILED до создания
  Draft/Variant и любых публикаций. Исходная запись и ошибка сохранены.
  Обе модели ответили через общий API; короткий запрос через тот же OpenCode
  прошёл за 28.7 секунды. Лимит ожидания развёрнутой задачи был 180 секунд.
- Добавлен явный prepare --retry-record для той же записи и текущего часа:
  сверяются исходник, политика, отсутствие вариантов и внешних операций;
  прежний slot и история ошибок сохраняются. Повтор неизвестной отправки
  эта команда не разрешает. Версия согласованно повышена до 0.6.2.
- Проверки: 11 новых retry tests, Ruff и diff --check прошли; ранее дополнительно
  прошли 23 browser-authorization/boundary tests (всего 118 до retry-изменения).
  На VM в ручном NASA source отсутствуют фоновые collect jobs. При установке
  исправлена конфигурация HTML source: extraction_profile должен быть строкой,
  даже при ручном режиме; схема после исправления прошла.
- Ограничения на момент записи: живой перевод ещё восстанавливается;
  отправок этого теста нет. Реальная доставка и почасовой контроль ещё
  требуют проверки и будут записаны отдельно. Приватные данные не включены
  в исходники; состояние до исправления сохранено в GitHub commit b7f8796.

## 0.6.1 — 2026-10-17: отдельный почасовой тест переведённых статей

- Перед изменениями исходный commit 29f01ac сохранён на GitHub tag
  v0.6.0-pre-hourly-tests-20261017. На VM сохранены закрытые копии исходников,
  конфигурации и PostgreSQL dump; до работ активных dispatches и mutations нет.
- Зафиксировано новое поручение владельца: выбирать, проверять и публиковать
  одну новую статью в час через max_test. Оно не включает Reddit или Пикабу.
  Версия согласованно повышена в pyproject.toml и __init__.py.
- Добавлены generic ArticleQuality (Qwen → независимый Ministral → LanguageTool),
  отдельные промпты и helper копирования полного narrative-текста по проверенной
  NASA SVS-ссылке. Поисковый excerpt и подписи медиа не подменяют статью.
  Исправлено реальное отличие API: metadata URL использует http, при этом
  запрос, сохранённая ссылка и разрешённый источник остаются HTTPS.
- Добавлен защищённый журнал почасового цикла, проверка прав, дат, источников,
  hash исходника/перевода, отдельные prepare/publish/status и согласование
  точного reviewed-hash по поручению владельца. Отправляет прежний worker;
  ошибочные и неизвестные результаты не запускают повторную публикацию.
- Postiz умеет один раз создать связанную QUEUE запись с датой; прежний
  stage_remote без даты сохраняет DRAFT. Подтверждение требует verified
  dispatch и Postiz PUBLISHED; завершённый тест возвращает адресат на паузу.
  Добавлен ручной режим источника без фоновых collect jobs, пример политики,
  инструкция и промпт почасового контроля.
- Проверки: 95 targeted tests прошли (52 cycle/hub/bridge, 36 quality/manual,
  7 NASA fetch), Ruff и git diff --check. Все службы, контейнеры, HTTP/TCP
  и каталог моделей доступны. Реальная NASA-ссылка прочитана: 820 символов
  полного исходного narrative-текста, без скачивания медиа и публикации.
- Ограничения на момент записи: это проверки кода и инфраструктуры; первый
  живой перевод/почасовая доставка ещё проверяются. Reddit не одобрен,
  почасовая автоматизация Codex пока не включена. Живые результаты фиксируются
  следующей записью; исходные секреты и runtime evidence остаются ignored.

## 0.6.0 — 2026-10-17: уточнение заявки Reddit API и актуальный порядок доступа

- До изменений исходный commit 3fc3e93 сохранён на GitHub в tag
  v0.6.0-pre-reddit-access-20261017.
- Проверены действующая Responsible Builder Policy, поля Data Access Request
  и прежний общий ответ поддержки. Форма открылась в рабочем браузере;
  сетевой доступ к ней непосредственно с Linux VM повторно не проверялся.
- Через авторизованный Gmail в прежний тикет отправлено подробное продолжение
  с запросом решения по личному некоммерческому сценарию вне Devvit:
  read-only API, локальный inference, перевод и внешнее распространение.
  Запрошены порядок регистрации и требования к объёму, хранению и удалению;
  новая заявка-дубликат не подавалась. Приватные детали и снимок — только
  в игнорируемых data; исходный репозиторий не открывался публично.
- Актуализированы документ доступа, Linux-инструкция и промпт переноса;
  добавлен промпт продолжения получения доступа. Описаны поля формы,
  callback текущего OAuth-helper и отдельные этапы проверки после одобрения.
- Проверки: Gmail показал «Сообщение отправлено», исходящее письмо видно
  в той же переписке. Локальный OAuth status подтвердил отсутствие client ID,
  client secret, refresh token и user agent, false для обоих разрешений
  и automation_enabled; сетевых API-запросов этот status не выполняет.
  git diff --check и check-ignore приватных доказательств прошли.
- Ограничения: отправка не доказывает приём уточнения командой рассмотрения
  или одобрение. OAuth и API-сбор не включены, внешних публикаций не было.
  Scheduler, полная дедупликация и обработка удаления внешних копий ещё нужны.
  Изменена документация; код и версия пакета не менялись, тесты кода не запускались.

## 0.6.0 — 2026-10-15: автозапуск VM и проверка редакционного стека

- До изменений текущий исходный commit 88ae7d9 сохранён в GitHub tag
  v0.6.0-pre-autostart-20261015; исходные конфигурации VM 109 и 112 сохранены
  на Proxmox в закрытых root-файлах.
- Proxmox теперь запускает VM 109 с LLM первой (order 10, up 90 секунд),
  VM 112 с редакцией второй (order 20, up 30 секунд). VM 110 сохранена
  выключенным резервом; LXC 111 с собственным автозапуском не менялся.
  Порядок воспроизводится отдельным скриптом и при новом создании VM 112.
- В VM 112 включён автозапуск защищённых display/VNC/noVNC; ручная служба
  входа владельца в MAX осталась отдельной. Docker, nginx, Hub, OpenCode,
  LanguageTool и restart policy основных Postiz-контейнеров проверены.
- Добавлены скрипт проверки с Windows и read-only Linux проверка systemd,
  контейнеров, HTTP/TCP и каталога моделей; режим -Deep выполняет один
  реальный короткий ответ Qwen и Ministral. Прокси LAN исключён.
- Проверки: `bash -n` Proxmox-скрипта и его исполнение прошли; оба порядка
  подтверждены `qm config`. Базовый healthcheck прошёл все проверки;
  первоначальный deep check выявил, что 16 токенов Qwen тратит на скрытое
  рассуждение. Тестовый запрос исправлен на `enable_thinking=false`, после
  чего обе модели дали завершённые ответы. VM 112 перезагружена при нуле
  активных dispatches/mutation leases и всех paused targets; Docker, Hub,
  Postiz и дисплейные службы поднялись сами. После перехода Temporal в healthy
  итоговый deep check прошёл с кодом 0. После перезапуска в БД по-прежнему
  один исторический published dispatch, нет активных leases, все targets paused.
- Ограничения: реальная загрузка Proxmox и физического GPU-узла после
  отключения питания не проверялась. Отдельный GPU-компьютер запускается
  независимо; действительность браузерной сессии MAX и доставка поста не
  подтверждаются инфраструктурным healthcheck.

## 0.6.0 — 2026-10-15: инструкция по ручной публикации на Linux VM

- Добавлена пошаговая инструкция для собственного текста и ручного перевода
  разрешённого материала Reddit: ссылки на Hub, Postiz, браузер и канал MAX,
  вход, создание и синхронизация черновика, календарь, согласование, проверка
  доставки и действия при unknown.
- Уточнено фактическое поведение кнопки «Открыть черновик в Postiz»: она
  создаёт запись, а вкладку нужно открыть отдельно. Отмечены статус QUEUE,
  роль владельца и отдельная проверка прав для чужого текста.
- Проверки: кнопки и переходы сверены с Hub UI/API и документацией 0.6.0;
  прямые HTTP-запросы к Hub и Postiz вернули 200. Прежний 503 воспроизводился
  только через прокси рабочего компьютера. Файл доступа проверен на наличие
  паролей без вывода значений; секреты остаются в ignored data.
- Ограничения: инструкция описывает ручной путь, повторную живую публикацию
  не выполняли; одобрения Reddit и полного автоматического цикла пока нет.

## 0.6.0 — 2026-10-14: повторное обращение о разрешённом доступе Reddit

- До изменения истории проверен сохранённый на GitHub main и release tag
  v0.6.0-reddit-intake на commit 535aaa2; исходная версия доступна для возврата.
- Официальная форма недоступна в браузере из-за ERR_NAME_NOT_RESOLVED.
  Через уже авторизованный Gmail отправлен один запрос на указанный в Help
  Center контакт API: с описанием личного некоммерческого сценария, объёма
  10 публикаций каждые четыре часа, локального перевода и внешнего распространения.
  Отдельно запрошены разрешённый путь подачи и требования к правам авторов.
- Проверки: поиск предыдущих обращений за 30 дней не дал результатов;
  Gmail показал подтверждение отправки, письмо открыто в «Отправленных».
  Точный текст и снимок сохранены только в ignored data; git check-ignore пройден.
- Ограничения: это отправленное обращение, не подтверждение получения Reddit,
  не зарегистрированный API ticket и не одобрение. OAuth и сбор Reddit не включены,
  автоматическое расписание не запущено; обработка удаления внешних копий
  остаётся нерешённой. Код и runtime-конфигурация не менялись, тесты не запускались.

## 0.6.0 — 2026-10-14: установка двух входов и итоговая проверка

- Версия 0.6.0 установлена на Linux VM, схема PostgreSQL обновлена до 0006.
  Перед установкой сохранены локальные private backups public-схемы БД
  и прежних исходников. Runtime TOPIC, credentials, browser profile и volumes
  сохранены; Postiz image 0.5.0 не менялся, обновлён Python Hub.
- Проверены отсутствие sending/unknown и mutation leases перед установкой.
  После обновления рабочие счётчики прежние: 21 story/draft, два variants,
  один исторический dispatch, ноль активных dispatches; все адресаты на паузе.
- Реальные Qwen → Ministral → LanguageTool подготовили собственный английский
  технический пример в изолированной PostgreSQL-схеме: ready, проверен дубль,
  reddit_requests=0, postiz_drafts=0, external_posts=0, production_rows_changed=0.
  Схема после проверки удалена; это не материал, полученный от Reddit.
- Проверены реальный owner UI и reload, отдельные поля ручного ввода и Gmail,
  пустое поле пароля, ноль page errors. Исправлено доступное имя MAX selector.
  Первые QA-запуски выявили стартовый 502 и точное имя selector; readiness
  и aria-label исправлены, итоговый QA прошёл. Postiz LOCAL login/calendar/reload
  также прошли без page errors и старых localhost запросов.
- Полный Linux run: 324 passed; после финального изменения API и дополнительной
  проверки неизвестного результата Postiz отдельно прошли 20 intake tests.
  Это не повторный полный run на 325 тестах. Ruff, pip check, diff check прошли;
  wheel и sdist собраны. Gmail TLS соединение проверено без авторизации.
- IFTTT/Reddit account connection и Gmail login пока требуют действий владельца.
  Окно IFTTT сообщает о недоступности reCAPTCHA. Gmail credential не задан,
  забор почты выключен; новых публикаций нет. Два входа и подготовка черновиков
  реализованы; живой автоматический цикл из IFTTT и партии 10/4 часа не заявляются.

## 0.6.0 — 2026-10-14: общие Linux проверки перед установкой

- На Linux завершился полный набор: 324 passed за 156.90 s, включая реальные
  PostgreSQL migrations/locking/restore в случайных изолированных схемах.
  Отдельно прошли 37 Reddit/PostgreSQL tests, Ruff и pip check.
- Windows targeted run: 69 passed. Общий Windows run остановлен: локальная
  тестовая PostgreSQL 127.0.0.1:55438 недоступна. Вместо выдачи этой проверки
  за успешную весь набор выполнен на сервере в изолированных схемах.
- Предварительная синтетическая quality probe использовала старый путь
  migrations и завершилась до вызова моделей; helper исправлен на путь
  проверяемого дерева исходников. Рабочая схема не менялась.
- Открыта регистрация IFTTT: страница сообщает о недоступности reCAPTCHA.
  Аккаунт, OAuth Reddit и Gmail остаются неподключёнными; обход не выполнялся.

## 0.6.0 — 2026-10-14: общая очередь ручного отбора и IFTTT

- До изменений сохранён GitHub tag v0.5.0-pre-reddit-intake-20261014
  на исходном commit a7e0cde. Прежние записи истории сохранены.
- Добавлены форма ручного ввода Reddit в Hub и отдельный вход IFTTT через
  read-only Gmail IMAP. Оба используют новую таблицу PostgreSQL (migration 0006)
  с уникальностью Reddit ID, нормализованного исходника и готового перевода.
- Ссылка без текста сохраняется закладкой без сетевого чтения Reddit.
  Права на точный исходник подтверждает owner; изменение текста отзывает
  подтверждение. Почта не может передать права согласования или публикации.
- Очередь использует сохранённый захват и явное восстановление после обрыва;
  Qwen → независимый Ministral → LanguageTool создают отдельный MAX-вариант.
  Нет автоматического создания Пикабу-варианта для этой очереди.
- Добавлены настройки Gmail и секретный шаблон IFTTT. Читается только ярлык
  Reddit-IFTTT, до 25 писем по 128 KiB, сохраняются UID/UIDVALIDITY.
  Письма не изменяются; ошибки доступа останавливают автоматические попытки.
  Credential хранится только в ignored data, ответы API его не раскрывают.
- Добавлены операторская инструкция и сохранённый промпт. Рабочие секреты,
  cookies и почтовые настройки исключены из исходников и source archive.
- Проверки: 69 targeted tests прошли на изолированном SQLite, Ruff и pip check
  прошли. PostgreSQL, серверный интерфейс и живые модели проверяются отдельно.
- Ограничения: аккаунт IFTTT и Reddit connector ещё не подключены, Gmail
  app password отсутствует. Доставка IFTTT/Gmail и новые публикации MAX
  не подтверждены. Автоматические партии по десять каждые четыре часа
  и автоматическое согласование не включены; смысловые дубли/удаление копий
  требуют следующего этапа. Публичный API/RSS Reddit меняют доступность;
  долгосрочная работа стороннего коннектора не гарантируется.

## 0.5.0 — 2026-10-14: подтверждение перевода и перезапуска OpenCode

- Реальный restart OpenCode сменил credential; уже созданный client
  перечитал его и успешно выполнил inference без пересоздания объекта.
  Значения паролей в отчёт не выводились.
- На собственной синтетической английской истории прошла вся цепочка
  Qwen → Ministral → локальный LanguageTool, результат 352 символа.
  Это реальный вызов моделей через OpenCode, но не живой Reddit:
  reddit_requests=0, external_posts=0. Ранее записанная неудача относится
  к предыдущему запуску до исправлений.
- Общий набор завершился: 302 passed; после последних исправлений
  отдельно прошли 17 credential/Reddit tests и Ruff. pip check прошёл.
- Ручной вход Linux MAX использует существующий проверенный login transport,
  ограничен четырьмя часами и запускается с NoNewPrivileges=yes.
  Автозапуск GUI при boot не включён. Nginx ожидает network-online;
  provision script согласован с фиксированным минимумом RAM 10 GiB.
- Прежние записи истории сохранены. Reddit application/refresh token,
  новое имя публичного канала и production автопубликация не подтверждены;
  запросы на ручной вход и отправку официальной заявки переданы владельцу.

## 0.5.0 — 2026-10-14: работающий Linux экземпляр и проверки переноса

- Успешно собран max-postiz-linux:0.5.0, image SHA
  7940324a4e3d4f7193826b90679dfba192689b335fb9d8c53071277d01fb82e9.
  Новый frontend использует LAN origin. Проверены LOCAL login, календарь,
  reload, отсутствие page errors и запросов к прежнему localhost:4007.
  Повторная UI-проверка под systemd NoNewPrivileges=yes прошла.
- Восстановлены обе БД. В Hub 21 story/draft, два variants и один старый
  dispatch; в Postiz одна PUBLISHED-запись и DRAFT. Проверены нулевые active
  dispatches/mutation leases и пауза всех адресатов. Девять контейнеров Linux
  запущены, включая отдельную PostgreSQL Hub; служебная история Temporal новая.
- MAX provider вызван на существующем DRAFT: bridge/status/pending прошли,
  target paused, approval=false, external_posts=0. Живой доставки из Linux нет.
- Установлен штатный Google Chrome с sandbox и штатным AppArmor-профилем;
  глобальное разрешение user namespaces не ослаблялось. Подготовлен ручной
  owner login через noVNC: loopback backend, VPN/LAN ACL, owner session и
  Origin gate. Проверены anonymous redirect, viewer 403, owner 200,
  foreign Origin 403. Браузер требует ручной авторизации владельца.
- У публичного и внутреннего Postiz разные origins: добавлено явное
  POSTIZ_PUBLIC_ORIGIN. Разрешённый upload URL переписывается на внутренний
  endpoint; посторонние и scheme-relative origins не принимаются.
  Reconnect helper больше не привязан к localhost и его cookie domain.
- Старый Windows worker остановлен. Compose остановлен без удаления volumes;
  после обнаруженного повторного запуска restart policy восьми прежних
  контейнеров изменена на unless-stopped, затем они остановлены повторно.
- Доизменённый GitHub snapshot дополнен двумя исходными корневыми metadata
  файлами без переписывания первого тега: branch pre-linux-vm-backup,
  tag v0.4.1-complete-pre-linux-vm-20261014, commit 54c9c39, 205 файлов.
- Полный набор: 302 passed за 257.82 s, Ruff и pip check прошли.
  Позднейшие проверки смены credentials/Reddit: 17 passed.
- OpenCode client перечитывает managed credentials перед запросами,
  сохраняя приоритет явного env secret; wrapper заменяет access.json атомарно.
  Для JSON-ответов Reddit введены ограниченные повторные попытки без обхода
  quality gate. Промпт review не требует придумывать новые события ради концовки.
- Ограничения: заявка Reddit не отправлена (официальная форма с VM: 403),
  OAuth-приложения/токенов нет. Синтетический тест сложной цепочки перевода
  пока не прошёл целиком; это не выдаётся за живой Reddit или готовую редакцию.
  Batch scheduler/дедупликация Reddit, автоматические согласования и удаление
  источников/постов ещё не реализованы. Автоматическая публикация выключена.
  Операционная инструкция: docs/LINUX_DEPLOYMENT.md.

## 0.5.0 — 2026-10-14: проверки Linux и исправление сборки frontend

- Обе модели дали реальный ответ через OpenCode 2.0.17 внутри Linux VM:
  proxmox/qwen35-9b и proxmox/qwen25-coder-7b (Ministral).
- 23 отдельных Reddit/portability tests и Ruff прошли после исправления
  регистрации SQLite metadata. Добавлен read-only OAuth authorization helper,
  токены сохраняются в игнорируемом .env.local, state проверяется в callback.
- Сборка frontend webpack прошла компиляцию и TypeScript, затем обнаружила
  потерю default-stylesheet.css при bundling jsdom. В Next config явно
  externalized isomorphic-dompurify/jsdom; повторная сборка ещё проверяется.
- Фактический маршрут компьютера владельца — существующий WireGuard,
  source 10.77.0.2. Этот адрес разрешён firewall VM; дополнительная учётная
  запись Tailscale не нужна, установленный daemon выключен, его правила убраны.
- Попытка live migration системного диска завершилась QMP timeout:
  root остаётся на local-zfs, контейнеры — на отдельном 160 GiB диске box.
  Sync root zvol восстановлен в standard перед восстановлением БД.
- sdist теперь включает Linux shell scripts/units, текстовые промпты и
  корневые Git/Docker metadata. Заявка Reddit и внешняя доставка не выполнены.

## 0.5.0 — 2026-10-14: Linux VM и подготовка Reddit OAuth

- На Proxmox создана отдельная Ubuntu 24.04 VM 112: 4 vCPU, 10 GiB RAM,
  системный диск 120 GiB на local-zfs, адрес DHCP 10.20.30.45.
  Существующая VM 109 и общий API LLM не перенастраиваются.
- После проверки задержек SSD/ZFS к новой VM добавлен отдельный диск
  160 GiB на box для контейнеров. Bootstrap, systemd units, firewall,
  внутренний bridge и сборка frontend Postiz под LAN origin сохранены
  в deployment/linux. Во время начальной установки sync отключался только
  для нового системного zvol; таймер восстановления вернул sync=standard.
- Подготовлены приватные дампы Hub/Postiz после проверки: все targets paused,
  нет активных dispatches, sending/unknown и mutation leases; в Postiz одна
  опубликованная запись и один черновик. Cookies Windows не переносились.
- Добавлен Reddit OAuth read client без анонимного fallback и повторов 429/403,
  текстовый shortlist, перевод Qwen, независимый review Ministral и обязательная
  локальная проверка LanguageTool. Промпты сохранены отдельно в prompts.
- Проверки: 13 новых локальных тестов и Ruff прошли; доступ к /v1/models
  с новой VM подтверждён. Это ещё не проверка генерации через OpenCode,
  Reddit OAuth, перенесённого Postiz или доставки в MAX.
- Ограничения на момент записи: сборка/перенос продолжаются, Reddit не одобрен,
  MAX требует нового входа владельца; автоматический цикл не включён.
  Официальная форма Reddit при запросе с VM вернула 403; обход не выполняется.
  Архив исходников теперь сохраняет также .dockerignore и .gitattributes.

## 0.5.0 — 2026-10-14: изолированная проверка переносимости

- Отдельный запуск portability tests выявил зависимость SQLite schema от
  порядка импортов: hub_targets отсутствовала при раннем создании базового
  Service. Регистрация hub metadata теперь выполняется до создания схемы
  для обоих backend, как уже делалось для PostgreSQL.
- Для Linux units/YAML/CJS/промптов закреплены LF; открытый bind внутреннего
  Hub документирован рядом с настройкой и ограничивается firewall VM.
- Проверки повторяются отдельно; живые внешние публикации не запускались.

## Не выпущено — 2026-10-14: начало переноса в Linux VM

- До изменений создан приватный GitHub repository max-postiz-editorial:
  исходный снимок 0.4.1, commit 15ef006, tag
  v0.4.1-pre-linux-vm-20261014. Все 203 файла сверены с исходным архивом,
  индекс проверен по allowlist и просканирован на runtime-секреты.
  Push main и тега выполнен, приватность и удалённый SHA подтверждены.
- Владелец выбрал Linux VM с существующей LLM и разрешил оформление
  доступа Reddit. Проверка серверного доступа и подготовка реализации начаты;
  создание VM, OAuth-доступ и автопубликация пока не объявляются выполненными.

## 0.4.1 — 2026-10-14: план серверной редакции и профиля Reddit

- По уточнению владельца зафиксированы десять отдельных публикаций каждые
  четыре часа, до 60 в сутки: английские истории, наука и юмор, без политики/NSFW.
  Для этого профиля запрошено автоматическое согласование по правилам.
- Подготовлены варианты Linux/Windows VM, LAN/VPN/HTTPS и перенос URL Postiz,
  новая авторизация браузера, политика качества, дедупликация и перезапуск.
  Сохранены план, промпт, выключенный JSON-пример и неотправленный черновик
  заявки Reddit. Начальный формат — текст; медиа/OCR вынесены отдельно.
- Проверены текущие инструкции/код и официальные требования Reddit,
  Tailscale и Caddy. Владелец уточнил: OAuth-приложение ещё не создано.
  OAuth-доступ и права на перевод/внешнюю публикацию остаются условиями запуска;
  текущий MAX adapter не умеет автоматически отзывать пост при удалении источника.
- Версия исполняемого кода не менялась. VM, публичное имя канала и рабочая
  конфигурация не менялись; новый сбор и авторежим не включены. Полученные
  данные входа не внесены в документы или публикуемую конфигурацию.
- JSON-пример проверен: выключен, 6 слотов × 10 = 60 постов в сутки,
  OAuth/права/удаление отмечены неподготовленными. Обновлены переносимые
  артефакты с планом и промптом; runtime-регрессия для документации не запускалась.

## 0.4.1 — 2026-10-13: тестовая публикация подтверждена

- По явному согласованию владельца тестовый текст без вложений поставлен
  в Postiz на 15:21 Europe/Moscow; агент согласовал ревизию 3 и отправил
  её через прежний браузер MAX. Одна попытка, один новый пост.
- Подтверждены точный текст после reload и повторного открытия браузерной
  сессии; Postiz получил PUBLISHED, Temporal workflow завершён COMPLETED.
  Все адресаты после проверки снова paused. Новая живая доставка подтверждена.
- Исправление browser authorization проверено 64 тестами: 48 browser/bridge
  (77.41 s) и 16 hub/PostgreSQL (18.55 s); Ruff прошёл. Артефакты 0.4.1
  обновлены, доказательства и журналы только в ignored data.
- Обновлены промпт адаптера, README, инструкция и отчёт живой публикации;
  результат сверки native ID явно отделён от локального ID операции.
- Native ID и permalink MAX не извлекаются: browser-... — ID операции.
  Чистая повторная сборка overlay, медиа/видео, правка и удаление опубликованного
  сообщения остаются отдельными проверками. Исходный проект MAX не перезаписывался.

## 0.4.1 — 2026-10-13: проверка согласования перед браузерной отправкой

- После явного согласования тестового текста владельцем обнаружена
  несовместимость: browser worker проверял legacy Intent вместо hub Dispatch.
  Добавлена проверка новой очереди без включения legacy-публикаций.
- Перед открытием браузера и непосредственно перед кликом проверяются
  sending, действующая lease, дата, TargetApproval, актуальная версия,
  адресат, пауза, полный snapshot и вложение; повторяется guard Postiz.
- 48 тестов browser/MAX bridge прошли (77.41 s), включая десять новых
  проверок авторизации настоящего worker и прежние browser-контракты.
  Дополнительно 16 hub/PostgreSQL тестов прошли (18.55 s), Ruff прошёл.
  Тестовая живая отправка продолжается.
- Версия пакета и __version__ согласованы. Текст владельца не изменён;
  реальная доставка пока не объявляется успешной.
- Уточнена документация результата браузера: external_id browser-...
  идентифицирует локальную операцию, native ID/ссылка сообщения MAX
  не извлекаются. Не выдавать его за настоящий идентификатор MAX.

## 0.4.0 — 2026-10-13: подготовка тестовой публикации

- По запросу владельца создан отдельный текстовый draft в Postiz для
  прежнего max_test, импортирован в агент через реальный MAX bridge.
  Проверены пауза адресата, отсутствие активных dispatch и mutation lease.
- Точный текст предложен владельцу для согласования по AGENTS.md.
  Состояние awaiting_text_approval хранится в ignored data; согласование,
  постановка в QUEUE и внешняя отправка до ответа не выполнялись.
- Версия кода не менялась; новая живая доставка пока не подтверждена.

## 0.4.0 — 2026-10-13: запуск компонентов

- По указанию владельца запущены локальный PostgreSQL на 55438,
  Docker Desktop и стек Postiz, OpenCode 2.0.17 на 4108, панель и worker
  редакции MAX на 8090. OpenCode и агент работают фоновыми процессами;
  отдельный автозапуск служб Windows не устанавливался.
- Проверены реальный ответ Qwen через OpenCode и общий API 8083,
  HTTP панели и Postiz, локальный вход владельца, восемь контейнеров
  (семь healthy), действующая интеграция MAX и живой pending-мост из контейнера.
- Browser inspect подтвердил доступ к публикации MAX; сообщение не отправлялось.
  Все адресаты сохранили паузу, активных dispatch нет. Состояние и PID
  сохранены в ignored data; runtime-секреты в историю не включены.
- Версия кода не менялась. Проверки запуска не заменяют приёмку реальной
  доставки; прежнее ограничение чистой сборки адаптера сохраняется.

## 0.4.0 — 2026-10-13

- Добавлен собственный провайдер MAX — браузер для Postiz 2.25.1: подключение
  через штатный API, редактор, календарь, pending/status и блокировка изменений.
  Отправляет только наш агент через прежний браузер; токен MAX Bot API не нужен.
- Тестовый канал подключён внутри Postiz. Реальная Qwen3.5-9B подготовила
  черновик; проверены редактор, сохранение правки и импорт ревизии 2.
  Проверен вызов скомпилированного провайдера к живому агенту: pending,
  review_pending, paused; согласований и новых внешних публикаций 0.
- Миграция 0005 добавляет зеркало постов. Согласование связано с полным
  снимком, адресатом, точной датой и хешем медиа. Правки снимают согласование;
  sending/unknown блокируют изменение и слепую повторную отправку.
  Авторский текст без источников отдельно подтверждает владелец.
- Проверки: 276 passed (531.27 s), повторные 22 ключевых теста passed
  (62.39 s), Ruff, скомпилированный контракт провайдера и реальный HTTP-мост.
  Восемь контейнеров запущены, семь healthy; activity worker очереди MAX виден.
- Образ собран и развёрнут; после первой полной сборки исправлены порядок
  импортов и регистрация очереди по закреплённым SHA-256 compiled-модулей.
  Чистая повторная сборка актуального Dockerfile не подтверждена из-за
  ресурсов Docker/WSL; ограничения описаны в отчёте адаптера.
- Во время диагностики отозван и заменён локальный пароль PostgreSQL;
  приватные настройки обновлены, панель перезапущена. Значения не сохраняются
  в документах и исходном архиве.
- Обновлены промпты, инструкция, история и переносимые артефакты 0.4.0.
  Изменения ограничены текущим проектом. Живая доставка остаётся отдельной приёмкой
  после согласования конкретного текста; адресаты оставлены на паузе.
- ZIP relocation smoke прошёл: 197 файлов, targets disabled, без runtime
  секретов; сохранён final Docker image для восстановления. Source patch
  применён к закреплённой базе без сети. Аудит source MAX относительно
  импортированного снимка: 80/85 совпадают, пять файлов источника отличаются;
  они не перезаписывались адаптером. Секреты сканируются в ZIP, wheel и sdist.

## 0.4.0 — 2026-10-12 (в разработке)

- Реализуется собственный провайдер MAX — браузер для установленного Postiz
  2.25.1 и локальный мост к редакционному агенту. Bot API не используется.
- Добавлены зеркало Postiz, миграция 0005, контроль версии/даты/вложений,
  отдельное подтверждение авторского текста владельцем и блокировка правок
  на время sending/unknown. Делегированный ключ не даёт права согласования.
- Первые 15 тестов адаптера прошли; сборка и живая проверка продолжаются.
  Новые внешние публикации не отправлялись.

## Не выпущено — 2026-10-12: подключение MAX внутри Postiz

- Исправлено ошибочное сравнение Vezdepost с Postiz: запрос OWNER:main
  относился к другому репозиторию. Число 652 не характеризует отставание
  Vezdepost; этот вывод удалён из плана и документации.

- Проверен тестовый адресат -79860712657053 и список Add Channel:
  установленный Postiz 2.25.1 не содержит провайдера MAX.
- Владелец подтвердил, что бот не создан; токена и нативной интеграции нет.
  Подключение не выполнено, фиктивная запись в БД не создавалась.
- Проверен MAX provider Vezdepost на commit ff4dbe31bfd5209935d32303de2be1def56ba9ad;
  зафиксированы provenance, требования прав и план адаптации к установленной версии.
- Сохранена инструкция MAX_POSTIZ_CONNECTION.md с официальными шагами
  создания и модерации бота. Работающая инфраструктура и маршруты не менялись;
  внешних публикаций нет. Версия исполняемого пакета остаётся 0.3.2.

## 0.3.2 — 2026-10-12

- По уточнению владельца подключён общий API http://10.20.30.95:8083/v1
  вместо отдельного 8080. Основная модель — proxmox/qwen35-9b;
  Ministral — явный выбор proxmox/qwen25-coder-7b.
- Проверены GET models/health, прямые ответы обеих моделей и реальные
  ответы через OpenCode; Qwen подготовил структурированный русский черновик.
- Исправлен HTTP 503 от унаследованного прокси: скрипт запуска добавляет
  настроенные приватные IP в NO_PROXY дочернего OpenCode, сохраняя прокси
  для остальных адресов. Добавлен регрессионный тест.
- Обновлены активная конфигурация, переносимый пример, промпты, правила
  проекта и отчёт LLM_CONNECTION_REPORT.md. Версия пакета — 0.3.2.
- Проверки: 26 тестов прошли (29.09 s), Ruff src/scripts/tests прошёл;
  панель показывает основную Qwen и подключённый Postiz. Wheel/sdist 0.3.2
  и переносимый ZIP обновлены; секреты исключены.
- В пробном черновике есть ошибка source_ids: подтверждена генерация,
  редакционная готовность к публикации не подтверждена. Адресаты на паузе,
  внешних постов нет, исходный MAX не менялся.

## 0.3.1 — 2026-10-12

- Установлен Docker Desktop 4.94.0 для пользователя: Engine 29.8.2, Compose 5.5.1.
  SHA-256 установщика и подпись Docker проверены. WSL 2.6.1 уже был установлен.
- Развёрнут Postiz 2.25.1 на http://localhost:4007: восемь контейнеров работают,
  семь сервисов имеют успешный healthcheck; admin-tools работает без healthcheck.
  Все образы закреплены по digest в `deployment/postiz/images.lock.yaml`.
- Создан локальный владелец, Public API подключён к редакции. Учётная запись
  и ключ сохранились после перезапуска Postiz и его PostgreSQL; интеграций пока 0.
- Исправлены повторная привязка `127.0.0.1`, DNS-зависимый healthcheck Temporal
  и конфликт порта 8080: Temporal UI доступен на http://localhost:8088.
  Сеть Temporal теперь принадлежит Compose-проекту `max-postiz`.
- Добавлены скрипты управления, bootstrap владельца, фиксации образов и
  браузерной QA. Закрыт вывод cookies в ошибках QA, диагностические сессии
  аннулированы сменой JWT signing key. Секреты остаются в ignored-файлах.
- Сохранён промпт развёртывания; добавлены правила постоянного ведения истории
  в `AGENTS.md` и включение этих файлов в переносимый исходный архив.
- Согласованы версия пакета и `__version__`.
- Проверки: Docker Compose config, 25 контрактных тестов (43.39 s), Ruff,
  pip check, реальные GET integrations/posts, вход и календарь Postiz после
  reload, 0 ошибок JavaScript. Панель редакции перезапущена и видит Postiz.
- Собраны wheel/sdist 0.3.1 и исходный ZIP из 170 файлов. Промпты, правила и
  история включены; приватные env/runtime исключены. Проверка актуальных
  секретов в ZIP и relocation smoke на новом пути прошли. 85 исходных файлов
  MAX сохранили прежние SHA-256.
- MAX/Пикабу сохраняют прежние маршруты и паузу; новых внешних постов нет.
  Нативные социальные интеграции Postiz и approval guard не настроены;
  недоступность LLM остаётся отдельным ограничением.

## 0.3.0 — 2026-10-11

Первичная запись восстановлена по документации и отчёту проверки проекта.

- Реализована редакционная связка: сбор RSS/HTML/API, PostgreSQL-архив,
  дедупликация, локальная LLM через OpenCode, версии, согласование и календарь.
- Подключены прежние адресаты MAX и Пикабу; публикации остаются на паузе.
- Добавлены Postiz API-адаптер и подготовленный Compose, импорт истории MAX,
  зашифрованная резервная копия и переносимый исходный архив.
- Зафиксированы 254 регрессионных теста и 51 дополнительная проверка
  (наборы пересекаются). Живые LLM и Postiz в этой версии не подтверждены.

## 0.12.0 — 2026-10-31: явный локальный HTTP proxy для Telegram

- В тестовой копии добавлен `options.proxy_env`, например `TELEGRAM_PROXY_URL`.
  Разрешён только явно выбранный HTTP proxy на loopback с указанным портом,
  без userinfo, постороннего path, query и fragment. Ambient proxy-переменные
  не включают маршрутизацию, отсутствующая явная настройка не заменяется direct.
- TLS, `trust_env=false`, фиксированный Telegram API origin, запрет redirects,
  exact approval, durable шаги и unknown сохраняются. Injected fixture transport
  не переопределяется proxy mounts и не становится живой интеграцией.
- Проверки: 146 Telegram HTTP/Unicode/media/authorization/proxy тестов passed;
  Ruff для изменённых Python-файлов прошёл. Документация настройки находится
  в `docs/TELEGRAM_PROXY.md`; системные proxy/VPN и исходный checkout не менялись.
- Ограничения: SOCKS не включён; смена env требует нового экземпляра адаптера.
  Создание бота/канала, GET API root и inspect не доказывают доставку поста.
  Живая публикация этой правкой не проверялась; полный QA после изменения
  должен относиться к обновлённым hashes, а не к прежнему frozen snapshot.

## 0.12.0 — 2026-10-31: свежая сверка известной записи VK browser

- Reconcile известного ID выполняет два новых чтения стены даже при прежнем
  confirmed-журнале. Старый proof подтверждает прошлую доставку; при удалённом
  или изменённом тексте/авторе/фото возвращается unknown, ID сохраняется,
  прежний proof отмечается как historical. Повторной отправки/загрузки нет.
- Проверки: 7 focused Playwright/HTTP fixture-тестов passed, включая обе свежие
  загрузки, удалённую запись и смену текста, автора, фото; Ruff прошёл.
  Полный обновлённый модуль и повторный общий QA проверяются отдельно.
- Ограничения: финальные живые DOM selectors VK и новая живая публикация
  по-прежнему не подтверждены. Fixture остаётся fixture и не получает
  verified=true; исходный checkout и рабочий сервер не менялись.

## 0.12.0 — 2026-10-31: личная стена VK и строгая граница browser receipt

- Добавлен явный `personal-wall-settings-v1` для собственной положительной
  стены: точный account/wall, новый пустой composer, пустые соавторы, видимость
  «Все», отсутствие лишних режимов и scoped media. Наблюдённый включённый по
  умолчанию Story switch выключается штатно после отдельного durable intent
  и повторной авторизации; перед click проверяется снова. Это доказательство
  настройки отправителя, а не независимая проверка всех Story аккаунта.
- `vk-spacer-div-v1` сохраняет точный текст наблюдённого preview. Непроверенные
  block boundaries, скрытый BR/текст и лишние вложения блокируют отправку.
  Query `trackcode` допускается только у author href; другие ссылки сохраняют
  строгий запрет query и неподходящих схем/origin.
- Адаптер сравнивает SHA всего текста и каждого изображения в обоих reload
  с реальным frozen plan, проверяет author proof и типы receipt. Reconcile
  строит plan из sealed журнала с точными options/destination; совпадения
  верхнего snapshot hash недостаточно. Malformed stdout остаётся unknown.
- Проверки: 68 focused protocol/Playwright fixture-тестов passed; ранее полный
  модуль 116 passed до последних receipt/link/parser hardening. Полный Ruff
  `src tests scripts` и `git diff --check` прошли. Окончательный общий QA и
  обновлённый полный VK-модуль фиксируются отдельно по финальному source hash.
- Ограничения: живая доставка этим изменением не подтверждена. Контракт
  сообществ сохраняет отдельные явные recipient/author proof. Проверка
  text-only не включает `media_contract_verified`; fixture всегда unverified.

## 0.12.0 — 2026-10-31: DB snapshot и durable origin на границе VK receipt

- Reconcile VK browser теперь требует отдельный frozen DB snapshot и до
  subprocess сравнивает его с request-журналом. План строится по DB snapshot,
  что уточняет предыдущую запись о восстановлении плана только из журнала.
- Confirmed stdout связывается с перечитанным после worker sealed журналом,
  включая точные ID/URL/proof/clicks и исходный fixture marker. Marker нельзя
  повысить до live через stdout. Текущая сверка требует свежий timestamp;
  нечисловой, неограниченный, старый или будущий proof остаётся unknown.
- Проверки: 55 focused fixture/protocol тестов passed перед последним
  расширением timestamp controls; отдельные 43 DB/receipt cases passed.
  Full Ruff `src tests scripts` прошёл; окончательный QA по финальным hashes
  фиксируется отдельно. Реальные внешние отправки этой правкой не выполнялись.

## 0.12.0 — 2026-10-31: проверка targets ссылок в тексте VK

- До Send и при обоих чтениях записи каждый anchor внутри полного текста
  обязан вести на точный HTTP(S) URL из согласованного текста или frozen sources.
  Правильный видимый текст с посторонним href больше не считается exact.
  Author/permalink проверяются прежними отдельными правилами.
- Link targets входят в captured snapshot и typed receipt reloads. Неизвестный
  redirect, пустой href, javascript и посторонний адрес дают отказ до click
  либо unknown с сохранением ID после доставки; повторной отправки нет.
- Проверки: 52 focused fixture/protocol tests passed, включая zero-Send,
  допустимые source/body URL, unknown native readback и no-resend. Ruff own
  Python-файлов прошёл; полный модуль и общий QA фиксируются отдельно.
- Ограничения: VK redirect unwrap не реализован без доказанного контракта;
  эта правка не подтверждает живую доставку и не включает media flag.
