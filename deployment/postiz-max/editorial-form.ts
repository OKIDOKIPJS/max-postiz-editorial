export type EditorialSettings = {
  interval_seconds?: number; source?: string;
  nasa?: Record<string, unknown>; reddit?: Record<string, unknown>;
  reserve?: Record<string, unknown>;
  [key: string]: unknown;
};
export type EditorialForm = {
  source: string; interval: string; nasaKeywords: string; nasaAuthors: string;
  nasaRating: string; subreddits: string; redditAuthors: string; redditKeywords: string; redditRating: string;
  redditComments: string; redditAge: string; redditLimit: string; redditSort: string; redditPeriod: string;
  reserveEnabled: string; reserveTarget: string; reserveLow: string; reserveMaximum: string; reserveBatch: string;
};
const listText = (value: unknown) => Array.isArray(value) ? value.join('\n') : '';
export const splitList = (value: string) => [...new Set(value.split(/[,\n]/).map(item => item.trim()).filter(Boolean))];
export function formFromSettings(settings: EditorialSettings): EditorialForm {
  return {
    source: settings.source || 'nasa_svs', interval: String((settings.interval_seconds || 3600) / 60),
    nasaKeywords: listText(settings.nasa?.keywords), nasaAuthors: listText(settings.nasa?.authors),
    nasaRating: '0',
    subreddits: listText(settings.reddit?.subreddits), redditAuthors: listText(settings.reddit?.authors),
    redditKeywords: listText(settings.reddit?.keywords), redditRating: String(settings.reddit?.min_score ?? 0),
    redditComments: String(settings.reddit?.min_comments ?? 0), redditAge: String(settings.reddit?.max_age_hours ?? 72),
    redditLimit: String(settings.reddit?.limit ?? 25), redditSort: String(settings.reddit?.sort ?? 'top'),
    redditPeriod: String(settings.reddit?.time_filter ?? 'week'),
    reserveEnabled: String(settings.reserve?.enabled ?? false), reserveTarget: String(settings.reserve?.target ?? 6),
    reserveLow: String(settings.reserve?.low ?? 3), reserveMaximum: String(settings.reserve?.maximum ?? 12),
    reserveBatch: String(settings.reserve?.batch ?? 3),
  };
}
export function settingsFromForm(settings: EditorialSettings, form: EditorialForm): EditorialSettings {
  const minutes = Number(form.interval), nasaRating = Number(form.nasaRating), redditRating = Number(form.redditRating);
  const comments = Number(form.redditComments), age = Number(form.redditAge), limit = Number(form.redditLimit);
  const target = Number(form.reserveTarget), low = Number(form.reserveLow), maximum = Number(form.reserveMaximum), batch = Number(form.reserveBatch);
  if (!['true', 'false'].includes(form.reserveEnabled) || ![target, low, maximum, batch].every(Number.isSafeInteger) ||
      low < 1 || low >= target || target > maximum || maximum > 24 || batch < 1 || batch > 3) throw new Error('EDITORIAL_FORM_INVALID');
  if (!['nasa_svs', 'open_science', 'reddit'].includes(form.source) || !Number.isSafeInteger(minutes) || minutes < 60 || minutes > 1440 ||
      nasaRating !== 0 || !Number.isSafeInteger(redditRating) || redditRating < 0 || redditRating > 1000000 ||
      !Number.isSafeInteger(comments) || comments < 0 || comments > 1000000 || !Number.isSafeInteger(age) || age < 1 || age > 8760 ||
      !Number.isSafeInteger(limit) || limit < 1 || limit > 100 || !['top', 'new', 'hot'].includes(form.redditSort) ||
      !['hour', 'day', 'week', 'month', 'year', 'all'].includes(form.redditPeriod)) {
    throw new Error('EDITORIAL_FORM_INVALID');
  }
  const lists = [form.nasaKeywords, form.nasaAuthors, form.subreddits, form.redditAuthors, form.redditKeywords].map(splitList);
  if (lists.some((items, index) => items.length > (index < 2 ? 50 : index === 4 ? 30 : 20) || items.some(item => item.length > 120)) ||
      (form.source === 'reddit' && !lists[2].length && !lists[3].length)) throw new Error('EDITORIAL_FORM_INVALID');
  // Не теряем дополнительные серверные настройки при сохранении видимых полей.
  return {
    ...settings, source: form.source, interval_seconds: minutes * 60,
    ...((settings.reserve || form.reserveEnabled === 'true') ? { reserve: {
      enabled: form.reserveEnabled === 'true', target, low, maximum, batch,
    } } : {}),
    nasa: { ...settings.nasa, keywords: lists[0], authors: lists[1], min_rating: nasaRating },
    reddit: { ...settings.reddit, subreddits: lists[2], authors: lists[3], keywords: lists[4], min_score: redditRating,
      min_comments: comments, max_age_hours: age, limit, sort: form.redditSort, time_filter: form.redditPeriod },
  };
}
export function safeSourceUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return;
  try { const url = new URL(value); if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href; } catch { /* Неверная ссылка остаётся обычным текстом. */ }
}

