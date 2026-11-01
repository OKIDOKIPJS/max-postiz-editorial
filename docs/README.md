# Навигатор документации 0.11.9

31 октября, 00:43 МСК: **0.11.9 / RUN87**, **шесть закрытых готовых статей**
и отдельный pending на 01:01:47. Автопополнение подтверждено ACCEPTED_SIX;
доставка 00:01:46 подтверждена в самом MAX двумя reload, одной попыткой,
точным текстом, изображением и настоящим жирным заголовком. QA28 — **1286
passed в 33 модулях**, без ошибок и пропусков. Доказательства и ограничения:
[VM_EDITORIAL](VM_EDITORIAL.md), [CHANGELOG](../CHANGELOG.md).
Этот архив закрепляет 0.11.9; параллельная 0.11.10 сохраняется отдельно.

Срез 30 октября, 23:13 МСК: **0.11.9 / RUN87**, QA28 — **1286 passed в 33
модулях** без ошибок и пропусков. Установка и один перезапуск завершены;
история сохранена, резерв готовится автоматически. Публикация 23:01:30 МСК
независимо подтверждена в MAX до обновления. Шесть закрытых usable статей
и следующая доставка после установки ещё проверяются.
Актуальные датированные доказательства — [VM_EDITORIAL](VM_EDITORIAL.md),
изменения и ограничения — [CHANGELOG](../CHANGELOG.md).

Ниже сохранён прежний датированный срез 0.11.7.

30 октября 2026, Europe/Moscow: Python 0.11.7 установлен на VM, цикл revision 83
возобновлён. Linux/PostgreSQL qa-v25 прошла 745 тестов в 23 модулях без ошибок
и пропусков. В 11:41:52 МСК новая статья из резерва фактически доставлена:
verified dispatch, Postiz PUBLISHED и два независимых reload MAX с точным текстом,
фото перед ним и жирным заголовком. Следующий слот 12:41:53 МСК.

Старая попытка 01:24 отдельно закрыта по прямому поручению владельца без повторной
отправки; оригинальный UNKNOWN Attempt, raw и использованный источник сохранены.
Первоначально подготовлены шесть статей; на срезе 11:50 пять quality_ready,
одна отдельно назначена и одна готовится. Это датированный срез, а не обещание
бессбойного будущего графика. Текущая инструкция и доказательства:
[README проекта](../README.md), [VM_EDITORIAL](VM_EDITORIAL.md),
[append-only CHANGELOG](../CHANGELOG.md).

Все прежние ссылки ниже сохранены. Руководство редакции 0.11.5 не считается
повторно проверенным целиком из-за обновления навигатора. Историческая команда
не является поручением выполнить её на production.

## Основные документы

| Документ | Область |
| --- | --- |
| [README проекта](../README.md) | Краткое состояние, запуск и ограничения |
| [AGENTS](../AGENTS.md) | Правила и история полномочий владельца |
| [CHANGELOG](../CHANGELOG.md) | Append-only история версий и проверок |
| [TOPIC](../TOPIC.md) | Машиночитаемый базовый профиль; runtime-настройки сервера хранятся отдельно |

## Все руководства и отчёты

