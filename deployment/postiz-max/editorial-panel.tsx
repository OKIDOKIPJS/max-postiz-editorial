'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChannelForm, EditorialForm, EditorialSettings, ManagedPlatform, OriginalMedia, RouteSetup, SetupTarget,
  channelForm, channelTarget, formFromSettings, imageSha256, originalInput, originalMedia,
  safeSourceUrl, settingsFromForm } from './editorial-form';
import styles from './editorial.module.css';

// noVNC строит WebSocket URL от корня origin; префикс закрытого прокси задаём явно.
export const OWNER_BROWSER_URL = '/owner-browser/vnc.html?autoconnect=1&resize=scale&path=owner-browser/websockify';

type Recent = { id: string; title?: string; source?: string; state?: string; updated_at?: string | number; url?: string; error?: string };
type State = {
  revision: number; paused: boolean; phase?: string; updated_at?: string | number; next_run_at?: string | number;
  last_error?: { code: string; message?: string } | null; recent?: Recent[];
  capabilities?: { reddit?: { available?: boolean; reason?: string } };
  reserve?: { enabled?: boolean; ready?: number; target?: number; next_delivery_at?: number };
  preparation?: { phase?: string; last_error?: { code: string } | null };
};
export type RouteTarget = {
  id: string; platform: string; route: string; destination: string; enabled: boolean; paused: boolean;
  binding_hash: string; live_verified: boolean; status_kind: string; credentials_configured?: boolean;
  adapter_mode?: string; owner_configured?: boolean; profile_exists?: boolean; dom_contract_verified?: boolean;
};
export type RouteVariant = {
  id: string; target_id: string; title: string; revision: number; state: string;
  content_hash: string; media_count: number; source_count: number;
  manual_approval_allowed?: boolean; approval_mode?: string; origin?: string;
};
export type RouteDispatch = {
  id: string; target_id: string; title: string; state: string; due_at: number; verified: boolean | null;
  external_id?: string | null; permalink?: string | null; error?: string | null; attempts: number; snapshot_hash: string;
  evidence_kind?: 'fixture' | 'api_receipt' | 'independent_readback' | 'unverified';
  destination?: string | null; platform?: string | null; route?: string | null;
};
export type RoutePages = { variant_page: number; dispatch_page: number; dispatch_filter: 'all' | 'unresolved' };
export type RoutePagination = RoutePages & { page_size: number; variant_count: number; variant_active_count: number;
  variant_has_next: boolean; dispatch_count: number; dispatch_active_count: number; dispatch_has_next: boolean };
export type RouteInventory = { targets: RouteTarget[]; variants: RouteVariant[]; dispatches: RouteDispatch[]; pagination?: RoutePagination };
export type RouteDetails = { variant_id: string; target_id: string; title: string; body: string; revision: number;
  content_hash: string; destination: string; platform: string; route: string; binding_hash: string;
  metadata: Record<string, unknown>; media: Record<string, unknown>[]; sources: Record<string, unknown>[];
  read_only: true; manual_approval_allowed: boolean; media_policy?: { id: string; label: string; description: string } | null };
export type RouteAction = { action: 'inspect' | 'details' | 'preflight' | 'preview' | 'approve' | 'pause' | 'resume' | 'reconcile'; id: string;
  revision?: number; content_hash?: string; due_at?: number };