// Формы подключения не принимают пароли браузера, пути профилей и флаги проверки.
export type ManagedPlatform = 'max' | 'telegram' | 'vk';
export type ChannelOptions = { max_chars?: number; media_layout?: 'atomic' | 'photo_then_text' | 'auto';
  request_timeout_seconds?: number; owner?: string; operation_timeout_seconds?: number };
export type SetupTarget = { id: string; profile_id: string; platform: ManagedPlatform; route: string;
  destination: string; enabled: boolean; paused: boolean; binding_hash: string; options: ChannelOptions;
  capabilities: Record<string, boolean>; diagnostics: { blocked_reasons?: string[]; [key: string]: unknown } };
export type RouteSetup = { config_hash: string; targets: SetupTarget[];
  max_channels: { id: string; name: string }[];
  profiles: { id: string; name: string; enabled: boolean; publication_mode: string }[];
  restart: { required: boolean; can_request: boolean; blocked_reasons: string[] }; external_mutations: 0 };
export type OriginalMedia = { path: string; mime: 'image/png' | 'image/jpeg'; sha256: string };
export type OriginalInput = { title: string; body: string; media: OriginalMedia[]; metadata: Record<string, unknown> };
export const managedPlatformRoutes: Record<ManagedPlatform, string> = {
  max: 'max_browser', telegram: 'telegram_api', vk: 'vk_browser',
};
export const setupId = (value: unknown): value is string => typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
export const setupHash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
export function originalMedia(value: unknown): OriginalMedia {
  const item = value as OriginalMedia;
  if (!item || typeof item !== 'object' || Array.isArray(item) || Object.keys(item).sort().join(',') !== 'mime,path,sha256' ||
      !['image/png', 'image/jpeg'].includes(item.mime) || !setupHash(item.sha256) ||
      typeof item.path !== 'string' || !/^data\/[A-Za-z0-9_/-]+\.(?:png|jpe?g)$/.test(item.path) ||
      item.path.split('/').some(part => !part || part === '.' || part === '..')) {
    throw new Error('EDITORIAL_ORIGINAL_MEDIA_INVALID');
  }
  return { path: item.path, mime: item.mime, sha256: item.sha256 };
}
export function originalInput(title: string, body: string, media: OriginalMedia[], metadata: Record<string, unknown> = {}): OriginalInput {
  if (!title.trim() || title.length > 300 || /[\0\r\n]/.test(title) || !body.trim() || body.length > 40000 || body.includes('\0') ||
      !Array.isArray(media) || media.length > 1 || !metadata || typeof metadata !== 'object' || Array.isArray(metadata) ||
      Object.keys(metadata).some(key => !['tags', 'community', 'ai_generated'].includes(key))) throw new Error('EDITORIAL_ORIGINAL_INVALID');
  return { title, body, media: media.map(originalMedia), metadata: { ...metadata } };
}
export type ChannelForm = { id: string; profile_id: string; platform: ManagedPlatform; destination: string; enabled: boolean;
  owner: string; max_chars: string; media_layout: string; request_timeout_seconds: string; operation_timeout_seconds: string };
