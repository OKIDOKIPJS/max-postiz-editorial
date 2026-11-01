# Создание каналов MAX и сверка результата в браузере.
"""Owner CLI for creating a channel through MAX's public browser interface."""

import json
import os
import re
from contextlib import suppress
from urllib.parse import urlsplit

from .browser_runtime import COMPOSER, ORIGIN, ensure_channel, session
from .config import AgentError, Settings, fingerprint, inside, parse_json


# Сохраняем журнал операции перед следующим внешним действием.
def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


# Сохраняем разрешённый адресат после проверки результата.
def bind(settings, result):
    path = settings.root / 'config/app.json'
    app = parse_json(path.read_text(encoding='utf-8-sig'))
    # Обрабатываем ветвь при условии: app['max'] != settings.app['max'].
    if app['max'] != settings.app['max']:
        raise AgentError('BROWSER_CONFIG_CHANGED')
    app['max']['browser']['channels'][result['chat_id']] = result['name']
    save(path, app)


# Проверяем адрес и свойства созданного канала.
def verify_created(page, settings, identity, chat_id, receipt):
    candidate = Settings(settings.root, json.loads(json.dumps(settings.app)), settings.topic, settings.sources)
    candidate.app['max']['browser']['channels'][chat_id] = identity['name']
    ensure_channel(page, candidate, chat_id)
    page.reload(wait_until='domcontentloaded')
    ensure_channel(page, candidate, chat_id, navigate=False)
    # Обрабатываем ветвь при условии: page.locator(COMPOSER).inner_text().strip().
    if page.locator(COMPOSER).inner_text().strip():
        raise AgentError('BROWSER_COMPOSER_NOT_EMPTY')
    evidence = receipt.with_suffix('.png')
    # Show the channel and its title, excluding unrelated chats in the sidebar.
    page.screenshot(path=str(evidence), clip={'x': 480, 'y': 0, 'width': 800, 'height': 900})
    result = {'state': 'confirmed', **identity, 'chat_id': chat_id,
              'url': ORIGIN + '/' + chat_id,
              'evidence': str(evidence.relative_to(settings.root))}
    save(receipt, result)
    return result


# Создаём канал и сверяем его фактическое появление.
def create_channel(settings, name, description='', *, bind_channel=False, recover_chat_id=None,
                   authorize_before_create=None):
    if authorize_before_create is not None and (bind_channel or not callable(authorize_before_create)):
        raise AgentError('BROWSER_CONFIG_CHANGED')
    # Обрабатываем ветвь при условии: settings.app['max']['adapter_mode'] != 'browser'.
    if settings.app['max']['adapter_mode'] != 'browser':
        raise AgentError('BROWSER_MODE_NOT_ENABLED')
    name = name.strip()
    # Обрабатываем ветвь при условии: not name or len(name) > 200 or len(description) > 400.
    if not name or len(name) > 200 or len(description) > 400:
        raise AgentError('BROWSER_CHANNEL_FIELDS_INVALID')
    identity = {'name': name, 'description': description}
    receipt = inside(settings.root, 'data/browser-channels/' + fingerprint(identity) + '.json')
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with session(settings) as page:
        # Обрабатываем ветвь при условии: receipt.exists().
        if receipt.exists():
            saved = parse_json(receipt.read_text(encoding='utf-8'))
            # Обрабатываем ветвь при условии: saved['state'] != 'confirmed'.
            if saved['state'] != 'confirmed':
                known_id = recover_chat_id or saved.get('chat_id')
                # Обрабатываем ветвь при условии: known_id.
                if known_id:
                    result = verify_created(page, settings, identity, known_id, receipt)
                    # Обрабатываем ветвь при условии: bind_channel.
                    if bind_channel:
                        bind(settings, result)
                    return result
                return {**saved, 'state': 'unknown'}
            # Reusing a receipt still requires access in the currently signed-in account.
            candidate = Settings(settings.root, json.loads(json.dumps(settings.app)), settings.topic, settings.sources)
            candidate.app['max']['browser']['channels'][saved['chat_id']] = name
            ensure_channel(page, candidate, saved['chat_id'])
            # Обрабатываем ветвь при условии: bind_channel.
            if bind_channel:
                bind(settings, saved)
            return saved
        # Обрабатываем ветвь при условии: recover_chat_id.
        if recover_chat_id:
            raise AgentError('BROWSER_CREATION_RECEIPT_REQUIRED')
        page.goto(ORIGIN, wait_until='domcontentloaded')
        page.get_by_role('button', name='Настройки', exact=True).wait_for()
        page.get_by_role('button', name='Начать общение', exact=True).click()
        page.get_by_role('menu').get_by_text('Создать канал', exact=True).click()
        page.get_by_placeholder('Название', exact=True).fill(name)
        page.get_by_placeholder('Описание', exact=True).fill(description)
        button = page.get_by_role('button', name='Создать канал', exact=True)
        button.wait_for()
        # Обрабатываем ветвь при условии: not button.is_enabled().
        if not button.is_enabled():
            raise AgentError('BROWSER_CHANNEL_CREATE_UNAVAILABLE')
        # Обрабатываем ветвь при условии: Settings.load(settings.root).hash != settings.hash.
        if authorize_before_create is not None:
            if bind_channel or authorize_before_create() is not True:
                raise AgentError('BROWSER_CONFIG_CHANGED')
        elif Settings.load(settings.root).hash != settings.hash:
            raise AgentError('BROWSER_CONFIG_CHANGED')
        # A crash or timeout after this record must never cause a second creation click.
        result = {'state': 'creating', **identity}
        save(receipt, result)
        # Выполняем операцию с отдельной обработкой возможных ошибок.
        try:
            button.click()
            page.get_by_text('Приватный канал создан', exact=True).wait_for()
            page.get_by_role('button', name='Продолжить', exact=True).click()
            page.locator('#main-header-title').filter(has_text='Окно чата с ' + name).wait_for(state='attached')
            parsed = urlsplit(page.url)
            # Обрабатываем ветвь при условии: parsed.scheme != 'https' or parsed.hostname != 'web.max.ru' or (not re.fullmatch('/-?\\d+', parsed.path)).
            if parsed.scheme != 'https' or parsed.hostname != 'web.max.ru' or not re.fullmatch(r'/-?\d+', parsed.path):
                raise AgentError('BROWSER_WRONG_CHANNEL')
            chat_id = parsed.path[1:]
            result['chat_id'] = chat_id
            result = verify_created(page, settings, identity, chat_id, receipt)
        # Обрабатываем Exception и выбираем допустимый исход.
        except Exception:
            result['state'] = 'unknown'
            save(receipt, result)
            # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
            with suppress(Exception):
                page.screenshot(path=str(receipt.with_suffix('.png')), clip={'x': 600, 'y': 0, 'width': 680, 'height': 900})
        # Обрабатываем ветвь при условии: result['state'] == 'confirmed' and bind_channel.
        if result['state'] == 'confirmed' and bind_channel:
            bind(settings, result)
        return result