type RouteActionHandler = (action: RouteAction) => Promise<boolean>;
type RouteDetailsHandler = (variant: RouteVariant) => Promise<void>;
type RoutePageHandler = (pages: RoutePages) => void;
type RoutePreviewImage = { data_url: string; sha256: string; mime: string; width: number; height: number; preview: true };
type RoutePreviewHandler = (variant: RouteVariant) => Promise<unknown | null>;
const managedRoutes = new Set(['max_browser', 'max_api', 'telegram_api', 'vk_api', 'vk_browser', 'pikabu_browser']);
const errors: Record<string, string> = {
  EDITORIAL_SERVICE_UNAVAILABLE: 'Редакционный сервис недоступен. Проверьте его состояние на сервере и обновите страницу.',
  EDITORIAL_OWNER_REQUIRED: 'Управление редакцией доступно только владельцу этой организации.',
  EDITORIAL_COOKIE_SESSION_REQUIRED: 'Войдите в Postiz обычным способом и обновите страницу.',
  EDITORIAL_ORIGIN_REQUIRED: 'Откройте редакцию с того же адреса, что и Postiz.',
  EDITORIAL_NOT_CONFIGURED: 'Подключение редакционного сервиса ещё не настроено на сервере.',
  EDITORIAL_REVISION_CONFLICT: 'Настройки уже изменились. Обновите их и повторите сохранение.',
  EDITORIAL_PAUSE_BEFORE_SETTINGS: 'Сначала поставьте цикл на паузу, затем сохраните настройки.',
  EDITORIAL_SETTINGS_INVALID: 'Настройки не прошли проверку. Проверьте диапазоны и списки источников.',
  EDITORIAL_AUTOMATIC_POLICY_REQUIRED: 'Автоматическая политика публикации ещё не подключена. Цикл остался на паузе.',
  REDDIT_APPROVAL_REQUIRED: 'Доступ Reddit или права на перевод и внешнюю публикацию ещё не подтверждены.',
  REDDIT_SETTINGS_INVALID: 'Проверьте настройки Reddit: имена сообществ и авторов, рейтинг и период.',
  INPUT_INVALID: 'Проверьте заполненные поля и обновите сохранённые настройки.',
  NASA_RATING_UNAVAILABLE: 'Научные источники не предоставляют проверенный рейтинг материалов. Рейтинг должен оставаться равным нулю.',
  EDITORIAL_SOURCE_POLICY_REQUIRED: 'Для выбранного источника не настроены проверенные права и политика обработки. Публикация заблокирована.',
  ARTICLE_RECONCILIATION_REQUIRED: 'Есть незавершённая отправка. Требуется сверка результата; повторная отправка заблокирована.',
  EDITORIAL_LEADERSHIP_LOST: 'Потеряно право управления циклом. Требуется сверка серверного процесса.',
  REDDIT_COLLECTION_CONFIG_INVALID: 'Проверьте настройки Reddit: имена сообществ и авторов, рейтинг и период.',
  REDDIT_COLLECTION_SCOPE_REQUIRED: 'Для Reddit укажите хотя бы одно сообщество или одного автора.',
  ARTICLE_QUALITY_REJECTED: 'Перевод отклонён проверкой качества. Автоматической публикации нет.',
  ARTICLE_SPELLING_CHECK_UNAVAILABLE: 'Проверка орфографии недоступна. Публикация заблокирована.',
  OPENCODE_GENERATION_FAILED: 'Модель не завершила обработку. Публикация заблокирована.',
  EDITORIAL_FORM_INVALID: 'Проверьте поля: интервал от 60 минут, целые неотрицательные рейтинги и допустимые списки.',
  REDDIT_ACCESS_REQUIRED: 'Для Reddit требуется разрешённый доступ к API.',
  REDDIT_RIGHTS_REQUIRED: 'Для Reddit требуется подтверждение прав на внешнюю публикацию.',
  ARTICLE_RESERVE_EMPTY: 'Запас проверенных статей пуст. Подготовка пополняет его; проверки качества сохраняются.',
  ARTICLE_RESERVE_PREPARATION_ACTIVE: 'Есть незавершённая подготовка. Её исход нужно сверить до нового обращения к модели.',
  SCIENCE_SOURCES_UNAVAILABLE: 'Научные источники сейчас недоступны. История и запас статей сохранены.',
  ARTICLE_RESERVE_CLAIM_LOST: 'Не удалось подтвердить владельца подготовки. Работа остановлена до сверки.',
  ARTICLE_RESERVE_ASSIGNMENT_LOST: 'Назначение статьи требует сверки. Повторная отправка заблокирована.',
  EDITORIAL_ROUTE_ACTION_INVALID: 'Выберите сохранённый канал или материал, проверьте версию и будущую дату публикации.',
  EDITORIAL_ROUTE_SCHEDULE_INVALID: 'Укажите будущую дату и время по Москве.',
  EDITORIAL_ROUTE_CONSENT_REQUIRED: 'Выполните проверку и явно согласуйте выбранную версию, канал и дату.',
  EDITORIAL_ROUTE_RESPONSE_INVALID: 'Не удалось прочитать список каналов. Обновите статус.',
  TARGET_PAUSED: 'Канал на паузе. Согласованные материалы сохраняются до возобновления.',
  APPROVAL_STALE: 'Текст, вложения или настройки изменились. Обновите материал и согласуйте новую версию.',
  CANCEL_OR_RECONCILE_FIRST: 'Есть незавершённая отправка. Сначала сверьте её результат.',
  VK_TOKEN_REQUIRED: 'Для VK ещё не настроен доступ на сервере.',
  TELEGRAM_TOKEN_REQUIRED: 'Для Telegram ещё не настроен доступ на сервере.',
  ROUTE_ACTION_INVALID: 'Проверьте выбранный канал, материал и дату.',
  SCHEDULE_FUTURE_REQUIRED: 'Дата публикации должна быть в будущем.',
  ROUTE_RECONCILE_STATE_INVALID: 'Состояние отправки уже изменилось. Обновите историю.',
  HUMAN_REVIEW_CONFIRMATION_REQUIRED: 'Этот материал относится к отдельному почасовому сценарию и не согласуется в списке ручных черновиков.',
  VK_READBACK_UNAVAILABLE: 'VK принял пост, но независимая сверка недоступна. Проверьте результат отдельной сверкой; повторной отправки нет.',
  VK_READBACK_MISMATCH: 'Текст или вложения найденного поста VK не совпали с согласованной версией. Требуется сверка.',
  VK_UPLOAD_UNKNOWN: 'Исход загрузки изображения в VK неизвестен. Повторная загрузка и отправка заблокированы.',
  VK_CAPTCHA_REQUIRED: 'VK запросил проверку доступа. Отправка остановлена.',
  VK_RATE_LIMIT: 'VK временно ограничил запросы. Повторной отправки нет.',
  EDITORIAL_ROUTE_PREVIEW_INVALID: 'Не удалось безопасно показать изображение этой версии. Согласование не выполнено.',
  ROUTE_PREVIEW_SIZE_INVALID: 'Изображение не удалось подготовить для просмотра. Исходный файл не изменён.',
  EDITORIAL_ROUTE_DETAILS_INVALID: 'Не удалось прочитать полную версию материала. Согласование недоступно.',
  ROUTE_DETAILS_SIZE_INVALID: 'Полный материал превышает допустимый размер ответа. Согласование не выполнено.',
  ROUTE_LIST_SIZE_INVALID: 'Список превышает допустимый размер ответа. Материалы и история сохранены.',
  EDITORIAL_ROUTE_LIST_INVALID: 'Неверная страница списка. Обновите статус.',
  ROUTE_LIST_INVALID: 'Неверная страница списка. Обновите статус.',
  EDITORIAL_ROUTE_ADMIN_INVALID: 'Проверьте поля подключения или материала. Настройки не сохранены.',
  EDITORIAL_CHANNEL_INVALID: 'Проверьте идентификатор, профиль, адрес канала и параметры подключения.',
  EDITORIAL_ORIGINAL_INVALID: 'Укажите заголовок до 300 знаков, текст до 40 000 знаков и не более одного изображения.',
  EDITORIAL_ORIGINAL_MEDIA_INVALID: 'Изображение не прошло проверку. Выберите исходный PNG или JPEG заново.',
  EDITORIAL_UPLOAD_INVALID: 'Выберите PNG или JPEG размером не более 10 МБ.',
  ROUTE_ADMIN_BUSY: 'Канал занят отправкой или сверкой. Настройки и материал не изменены.',
  ROUTE_ADMIN_CONFLICT: 'Данные уже изменились. Обновите подключения перед сохранением.',
  ROUTE_ADMIN_RECOVERY_REQUIRED: 'Предыдущее сохранение требует сверки. Каналы остаются на паузе. После восстановления доступности БД повторите действие. Несовпадающие данные автоматически не заменяются.',
  ROUTE_ADMIN_PAUSE_REQUIRED: 'Сначала приостановите канал. Сохранение не включает отправку.',
  ROUTE_RUNTIME_RESTART_REQUIRED: 'Настройки сохранены. Перезапустите редакционный сервис перед проверкой подключения.',
  OWNER_ORIGINAL_ATTESTATION_REQUIRED: 'Подтвердите авторство текста и права на выбранное изображение.',
  OWNER_ORIGINAL_ALREADY_EXISTS: 'Такой авторский материал уже сохранён. Найдите его в списке черновиков.',
  OWNER_ORIGINAL_RUSSIAN_REQUIRED: 'Материал должен быть на русском языке.',
  OWNER_ORIGINAL_ATTESTATION_STALE: 'Изменённый материал требует нового подтверждения авторства.',
  BROWSER_LOGIN_REQUIRED: 'Выполните вход в выбранный аккаунт, затем отдельно проверьте доступ.',
  BROWSER_SERVER_SESSION_REQUIRED: 'Для этого подключения ещё требуется вход владельца на сервере.',
  TARGET_BINDING_CONFLICT: 'Подключение уже изменилось. Выберите канал заново и проверьте актуальные параметры.',
  ROUTE_CONFIG_CONFLICT: 'Подключения изменились. Обновите список и выберите канал заново.',
  ROUTE_CONFIGURATION_CHANGED: 'Сохранённые параметры не совпадают с действующим подключением. Требуется обновление состояния.',
  TARGET_PAUSE_REQUIRED: 'Перед этим действием поставьте выбранный канал на паузу.',
  ROUTE_ADMIN_OPTIONS_INVALID: 'Параметры не подходят выбранной площадке.',
  ROUTE_ADMIN_TARGET_UNSUPPORTED: 'Это подключение настраивается отдельно. Выберите MAX, Telegram или личную страницу VK.',
  LOGIN_SELECTION_ACTIVE: 'Окно входа уже открыто. Завершите вход и дождитесь закрытия сеанса перед изменениями.',
  LOGIN_SELECTION_STALE: 'Подготовленный вход устарел. Обновите подключение и подготовьте новое окно.',
  LOGIN_SELECTION_INVALID: 'Не удалось подтвердить выбранное окно входа. Подготовьте вход заново.',
  CREDENTIAL_FORMAT_INVALID: 'Проверьте токен Telegram-бота. Значение не сохранено.',
  CREDENTIAL_ENV_NOT_DEDICATED: 'Для этого канала ещё не настроено отдельное хранение токена.',
  CREDENTIAL_TARGET_INVALID: 'Токен можно сохранить только для выбранного Telegram-канала.',
  MEDIA_CONTENT_INVALID: 'Изображение повреждено или его формат не поддерживается. Выберите PNG или JPEG.',
  MEDIA_HASH_CHANGED: 'Байты изображения не совпали с выбранным файлом. Загрузите исходный файл заново.',
  MEDIA_TYPE_MISMATCH: 'Содержимое изображения не совпало с форматом PNG или JPEG.',
  ORIGINAL_DISPATCH_HISTORY_IMMUTABLE: 'Этот материал уже участвовал в отправке. Сохранённую историю нельзя редактировать.',
  OWNER_ORIGINAL_REQUIRED: 'Здесь редактируются только собственные авторские материалы.',
  REVISION_CONFLICT: 'Версия материала уже изменилась. Откройте актуальный материал перед правкой.',
  SOURCE_REVISION_STALE: 'Исходная версия материала изменилась. Обновите полный материал.',
  BROWSER_PROFILE_MISSING: 'Для аккаунта ещё не сохранён браузерный профиль. Выполните вход владельца.',
  BROWSER_CHANNEL_NOT_ALLOWED: 'Канал MAX не включён в разрешённый список. Выберите сохранённый разрешённый канал.',
  BROWSER_DEPENDENCY_MISSING: 'На сервере ещё не установлены необходимые компоненты браузера.',
  BROWSER_CGROUP_DELEGATION_REQUIRED: 'Для браузера на сервере ещё не настроено безопасное завершение процесса.',
  CREDENTIAL_MISSING: 'Токен ещё не сохранён. Введите его в форме подключения.',
  DOM_CONTRACT_NOT_VERIFIED: 'Публикация через этот редактор ещё не подтверждена проверкой.',
  VK_BROWSER_DOM_CONTRACT_INCOMPLETE: 'Редактор VK требует проверки совместимости перед отправкой.',
  VK_BROWSER_MEDIA_EVIDENCE_UNVERIFIED: 'Публикация изображений VK ещё не прошла необходимую проверку.',
  TARGET_DISABLED: 'Подключение выключено. Включите его в настройках; канал останется на паузе.',
  PROFILE_DISABLED: 'Редакционный профиль выключен. Выберите включённый профиль.',
  PROFILE_DRY_RUN: 'Этот редакционный профиль работает без отправки. Выберите другой профиль.',
  EDITORIAL_MAINTENANCE_BUSY: 'Есть незавершённая подготовка или отправка. Применение настроек пока недоступно; дождитесь завершения или сверки.',
  POSTIZ_NEAR_QUEUE_RESTART_BLOCKED: 'В ближайшие пять минут запланирована публикация. Примените настройки после неё.',
  RESTART_ALREADY_REQUESTED: 'Запрос применения настроек уже принят. Дождитесь перезапуска и обновите подключения.',
  RESTART_SELECTION_INVALID: 'Не удалось подтвердить запрос применения настроек. Обновите подключения.',
};
export function errorText(code: string) { return errors[code] || `Действие остановлено. Код: ${code}`; }
export function readableTime(value?: string | number) {
  if (value === undefined || value === null || value === '') return '—';
  const date = new Date(typeof value === 'number' && value < 100000000000 ? value * 1000 : value);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });
}
export function moscowSchedule(value: string, currentTime = Date.now()): number | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null;
  const timestamp = Date.parse(`${value}:00+03:00`);
  if (!Number.isFinite(timestamp) || timestamp <= currentTime ||
      new Date(timestamp + 3 * 3600000).toISOString().slice(0, 16) !== value) return null;
  return Math.floor(timestamp / 1000);
}
export function routePlatform(platform: string) {
  return ({ max: 'MAX', telegram: 'Telegram', vk: 'ВКонтакте', pikabu: 'Пикабу' } as Record<string, string>)[platform] || platform;
}
export function routeDeliveryText(dispatch: RouteDispatch) {
  if (dispatch.evidence_kind === 'fixture') return 'Локальная проверка; живая доставка не проверена';
  if (dispatch.state === 'unknown') return 'Исход неизвестен — требуется сверка';
  if (dispatch.state === 'published') {
    if (dispatch.evidence_kind === 'independent_readback' && dispatch.verified === true) return 'Доставка подтверждена';
    if (dispatch.evidence_kind === 'api_receipt' && dispatch.verified === false) return 'API-квитанция; без независимой сверки';
    if (dispatch.verified === true) return 'В истории отмечено подтверждение; новых доказательств нет';
    return 'Отправка отмечена; подтверждения нет';
  }
  return phaseText(dispatch.state);
}
export function routeAccessText(value: unknown) {
  const neutral = 'Проверка доступа завершена; доставка не проверена.';
  if (!value || typeof value !== 'object' || Array.isArray(value)) return neutral;
  const access = value as Record<string, unknown>;
  if (access.fixture === true) return 'Локальная проверка доступа; живая доставка не проверена.';
  if (['fixture', 'live_verified', 'publish', 'can_publish'].some(key =>
    key in access && typeof access[key] !== 'boolean')) return neutral;
  if ('state' in access && access.state !== 'access_only' && access.state !== 'available') return neutral;
  if ('verification' in access && access.verification !== 'access_only') return neutral;
  if ('publish' in access && 'can_publish' in access && access.publish !== access.can_publish) return neutral;
  const permission = typeof access.publish === 'boolean' ? access.publish : access.can_publish;
  const connected = access.live_verified === true || (access.fixture === false &&
    (access.verification === 'access_only' || access.state === 'access_only'));
  if (!connected || typeof permission !== 'boolean') return neutral;
  return permission
    ? 'Доступ и право публикации подтверждены. Доставка ещё не проверена.'
    : 'Соединение с площадкой подтверждено; право публикации не подтверждено. Доставка не проверена.';
}
export function routePreviewImages(value: unknown, variant: RouteVariant): RoutePreviewImage[] {
  const result = value as any;
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('EDITORIAL_ROUTE_PREVIEW_INVALID');
  if (result.variant_id !== variant.id || result.revision !== variant.revision ||
      result.content_hash !== variant.content_hash) throw new Error('APPROVAL_STALE');
  let size: number;
  try { size = new TextEncoder().encode(JSON.stringify(result)).byteLength; }
  catch { throw new Error('EDITORIAL_ROUTE_PREVIEW_INVALID'); }
  if (size > 1024 * 1024 || result.preflight_only !== true || !Array.isArray(result.images) ||
      !result.images.length || result.images.length !== variant.media_count) throw new Error('EDITORIAL_ROUTE_PREVIEW_INVALID');
  return result.images.map((image: any) => {
    if (!image || typeof image !== 'object' || image.preview !== true || image.mime !== 'image/jpeg' ||
        typeof image.data_url !== 'string' || !/^data:image\/jpeg;base64,\/9j\/[A-Za-z0-9+/]*={0,2}$/.test(image.data_url) ||
        typeof image.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(image.sha256) ||
        !Number.isSafeInteger(image.width) || !Number.isSafeInteger(image.height) || image.width < 1 || image.height < 1 ||
        image.width > 40000000 || image.height > 40000000 || image.width * image.height > 40000000) {
      throw new Error('EDITORIAL_ROUTE_PREVIEW_INVALID');
    }
    return { data_url: image.data_url, sha256: image.sha256, mime: image.mime,
      width: image.width, height: image.height, preview: true };
  });
}
export function routeDetailsResult(value: unknown, variant: RouteVariant, target: RouteTarget): RouteDetails {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('EDITORIAL_ROUTE_DETAILS_INVALID');
  const result = value as RouteDetails;
  if (result.variant_id !== variant.id || result.revision !== variant.revision || result.content_hash !== variant.content_hash ||
      result.target_id !== variant.target_id || result.target_id !== target.id || result.destination !== target.destination ||
      result.platform !== target.platform || result.route !== target.route || result.binding_hash !== target.binding_hash ||
      result.title !== variant.title) throw new Error('APPROVAL_STALE');
  let size: number;
  try { size = new TextEncoder().encode(JSON.stringify(value)).byteLength; }
  catch { throw new Error('EDITORIAL_ROUTE_DETAILS_INVALID'); }
  const record = (item: unknown): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item);
  if (size > 900000 || result.read_only !== true || typeof result.body !== 'string' ||
      typeof result.manual_approval_allowed !== 'boolean' ||
      result.manual_approval_allowed !== (variant.manual_approval_allowed !== false) ||
      !record(result.metadata) || !Array.isArray(result.media) || !result.media.every(record) ||
      result.media.length !== variant.media_count || !Array.isArray(result.sources) || !result.sources.every(record) ||
      result.sources.length !== variant.source_count) throw new Error('EDITORIAL_ROUTE_DETAILS_INVALID');
  if (result.media_policy !== undefined && result.media_policy !== null) {
    const policy = result.media_policy;
    if (target.platform !== 'vk' || target.route !== 'vk_browser' || variant.media_count !== 1 || !record(policy) ||
        Object.keys(policy).sort().join(',') !== 'description,id,label' ||
        !['exact-bytes-v1', 'exact-pixels-v1', 'native-asset-lineage-v1', 'native-uniform-edge-lineage-v1'].includes(policy.id) ||
        typeof policy.label !== 'string' || !policy.label || policy.label.length > 200 ||
        typeof policy.description !== 'string' || !policy.description || policy.description.length > 2000) throw new Error('EDITORIAL_ROUTE_DETAILS_INVALID');
  }
  return result;
}
export function routeListPath(pages: RoutePages): string {
  if ([pages.variant_page, pages.dispatch_page].some(page => !Number.isSafeInteger(page) || page < 0 || page > 1000000) ||
      !['all', 'unresolved'].includes(pages.dispatch_filter)) throw new Error('EDITORIAL_ROUTE_LIST_INVALID');
  if (pages.variant_page === 0 && pages.dispatch_page === 0 && pages.dispatch_filter === 'all') return 'routes';
  return `routes?variant_page=${pages.variant_page}&dispatch_page=${pages.dispatch_page}&dispatch_filter=${pages.dispatch_filter}`;
}
const phases: Record<string, string> = {
  idle: 'Ожидание следующего цикла', paused: 'На паузе', collecting: 'Подбор источников', preparing: 'Перевод и проверка',
  translating: 'Перевод', reviewing: 'Независимая проверка', publishing: 'Публикация', verifying: 'Проверка доставки',
  needs_attention: 'Требуется проверка ошибки', blocked: 'Цикл остановлен проверками', ready: 'Готово к следующему циклу',
  automatic_ready: 'Черновик прошёл машинные проверки', staging_automatic_draft: 'Сохранение черновика в Postiz',
  automatic_scheduling: 'Назначение даты в Postiz', automatic_pending_approval: 'Проверка точной версии и даты',
  prepared: 'Подготовлен', pending: 'Ожидает отправки', scheduled: 'Запланирован', sending: 'Отправляется',
  published: 'Доставка подтверждена', published_unverified: 'Публикация пока не подтверждена',
  unknown: 'Исход неизвестен — требуется сверка', cancelled: 'Отменён', failed: 'Ошибка', retry_wait: 'Ожидание',
  awaiting_human_review: 'Архивный черновик для ручной проверки', awaiting_postiz: 'Ожидает подтверждения Postiz',
  selecting: 'Подбор новой статьи', scheduling: 'Назначение публикации', waiting_dispatch: 'Ожидание отправки и подтверждения',
  reconciling: 'Сверка состояний Postiz и MAX', rejected: 'Материал отклонён проверками',
  hour_used: 'В этом часу новая статья уже обрабатывалась', no_source: 'Подходящий материал не найден',
  selection_slot_expired: 'Час отбора завершился — ожидаем следующий', waiting_interval: 'Ожидание допустимого интервала между доставками',
  quality_ready: 'Проверен и сохранён в запасе', staging_reserve_draft: 'Передача проверенной статьи в Postiz',
  reserve_satisfied: 'Запас пополнен', reserve_refilled: 'Пополнение запаса', reserve_empty: 'Ожидаем проверенную статью',
  waiting_source: 'Ожидание следующего подбора', reserve_disabled: 'Резерв выключен',
  preparation_deferred: 'Подготовка ждёт завершения текущей отправки',
};
export function phaseText(phase?: string) { return phases[phase || ''] || phase || '—'; }
export function sourceText(source?: string) {
  return ({ nasa_svs: 'NASA SVS', nasa_svs_articles: 'NASA SVS', open_science: 'Открытая наука', noirlab_iotw: 'NOIRLab', esahubble_potw: 'ESA/Hubble', reddit: 'Reddit' } as Record<string, string>)[source || ''] || source || '—';
}
async function api(path: string, method = 'GET', body?: unknown, signal?: AbortSignal) {
  // Нативный fetch не переносит cookie в auth-header и не раскрывает внутренний ключ.
  const response = await fetch(`/api/editorial/${path}`, {
    method, credentials: 'same-origin', cache: 'no-store', signal,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  });
  let result: any;
  try { result = await response.json(); } catch { throw new Error('EDITORIAL_RESPONSE_INVALID'); }
  if (!response.ok) throw new Error(typeof result?.code === 'string' ? result.code : 'EDITORIAL_REQUEST_FAILED');
  return result;
}
export function EditorialRecords({ records }: { records: Recent[] }) {
  return <div className={styles.tableWrap}><table className={styles.table}>
    <thead><tr><th>Материал</th><th>Источник</th><th>Состояние</th><th>Время</th></tr></thead>
    <tbody>{records.map(record => <tr key={record.id}>
      <td>{safeSourceUrl(record.url) ? <a href={safeSourceUrl(record.url)} target="_blank" rel="noopener noreferrer">{record.title || record.id}</a> : record.title || record.id}</td>
      <td>{sourceText(record.source)}</td><td>{phaseText(record.state)}{record.error && <span className={styles.detail}>{errorText(record.error)}</span>}</td><td>{readableTime(record.updated_at)}</td>
    </tr>)}</tbody></table>{!records.length && <p className={styles.muted}>Записей пока нет. История появится после первого цикла.</p>}</div>;
}