export function channelForm(target?: SetupTarget): ChannelForm {
  return { id: target?.id || '', profile_id: target?.profile_id || '', platform: target?.platform || 'max',
    destination: target?.destination || '', enabled: target?.enabled ?? true, owner: target?.options.owner || '',
    max_chars: String(target?.options.max_chars ?? ''), media_layout: target?.options.media_layout || 'auto',
    request_timeout_seconds: String(target?.options.request_timeout_seconds ?? ''),
    operation_timeout_seconds: String(target?.options.operation_timeout_seconds ?? '') };
}
export function channelTarget(form: ChannelForm) {
  if (!setupId(form.id) || !form.profile_id || form.profile_id.length > 128 ||
      !Object.prototype.hasOwnProperty.call(managedPlatformRoutes, form.platform) ||
      !form.destination || form.destination.length > 200 || /[\0\r\n]/.test(form.destination) || typeof form.enabled !== 'boolean') {
    throw new Error('EDITORIAL_CHANNEL_INVALID');
  }
  const options: ChannelOptions = {};
  const bounded = (value: string, minimum: number, maximum: number) => {
    if (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) < minimum || Number(value) > maximum) {
      throw new Error('EDITORIAL_CHANNEL_INVALID');
    }
    return Number(value);
  };
  if (form.max_chars !== '') options.max_chars = bounded(form.max_chars, 1, 40000);
  if (form.platform === 'telegram') {
    if (!['atomic', 'photo_then_text', 'auto'].includes(form.media_layout)) throw new Error('EDITORIAL_CHANNEL_INVALID');
    options.media_layout = form.media_layout as ChannelOptions['media_layout'];
    if (form.request_timeout_seconds !== '') options.request_timeout_seconds = bounded(form.request_timeout_seconds, 1, 120);
  }
  if (form.platform === 'vk') {
    if (!/^[1-9][0-9]{0,19}$/.test(form.owner) || form.destination !== form.owner) throw new Error('EDITORIAL_CHANNEL_INVALID');
    options.owner = form.owner;
    if (form.operation_timeout_seconds !== '') options.operation_timeout_seconds = bounded(form.operation_timeout_seconds, 30, 600);
  }
  if (form.platform === 'max' && !/^-?\d+$/.test(form.destination)) throw new Error('EDITORIAL_CHANNEL_INVALID');
  return { profile_id: form.profile_id, platform: form.platform, route: managedPlatformRoutes[form.platform],
    destination: form.destination, enabled: form.enabled, options };
}

// WebCrypto недоступен на сохранённом LAN HTTP origin. SHA-256 нужен и там;
// считаем его локально над исходными байтами, не отправляя файл стороннему сервису.
export function imageSha256(bytes: Uint8Array): string {
  const constants = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  const state = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const size = Math.ceil((bytes.length + 9) / 64) * 64, data = new Uint8Array(size);
  data.set(bytes); data[bytes.length] = 0x80;
  const view = new DataView(data.buffer), bitLength = bytes.length * 8;
  view.setUint32(size - 8, Math.floor(bitLength / 0x100000000)); view.setUint32(size - 4, bitLength >>> 0);
  const rotate = (value: number, bits: number) => (value >>> bits) | (value << (32 - bits));
  const words = new Uint32Array(64);
  for (let offset = 0; offset < size; offset += 64) {
    for (let index = 0; index < 16; index++) words[index] = view.getUint32(offset + index * 4);
    for (let index = 16; index < 64; index++) {
      const a = words[index - 15], b = words[index - 2];
      words[index] = (words[index - 16] + (rotate(a,7) ^ rotate(a,18) ^ (a >>> 3)) + words[index - 7] +
        (rotate(b,17) ^ rotate(b,19) ^ (b >>> 10))) >>> 0;
    }
    let [a,b,c,d,e,f,g,h] = state;
    for (let index = 0; index < 64; index++) {
      const first = (h + (rotate(e,6) ^ rotate(e,11) ^ rotate(e,25)) + ((e & f) ^ (~e & g)) + constants[index] + words[index]) >>> 0;
      const second = ((rotate(a,2) ^ rotate(a,13) ^ rotate(a,22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      h=g; g=f; f=e; e=(d+first)>>>0; d=c; c=b; b=a; a=(first+second)>>>0;
    }
    for (const [index, value] of [a,b,c,d,e,f,g,h].entries()) state[index] = (state[index] + value) >>> 0;
  }
  return state.map(value => value.toString(16).padStart(8, '0')).join('');
}