| Документ | Статус |
| --- | --- |
| [ACCEPTANCE.md](ACCEPTANCE.md) | Руководство; сохранённая редакция 0.11.5 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Руководство; сохранённая редакция 0.11.5 |
| [ARGOS_CASCADE_EVALUATION.md](ARGOS_CASCADE_EVALUATION.md) | Датированный отчёт / исследование / provenance |
| [COMPONENTS_AND_CONNECTIONS.md](COMPONENTS_AND_CONNECTIONS.md) | Датированный отчёт / исследование / provenance |
| [CONTRACTS.md](CONTRACTS.md) | Руководство; сохранённая редакция 0.11.5 |
| [DATA_MODEL.md](DATA_MODEL.md) | Руководство; сохранённая редакция 0.11.5 |
| [DECISIONS.md](DECISIONS.md) | Актуальная сводка и сохранённые исторические разделы |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Актуальная сводка и сохранённые исторические разделы |
| [E1000E_RECOVERY.md](E1000E_RECOVERY.md) | Датированный отчёт / исследование / provenance |
| [EDITORIAL_WORKFLOW.md](EDITORIAL_WORKFLOW.md) | Руководство; сохранённая редакция 0.11.5 |
| [HOURLY_ARTICLE_TESTS.md](HOURLY_ARTICLE_TESTS.md) | Датированный отчёт / исследование / provenance |
| [HOURLY_RESERVE_PLAN.md](HOURLY_RESERVE_PLAN.md) | Руководство; сохранённая редакция 0.11.5 |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Руководство; сохранённая редакция 0.11.5 |
| [LINUX_DEPLOYMENT.md](LINUX_DEPLOYMENT.md) | Актуальная сводка и сохранённые исторические разделы |
| [LLM_CONNECTION_REPORT.md](LLM_CONNECTION_REPORT.md) | Датированный отчёт / исследование / provenance |
| [MADLAD_AUTOMATIC_WORKFLOW.md](MADLAD_AUTOMATIC_WORKFLOW.md) | Руководство; сохранённая редакция 0.11.5 |
| [MANUAL_PUBLICATION.md](MANUAL_PUBLICATION.md) | Отдельный ручной маршрут; не инструкция автоматического max_test |
| [MAX_BROWSER_ADAPTER.md](MAX_BROWSER_ADAPTER.md) | Актуальная сводка и сохранённые исторические разделы |
| [MAX_BROWSER_ADAPTER_REPORT.md](MAX_BROWSER_ADAPTER_REPORT.md) | Датированный отчёт / исследование / provenance |
| [MAX_POSTIZ_CONNECTION.md](MAX_POSTIZ_CONNECTION.md) | Датированный отчёт / исследование / provenance |
| [MAX_TEST_PUBLICATION_REPORT.md](MAX_TEST_PUBLICATION_REPORT.md) | Датированный отчёт / исследование / provenance |
| [NETWORK_RECOVERY.md](NETWORK_RECOVERY.md) | Датированный отчёт / исследование / provenance |
| [OPERATIONS.md](OPERATIONS.md) | Актуальная сводка и сохранённые исторические разделы |
| [POSTIZ_DEPLOYMENT_REPORT.md](POSTIZ_DEPLOYMENT_REPORT.md) | Датированный отчёт / исследование / provenance |
| [PROJECT_BRIEF.md](PROJECT_BRIEF.md) | Руководство; сохранённая редакция 0.11.5 |
| [REDDIT_ACCESS_REQUEST.md](REDDIT_ACCESS_REQUEST.md) | Отдельная спецификация / staging; автоматический Reddit не включён |
| [REDDIT_INTAKE.md](REDDIT_INTAKE.md) | Отдельная спецификация / staging; автоматический Reddit не включён |
| [RELEASE_0.11.5.md](RELEASE_0.11.5.md) | Исторический выпуск 0.11.5 |
| [REUSE_MAP.md](REUSE_MAP.md) | Датированный отчёт / исследование / provenance |
| [SCIENTIFIC_SPELLING.md](SCIENTIFIC_SPELLING.md) | Руководство; сохранённая редакция 0.11.5 |
| [SERVER_REDDIT_AUTOMATION_PLAN.md](SERVER_REDDIT_AUTOMATION_PLAN.md) | Отдельная спецификация / staging; автоматический Reddit не включён |
| [STARTUP_AND_HEALTH.md](STARTUP_AND_HEALTH.md) | Актуальная сводка и сохранённые исторические разделы |
| [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) | Руководство; сохранённая редакция 0.11.5 |
| [TRANSLATION_20_ARTICLES.md](TRANSLATION_20_ARTICLES.md) | Датированный отчёт / исследование / provenance |
| [TRANSLATION_EVALUATION_RUBRIC.md](TRANSLATION_EVALUATION_RUBRIC.md) | Датированный отчёт / исследование / provenance |
| [TRANSLATION_MODEL_COMPARISON.md](TRANSLATION_MODEL_COMPARISON.md) | Датированный отчёт / исследование / provenance |
| [VALIDATION_REPORT.md](VALIDATION_REPORT.md) | Датированный отчёт / исследование / provenance |
| [VM_EDITORIAL.md](VM_EDITORIAL.md) | Текущий срез 0.11.7 и датированная история VM |
| [WORKSTATION_POWER.md](WORKSTATION_POWER.md) | Датированный отчёт / исследование / provenance |

## Развёртывание и машинные контракты

- [Postiz: старый стенд](../deployment/postiz/README.md).
- [Переводчики: backend и benchmark](../deployment/translation/README.md).
- `deployment/linux/` содержит сервисы и проверки работающего Linux-профиля.
- `prompts/`, `schemas/`, `migrations/` и примеры `config/` входят в исходники.
  Версионированные промпты и policy hashes нельзя обновлять редактурой документации.
- `.env`, приватные конфигурации, raw, фото, cookies, БД и модели остаются
  в игнорируемых файлах. Ссылки на закрытые доказательства не заменяются
  публикацией этих файлов в Git.