export function NasaRatingField({ mixed = false }: { mixed?: boolean } = {}) {
  return <label className={styles.field}><span>{mixed ? 'Рейтинг источника' : 'Рейтинг NASA'}</span>
    <input type="number" value="0" disabled readOnly />
    <span className={styles.muted}>Источник не предоставляет оценку</span>
  </label>;
}

type AdminHandler = (body: Record<string, unknown>) => Promise<any>;
export const adminErrorCode = (reason: unknown, fallback = 'EDITORIAL_REQUEST_FAILED') => reason instanceof Error &&
  Object.prototype.hasOwnProperty.call(errors, reason.message) ? reason.message : fallback;
export function ChannelSetup({ setup, pending, onAdmin }: { setup: RouteSetup; pending: boolean; onAdmin: AdminHandler }) {
  const [selected, setSelected] = useState('');
  const [form, setForm] = useState<ChannelForm>(channelForm());
  const [configHash, setConfigHash] = useState(setup.config_hash);
  const [bindingHash, setBindingHash] = useState<string | null>(null);
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const [login, setLogin] = useState<{ target_id: string; login_url: string; expires_at: number } | null>(null);
  const current = setup.targets.find(target => target.id === selected);
  const change = (key: keyof ChannelForm, value: string | boolean) => setForm(previous => ({ ...previous, [key]: value }));
  const select = (id: string) => { const target = setup.targets.find(item => item.id === id);
    setSelected(id); setForm(channelForm(target)); setConfigHash(setup.config_hash); setBindingHash(target?.binding_hash ?? null);
    setToken(''); setLogin(null); setError(''); };
  const stale = configHash !== setup.config_hash || Boolean(current && current.binding_hash !== bindingHash);
  const configure = async () => {
    setError(''); setLogin(null);
    try { const target = channelTarget(form);
      if (!setup.profiles.some(profile => profile.id === target.profile_id && profile.enabled && profile.publication_mode !== 'dry_run')) throw new Error('EDITORIAL_CHANNEL_INVALID');
      if (target.platform === 'max' && !setup.max_channels.some(channel => channel.id === target.destination)) throw new Error('BROWSER_CHANNEL_NOT_ALLOWED');
      if (stale) throw new Error('ROUTE_ADMIN_CONFLICT');
      const result = await onAdmin({ action: 'configure_target', id: form.id, expected_config_hash: configHash,
        expected_binding_hash: bindingHash, target });
      setSelected(form.id); setConfigHash(result.config_hash); setBindingHash(result.binding_hash); setToken('');
    } catch (reason) { setError(adminErrorCode(reason, 'EDITORIAL_CHANNEL_INVALID')); }
  };
  return <div className={styles.workspace}>
    <h3>Подключить или изменить канал</h3>
    <label className={styles.field}><span>Подключение</span><select value={selected} disabled={pending} onChange={event => select(event.target.value)}>
      <option value="">Новый канал</option>{setup.targets.map(target => <option value={target.id} key={target.id}>
        {routePlatform(target.platform)} · {target.destination} ({target.id})</option>)}</select></label>
    {current && <div className={styles.connectionStatus}>
      <p>{current.paused ? 'Канал на паузе' : 'Канал работает'} · {current.enabled ? 'Подключение включено' : 'Подключение выключено'}</p>
      <p className={styles.muted}>{current.diagnostics.local_prerequisites === true ? 'Локальные проверки подключения пройдены. Доставку проверяют отдельно.' : 'Подключение требует настройки или проверки.'}</p>
      {current.diagnostics.blocked_reasons?.map(code => <p key={code} className={styles.muted}>{errorText(code)}</p>)}
    </div>}
    {error && <div role="alert" className={styles.notice}>{errorText(error)}</div>}
    {stale && <p className={styles.notice}>Подключения изменились. Выберите канал заново, чтобы загрузить актуальные настройки перед сохранением.</p>}
    <form onSubmit={event => { event.preventDefault(); void configure(); }}>
      <div className={styles.grid}>
        <label className={styles.field}><span>Площадка</span><select value={form.platform} disabled={pending || Boolean(current)}
          onChange={event => { setForm(previous => ({ ...channelForm(), id: previous.id, profile_id: previous.profile_id,
            platform: event.target.value as ManagedPlatform })); setToken(''); }}>
          <option value="max">MAX</option><option value="telegram">Telegram</option><option value="vk">ВКонтакте</option></select></label>
        <label className={styles.field}><span>Идентификатор канала</span><input value={form.id} disabled={pending || Boolean(current)} maxLength={64}
          placeholder="science_telegram" onChange={event => change('id', event.target.value)} required />
          <span className={styles.muted}>Латинские строчные буквы, цифры, дефис или подчёркивание; начинается с буквы.</span></label>
        <label className={styles.field}><span>Редакционный профиль</span><select value={form.profile_id} disabled={pending} required onChange={event => change('profile_id', event.target.value)}>
          <option value="">Выберите профиль</option>{setup.profiles.map(profile => <option value={profile.id} key={profile.id}
            disabled={!profile.enabled || profile.publication_mode === 'dry_run'}>{profile.name || profile.id}{!profile.enabled ? ' · выключен' : profile.publication_mode === 'dry_run' ? ' · без отправки' : ''}</option>)}</select></label>
        <label className={styles.field}><span>{form.platform === 'vk' ? 'ID личной страницы получателя' : form.platform === 'telegram' ? 'ID или @имя Telegram-канала' : 'Числовой ID канала MAX'}</span>
          {form.platform === 'max' ? <select value={form.destination} disabled={pending} required onChange={event => change('destination', event.target.value)}>
            <option value="">Выберите разрешённый канал</option>{setup.max_channels.map(channel => <option key={channel.id} value={channel.id}>{channel.name} · {channel.id}</option>)}</select>
            : <input value={form.destination} disabled={pending} maxLength={200} onChange={event => change('destination', event.target.value)} required />}
          {form.platform === 'max' && <span className={styles.muted}>В списке только ранее разрешённые каналы MAX. Другой канал подключается отдельной настройкой владельца.</span>}</label>
        {form.platform === 'vk' && <label className={styles.field}><span>ID владельца VK</span><input inputMode="numeric" value={form.owner} disabled={pending}
          onChange={event => change('owner', event.target.value)} required /><span className={styles.muted}>Должен совпадать с владельцем выбранной личной страницы.</span></label>}
        <label className={styles.field}><span>Лимит знаков (необязательно)</span><input type="number" min={1} max={40000} step={1} value={form.max_chars}
          disabled={pending} onChange={event => change('max_chars', event.target.value)} /></label>
        {form.platform === 'telegram' && <label className={styles.field}><span>Изображение и длинный текст</span><select value={form.media_layout} disabled={pending}
          onChange={event => change('media_layout', event.target.value)}><option value="auto">Подпись, а при превышении лимита — отдельный текст</option>
          <option value="atomic">Только одно сообщение с подписью</option><option value="photo_then_text">Изображение, затем текст</option></select></label>}
      </div>
      <details className={styles.advanced}><summary>Дополнительные параметры</summary><div className={styles.grid}>
        {form.platform === 'telegram' && <label className={styles.field}><span>Ожидание ответа Telegram, секунд</span><input type="number" min={1} max={120} step={1}
          value={form.request_timeout_seconds} disabled={pending} onChange={event => change('request_timeout_seconds', event.target.value)} /></label>}
        {form.platform === 'vk' && <label className={styles.field}><span>Время операции VK, секунд</span><input type="number" min={30} max={600} step={1}
          value={form.operation_timeout_seconds} disabled={pending} onChange={event => change('operation_timeout_seconds', event.target.value)} /></label>}
        <label className={styles.consent}><input type="checkbox" checked={form.enabled} disabled={pending} onChange={event => change('enabled', event.target.checked)} />Подключение включено</label>
      </div></details>
      <div className={styles.actions}><button type="submit" className={styles.button} disabled={pending || stale || Boolean(current && !current.capabilities.can_configure)}>Сохранить подключение на паузе</button>
        <span className={styles.muted}>После изменения потребуется перезапуск редакционного сервиса и новая проверка доступа.</span></div>
    </form>
    {current?.platform === 'telegram' && <form className={styles.secretForm} autoComplete="off" onSubmit={async event => {
      event.preventDefault(); setError(''); const value = token; setToken('');
      try { if (stale || !bindingHash) throw new Error('ROUTE_ADMIN_CONFLICT');
        await onAdmin({ action: 'set_secret', id: current.id, expected_binding_hash: bindingHash, value }); }
      catch (reason) { setError(adminErrorCode(reason)); }
      finally { setToken(''); }
    }}><label className={styles.field}><span>Токен Telegram-бота</span><input type="password" value={token} disabled={pending || !current.capabilities.can_set_secret}
        autoComplete="new-password" spellCheck={false} onChange={event => setToken(event.target.value)} />
        <span className={styles.muted}>{current.diagnostics.credentials_configured === true ? 'Токен уже сохранён. Можно заменить его новым.' : 'Введите токен бота с правом публикации в выбранном канале.'} Значение не показывается после сохранения.</span></label>
      <button type="submit" className={styles.button} disabled={pending || stale || !current.capabilities.can_set_secret || !token}>Сохранить токен</button></form>}
    {current?.capabilities.can_login && <div className={styles.actions}>
      <button type="button" className={styles.button} disabled={pending || stale} onClick={async () => { setError(''); setLogin(null);
        try { if (stale || !bindingHash) throw new Error('ROUTE_ADMIN_CONFLICT');
          const value = await onAdmin({ action: 'prepare_login', id: current.id, expected_binding_hash: bindingHash });
          if (value?.target_id !== current.id || value?.login_url !== '/owner-browser/' || value?.live_verified !== false) throw new Error('EDITORIAL_RESPONSE_INVALID'); setLogin(value);
        } catch (reason) { setError(adminErrorCode(reason)); }
      }}>Подготовить вход в {routePlatform(current.platform)}</button>
      {login?.target_id === current.id && login.expires_at > Date.now() / 1000 && <a href={login.login_url} className={styles.button}>Открыть вход владельца</a>}
      <span className={styles.muted}>Окно предназначено только для входа в выбранный аккаунт. Затем закройте его и отдельно проверьте доступ.</span>
    </div>}
  </div>;
}

