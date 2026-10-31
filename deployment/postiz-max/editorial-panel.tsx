'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { EditorialForm, EditorialSettings, formFromSettings, safeSourceUrl, settingsFromForm } from './editorial-form';
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
  id: string; target_id: string; title: string; body: string; revision: number; state: string;
  content_hash: string; media_count: number; source_count: number;
  manual_approval_allowed?: boolean; approval_mode?: string; origin?: string;
};
export type RouteDispatch = {
  id: string; target_id: string; title: string; state: string; due_at: number; verified: boolean | null;
  external_id?: string | null; permalink?: string | null; error?: string | null; attempts: number; snapshot_hash: string;
  evidence_kind?: 'fixture' | 'api_receipt' | 'independent_readback' | 'unverified';
  destination?: string | null; platform?: string | null; route?: string | null;
};
export type RouteInventory = { targets: RouteTarget[]; variants: RouteVariant[]; dispatches: RouteDispatch[] };
export type RouteAction = { action: 'inspect' | 'preflight' | 'preview' | 'approve' | 'pause' | 'resume' | 'reconcile'; id: string;
  revision?: number; content_hash?: string; due_at?: number };
type RouteActionHandler = (action: RouteAction) => Promise<boolean>;
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

export function RouteDraft({ variant, target, pending, onAction, onPreview }: {
  variant: RouteVariant; target: RouteTarget; pending: boolean; onAction: RouteActionHandler; onPreview?: RoutePreviewHandler;
}) {
  const [schedule, setSchedule] = useState('');
  const [consent, setConsent] = useState(false);
  const [checkedHash, setCheckedHash] = useState<string | null>(null);
  const [preview, setPreview] = useState<RoutePreviewImage[] | null>(null);
  const [previewError, setPreviewError] = useState('');
  const dueAt = moscowSchedule(schedule);
  useEffect(() => { setConsent(false); setCheckedHash(null); setPreview(null); setPreviewError(''); },
    [variant.id, variant.revision, variant.content_hash, target.id, target.destination,
      target.binding_hash, target.paused, target.enabled]);
  const checked = checkedHash === variant.content_hash;
  const imageReviewed = variant.media_count === 0 || preview !== null;
  return <article className={styles.routeDraft} aria-label={`Черновик: ${variant.title}`}>
    <div className={styles.routeHeading}><h3>{variant.title || 'Без заголовка'}</h3>
      <span className={styles.badge}>{routePlatform(target.platform)} · Версия {variant.revision}</span></div>
    <p className={styles.muted}>Канал: {target.destination} · Вложений: {variant.media_count} · Источников: {variant.source_count}</p>
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
    <div className={styles.fullBody}>{variant.body}</div>
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
      disabled={pending || !checked || !imageReviewed || dueAt === null || !target.enabled || target.paused}
      onChange={event => setConsent(event.target.checked)} />
      <span>Я проверил материал и согласую версию {variant.revision} для канала {target.destination}
        {dueAt !== null ? ` на ${readableTime(dueAt)} (МСК)` : ' на выбранную дату'}.</span></label>
    <div className={styles.actions}><button type="button" className={`${styles.button} ${styles.primary}`}
      disabled={pending || !target.enabled || target.paused || !checked || !imageReviewed || !consent || dueAt === null}
      onClick={async () => {
        const exactDate = moscowSchedule(schedule);
        if (pending || !target.enabled || target.paused || !consent || !checked || !imageReviewed || exactDate === null) return;
        setConsent(false);
        await onAction({ action: 'approve', id: variant.id, revision: variant.revision,
          content_hash: variant.content_hash, due_at: exactDate });
      }}>Согласовать версию и дату</button></div>
    {target.paused && <p className={styles.muted}>Канал на паузе. Перед согласованием возобновите его отдельной кнопкой.</p>}
  </article>;
}

export function EditorialRoutes({ inventory, pending, error, onAction, onPreview }: {
  inventory: RouteInventory | null; pending: boolean; error?: string; onAction: RouteActionHandler; onPreview?: RoutePreviewHandler;
}) {
  const targets = (inventory?.targets || []).filter(target => managedRoutes.has(target.route));
  const variants = (inventory?.variants || []).filter(variant => variant.manual_approval_allowed !== false &&
    ['review_pending', 'ready', 'draft'].includes(variant.state));
  const dispatches = inventory?.dispatches || [];
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
      {variants.map(variant => { const target = targets.find(item => item.id === variant.target_id);
        return target ? <RouteDraft key={`${variant.id}:${variant.revision}:${variant.content_hash}:${target.binding_hash}`}
          variant={variant} target={target} pending={pending} onAction={onAction} onPreview={onPreview} /> : null; })}
      {!variants.length && <p className={styles.muted}>Новых черновиков для согласования пока нет.</p>}
      <h3 className={styles.subheading}>История отправок</h3>
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
      </table>{!dispatches.length && <p className={styles.muted}>Отправок пока нет.</p>}</div>
    </>}
  </section>;
}

export function EditorialPanel() {
  const [state, setState] = useState<State | null>(null);
  const [settings, setSettings] = useState<EditorialSettings | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const [form, setForm] = useState<EditorialForm | null>(null);
  const [dirty, setDirty] = useState(false), [pending, setPending] = useState(false), [error, setError] = useState('');
  const [routes, setRoutes] = useState<RouteInventory | null>(null);
  const [routeError, setRouteError] = useState('');
  const [routeNotice, setRouteNotice] = useState('');
  const mounted = useRef(true);
  const refresh = useCallback(async (loadSettings: boolean, signal?: AbortSignal) => {
    const [current, channels, saved] = await Promise.allSettled([
      api('state', 'GET', undefined, signal), api('routes', 'GET', undefined, signal),
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
        <a className={styles.button} href={OWNER_BROWSER_URL}>Вход в MAX</a>
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
    {routeNotice && <div role="status" className={styles.notice}>{routeNotice}</div>}
    <EditorialRoutes inventory={routes} pending={pending || Boolean(routeError)} error={routeError} onAction={routeAction} onPreview={routePreview} />
    <section className={styles.card}><h2>Последние материалы</h2><EditorialRecords records={state?.recent || []} /></section>
  </main>;
}
