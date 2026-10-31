import { Body, Controller, Get, HttpCode, HttpException, Post, Put, Req } from '@nestjs/common';

type EditorialRequest = {
  headers: Record<string, string | string[] | undefined>;
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

  private async forward(request: EditorialRequest, route: 'state' | 'settings' | 'control' | 'routes' | 'route-action', method: string, body?: unknown) {
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
      const response = await fetch(`${url.href}/${route}`, {
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
        const code = typeof result.code === 'string' && /^[A-Z][A-Z0-9_]{1,99}$/.test(result.code)
          ? result.code : 'EDITORIAL_REQUEST_FAILED';
        this.fail(code, [400, 403, 409, 422, 503].includes(response.status) ? response.status : 502);
      }
      return result;
    } catch (error) {
      if (error instanceof HttpException) throw error;
      this.fail('EDITORIAL_SERVICE_UNAVAILABLE', 503);
    } finally { clearTimeout(timer); }
  }

  @Get('/state')
  state(@Req() request: EditorialRequest) { return this.forward(request, 'state', 'GET'); }

  @Get('/settings')
  settings(@Req() request: EditorialRequest) { return this.forward(request, 'settings', 'GET'); }

  @Get('/routes')
  routes(@Req() request: EditorialRequest) { return this.forward(request, 'routes', 'GET'); }

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
        !['inspect', 'preflight', 'preview', 'approve', 'pause', 'resume', 'reconcile'].includes(body.action) ||
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
