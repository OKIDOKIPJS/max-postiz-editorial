import { Body, Controller, Get, HttpCode, HttpException, Post, Put, Req } from '@nestjs/common';

type EditorialRequest = {
  headers: Record<string, string | string[] | undefined>;
  query?: Record<string, unknown>;
  cookies?: Record<string, string | undefined>;
  user?: { id?: string; activated?: boolean };
  org?: { id?: string; users?: { role?: string; disabled?: boolean }[] };
};

// Этот BFF доверяет только пользователю и организации, заново проверенным AuthMiddleware.
@Controller('/editorial')
export class EditorialController {
  private fail(code: string, status = 403): never {
    throw new HttpException({ code }, status);
  }

  private authorize(request: EditorialRequest, mutation: boolean) {
    const { headers, cookies = {}, user, org } = request;
    const owner = process.env.EDITORIAL_OWNER_USER_ID;
    const organization = process.env.EDITORIAL_OWNER_ORGANIZATION_ID;
    if (!owner || !organization) this.fail('EDITORIAL_NOT_CONFIGURED', 503);
    if (!cookies.auth || headers.auth || headers.authorization || cookies.impersonate || headers.impersonate) {
      this.fail('EDITORIAL_COOKIE_SESSION_REQUIRED');
    }
    const memberships = org?.users;
    if (user?.id !== owner || user.activated !== true || org?.id !== organization ||
        !Array.isArray(memberships) || memberships.length !== 1 ||
        memberships[0].role !== 'SUPERADMIN' || memberships[0].disabled !== false) {
      this.fail('EDITORIAL_OWNER_REQUIRED');
    }
    let origin: string;
    try {
      const configured = new URL(process.env.FRONTEND_URL || '');
      if (!['http:', 'https:'].includes(configured.protocol) || configured.username || configured.password) throw new Error();
      origin = configured.origin;
    } catch { this.fail('EDITORIAL_NOT_CONFIGURED', 503); }
    if (headers.origin !== undefined) {
      if (headers.origin !== origin) this.fail('EDITORIAL_ORIGIN_REQUIRED');
    } else {
      // На HTTP в локальной сети браузер может не послать Sec-Fetch-Site.
      // Для GET допустим проверенный Referer; изменение всегда требует Origin.
      let refererMatches = false;
      try { refererMatches = typeof headers.referer === 'string' && new URL(headers.referer).origin === origin; } catch { /* Нет доверенного Referer. */ }
      if (mutation || (headers['sec-fetch-site'] !== 'same-origin' && !refererMatches)) this.fail('EDITORIAL_ORIGIN_REQUIRED');
    }
    if (mutation && (typeof headers['content-type'] !== 'string' ||
        !/^application\/json(?:\s*;|$)/i.test(headers['content-type']))) {
      this.fail('EDITORIAL_JSON_REQUIRED', 415);
    }
    return { owner, organization };
  }

  private revision(value: unknown): value is number {
    return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1;
  }

