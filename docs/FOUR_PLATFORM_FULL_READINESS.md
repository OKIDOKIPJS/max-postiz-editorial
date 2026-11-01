# Полная приёмка 0.12.2 в отдельной копии

1 ноября 2026, Europe/Moscow. Продолжение релиза 0.12.1; рабочий сервер
и исходный checkout не обновляются. Пикабу исключён из новых тестов по прямому
поручению владельца. Его unknown, пауза, профиль и история сохраняются.

## Изолированный Linux runtime

Создан отдельный Ubuntu 24.04.5 WSL2 runtime на ext4. Базовый rootfs получен
из [официального каталога Canonical](https://cdimage.ubuntu.com/ubuntu-base/releases/noble/release/)
и проверен по SHA256 каталога. Runtime имеет отдельного non-root пользователя,
systemd, собственные Python/Playwright/Chromium и Docker-хранилище. Windows
automount и interop выключены внутри новой distro; default distro, Docker Desktop
и общая .wslconfig не менялись. Installer, VHDX, слои и приватные отчёты находятся
только в локальном runtime-хранилище и не входят в архив исходников.

## Linux-supervisor: принято

Программа запускалась в новой transient systemd service с Delegate=yes,
PrivateNetwork=yes, NoNewPrivileges=yes, KillMode=control-group и конечным
RuntimeMaxSec. Процесс проверял своё членство в non-root cgroup v2; отдельные
private UUID cgroup создавал и удалял штатный browser_process.py.

Все 8 native сценариев прошли: normal orphan/fork/setsid tree, timeout,
заблокированный stdin, две UTF-8 pipes, удерживаемые pipes, SIGINT, обрабатываемый
SIGTERM и настоящий Chromium. Для каждого проверены PID/starttime, удаление
private cgroup, сохранение sentinel и неизменность loopback-only namespace.
Chromium 153.0.8010.12 запускался с sandbox и очищен вместе с дочерними процессами.
Arbitrary SIGKILL не выдаётся за перехваченный библиотекой сигнал; завершение
такого service-shaped runtime обеспечивает внешняя systemd cgroup.

Отдельная service без Delegate отклонила запуск до Popen. Существующий модуль
supervisor прошёл на Linux: 29 passed; на Windows: 27 passed, 2 Linux skipped.
Исправлена только test fixture: завершение worker с кодом 7 теперь происходит
после чтения stdin, сохраняя исходные assertions. Сам supervisor не изменён.
Первоначальные неуспешные collection/fixture попытки сохранены приватно.

Это native-проверка Linux/WSL2, а не запуск на рабочей production VM.

## Telegram: текст доставлен, изображения не проверялись

Одна согласованная текстовая запись отправлена из копии 1 ноября в 08:00 МСК.
Bot API вернул успешный `sendMessage` и message ID; одна попытка, ошибок нет.
Дополнительно запись независимо прочитана в Telegram Web: точный полный текст,
жирный заголовок и нативное время 08:00:32. Снимок и отдельное наблюдение
сохранены в игнорируемых runtime-файлах. Новых отправок при этой сверке нет.

Автоматический маршрут по-прежнему различает `api_receipt` и независимый readback:
ручное наблюдение UI не меняет `verified=false` в историческом API-журнале.
Изображения и непрерывный цикл в Telegram этой проверкой не приняты. Тестовый
адресат остаётся на паузе; рабочий сервер не участвовал.

## Docker/Next: сборка и frontend-проверка приняты

Итоговый образ: `max-postiz-linux:0.12.2-full-readiness-20261101-r4`, immutable ID
`sha256:89d90a1aebb1791ea77de940d60568a85239f8d1594049124b08b759e34ccc75`.
Настоящие Prisma/backend/orchestrator builds прошли в предыдущем native layer;
новый полный Next прошёл webpack, TypeScript, page data, static generation,
optimization и build traces. Финальная сборка повторно использовала именно
эти проверенные native layers: все три тяжёлых RUN фактически CACHED, затем
выполнились новые origin guard и ENV. Старый локальный compiled image не использован.

В финальном image проверены 22 compiled/native артефакта и восемь точных
source/config overlays. Сохранены настоящий DOMPurify/jsdom и его XSS-проверка,
MAX provider/guard, editorial server/client route и CSS. Шесть изменённых файлов
recipe/guard/test/fixture совпали с замороженными исходниками Windows-копии.

Native Next запускается в readonly-контейнере с сетью только lo: `/` и `/editorial`
дают ожидаемый redirect на auth. Отдельный dummy logout при пустом NOT_SECURED
подтвердил Secure и HttpOnly. Это frontend request-time проверка; TLS transport,
backend login/OAuth/выдача cookie и полный стек с БД этим тестом не принимаются.
Штатный SIGTERM и завершение проверены, OOM/принудительного cleanup нет.
После проверок осталось 0 контейнеров и 0 volumes; default CMD, backend,
db push/migration и внешние публикации Docker-проверками не запускались.

Образ хранится в отдельном WSL runtime `MaxPostizReady-20261101`, а не в Docker
рабочего сервера. Для безопасного просмотра сохранённого image после остановки
стенда можно выполнить в PowerShell:

```powershell
wsl -d MaxPostizReady-20261101 -u root -- systemctl start max-postiz-readiness-docker
wsl -d MaxPostizReady-20261101 -u root -- docker image inspect max-postiz-linux:0.12.2-full-readiness-20261101-r4 --format '{{.Id}}'
```

Используется отдельный daemon и ext4 data-root в новой distro. Штатные Docker
Desktop, контейнеры, volumes и БД рабочего сервера не используются. До сборки
проверяются ID/endpoint/data-root, отсутствие прежних ресурсов и фактические
cgroup RAM/swap/CPU limits. Base image берётся по закреплённому digest.

Проверки image выполняются с явным override entrypoint: унаследованный CMD
upstream запускает prisma-db-push, поэтому его нельзя использовать для обычного
осмотра. Рецепт выполняет только prisma generate и полные backend/orchestrator/
Next builds. Типизация, минимизация и функциональность не отключаются.

Закреплённая версия upstream использует isomorphic-dompurify с jsdom, который
читает CSS из собственного каталога. Его webpack-bundle не содержит этот файл.
`patch-production-frontend.cjs` сначала проверяет SHA исходного next.config,
затем добавляет только `serverExternalPackages: ['isomorphic-dompurify']`.
Это сохраняет настоящий sanitizer и его native файлы; mock/stub не применяется.

`production-origin.cjs` принимает только canonical origin. Для HTTPS финальный
`NOT_SECURED` пустой: строка `false` тоже truthy для upstream и недопустима.
HTTP разрешён только на `127.0.0.1` или `[::1]`; пользователь/пароль, path,
публичный HTTP и противоречивый cookie mode отклоняются. Штатный runner сам
передаёт вычисленный аргумент, а прямой Docker build обязан пройти тот же guard.
Финальный guard/ENV выполняется после Next: это request-time значение для
dynamic layouts/proxy/backend, при неизменном compile-time public origin.

Для образа стенда выбран `http://127.0.0.1:14007`. Для другого адреса
панели нужна отдельная сборка с новым origin; изменение runtime ENV само по
себе не меняет уже скомпилированный `NEXT_PUBLIC_BACKEND_URL`. Проверки стенда
не устанавливают TLS, reverse proxy, БД или образ на рабочую VM.

## VK с изображением: доставка подтверждена

Текстовый personal-text-v1 сохраняет запрет media. Для отдельного image preset
нужны наблюдённый file input, точная связь approved file с native photo asset,
полный preview, caption и две независимые загрузки опубликованной записи.
JPEG/CDN derivative не выдаётся за совпадение SHA исходного файла; perceptual
сходство само по себе не даёт verified.

Изображение и caption предъявляются владельцу до загрузки. Одна публикация
выполняется только с согласованным точным содержимым, адресатом и будущей датой.
Любой неизвестный upload/Publish блокирует повтор. Пикабу в проверку не входит.

Владелец согласовал исходную схему, точный caption, одну публикацию и выбор
будущего времени 1 ноября до 22:00 МСК; разрешено штатное пережатие без
кадрирования и изменения содержания. В отдельной вкладке нативно проверены
file input, завершённая загрузка, sortable photo ID, полный DOM caption и
предпросмотр. Предпросмотр содержит два изображения одного attachment с
одинаковым полным CDN URL, но не содержит photo ID. Код не должен приписывать
ему отсутствующий ID: требуется отдельный контракт связи с загруженным asset.
Native photo viewer недоступен для ещё не опубликованной фотографии; оригинал
проверяется после Publish. Снимок и полные наблюдения остаются приватными.

На этапе первичного наблюдения проверочный редактор закрыт штатным
«Выйти без сохранения», Publish не нажат. Само это наблюдение не являлось
проверкой доставки из серверного обработчика. Перед
настройкой image preset сохранены БД, конфигурация и история отдельной копии;
все четыре адресата на паузе, активных send/mutation lease нет.

Затем штатный отправитель выполнил одну согласованную публикацию. Начальный
unknown сохранён в истории; повторного Publish не было. После отдельного
принятия владельцем фактической картинки стандартный read-only reconcile
выполнил два независимых чтения стены и Original: точные caption, автор,
post/asset ID, SHA и размер согласованного фактического файла совпали.
Состояние стало `published`, `verified=true`, внешних изменений при сверке — 0.
Исходное согласование, snapshot и число попыток отправки не изменились.

Принятие относится только к этой картинке существующего поста; исходная политика
без crop не объявляется выполненной. Все адресаты копии вернулись на паузу,
прежние unknown и история остальных маршрутов сохранены. Живой VK-профиль
проверялся на Windows в копии. Авторизация на Linux/рабочую VM не переносилась;
native Linux-проверка supervisor и Chromium описана отдельно выше.

## Настройка image preset VK

Шаблон `config/vk_browser_image.target.example.json` по умолчанию выключен.
`personal-image-v1` предназначен только для собственной личной стены и требует
ровно один PNG/JPEG до 10 MiB и 40 млн пикселей. Пустое media не превращается
в текстовую отправку. Отдельный `personal-text-v1` сохраняет запрет изображений.

После проверки фактического UI оператор сохраняет локальный файл
`data/vk-media-<имя>.json`: `vk-native-image-observations-v1`, origin, owner,
policy, observed_at, native_observed, SHA полного DOM-контракта и пять scopes
из `vk_media.SCOPES`. В options задаются путь и SHA файла evidence. Включение
двух verified-флагов без этого файла не проходит preflight или doctor. Credentials,
browser profile, evidence и исходные изображения в исходный архив не входят.

`media_proof_policy` выбирается явно:

- `exact-bytes-v1` требует одинаковые SHA исходника и прочитанных bytes.
- `exact-pixels-v1` требует точные декодированные samples, ориентацию и цветовые
  metadata; это не перцептивное сравнение с допуском.
- `native-asset-lineage-v1` применяется только при согласованном штатном
  пережатии. Он связывает утверждённые bytes с uploaded photo ID, тем же preview
  CDN и опубликованной фотографией. SHA исходника и derivative сохраняются
  раздельно; размеры полного изображения обязаны совпадать, crop запрещён.

Изменение политики, evidence, media или адресата меняет binding и требует
нового точного согласования. До смены options проверяют sending/unknown и lease,
сохраняют прежние options и журнал. Остальные адресаты не синхронизируют заново
ради изменения VK. Неизвестный результат не отправляют повторно.

Read-only reconcile image-публикации без ID допускает только один click-intent
и валидный sealed upload для того же source/request/owner. Сначала находится
единственная запись с точным caption и photo asset, затем выполняются прежние
две проверки стены и оригинала. ID сохраняется вместе с полным подтверждением.
Нулевое/неоднозначное совпадение остаётся unknown; editor/upload/Publish при этом
не вызываются. Наблюдённый `vk-native-caption-away-prefix-v2` восстанавливает
полный согласованный URL только из штатного `away.php?to=...&utf=1` с точным
адресом назначения. Видимая подпись должна быть полным URL либо допустимым
префиксом того же host/path с native многоточием. Произвольные названия ссылок,
скрытые вставки, другой redirect, лишние query и неоднозначный DOM блокируются.
Парсер применяется только к image preset; текстовый маршрут не меняется.

## Отдельное принятие фактической картинки существующего поста

При живой проверке VK сохранил оригинал на один пиксель меньше справа и снизу:
1400×1000 превратилось в 1399×999. Удалён только однородный пустой фон; исходные
правила `native-asset-lineage-v1` всё равно не выполнены. Такой результат нельзя
обозначать как успешное прохождение первоначальной политики без кадрирования.
Повторная публикация не выполняется.

Владелец отдельно принял именно фактическую картинку уже существующего поста.
`accepted-actual-media-v1` сохраняет это решение отдельным Audit владельца,
привязанным к исходному approval, frozen snapshot, дате, source/actual SHA,
размерам, crop box, post/asset ID и SHA локального подтверждения владельца.
Исходные approval/options/snapshot и upload остаются неизменными. Одобрение
не даёт права новой отправки и не меняет правила для будущих изображений.

Только read-only reconcile читает это решение и заново проверяет его актуальность
до и после browser worker. Требуются единственное непротиворечивое решение,
неотозванный исходный approval, отсутствие lease, тот же адресат и source,
точный caption/автор/asset и два независимых readback оригинала с точным actual
SHA и native crop box. Отзыв или изменение связанного состояния блокирует
подтверждение. В evidence явно записываются отдельный вид принятия фактической
картинки и `original_policy_satisfied=false`; publish этот контракт не использует.

Нативный просмотрщик показывает экранное превью меньшего размера, чем Original.
Для отдельно принятой картинки разрешён только наблюдённый responsive URL
того же файла: все части адреса и порядок query неизменны, кроме единственного
canonical `cs=WIDTHx0`. Upsize, другой crop/адрес, повторные ключи и нарушение
пропорций отклоняются. Размеры самого Original не заменяются размерами превью:
его bytes, SHA и crop по-прежнему обязаны точно совпадать с решением владельца.
Семантическая native ссылка без собственного layout box допускается только
на опубликованном посте с одним непосредственно вложенным видимым IMG;
upload и preview сохраняют собственные строгие проверки.

## Проверки замороженного VK кода

Итоговый frozen-прогон текущих исходников: **611 passed, 3 skipped, 2 deselected,
1 warning**, 450.24 с, без изменения исполняемых файлов. Проверены все VK-модули, общий
supervisor, configuration и route recovery с fake Telegram. Ruff и pip check
прошли. Это профильная регрессия, а не новый полный прогон проекта. Тесты Пикабу
не запускались; результаты 476, 577 и 591 passed сохранены отдельными историческими traces.

Пропущены один symlink без Windows-привилегии и две Linux cgroup/setsid проверки.
Native Linux-сценарии отдельно приняты выше. Исключены один VK-case с именем
профиля Пикабу и отдельная PostgreSQL recovery интеграция: живые credentials
текущему прогону не передавались. Warning относится к deprecation Starlette
TestClient. Независимый review media/receipt/recovery и отдельного принятия
фактической картинки не оставил блокеров. Ещё 14 чистых mock-проверок прежнего
одноразового отправителя не выполняли реальных действий.