export function OriginalComposer({ setup, pending, editing, onAdmin, onCancel }: { setup: RouteSetup; pending: boolean;
  editing: RouteDetails | null; onAdmin: AdminHandler; onCancel: () => void }) {
  const [targetId, setTargetId] = useState(editing?.target_id || '');
  const [title, setTitle] = useState(editing?.title || ''), [body, setBody] = useState(editing?.body || '');
  const [media, setMedia] = useState<OriginalMedia[]>((editing?.media || []).map(originalMedia));
  const [attested, setAttested] = useState(false), [uploading, setUploading] = useState(false), [error, setError] = useState('');
  const [preview, setPreview] = useState('');
  const target = setup.targets.find(item => item.id === targetId), busy = pending || uploading;
  const allowed = Boolean(target?.enabled && target.capabilities.can_create_original && (!editing || editing.binding_hash === target.binding_hash));
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);
  useEffect(() => { setAttested(false); }, [targetId, target?.binding_hash, editing?.content_hash]);
  const selectImage = async (file?: File) => {
    setAttested(false); setError(''); if (!file || !target) return;
    setUploading(true);
    try {
      if (!['image/png', 'image/jpeg'].includes(file.type) || file.size < 1 || file.size > 10485760) throw new Error('EDITORIAL_UPLOAD_INVALID');
      const bytes = new Uint8Array(await file.arrayBuffer()), sha256 = imageSha256(bytes);
      let binary = ''; for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      const value = await onAdmin({ action: 'upload_media', id: target.id, expected_binding_hash: target.binding_hash,
        mime: file.type, sha256, data_base64: btoa(binary) });
      const saved = originalMedia(value?.media);
      if (saved.sha256 !== sha256 || saved.mime !== file.type || value?.external_mutations !== 0) throw new Error('EDITORIAL_ORIGINAL_MEDIA_INVALID');
      // Просмотр использует проверенные байты с явным растровым MIME.
      const image = saved.mime === 'image/png'
        ? new Blob([bytes], { type: 'image/png' })
        : new Blob([bytes], { type: 'image/jpeg' });
      setMedia([saved]); setPreview(URL.createObjectURL(image));
    } catch (reason) { setError(adminErrorCode(reason, 'EDITORIAL_UPLOAD_INVALID')); }
    finally { setUploading(false); }
  };
  return <div className={styles.workspace}><h3>{editing ? `Редактировать материал · версия ${editing.revision}` : 'Новый авторский материал'}</h3>
    <p className={styles.muted}>Сохранение создаёт черновик для проверки. Дату и согласование вы зададите отдельно для сохранённой версии.</p>
    {editing && <p className={styles.notice}>Изменение текста или изображения сбросит прежнюю проверку и согласование. История версий сохраняется.</p>}
    {error && <div role="alert" className={styles.notice}>{errorText(error)}</div>}
    <form onSubmit={async event => { event.preventDefault(); setError('');
      try {
        if (!target || !allowed || !attested) throw new Error('OWNER_ORIGINAL_ATTESTATION_REQUIRED');
        const metadata = Object.fromEntries(Object.entries(editing?.metadata || {}).filter(([key]) => ['tags', 'community', 'ai_generated'].includes(key)));
        const original = originalInput(title, body, media, metadata);
        await onAdmin({ action: editing ? 'revise_original' : 'create_original', id: editing?.variant_id || target.id,
          expected_binding_hash: target.binding_hash, original, attest_original: true,
          ...(editing ? { revision: editing.revision, content_hash: editing.content_hash } : {}) });
        setAttested(false); setTitle(''); setBody(''); setMedia([]); setPreview(''); onCancel();
      } catch (reason) { setAttested(false); setError(adminErrorCode(reason, 'EDITORIAL_ORIGINAL_INVALID')); }
    }}>
      <label className={styles.field}><span>Канал материала</span><select value={targetId} disabled={busy || Boolean(editing)} required onChange={event => {
        setTargetId(event.target.value); setAttested(false); setMedia([]); setPreview('');
      }}><option value="">Выберите канал</option>{setup.targets.map(item => <option key={item.id} value={item.id}
          disabled={!item.enabled || !item.capabilities.can_create_original}>{routePlatform(item.platform)} · {item.destination}</option>)}</select></label>
      <label className={styles.field}><span>Заголовок</span><input value={title} disabled={busy} maxLength={300} required onChange={event => { setTitle(event.target.value); setAttested(false); }} /></label>
      <label className={styles.field}><span>Текст на русском</span><textarea className={styles.originalBody} value={body} disabled={busy} maxLength={40000} required onChange={event => { setBody(event.target.value); setAttested(false); }} /></label>
      <label className={styles.field}><span>Изображение PNG или JPEG, до 10 МБ</span><input type="file" accept="image/png,image/jpeg" disabled={busy || !target?.capabilities.can_upload_media}
        onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; void selectImage(file); }} /></label>
      {uploading && <p role="status">Сохраняем исходное изображение…</p>}
      {media.length > 0 && <div>{preview && <figure className={styles.imagePreview}><img src={preview} alt="Выбранное изображение для нового черновика" /></figure>}
        <p className={styles.muted}>К материалу прикреплено одно исходное изображение. Перед согласованием просмотрите серверную версию.</p>
        <button type="button" className={styles.button} disabled={busy} onClick={() => { setMedia([]); setPreview(''); setAttested(false); }}>Убрать изображение</button></div>}
      <label className={styles.consent}><input type="checkbox" checked={attested} disabled={busy || !allowed}
        onChange={event => setAttested(event.target.checked)} /><span>Это мой авторский текст; у меня есть права на выбранное изображение и его публикацию. Это подтверждение не назначает отправку.</span></label>
      <div className={styles.actions}><button type="submit" className={`${styles.button} ${styles.primary}`} disabled={busy || !allowed || !attested}>Сохранить черновик для проверки</button>
        {editing && <button type="button" className={styles.button} disabled={busy} onClick={onCancel}>Отменить редактирование</button>}</div>
    </form>
  </div>;
}