  private async forward(request: EditorialRequest, route: 'state' | 'settings' | 'control' | 'routes' | 'route-action' | 'route-setup' | 'route-admin', method: string, body?: unknown, query = '') {
    const identity = this.authorize(request, method !== 'GET');
    let url: URL;
    const key = process.env.EDITORIAL_INTERNAL_KEY;
    const port = process.env.EDITORIAL_INTERNAL_PORT || '8092';
    try {
      url = new URL(process.env.EDITORIAL_INTERNAL_URL || '');
      if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', 'host.docker.internal'].includes(url.hostname) ||
          !['8092', '18092'].includes(port) || url.port !== port || url.pathname !== '/internal/editorial' ||
          url.username || url.password || url.search || url.hash || !key || key.length < 32) {
        throw new Error();
      }
    } catch { this.fail('EDITORIAL_NOT_CONFIGURED', 503); }
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 15000);
    try {
      const response = await fetch(`${url.href}/${route}${query}`, {
        method, redirect: 'error', signal: abort.signal,
        headers: {
          'Content-Type': 'application/json', 'X-Editorial-Key': key,
          'X-Editorial-Owner': identity.owner, 'X-Editorial-Organization': identity.organization,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      // Не пересылаем HTML, stack trace или заголовки внутреннего сервиса в браузер.
      const text = await response.text();
      if (text.length > 1024 * 1024) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      let result: any;
      try { result = JSON.parse(text); } catch { this.fail('EDITORIAL_RESPONSE_INVALID', 502); }
      if (!result || typeof result !== 'object' || Array.isArray(result)) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      if (!response.ok) {
        let code = typeof result.code === 'string' && /^[A-Z][A-Z0-9_]{1,99}$/.test(result.code)
          ? result.code : 'EDITORIAL_REQUEST_FAILED';
        // Значение write-only токена не должно стать кодом ответа даже при
        // ошибочном отражении ввода внутренним сервисом.
        if (route === 'route-admin' && (body as any)?.action === 'set_secret' && ![
          'CREDENTIAL_FORMAT_INVALID', 'CREDENTIAL_TARGET_INVALID', 'CREDENTIAL_ENV_NOT_DEDICATED',
          'TARGET_BINDING_CONFLICT', 'TARGET_PAUSE_REQUIRED', 'CANCEL_OR_RECONCILE_FIRST',
          'BRIDGE_EDIT_IN_PROGRESS', 'ROUTE_CONFIGURATION_CHANGED', 'LOGIN_SELECTION_ACTIVE',
          'ROUTE_RUNTIME_RESTART_REQUIRED', 'ROUTE_ADMIN_BUSY', 'ROUTE_ADMIN_CONFLICT',
          'ROUTE_ADMIN_RECOVERY_REQUIRED',
          'EDITORIAL_PAUSE_BEFORE_SETTINGS', 'EDITORIAL_REVISION_CONFLICT',
          'EDITORIAL_MAINTENANCE_BUSY', 'POSTIZ_NEAR_QUEUE_RESTART_BLOCKED', 'RESTART_ALREADY_REQUESTED', 'RESTART_SELECTION_INVALID',
          'INPUT_INVALID', 'ROUTE_CONFIG_CONFLICT', 'EDITORIAL_SERVICE_UNAVAILABLE',
        ].includes(code)) code = 'EDITORIAL_REQUEST_FAILED';
        this.fail(code, [400, 403, 409, 422, 503].includes(response.status) ? response.status : 502);
      }
      return result;
    } catch (error) {
      if (error instanceof HttpException) throw error;
      this.fail('EDITORIAL_SERVICE_UNAVAILABLE', 503);
    } finally { clearTimeout(timer); }
  }

  private setupHash(value: unknown): value is string { return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value); }
  private setupId(value: unknown): value is string { return typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value); }
  private exact(value: any, keys: string[]) {
    return value && typeof value === 'object' && !Array.isArray(value) &&
      Object.keys(value).length === keys.length && Object.keys(value).every(key => keys.includes(key));
  }
  private channelOptions(platform: string, value: any) {
    const allowed: Record<string, string[]> = { max: ['max_chars'],
      telegram: ['max_chars', 'media_layout', 'request_timeout_seconds'], vk: ['max_chars', 'owner', 'operation_timeout_seconds'] };
    if (!allowed[platform] || !value || typeof value !== 'object' || Array.isArray(value) ||
        Object.keys(value).some(key => !allowed[platform].includes(key))) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
    const bounded = (key: string, low: number, high: number) => value[key] === undefined ||
      (typeof value[key] === 'number' && Number.isSafeInteger(value[key]) && value[key] >= low && value[key] <= high);
    if (!bounded('max_chars', 1, 40000) || !bounded('request_timeout_seconds', 1, 120) || !bounded('operation_timeout_seconds', 30, 600) ||
        (value.owner !== undefined && (typeof value.owner !== 'string' || !/^[1-9][0-9]{0,19}$/.test(value.owner))) ||
        (value.media_layout !== undefined && !['atomic', 'photo_then_text', 'auto'].includes(value.media_layout))) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
    return { ...value };
  }
  private original(value: any) {
    if (!this.exact(value, ['title', 'body', 'media', 'metadata']) || typeof value.title !== 'string' ||
        !value.title.trim() || value.title.length > 300 || /[\0\r\n]/.test(value.title) || typeof value.body !== 'string' ||
        !value.body.trim() || value.body.length > 40000 || value.body.includes('\0') || !Array.isArray(value.media) || value.media.length > 1 ||
        !value.metadata || typeof value.metadata !== 'object' || Array.isArray(value.metadata) ||
        Object.keys(value.metadata).some(key => !['tags', 'community', 'ai_generated'].includes(key))) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
    if ((value.metadata.tags !== undefined && (!Array.isArray(value.metadata.tags) || value.metadata.tags.length > 10 ||
          value.metadata.tags.some((tag: unknown) => typeof tag !== 'string' || !tag.trim() || tag.length > 50))) ||
        (value.metadata.community !== undefined && (typeof value.metadata.community !== 'string' || value.metadata.community.length > 200)) ||
        (value.metadata.ai_generated !== undefined && typeof value.metadata.ai_generated !== 'boolean')) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
    const media = value.media.map((item: any) => {
      if (!this.exact(item, ['path', 'mime', 'sha256']) || !this.setupHash(item.sha256) ||
          !['image/png', 'image/jpeg'].includes(item.mime) || typeof item.path !== 'string' ||
          !/^data\/[A-Za-z0-9_/-]+\.(?:png|jpe?g)$/.test(item.path) || item.path.split('/').some((part: string) => !part || part === '.' || part === '..')) {
        this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
      }
      return { path: item.path, mime: item.mime, sha256: item.sha256 };
    });
    return { title: value.title, body: value.body, media, metadata: { ...value.metadata } };
  }

  // Отдельные формы владельца. Эти маршруты никогда не согласуют и не отправляют пост.
  @Get('/route-setup')
  async routeSetup(@Req() request: EditorialRequest) {
    const result = await this.forward(request, 'route-setup', 'GET');
    if (!this.setupHash(result.config_hash) || result.external_mutations !== 0 || !Array.isArray(result.targets) ||
        !Array.isArray(result.max_channels) || result.max_channels.length > 500 ||
        result.targets.length > 200 || !Array.isArray(result.profiles) || result.profiles.length > 200) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
    const profiles = result.profiles.map((profile: any) => {
      if (!profile || typeof profile.id !== 'string' || profile.id.length > 128 || typeof profile.name !== 'string' ||
          profile.name.length > 200 || typeof profile.enabled !== 'boolean' || typeof profile.publication_mode !== 'string' ||
          !/^[a-z_]{1,40}$/.test(profile.publication_mode)) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      return { id: profile.id, name: profile.name, enabled: profile.enabled, publication_mode: profile.publication_mode };
    });
    const routes: Record<string, string> = { max: 'max_browser', telegram: 'telegram_api', vk: 'vk_browser' };
    const targets = result.targets.map((target: any) => {
      if (!target || !this.setupId(target.id) || typeof target.profile_id !== 'string' || !routes[target.platform] ||
          target.route !== routes[target.platform] || typeof target.destination !== 'string' || target.destination.length > 200 ||
          typeof target.enabled !== 'boolean' || typeof target.paused !== 'boolean' || !this.setupHash(target.binding_hash)) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      const capabilities: Record<string, boolean> = {}, diagnostics: Record<string, unknown> = {};
      for (const key of ['can_configure', 'can_set_secret', 'can_create_original', 'can_upload_media', 'can_login']) {
        if (typeof target.capabilities?.[key] === 'boolean') capabilities[key] = target.capabilities[key];
      }
      for (const key of ['local_prerequisites', 'credentials_configured', 'profile_exists', 'owner_configured', 'dom_contract_verified', 'media_contract_verified']) {
        if (typeof target.diagnostics?.[key] === 'boolean') diagnostics[key] = target.diagnostics[key];
      }
      const reasons = target.diagnostics?.blocked_reasons;
      if (Array.isArray(reasons) && reasons.length <= 30 && reasons.every((code: unknown) => typeof code === 'string' && /^[A-Z][A-Z0-9_]{1,99}$/.test(code))) diagnostics.blocked_reasons = reasons;
      return { id: target.id, profile_id: target.profile_id, platform: target.platform, route: target.route,
        destination: target.destination, enabled: target.enabled, paused: target.paused, binding_hash: target.binding_hash,
        options: this.channelOptions(target.platform, target.options), capabilities, diagnostics };
    });
    if (!result.restart || typeof result.restart.required !== 'boolean' || typeof result.restart.can_request !== 'boolean' ||
        !Array.isArray(result.restart.blocked_reasons) || result.restart.blocked_reasons.length > 30 ||
        !result.restart.blocked_reasons.every((code: unknown) => typeof code === 'string' && /^[A-Z][A-Z0-9_]{1,99}$/.test(code))) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
    const maxChannels = result.max_channels.map((channel: any) => {
      if (!channel || typeof channel.id !== 'string' || !/^-?\d{1,64}$/.test(channel.id) ||
          typeof channel.name !== 'string' || !channel.name.trim() || channel.name.length > 300) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      return { id: channel.id, name: channel.name };
    });
    if (new Set(maxChannels.map((channel: any) => channel.id)).size !== maxChannels.length) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
    return { config_hash: result.config_hash, profiles, targets, max_channels: maxChannels, restart: { required: result.restart.required,
      can_request: result.restart.can_request, blocked_reasons: result.restart.blocked_reasons }, external_mutations: 0 };
  }

  @Post('/route-admin')
  async routeAdmin(@Req() request: EditorialRequest, @Body() body: any) {
    this.authorize(request, true);
    if (!body || typeof body !== 'object' || Array.isArray(body) || typeof body.id !== 'string' ||
        !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(body.id) ||
        (body.action !== 'revise_original' && !this.setupId(body.id))) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
    let supplied: any;
    if (body.action === 'request_restart') {
      if (!this.exact(body, ['action', 'id', 'expected_binding_hash', 'expected_config_hash']) || body.id !== 'server' ||
          body.expected_binding_hash !== null || !this.setupHash(body.expected_config_hash)) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
      supplied = { action: body.action, id: 'server', expected_binding_hash: null, expected_config_hash: body.expected_config_hash };
    } else if (body.action === 'configure_target') {
      const target = body.target, routes: Record<string, string> = { max: 'max_browser', telegram: 'telegram_api', vk: 'vk_browser' };
      if (!this.exact(body, ['action', 'id', 'expected_config_hash', 'expected_binding_hash', 'target']) ||
          !this.setupHash(body.expected_config_hash) || (body.expected_binding_hash !== null && !this.setupHash(body.expected_binding_hash)) ||
          !this.exact(target, ['profile_id', 'platform', 'route', 'destination', 'enabled', 'options']) ||
          typeof target.profile_id !== 'string' || !target.profile_id || target.profile_id.length > 128 ||
          !routes[target.platform] || target.route !== routes[target.platform] || typeof target.destination !== 'string' ||
          !target.destination || target.destination.length > 200 || /[\0\r\n]/.test(target.destination) || typeof target.enabled !== 'boolean') this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
      supplied = { action: body.action, id: body.id, expected_config_hash: body.expected_config_hash, expected_binding_hash: body.expected_binding_hash,
        target: { profile_id: target.profile_id, platform: target.platform, route: target.route, destination: target.destination,
          enabled: target.enabled, options: this.channelOptions(target.platform, target.options) } };
    } else {
      if (!this.setupHash(body.expected_binding_hash)) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
      const base = { action: body.action, id: body.id, expected_binding_hash: body.expected_binding_hash };
      if (body.action === 'prepare_login') {
        if (!this.exact(body, Object.keys(base))) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
        supplied = base;
      } else if (body.action === 'set_secret') {
        if (!this.exact(body, [...Object.keys(base), 'value']) || typeof body.value !== 'string' || !body.value ||
            body.value.length > 4096 || /[\s\0]/.test(body.value)) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
        supplied = { ...base, value: body.value };
      } else if (body.action === 'upload_media') {
        if (!this.exact(body, [...Object.keys(base), 'mime', 'sha256', 'data_base64']) || !['image/png', 'image/jpeg'].includes(body.mime) ||
            !this.setupHash(body.sha256) || typeof body.data_base64 !== 'string' || !body.data_base64.length ||
            body.data_base64.length > 13981016 || body.data_base64.length % 4 !== 0 ||
            !/^[A-Za-z0-9+/]*={0,2}$/.test(body.data_base64) ||
            body.data_base64.length / 4 * 3 - (body.data_base64.endsWith('==') ? 2 : body.data_base64.endsWith('=') ? 1 : 0) > 10485760) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
        supplied = { ...base, mime: body.mime, sha256: body.sha256, data_base64: body.data_base64 };
      } else if (body.action === 'create_original' || body.action === 'revise_original') {
        const editing = body.action === 'revise_original';
        if (!this.exact(body, [...Object.keys(base), 'original', 'attest_original', ...(editing ? ['revision', 'content_hash'] : [])]) ||
            body.attest_original !== true || (editing && (!this.revision(body.revision) || !this.setupHash(body.content_hash)))) this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
        supplied = { ...base, original: this.original(body.original), attest_original: true,
          ...(editing ? { revision: body.revision, content_hash: body.content_hash } : {}) };
      } else this.fail('EDITORIAL_ROUTE_ADMIN_INVALID', 422);
    }
    const result = await this.forward(request, 'route-admin', 'POST', supplied);
    if (body.action === 'request_restart') {
      if (typeof result.request_id !== 'string' || !/^[a-f0-9]{32}$/.test(result.request_id) ||
          result.state !== 'restart_requested' || result.restart_required !== true) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      return { request_id: result.request_id, state: 'restart_requested', restart_required: true };
    }
    // Не возвращаем произвольный success payload: даже ошибка сервиса не выдаст введённый token.
    if (body.action === 'prepare_login') {
      if (result.target_id !== body.id || result.login_url !== '/owner-browser/' || result.live_verified !== false ||
          typeof result.expires_at !== 'number' || !Number.isSafeInteger(result.expires_at) ||
          result.expires_at <= Date.now() / 1000 || result.expires_at > Date.now() / 1000 + 610) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      return { target_id: result.target_id, login_url: '/owner-browser/', expires_at: result.expires_at, live_verified: false };
    }
    if (body.action === 'set_secret') {
      if (result.target_id !== body.id || result.configured !== true || result.restart_required !== true || result.paused !== true) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      return { target_id: result.target_id, configured: true, restart_required: true, paused: true };
    }
    if (body.action === 'configure_target') {
      if (result.target_id !== body.id || !this.setupHash(result.binding_hash) || !this.setupHash(result.config_hash) || result.paused !== true || result.restart_required !== true) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      return { target_id: result.target_id, binding_hash: result.binding_hash, config_hash: result.config_hash, paused: true, restart_required: true };
    }
    if (body.action === 'upload_media') {
      const media = result.media;
      if (result.external_mutations !== 0 || !this.exact(media, ['path', 'mime', 'sha256']) || media.mime !== body.mime || media.sha256 !== body.sha256 ||
          media.path !== `data/media/${body.sha256}.${body.mime === 'image/png' ? 'png' : 'jpg'}`) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
      return { media: { path: media.path, mime: media.mime, sha256: media.sha256 }, external_mutations: 0 };
    }
    if (typeof result.variant_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(result.variant_id) ||
        !this.setupId(result.target_id) || !this.revision(result.revision) || !this.setupHash(result.content_hash) ||
        result.state !== 'review_pending' || result.approval_created !== false || result.external_mutations !== 0 ||
        (body.action === 'revise_original' && (result.variant_id !== body.id || result.revision !== body.revision + 1)) ||
        (body.action === 'create_original' && result.target_id !== body.id)) this.fail('EDITORIAL_RESPONSE_INVALID', 502);
    return { variant_id: result.variant_id, target_id: result.target_id, revision: result.revision,
      content_hash: result.content_hash, state: result.state, approval_created: false, external_mutations: 0 };
  }

  @Get('/state')
  state(@Req() request: EditorialRequest) { return this.forward(request, 'state', 'GET'); }

  @Get('/settings')
  settings(@Req() request: EditorialRequest) { return this.forward(request, 'settings', 'GET'); }

  @Get('/routes')
  routes(@Req() request: EditorialRequest) {
    this.authorize(request, false);
    const query = request.query || {};
    if (Object.keys(query).some(key => !['variant_page', 'dispatch_page', 'dispatch_filter'].includes(key))) {
      this.fail('EDITORIAL_ROUTE_LIST_INVALID', 422);
    }
    const page = (key: string) => {
      const value = query[key] === undefined ? '0' : query[key];
      if (typeof value !== 'string' || !/^(?:0|[1-9][0-9]{0,6})$/.test(value) || Number(value) > 1000000) {
        this.fail('EDITORIAL_ROUTE_LIST_INVALID', 422);
      }
      return value;
    };
    const filter = query.dispatch_filter === undefined ? 'all' : query.dispatch_filter;
    if (filter !== 'all' && filter !== 'unresolved') this.fail('EDITORIAL_ROUTE_LIST_INVALID', 422);
    const suffix = Object.keys(query).length
      ? `?variant_page=${page('variant_page')}&dispatch_page=${page('dispatch_page')}&dispatch_filter=${filter}` : '';
    return this.forward(request, 'routes', 'GET', undefined, suffix);
  }

  // Nginx использует только эту проверку сессии для закрытого окна входа в MAX.
  @Get('/browser-authorize')
  @HttpCode(204)
  browserAuthorize(@Req() request: EditorialRequest) { this.authorize(request, false); }

  @Put('/settings')
  updateSettings(@Req() request: EditorialRequest, @Body() body: any) {
    this.authorize(request, true);
    if (!body || Object.keys(body).some(key => !['revision', 'settings'].includes(key)) ||
        !this.revision(body.revision) || !body.settings || typeof body.settings !== 'object' || Array.isArray(body.settings)) {
      this.fail('EDITORIAL_SETTINGS_INVALID', 422);
    }
    return this.forward(request, 'settings', 'PUT', { revision: body.revision, settings: body.settings });
  }

  @Post('/control')
  control(@Req() request: EditorialRequest, @Body() body: any) {
    this.authorize(request, true);
    if (!body || Object.keys(body).some(key => !['action', 'revision'].includes(key)) ||
        !['start', 'pause'].includes(body.action) || !this.revision(body.revision)) {
      this.fail('EDITORIAL_CONTROL_INVALID', 422);
    }
    return this.forward(request, 'control', 'POST', {
      action: body.action, revision: body.revision,
    });
  }

  // Только перечисленные операции над сохранёнными объектами. URL, token,
  // cookie, текст публикации и произвольные команды от браузера не принимаем.
  @Post('/route-action')
  routeAction(@Req() request: EditorialRequest, @Body() body: any) {
    this.authorize(request, true);
    if (!body || typeof body !== 'object' || Array.isArray(body) ||
        !['inspect', 'details', 'preflight', 'preview', 'approve', 'pause', 'resume', 'reconcile'].includes(body.action) ||
        typeof body.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(body.id)) {
      this.fail('EDITORIAL_ROUTE_ACTION_INVALID', 422);
    }
    const approval = body.action === 'approve';
    const keys = approval ? ['action', 'id', 'revision', 'content_hash', 'due_at'] : ['action', 'id'];
    if (Object.keys(body).length !== keys.length || Object.keys(body).some(key => !keys.includes(key)) ||
        (approval && (!this.revision(body.revision) || typeof body.content_hash !== 'string' ||
          !/^[a-f0-9]{64}$/.test(body.content_hash) || typeof body.due_at !== 'number' ||
          !Number.isSafeInteger(body.due_at) || body.due_at <= Date.now() / 1000))) {
      this.fail('EDITORIAL_ROUTE_ACTION_INVALID', 422);
    }
    return this.forward(request, 'route-action', 'POST', approval ? {
      action: body.action, id: body.id, revision: body.revision,
      content_hash: body.content_hash, due_at: body.due_at,
    } : { action: body.action, id: body.id });
  }
}