export function EditorialWorkspace({ pending, editing, onChanged, onCancel }: { pending: boolean; editing: RouteDetails | null;
  onChanged: () => Promise<void>; onCancel: () => void }) {
  const [setup, setSetup] = useState<RouteSetup | null>(null), [busy, setBusy] = useState(false);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const mounted = useRef(true);
  const reload = useCallback(async () => { const result = await api('route-setup', 'GET');
    if (mounted.current) setSetup(result); }, []);
  useEffect(() => { mounted.current = true; void reload().catch(reason => { if (mounted.current) setError(adminErrorCode(reason)); });
    return () => { mounted.current = false; }; }, [reload]);
  const admin: AdminHandler = async body => {
    setBusy(true); setError(''); setNotice('');
    try { const result = await api('route-admin', 'POST', body);
      if (body.action !== 'upload_media') {
        await reload(); await onChanged();
        setNotice(['configure_target', 'set_secret'].includes(String(body.action))
          ? 'Настройки сохранены, канал оставлен на паузе. Нажмите «Применить изменения», затем проверьте доступ.'
          : body.action === 'prepare_login' ? 'Окно входа подготовлено для выбранного канала. Публикация не выполнялась.'
          : 'Черновик сохранён. Откройте его полную версию, выполните проверку и отдельно согласуйте дату.');
      }
      return result;
    } catch (reason) { setError(adminErrorCode(reason)); throw new Error(adminErrorCode(reason)); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <section className={styles.card} aria-labelledby="workspace-heading"><h2 id="workspace-heading">Подключения и свои материалы</h2>
    <p className={styles.muted}>MAX, Telegram и ВКонтакте. Подключите канал, подготовьте материал и согласуйте точную версию перед отправкой.</p>
    {error && <div role="alert" className={styles.notice}>{errorText(error)}</div>}{notice && <p role="status" className={styles.notice}>{notice}</p>}
    <button type="button" className={styles.button} disabled={pending || busy} onClick={() => { setError(''); void reload().catch(reason => setError(adminErrorCode(reason))); }}>Обновить подключения</button>
    {setup ? <>{setup.restart.required && <div className={styles.notice}><p>Настройки сохранены и ожидают применения. Каналы останутся на паузе.</p>
      {setup.restart.blocked_reasons.map(code => <p key={code}>{errorText(code)}</p>)}
      <button type="button" className={styles.button} disabled={pending || busy || Boolean(editing) || !setup.restart.can_request} onClick={async () => {
        setBusy(true); setError(''); setNotice('');
        try { await api('route-admin', 'POST', { action: 'request_restart', id: 'server', expected_binding_hash: null, expected_config_hash: setup.config_hash });
          setSetup(previous => previous && ({ ...previous, restart: { ...previous.restart, can_request: false } }));
          setNotice('Запрос принят. Дождитесь перезапуска и нажмите «Обновить подключения». Отправка не включается.');
        } catch (reason) { setError(adminErrorCode(reason)); } finally { setBusy(false); }
      }}>Применить изменения</button><p className={styles.muted}>Редакционный сервис кратко перезапустится. История и черновики сохранятся.</p></div>}
      <ChannelSetup setup={setup} pending={pending || busy || Boolean(editing)} onAdmin={admin} />
      <OriginalComposer key={editing ? `${editing.variant_id}:${editing.revision}:${editing.content_hash}` : 'new'} setup={setup} pending={pending || busy}
        editing={editing} onAdmin={admin} onCancel={onCancel} /></> : <p className={styles.muted}>Загрузка подключений…</p>}
  </section>;
}

export function RouteDraft({ variant, target, pending, onAction, onPreview, details, onDetails, onEdit, editing = false }: {
  variant: RouteVariant; target: RouteTarget; pending: boolean; onAction: RouteActionHandler; onPreview?: RoutePreviewHandler;
  details?: RouteDetails; onDetails?: RouteDetailsHandler; onEdit?: (details: RouteDetails) => void; editing?: boolean;
}) {
  const [schedule, setSchedule] = useState('');
  const [consent, setConsent] = useState(false);
  const [checkedHash, setCheckedHash] = useState<string | null>(null);
  const [preview, setPreview] = useState<RoutePreviewImage[] | null>(null);
  const [previewError, setPreviewError] = useState('');
  const dueAt = moscowSchedule(schedule);
  useEffect(() => { setConsent(false); setCheckedHash(null); setPreview(null); setPreviewError(''); },
    [variant.id, variant.revision, variant.content_hash, target.id, target.destination,
      target.binding_hash, target.paused, target.enabled, details?.content_hash, editing,
      details?.media_policy?.id, details?.media_policy?.label, details?.media_policy?.description]);
  let exact: RouteDetails | null = null;
  try { if (details) exact = routeDetailsResult(details, variant, target); } catch { /* Старая версия не открывает согласование. */ }
  const checked = exact !== null && checkedHash === variant.content_hash;
  const imageReviewed = variant.media_count === 0 || preview !== null;
  const policyReviewed = target.route !== 'vk_browser' || variant.media_count === 0 || Boolean(exact?.media_policy);
  return <article className={styles.routeDraft} aria-label={`Черновик: ${variant.title}`}>
    <div className={styles.routeHeading}><h3>{variant.title || 'Без заголовка'}</h3>
      <span className={styles.badge}>{routePlatform(target.platform)} · Версия {variant.revision}</span></div>
    <p className={styles.muted}>Канал: {target.destination} · Вложений: {variant.media_count} · Источников: {variant.source_count}</p>
    {!exact ? <div><p className={styles.muted}>Полный текст, вложения и параметры этой версии ещё не загружены. Согласование недоступно.</p>
      <button type="button" className={styles.button} disabled={pending || !onDetails}
        onClick={() => onDetails?.(variant)}>Открыть полный материал</button></div> : <>
    {variant.media_count > 0 && <div>
      <button type="button" className={styles.button} disabled={pending || !onPreview} onClick={async () => {
        setConsent(false); setPreview(null); setPreviewError('');
        try {
          const result = await onPreview?.(variant);
          setPreview(routePreviewImages(result, variant));
        } catch (reason) { setPreviewError(reason instanceof Error ? reason.message : 'EDITORIAL_ROUTE_PREVIEW_INVALID'); }
      }}>Показать изображение</button>
      {previewError && <div role="alert" className={styles.notice}>{errorText(previewError)}</div>}
      {preview?.map((image, index) => <figure key={image.sha256} className={styles.imagePreview}>
        <img src={image.data_url} alt={`Предпросмотр изображения ${index + 1}: ${variant.title}`} decoding="async" />
        <figcaption className={styles.muted}>Предпросмотр · {image.width} × {image.height}. На площадку отправляется исходный файл этой версии, его SHA проверен сервером.</figcaption>
      </figure>)}
      {!imageReviewed && <p className={styles.muted}>Перед согласованием покажите изображение этой версии.</p>}
    </div>}
    <div className={styles.fullBody}>{exact.body}</div>
    {exact.media_policy && <div className={styles.connectionStatus}><h4>Изображение: {exact.media_policy.label}</h4>
      <p className={styles.muted}>{exact.media_policy.description}</p></div>}
    {!policyReviewed && <p role="alert" className={styles.notice}>Условия проверки изображения для этой версии не подтверждены. Согласование недоступно.</p>}
    {variant.origin === 'owner_original' && ['max', 'telegram', 'vk'].includes(target.platform) && onEdit &&
      <button type="button" className={styles.button} disabled={pending || editing} onClick={() => {
        setConsent(false); setCheckedHash(null); setPreview(null); if (exact) onEdit(exact);
      }}>Редактировать авторский материал</button>}
    <h4>Параметры публикации</h4><pre className={styles.fullBody}>{JSON.stringify(exact.metadata, null, 2)}</pre>
    <h4>Вложения этой версии</h4><pre className={styles.fullBody}>{JSON.stringify(exact.media, null, 2)}</pre>
    <h4>Источники этой версии</h4><pre className={styles.fullBody}>{JSON.stringify(exact.sources, null, 2)}</pre>
    <div className={styles.actions}><button type="button" className={styles.button} disabled={pending}
      onClick={async () => { setConsent(false); setCheckedHash(null);
        if (await onAction({ action: 'preflight', id: variant.id })) setCheckedHash(variant.content_hash); }}>
      Проверить перед согласованием</button>
      {checked && <span className={styles.muted}>Проверка этой версии завершена</span>}</div>
    <label className={styles.field}><span>Дата и время публикации (Москва, UTC+3)</span>
      <input type="datetime-local" step="60" value={schedule} disabled={pending}
        onChange={event => { setSchedule(event.target.value); setConsent(false); }} />
      {schedule && dueAt === null && <span role="status" className={styles.muted}>Укажите корректную будущую дату по Москве.</span>}
    </label>
    <label className={styles.consent}><input type="checkbox" checked={consent}
      disabled={pending || !checked || !imageReviewed || !policyReviewed || dueAt === null || !target.enabled || target.paused}
      onChange={event => setConsent(event.target.checked)} />
      <span>Я проверил материал и согласую версию {variant.revision} для канала {target.destination}
        {dueAt !== null ? ` на ${readableTime(dueAt)} (МСК)` : ' на выбранную дату'}.</span></label>
    <div className={styles.actions}><button type="button" className={`${styles.button} ${styles.primary}`}
      disabled={pending || !target.enabled || target.paused || !checked || !imageReviewed || !policyReviewed || !consent || dueAt === null}
      onClick={async () => {
        const exactDate = moscowSchedule(schedule);
        if (pending || !target.enabled || target.paused || !consent || !checked || !imageReviewed || !policyReviewed || exactDate === null) return;
        setConsent(false);
        await onAction({ action: 'approve', id: variant.id, revision: variant.revision,
          content_hash: variant.content_hash, due_at: exactDate });
      }}>Согласовать версию и дату</button></div>
    {target.paused && <p className={styles.muted}>Канал на паузе. Перед согласованием возобновите его отдельной кнопкой.</p>}
    </>}
  </article>;
}

export function EditorialRoutes({ inventory, pending, error, onAction, onPreview, detailsById = {}, onDetails, onPage, onEdit, editingId }: {
  inventory: RouteInventory | null; pending: boolean; error?: string; onAction: RouteActionHandler; onPreview?: RoutePreviewHandler;
  detailsById?: Record<string, RouteDetails>; onDetails?: RouteDetailsHandler; onPage?: RoutePageHandler;
  onEdit?: (details: RouteDetails) => void; editingId?: string;
}) {
  const targets = (inventory?.targets || []).filter(target => managedRoutes.has(target.route));
  const variants = (inventory?.variants || []).filter(variant => variant.manual_approval_allowed !== false &&
    ['review_pending', 'ready', 'draft'].includes(variant.state));
  const dispatches = inventory?.dispatches || [];
  const pagination = inventory?.pagination;
  return <section className={styles.card} aria-labelledby="routes-heading"><h2 id="routes-heading">Каналы и публикации</h2>
    <p className={styles.muted}>MAX, Telegram, ВКонтакте и Пикабу. Настройка подключения и проверка доступа ещё не подтверждают доставку.</p>
    {error && <div role="alert" className={styles.notice}>{errorText(error)}</div>}
    {!inventory ? <p className={styles.muted}>Загрузка каналов…</p> : <>
      <div className={styles.routesGrid}>{targets.map(target => <article key={target.id} className={styles.routeTarget}>
        <div className={styles.routeHeading}><h3>{routePlatform(target.platform)}</h3>
          <span className={styles.badge}>{!target.enabled ? 'Выключен' : target.paused ? 'На паузе' : 'Отправка разрешена'}</span></div>
        <p className={styles.destination}>{target.destination}</p>
        <p className={styles.muted}>{target.adapter_mode === 'fake' ? 'Режим локальной проверки' : 'Настройки подключения'}
          {target.credentials_configured !== undefined && ` · Доступ ${target.credentials_configured ? 'настроен' : 'не настроен'}`}
          {target.owner_configured !== undefined && ` · Владелец ${target.owner_configured ? 'указан' : 'не указан'}`}
          {target.profile_exists !== undefined && ` · Профиль браузера ${target.profile_exists ? 'сохранён' : 'не найден'}`}
          {target.dom_contract_verified !== undefined && ` · Редактор ${target.dom_contract_verified ? 'проверен' : 'требует проверки'}`}</p>
        <div className={styles.actions}><button type="button" className={styles.button} disabled={pending}
          onClick={() => onAction({ action: 'inspect', id: target.id })}>Проверить доступ</button>
          <button type="button" className={styles.button} disabled={pending || !target.enabled}
            onClick={() => onAction({ action: target.paused ? 'resume' : 'pause', id: target.id })}>
            {target.paused ? 'Возобновить канал' : 'Приостановить канал'}</button></div>
      </article>)}</div>
      {!targets.length && <p className={styles.muted}>Каналы ещё не настроены.</p>}
      <h3 className={styles.subheading}>Черновики для согласования</h3>
      {pagination && <div className={styles.actions}><span className={styles.muted}>
        Страница материалов {pagination.variant_page + 1}; всего {pagination.variant_count}, ожидают проверки {pagination.variant_active_count}.</span>
        <button type="button" className={styles.button} disabled={pending || !onPage || pagination.variant_page === 0}
          onClick={() => onPage?.({ ...pagination, variant_page: pagination.variant_page - 1 })}>Предыдущие материалы</button>
        <button type="button" className={styles.button} disabled={pending || !onPage || !pagination.variant_has_next}
          onClick={() => onPage?.({ ...pagination, variant_page: pagination.variant_page + 1 })}>Следующие материалы</button></div>}
      {variants.map(variant => { const target = targets.find(item => item.id === variant.target_id);
        return target ? <RouteDraft key={`${variant.id}:${variant.revision}:${variant.content_hash}:${target.binding_hash}`}
          variant={variant} target={target} pending={pending} onAction={onAction} onPreview={onPreview}
          details={detailsById[variant.id]} onDetails={onDetails} onEdit={onEdit} editing={editingId === variant.id} /> : null; })}
      {!variants.length && <p className={styles.muted}>На этой странице нет черновиков для ручного согласования.</p>}
      <h3 className={styles.subheading}>История отправок</h3>
      {pagination && <div className={styles.actions}><span className={styles.muted}>
        Страница отправок {pagination.dispatch_page + 1}; в списке {pagination.dispatch_count}, требуют сверки {pagination.dispatch_active_count}.</span>
        <button type="button" className={styles.button} disabled={pending || !onPage}
          onClick={() => onPage?.({ ...pagination, dispatch_page: 0,
            dispatch_filter: pagination.dispatch_filter === 'unresolved' ? 'all' : 'unresolved' })}>
          {pagination.dispatch_filter === 'unresolved' ? 'Все отправки' : 'Только незавершённые'}</button>
        <button type="button" className={styles.button} disabled={pending || !onPage || pagination.dispatch_page === 0}
          onClick={() => onPage?.({ ...pagination, dispatch_page: pagination.dispatch_page - 1 })}>Предыдущие отправки</button>
        <button type="button" className={styles.button} disabled={pending || !onPage || !pagination.dispatch_has_next}
          onClick={() => onPage?.({ ...pagination, dispatch_page: pagination.dispatch_page + 1 })}>Следующие отправки</button></div>}
      <div className={styles.tableWrap}><table className={styles.table}>
        <thead><tr><th>Материал и канал</th><th>Состояние</th><th>Дата (МСК)</th><th>Проверка</th></tr></thead>
        <tbody>{dispatches.map(dispatch => {
          const destination = typeof dispatch.destination === 'string' && dispatch.destination ? dispatch.destination : 'Адресат не сохранён';
          const platform = typeof dispatch.platform === 'string' && dispatch.platform ? routePlatform(dispatch.platform) : null;
          const link = dispatch.evidence_kind === 'fixture' ? null : safeSourceUrl(dispatch.permalink || undefined);
          return <tr key={dispatch.id}><td>{dispatch.title || 'Без заголовка'}
            <span className={styles.detail}>{platform ? `${platform} · ${destination}` : destination}</span></td>
            <td>{routeDeliveryText(dispatch)}{dispatch.error && <span className={styles.detail}>{errorText(dispatch.error)}</span>}</td>
            <td>{readableTime(dispatch.due_at)}</td><td>
              {link && <a href={link} target="_blank" rel="noopener noreferrer">Открыть публикацию</a>}
              {dispatch.state === 'unknown' && <button type="button" className={styles.button}
                disabled={pending} onClick={() => onAction({ action: 'reconcile', id: dispatch.id })}>Сверить результат</button>}
              {dispatch.state === 'unknown' && !dispatch.external_id && <span className={styles.detail}>Сверка использует сохранённые доказательства. Повторной отправки нет.</span>}
            </td></tr>; })}</tbody>
      </table>{!dispatches.length && <p className={styles.muted}>На этой странице отправок нет.</p>}</div>
    </>}
  </section>;
}

type MonitorCommand = {
  command_id: string; action: string; state: string; phase?: string;
  created_at?: number; updated_at?: number; ready?: number; goal?: number;
};
type MonitorState = {
  available: boolean; read_only: true; enabled?: boolean; ledger_available?: boolean;
  freshness?: 'fresh' | 'stale' | 'unobserved';
  commands?: { enabled: boolean; paired: boolean; contract_verified: boolean;
    receiver_state: 'ready' | 'blocked' | 'unobserved'; receiver_heartbeat_at?: number;
    receiver_freshness: 'fresh' | 'stale' | 'unobserved'; history_available: boolean; recent: MonitorCommand[] };
  observer?: { heartbeat_at?: number; source_heartbeat_at?: number; sender_heartbeat_at?: number };
  reports?: { last: { state: string; created_at?: number } | null };
};
export function monitorStatusResult(value: unknown): MonitorState {
  const record = (item: unknown): item is Record<string, any> => !!item && typeof item === 'object' && !Array.isArray(item);
  if (!record(value) || typeof value.available !== 'boolean' || value.read_only !== true) throw new Error('MONITOR_RESPONSE_INVALID');
  if (!value.available) return { available: false, read_only: true };
  if (typeof value.enabled !== 'boolean' || typeof value.ledger_available !== 'boolean' ||
      !['fresh', 'stale', 'unobserved'].includes(value.freshness) || !record(value.commands) ||
      ['enabled', 'paired', 'contract_verified'].some(key => typeof value.commands[key] !== 'boolean') ||
      !record(value.observer) || !record(value.reports)) throw new Error('MONITOR_RESPONSE_INVALID');
  const stamp = (item: unknown): number | undefined => typeof item === 'number' && Number.isFinite(item) && item >= 0 ? item : undefined;
  const last = value.reports.last;
  const reportStates = ['pending', 'sending', 'unknown', 'confirmed', 'failed', 'cancelled', 'expired'];
  const receiverAt = stamp(value.commands.receiver_heartbeat_at);
  const receiverState = ['ready', 'blocked', 'unobserved'].includes(value.commands.receiver_state) ? value.commands.receiver_state : 'unobserved';
  const receiverFreshness = receiverAt !== undefined && ['fresh', 'stale'].includes(value.commands.receiver_freshness)
    ? value.commands.receiver_freshness : 'unobserved';
  const actions = ['status', 'reserve', 'errors', 'sources', 'refill', 'pause', 'reconcile', 'unblock', 'source_propose', 'source_settings',
    'help', 'tasks', 'task', 'cancel', 'resume_refill', 'resume_project', 'diagnostics'];
  const commandStates = ['queued', 'running', 'done', 'blocked', 'unknown', 'cancelled'];
  const commandPhases = ['paused', 'reserve_disabled', 'blocked', 'worker_busy', 'waiting_source', 'reserve_satisfied',
    'reserve_refilled', 'no_source', 'preparation_deferred', 'selecting', 'translating', 'reviewing', 'repairing',
    'preparing', 'quality_ready', 'reserve_empty', 'waiting_interval', 'waiting_services', 'cancelled', 'cancellation_requested'];
  const integer = (item: unknown, low: number, high: number): number | undefined => typeof item === 'number' &&
    Number.isSafeInteger(item) && item >= low && item <= high ? item : undefined;
  const recent: MonitorCommand[] = (Array.isArray(value.commands.recent) ? value.commands.recent : []).slice(0, 10)
    .filter(record).map(item => ({ command_id: typeof item.command_id === 'string' && /^[a-f0-9]{32}$/.test(item.command_id)
      ? item.command_id : 'unknown', action: actions.includes(item.action) ? item.action : 'unknown',
      state: commandStates.includes(item.state) ? item.state : 'unknown', phase: commandPhases.includes(item.phase) ? item.phase : undefined,
      created_at: stamp(item.created_at), updated_at: stamp(item.updated_at),
      ready: ['refill', 'reserve'].includes(item.action) ? integer(item.ready, 0, 24) : undefined,
      goal: item.action === 'refill' ? integer(item.goal, 1, 12) : undefined }));
  return { available: true, read_only: true, enabled: value.enabled, ledger_available: value.ledger_available,
    freshness: value.freshness, commands: { enabled: value.commands.enabled, paired: value.commands.paired,
      contract_verified: value.commands.contract_verified, receiver_state: receiverState,
      receiver_heartbeat_at: receiverAt, receiver_freshness: receiverFreshness,
      history_available: value.commands.history_available === true, recent },
    observer: { heartbeat_at: stamp(value.observer.heartbeat_at), source_heartbeat_at: stamp(value.observer.source_heartbeat_at),
      sender_heartbeat_at: stamp(value.observer.sender_heartbeat_at) },
    reports: { last: record(last) ? { state: reportStates.includes(last.state) ? last.state : 'unknown',
      created_at: stamp(last.created_at) } : null } };
}

export function MonitoringCommands({ available, recent }: { available: boolean; recent: MonitorCommand[] }) {
  const actions: Record<string, string> = { status: 'Статус', reserve: 'Резерв', errors: 'Ошибки', sources: 'Источники',
    refill: 'Пополнение резерва', pause: 'Пауза', reconcile: 'Сверка отправки', unblock: 'Диагностика блокировки',
    source_propose: 'Предложение источника', source_settings: 'Фильтры источников', help: 'Список команд',
    tasks: 'Задачи', task: 'Состояние задачи', cancel: 'Отмена задачи', resume_refill: 'Продолжение подготовки',
    resume_project: 'Снятие общей паузы для подготовки', diagnostics: 'Диагностика' };
  const states: Record<string, string> = { queued: 'Принято в очередь; выполнение ещё не подтверждено',
    running: 'Выполнение начато; итог ещё неизвестен', done: 'Обработка завершена', blocked: 'Заблокировано проверками',
    unknown: 'Исход неизвестен; требуется сверка', cancelled: 'Будущие проходы отменены; история сохранена' };
  const phases: Record<string, string> = { paused: 'Редакция на паузе', reserve_disabled: 'Резерв выключен',
    blocked: 'Остановлено проверками', worker_busy: 'Ожидание обработчика', waiting_source: 'Ожидание источника',
    reserve_satisfied: 'Проверка готового запаса', reserve_refilled: 'Пополнение запаса', no_source: 'Нет подходящего источника',
    preparation_deferred: 'Подготовка отложена', selecting: 'Выбор источника', translating: 'Перевод', reviewing: 'Проверка',
    repairing: 'Редактура', preparing: 'Подготовка', quality_ready: 'Проверено', reserve_empty: 'Запас пуст',
    waiting_interval: 'Ожидание интервала', waiting_services: 'Ожидание доступности моделей',
    cancelled: 'Отмена завершена', cancellation_requested: 'Отмена запрошена; текущая операция завершается' };
  return <div><h3 className={styles.subheading}>Последние команды</h3>
    <p className={styles.muted}>Последние 10 обновлённых задач. Состояние задачи и доставка ответа учитываются отдельно.</p>
    {!available ? <p className={styles.muted}>История команд пока недоступна.</p> : !recent.length
      ? <p className={styles.muted}>Команд пока нет.</p> : <div className={styles.routesGrid}>
        {recent.map((command, index) => <article className={styles.routeTarget} key={`${command.command_id}-${index}`}>
          <strong>{actions[command.action] || 'Команда'}</strong>
          <p>{states[command.state] || states.unknown}</p>
          {command.phase && <p className={styles.muted}>{phases[command.phase] || 'Этап не указан'}</p>}
          {(command.ready !== undefined || command.goal !== undefined) && <p>Готово по учёту: {command.ready ?? 'неизвестно'}
            {command.goal !== undefined && <> · Цель: {command.goal}</>}</p>}
          {command.state === 'done' && command.ready !== undefined && command.goal !== undefined && command.ready < command.goal &&
            <p className={styles.notice}>Целевой запас не достигнут.</p>}
          <p className={styles.detail}>Создана: {readableTime(command.created_at)} · Обновлена: {readableTime(command.updated_at)}
            <br />Номер: {command.command_id === 'unknown' ? 'не подтверждён' : command.command_id.slice(0, 8)}</p>
        </article>)}
      </div>}
  </div>;
}

export function MonitoringStatus({ value }: { value: MonitorState }) {
  if (!value.available) return <p className={styles.muted}>Мониторинг ещё не подключён в этой установке.</p>;
  const states: Record<string, string> = { pending: 'ожидает отправки', sending: 'отправляется',
    unknown: 'исход отправки неизвестен; требуется сверка', confirmed: 'доставка отчёта подтверждена',
    failed: 'ошибка до отправки', cancelled: 'отменён', expired: 'устарел и не отправлен' };
  const receiver = value.commands;
  const receiverStatus = receiver?.receiver_state === 'blocked' ? 'Приём команд заблокирован; требуется проверка доступа.'
    : receiver?.receiver_state === 'ready' && receiver.receiver_freshness === 'fresh' ? 'Приём команд отвечает.'
    : receiver?.receiver_freshness === 'stale' ? 'Сигнал приёма команд устарел; работа команд не подтверждена.'
    : 'Работа приёма команд ещё не подтверждена.';
  return <div aria-live="polite">
    <p><strong>{value.enabled ? 'Рассылка включена' : 'Рассылка выключена'}</strong> · {value.commands?.enabled ? 'Команды разрешены' : 'Команды выключены'}</p>
    <p className={styles.muted}>На момент чтения: {value.freshness === 'fresh' ? 'свежий сигнал наблюдателя' : value.freshness === 'stale' ? 'сигнал наблюдателя устарел' : 'сигналов ещё нет'}.
      Последний сигнал: {readableTime(value.observer?.heartbeat_at)}.</p>
    <p className={styles.muted}>Сигнал редакционного обработчика: {readableTime(value.observer?.source_heartbeat_at)} · Отправителя отчётов: {readableTime(value.observer?.sender_heartbeat_at)}</p>
    {!value.ledger_available && <p className={styles.notice}>Журнал наблюдателя пока недоступен. Работа рассылки не подтверждена.</p>}
    {!value.commands?.paired && <p className={styles.muted}>Управляющий чат ещё не привязан.</p>}
    {value.commands?.paired && !value.commands.contract_verified && <p className={styles.muted}>Чтение команд ещё не проверено.</p>}
    {value.commands?.enabled && <p className={value.commands.receiver_state === 'blocked' ? styles.notice : styles.muted}>{receiverStatus}</p>}
    <p className={styles.muted}>Последняя проверка приёма команд: {readableTime(value.commands?.receiver_heartbeat_at)}.</p>
    <p>Последний служебный отчёт: {value.reports?.last ? states[value.reports.last.state] || states.unknown : 'пока нет'}.
      {value.reports?.last && <span className={styles.detail}>Создан: {readableTime(value.reports.last.created_at)}</span>}</p>
    <MonitoringCommands available={value.commands?.history_available === true} recent={value.commands?.recent || []} />
  </div>;
}

export function Monitoring() {
  const [value, setValue] = useState<MonitorState | null>(null);
  const [preview, setPreview] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const load = async (showPreview: boolean) => {
    setPending(true); setError(''); setPreview('');
    try {
      if (showPreview) {
        const result = await api('monitor-preview');
        if (!result || result.available !== true || result.read_only !== true || result.external_mutations !== 0 ||
            typeof result.text !== 'string' || result.text.length > 4000) throw new Error('MONITOR_RESPONSE_INVALID');
        setPreview(result.text);
      } else { setValue(monitorStatusResult(await api('monitor'))); }
    } catch { setValue(null); setError('Не удалось прочитать мониторинг. Обновите статус позже.'); }
    finally { setPending(false); }
  };
  return <section className={styles.card} aria-labelledby="monitor-heading"><h2 id="monitor-heading">Служебный канал MAX</h2>
    <p className={styles.muted}>Состояние рассылки и команд с телефона. Предпросмотр показывает текст отчёта и не отправляет сообщение.</p>
    <div className={styles.actions}>
      <button type="button" className={styles.button} disabled={pending} onClick={() => load(false)}>{value ? 'Обновить мониторинг' : 'Показать мониторинг'}</button>
      <button type="button" className={styles.button} disabled={pending || value?.available !== true} onClick={() => load(true)}>Предпросмотр отчёта</button>
    </div>
    {error && <p role="alert" className={styles.notice}>{error}</p>}
    {value && <MonitoringStatus value={value} />}
    {preview && <pre className={styles.fullBody} aria-label="Предпросмотр служебного отчёта">{preview}</pre>}
  </section>;
}

export function EditorialPanel() {
  const [state, setState] = useState<State | null>(null);
  const [settings, setSettings] = useState<EditorialSettings | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const [form, setForm] = useState<EditorialForm | null>(null);
  const [dirty, setDirty] = useState(false), [pending, setPending] = useState(false), [error, setError] = useState('');
  const [routes, setRoutes] = useState<RouteInventory | null>(null);
  const [routeDetails, setRouteDetails] = useState<Record<string, RouteDetails>>({});
  const routePages = useRef<RoutePages>({ variant_page: 0, dispatch_page: 0, dispatch_filter: 'all' });
  const [routeError, setRouteError] = useState('');
  const [routeNotice, setRouteNotice] = useState('');
  const [editingOriginal, setEditingOriginal] = useState<RouteDetails | null>(null);
  const mounted = useRef(true);
  const refresh = useCallback(async (loadSettings: boolean, signal?: AbortSignal) => {
    const [current, channels, saved] = await Promise.allSettled([
      api('state', 'GET', undefined, signal), api(routeListPath(routePages.current), 'GET', undefined, signal),
      loadSettings ? api('settings', 'GET', undefined, signal) : Promise.resolve(null),
    ]);
    if (!mounted.current) return;
    if (current.status === 'rejected') throw current.reason;
    setState(current.value);
    if (channels.status === 'fulfilled' && channels.value && Array.isArray(channels.value.targets) &&
        Array.isArray(channels.value.variants) && Array.isArray(channels.value.dispatches)) {
      setRoutes(channels.value); setRouteError('');
    } else { setRouteError(channels.status === 'rejected' ? channels.reason.message : 'EDITORIAL_ROUTE_RESPONSE_INVALID'); }
    if (loadSettings) {
      if (saved.status === 'rejected') throw saved.reason;
      const result = saved.value;
      setSettings(result.settings); setRevision(result.revision); setForm(formFromSettings(result.settings)); setDirty(false);
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    const abort = new AbortController();
    refresh(true, abort.signal).catch(reason => { if (!abort.signal.aborted) setError(reason.message); });
    const timer = setInterval(() => { refresh(false, abort.signal).catch(reason => { if (!abort.signal.aborted) setError(reason.message); }); }, 20000);
    return () => { mounted.current = false; abort.abort(); clearInterval(timer); };
  }, [refresh]);
  const change = (key: keyof EditorialForm, value: string) => { setForm(previous => previous && ({ ...previous, [key]: value })); setDirty(true); };
  const perform = async (operation: () => Promise<unknown>, resetSettings = false) => {
    setPending(true); setError('');
    try { await operation(); await refresh(resetSettings); return true; }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'EDITORIAL_REQUEST_FAILED'); return false; }
    finally { setPending(false); }
  };
  const routeAction: RouteActionHandler = async action => {
    setRouteNotice('');
    let result: any;
    const success = await perform(async () => {
      result = await api('route-action', 'POST', action);
      if (action.action === 'preflight') {
        const exact = routes?.variants.find(variant => variant.id === action.id);
        if (!exact || result?.revision !== exact.revision || result?.content_hash !== exact.content_hash ||
            result?.target_id !== exact.target_id) throw new Error('APPROVAL_STALE');
      }
    });
    if (success) {
      let message = ({ inspect: 'Проверка доступа завершена; это не подтверждение доставки.',
      details: 'Полная версия открыта для просмотра; согласование не выполнено.',
      preflight: 'Проверка выбранной версии завершена.', preview: 'Изображение подготовлено для просмотра.', approve: 'Версия, канал и дата согласованы.',
      pause: 'Канал приостановлен; история сохранена.', resume: 'Канал возобновлён.',
      reconcile: 'Сверка завершена. Проверьте обновлённое состояние отправки.' })[action.action];
      if (action.action === 'inspect') {
        message = routeAccessText(result?.access);
      }
      if (action.action === 'reconcile' && result?.state === 'unknown') message = 'Исход остаётся неизвестным. Повторной отправки нет.';
      setRouteNotice(message);
    }
    return success;
  };
  const routePreview: RoutePreviewHandler = async variant => {
    let result: unknown = null;
    const success = await perform(async () => {
      result = await api('route-action', 'POST', { action: 'preview', id: variant.id });
    });
    return success ? result : null;
  };
  const loadRouteDetails: RouteDetailsHandler = async variant => {
    await perform(async () => {
      const target = routes?.targets.find(item => item.id === variant.target_id);
      if (!target) throw new Error('APPROVAL_STALE');
      const value = await api('route-action', 'POST', { action: 'details', id: variant.id });
      const exact = routeDetailsResult(value, variant, target);
      setRouteDetails(previous => ({ ...previous, [variant.id]: exact }));
    });
  };
  const changeRoutePage: RoutePageHandler = pages => {
    routePages.current = { variant_page: pages.variant_page, dispatch_page: pages.dispatch_page, dispatch_filter: pages.dispatch_filter };
    setRouteDetails({});
    return perform(() => Promise.resolve());
  };
  const editOriginal = (details: RouteDetails) => {
    try {
      const variant = routes?.variants.find(item => item.id === details.variant_id);
      const target = routes?.targets.find(item => item.id === details.target_id);
      if (!variant || !target || variant.origin !== 'owner_original' || !['max', 'telegram', 'vk'].includes(target.platform)) throw new Error('APPROVAL_STALE');
      const exact = routeDetailsResult(details, variant, target);
      exact.media.map(originalMedia);
      setEditingOriginal(exact); setRouteNotice('Редактируется авторский материал. Прежнее согласование нельзя использовать для изменённой версии.');
    } catch { setError('EDITORIAL_ROUTE_DETAILS_INVALID'); }
  };
  const control = (action: 'start' | 'pause') => perform(() => api('control', 'POST', { action, revision: state?.revision }), !dirty);
  const field = (label: string, key: keyof EditorialForm, kind = 'text', help?: string) => <label className={styles.field}>
    <span>{label}</span>{kind === 'textarea'
      ? <textarea value={form?.[key] || ''} onChange={event => change(key, event.target.value)} disabled={pending} />
      : <input type={kind} value={form?.[key] || ''} onChange={event => change(key, event.target.value)} disabled={pending}
        min={key === 'interval' ? 60 : ['redditAge', 'redditLimit'].includes(key) ? 1 : 0}
        max={key === 'interval' ? 1440 : key === 'redditAge' ? 8760 : key === 'redditLimit' ? 100 : 1000000} step="1" required />}
    {help && <span className={styles.muted}>{help}</span>}
  </label>;
  return <main className={styles.panel}>
    <div className={styles.header}><div><h1>Редакция</h1><p className={styles.muted}>Подбор → перевод → проверка → календарь Postiz → MAX</p></div>
      <span className={`${styles.badge} ${state?.paused === false ? styles.running : styles.paused}`}>
        {!state ? 'Проверяем подключение' : state.paused ? 'На паузе' : 'Работает'}</span></div>
    {error && <div role="alert" className={`${styles.notice} ${styles.error}`}>{errorText(error)}</div>}
    <section className={styles.card} aria-labelledby="cycle-heading"><h2 id="cycle-heading">Управление циклом</h2>
      <p aria-live="polite">{state ? phaseText(state.phase) : 'Загрузка состояния…'}</p>
      <p className={styles.muted}>Последнее обновление: {readableTime(state?.updated_at)} · Следующий цикл: {readableTime(state?.next_run_at)}</p>
      {state?.reserve?.enabled && <div aria-live="polite">
        <p>Проверенных статей в запасе: <strong>{state.reserve.ready ?? 0}</strong> / {state.reserve.target ?? 6} · Ближайшая отправка: {readableTime(state.reserve.next_delivery_at)}</p>
        <p className={styles.muted}>Подготовка: {phaseText(state.preparation?.phase)}</p>
        {state.preparation?.last_error && <div className={styles.notice}>{errorText(state.preparation.last_error.code)}</div>}
      </div>}
      {state?.last_error && <div className={styles.notice}>{errorText(state.last_error.code)}</div>}
      <div className={styles.actions}>
        <button className={`${styles.button} ${styles.primary}`} disabled={pending || !state || !state.paused || dirty} onClick={() => control('start')}>Запустить цикл</button>
        <button className={`${styles.button} ${styles.danger}`} disabled={pending || !state || state.paused} onClick={() => control('pause')}>Поставить на паузу</button>
        <button className={styles.button} disabled={pending} onClick={() => perform(() => Promise.resolve())}>Обновить статус</button>
      </div><p className={styles.muted}>Пауза сохраняет черновики и историю. Отправка с неизвестным исходом требует сверки и не повторяется автоматически.</p>
    </section>
    <section className={styles.card} aria-labelledby="settings-heading"><h2 id="settings-heading">Источники и отбор</h2>
      {form && settings ? <form onSubmit={event => { event.preventDefault(); perform(() => api('settings', 'PUT', { revision, settings: settingsFromForm(settings, form) }), true); }}>
        <div className={styles.grid}>
          <label className={styles.field}>Источник<select value={form.source} onChange={event => change('source', event.target.value)} disabled={pending}><option value="open_science">Открытая наука: NASA, NOIRLab, ESA/Hubble</option><option value="nasa_svs">NASA SVS</option><option value="reddit">Reddit</option></select></label>
          {field('Интервал между статьями, минут', 'interval', 'number', 'Не менее 60 минут. Догоняющих пачек после простоя нет.')}
          <label className={styles.field}>Резерв проверенных статей<select value={form.reserveEnabled} onChange={event => change('reserveEnabled', event.target.value)} disabled={pending}><option value="false">Выключен</option><option value="true">Включён</option></select></label>
          {field('Целевой запас статей', 'reserveTarget', 'number')}
          {field('Пополнять при остатке', 'reserveLow', 'number')}
          {field('Предельный запас', 'reserveMaximum', 'number')}
          {field('Новых источников за проход', 'reserveBatch', 'number', 'От 1 до 3. На каждую статью сохраняется предел двух исправлений.')}
        </div>
        {['nasa_svs', 'open_science'].includes(form.source) ? <div className={styles.grid}>
          {field(form.source === 'open_science' ? 'Авторы' : 'Авторы NASA', 'nasaAuthors', 'textarea', 'По одному имени на строку или через запятую. Пусто — любой автор.')}
          {field(form.source === 'open_science' ? 'Ключевые слова' : 'Ключевые слова NASA', 'nasaKeywords', 'textarea', 'Подойдёт любая из указанных фраз. Пусто — без фильтра по словам.')}
          <NasaRatingField mixed={form.source === 'open_science'} />
        </div> : <>
          {state?.capabilities?.reddit?.available !== true && <div className={styles.notice}>Reddit включается только после проверки доступа к API и прав на публикацию. Выбор источника сам по себе не снимает эти проверки.</div>}
          <div className={styles.grid}>
            {field('Сообщества Reddit', 'subreddits', 'textarea', 'Имена без r/. Нужно хотя бы сообщество или автор.')}
            {field('Авторы Reddit', 'redditAuthors', 'textarea', 'Имена без u/. При заполненных сообществах выбираются только эти авторы в этих сообществах.')}
            {field('Ключевые слова Reddit', 'redditKeywords', 'textarea', 'Любая фраза, без учёта регистра.')}
            {field('Минимальный рейтинг', 'redditRating', 'number')}{field('Минимум комментариев', 'redditComments', 'number')}
            {field('Возраст публикации, максимум часов', 'redditAge', 'number')}{field('Кандидатов за запрос', 'redditLimit', 'number')}
            <label className={styles.field}>Сортировка<select value={form.redditSort} onChange={event => change('redditSort', event.target.value)} disabled={pending}><option value="top">Лучшие</option><option value="new">Новые</option><option value="hot">Популярные сейчас</option></select></label>
            <label className={styles.field}>Период рейтинга<select value={form.redditPeriod} onChange={event => change('redditPeriod', event.target.value)} disabled={pending}>{[['hour', 'Час'], ['day', 'День'], ['week', 'Неделя'], ['month', 'Месяц'], ['year', 'Год'], ['all', 'За всё время']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          </div>
        </>}
        <div className={styles.actions} style={{ marginTop: 20 }}>
          <button type="submit" className={`${styles.button} ${styles.primary}`} disabled={pending || !dirty}>Сохранить настройки</button>
          <button type="button" className={styles.button} disabled={pending} onClick={() => perform(() => Promise.resolve(), true)}>Загрузить сохранённые</button>
          {dirty && <span className={styles.muted}>Есть несохранённые изменения</span>}
        </div>
      </form> : <p className={styles.muted}>Загрузка настроек…</p>}
    </section>
    <EditorialWorkspace pending={pending} editing={editingOriginal} onCancel={() => setEditingOriginal(null)} onChanged={async () => {
      setRouteDetails({}); await refresh(false);
    }} />
    {routeNotice && <div role="status" className={styles.notice}>{routeNotice}</div>}
    <EditorialRoutes inventory={routes} pending={pending || Boolean(routeError) || Boolean(editingOriginal)} error={routeError} onAction={routeAction} onPreview={routePreview}
      detailsById={routeDetails} onDetails={loadRouteDetails} onPage={changeRoutePage} onEdit={editOriginal} editingId={editingOriginal?.variant_id} />
    <Monitoring />
    <section className={styles.card}><h2>Последние материалы</h2><EditorialRecords records={state?.recent || []} /></section>
  </main>;
}
