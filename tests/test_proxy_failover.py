import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import checkin
from utils.config import AccountConfig, AppConfig
from utils.proxy import (
	get_agentrouter_proxy_attempts,
	get_playwright_proxy,
	get_proxy_server,
	proxy_attempt,
	redact_proxy_url,
)

PRIMARY = 'https://primary.example:443'
FALLBACKS = [f'https://fallback{n}.example:443' for n in range(1, 6)]
FAILED = (False, None, {'success': False, 'error': 'FIXTURE_FAILURE'})
PASSED = (True, {'success': True}, {'success': True})


@pytest.fixture(autouse=True)
def proxy_env(monkeypatch):
	monkeypatch.setenv('CHECKIN_PROXY_URL', PRIMARY)
	monkeypatch.setenv('AGENTROUTER_PROXY_FALLBACKS', json.dumps(FALLBACKS))
	monkeypatch.setenv('AGENTROUTER_MAX_RETRIES', '3')
	monkeypatch.setenv('DEBUG_MODE', 'false')
	monkeypatch.delenv('PROVIDERS', raising=False)


def account(provider='agentrouter', **kwargs):
	return AccountConfig(
		provider=provider, name='TARGET', cookies={'session': 'FIXTURE_SESSION', 'acw_tc': 'STALE_WAF'}, **kwargs
	)


def test_default_is_three_additional_retries(monkeypatch):
	monkeypatch.delenv('AGENTROUTER_MAX_RETRIES')
	assert get_agentrouter_proxy_attempts() == [PRIMARY, *FALLBACKS[:3]]


@pytest.mark.parametrize('limit', ['0', '1', '2', '5', '20'])
def test_configurable_limit_never_cycles_pool(monkeypatch, limit):
	monkeypatch.setenv('AGENTROUTER_MAX_RETRIES', limit)
	assert get_agentrouter_proxy_attempts() == [PRIMARY, *FALLBACKS[: int(limit)]]


@pytest.mark.parametrize('limit', ['-1', 'no', '1.5', ''])
def test_invalid_limit_warns_and_uses_default(monkeypatch, capsys, limit):
	monkeypatch.setenv('AGENTROUTER_MAX_RETRIES', limit)
	assert get_agentrouter_proxy_attempts() == [PRIMARY, *FALLBACKS[:3]]
	assert 'Invalid AGENTROUTER_MAX_RETRIES' in capsys.readouterr().out


def test_pool_deduplicates_normalized_host_port_and_primary(monkeypatch):
	monkeypatch.setenv(
		'AGENTROUTER_PROXY_FALLBACKS',
		json.dumps([PRIMARY.upper() + '/', *FALLBACKS[:1], FALLBACKS[0] + '/', FALLBACKS[1]]),
	)
	assert get_agentrouter_proxy_attempts() == [PRIMARY, *FALLBACKS[:2]]


def test_pool_can_be_newline_delimited(monkeypatch):
	monkeypatch.setenv('AGENTROUTER_PROXY_FALLBACKS', '\n'.join(FALLBACKS))
	assert get_agentrouter_proxy_attempts() == [PRIMARY, *FALLBACKS[:3]]


@pytest.mark.parametrize('raw', ['', '[broken json', '{"not":"a URL list"}'])
def test_empty_or_bad_pool_retains_primary(monkeypatch, raw):
	monkeypatch.setenv('AGENTROUTER_PROXY_FALLBACKS', raw)
	assert get_agentrouter_proxy_attempts() == [PRIMARY]


def test_invalid_pool_entries_are_not_used_or_logged(monkeypatch, capsys):
	monkeypatch.setenv(
		'AGENTROUTER_PROXY_FALLBACKS',
		json.dumps([None, 123, 'ss://PASSWORD@host:8888', 'https://user:PASSWORD@host:bad', *FALLBACKS[:1]]),
	)
	assert get_agentrouter_proxy_attempts() == [PRIMARY, FALLBACKS[0]]
	assert 'PASSWORD' not in capsys.readouterr().out


def test_no_primary_preserves_direct_first_attempt(monkeypatch):
	monkeypatch.delenv('CHECKIN_PROXY_URL')
	assert get_agentrouter_proxy_attempts() == [None, *FALLBACKS[:3]]


def test_bad_primary_still_allows_fallback(monkeypatch):
	monkeypatch.setenv('CHECKIN_PROXY_URL', 'https://host:bad')
	assert get_agentrouter_proxy_attempts()[1:] == FALLBACKS[:3]


def test_proxy_scope_resets_even_after_exception_and_respects_opt_out():
	with pytest.raises(RuntimeError), proxy_attempt(FALLBACKS[0]):
		assert get_proxy_server() == FALLBACKS[0]
		assert get_playwright_proxy() == {'server': FALLBACKS[0]}
		assert get_proxy_server(use_proxy=False) is None
		assert get_playwright_proxy(use_proxy=False) is None
		with proxy_attempt(None):
			assert get_proxy_server() is None
		raise RuntimeError('FIXTURE')
	assert get_proxy_server() == PRIMARY


def test_proxy_log_redaction():
	assert (
		redact_proxy_url('https://username:PASSWORD@host.example:443/path?TOKEN=secret') == 'https://host.example:443'
	)
	assert redact_proxy_url('https://[2001:db8::1]:443') == 'https://[2001:db8::1]:443'
	assert redact_proxy_url('https://host:bad') == '<invalid proxy>'
	assert redact_proxy_url(None) == 'direct'


@pytest.mark.asyncio
async def test_first_success_stops_immediately(monkeypatch):
	once = AsyncMock(return_value=PASSED)
	monkeypatch.setattr(checkin, '_check_in_account_once', once)
	assert await checkin.check_in_account(account(), 0, AppConfig.load_from_env()) == PASSED
	assert once.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('first', [FAILED, RuntimeError('PASSWORD must never be printed')])
async def test_false_or_any_exception_retries_then_stops(monkeypatch, capsys, first):
	once = AsyncMock(side_effect=[first, PASSED])
	monkeypatch.setattr(checkin, '_check_in_account_once', once)
	assert await checkin.check_in_account(account(), 0, AppConfig.load_from_env()) == PASSED
	assert once.await_count == 2
	assert once.await_args_list[0].kwargs['fresh_profile'] is False
	assert once.await_args_list[1].kwargs['fresh_profile'] is True
	assert 'PASSWORD' not in capsys.readouterr().out
	assert get_proxy_server() == PRIMARY


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [FAILED, OSError('FIXTURE')])
async def test_exhaustion_returns_failure_after_four_attempts(monkeypatch, capsys, outcome):
	once = AsyncMock(side_effect=[outcome] * 8)
	monkeypatch.setattr(checkin, '_check_in_account_once', once)
	assert not (await checkin.check_in_account(account(), 0, AppConfig.load_from_env()))[0]
	assert once.await_count == 4
	assert 'All 4 available proxy attempt(s) failed' in capsys.readouterr().out
	assert get_proxy_server() == PRIMARY


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['anyrouter', 'custom', 'agentrouter-opt-out'])
async def test_other_providers_or_explicit_proxy_opt_out_not_retried(monkeypatch, provider):
	config = AppConfig.load_from_env()
	if provider == 'agentrouter-opt-out':
		provider = 'agentrouter'
		config.providers[provider].use_proxy = False
	once = AsyncMock(return_value=FAILED)
	monkeypatch.setattr(checkin, '_check_in_account_once', once)
	assert await checkin.check_in_account(account(provider), 0, config) == FAILED
	assert once.await_count == 1
	assert once.await_args.kwargs == {}


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_or_retried(monkeypatch):
	once = AsyncMock(side_effect=asyncio.CancelledError)
	monkeypatch.setattr(checkin, '_check_in_account_once', once)
	with pytest.raises(asyncio.CancelledError):
		await checkin.check_in_account(account(), 0, AppConfig.load_from_env())
	assert once.await_count == 1
	assert get_proxy_server() == PRIMARY


@pytest.mark.asyncio
async def test_async_tasks_do_not_share_attempt_proxy():
	async def resolve(server):
		with proxy_attempt(server):
			await asyncio.sleep(0)
			return get_proxy_server(), get_playwright_proxy()

	results = await asyncio.gather(*(resolve(server) for server in FALLBACKS))
	assert results == [(server, {'server': server}) for server in FALLBACKS]
	assert get_proxy_server() == PRIMARY


@pytest.mark.asyncio
@pytest.mark.parametrize(
	'first_failure', ['non-json', 'api-rejected', 'http-403', 'network', 'browser-launch', 'missing-waf']
)
async def test_real_full_flow_restarts_browser_and_http_on_same_proxy(monkeypatch, first_failure):
	browsers, clients = [], []
	launch_calls = []

	class Browser:
		closed = False

		async def new_page(self):
			return SimpleNamespace(context=self, goto=AsyncMock())

		async def cookies(self):
			if first_failure == 'missing-waf' and len(launch_calls) == 1:
				return []
			return [{'name': 'acw_tc', 'value': f'waf-{len(launch_calls)}'}]

		async def close(self):
			self.closed = True

	async def launch(**kwargs):
		launch_calls.append(kwargs)
		if first_failure == 'browser-launch' and len(launch_calls) == 1:
			raise RuntimeError('FIXTURE_LAUNCH_FAILURE')
		browser = Browser()
		browsers.append(browser)
		return browser

	class Client:
		def __init__(self, **kwargs):
			self.kwargs = kwargs
			self.cookies = {}
			self.closed = False
			clients.append(self)

		def __enter__(self):
			return self

		def __exit__(self, *args):
			self.closed = True

		def get(self, *args, **kwargs):
			if len(launch_calls) == 1:
				if first_failure == 'non-json':
					return httpx.Response(200, text='<html>FIXTURE_WAF</html>')
				if first_failure == 'api-rejected':
					return httpx.Response(200, json={'success': False})
				if first_failure == 'http-403':
					return httpx.Response(403)
				if first_failure == 'network':
					raise httpx.ConnectError('FIXTURE_NETWORK_ERROR')
			return httpx.Response(200, json={'success': True, 'data': {'quota': 500000, 'used_quota': 0}})

	monkeypatch.setattr(checkin, 'launch_async', launch)
	monkeypatch.setattr(checkin, 'prepare_browser_page', AsyncMock())
	monkeypatch.setattr(checkin, 'wait_for_waf_ready', AsyncMock())
	monkeypatch.setattr(checkin.httpx, 'Client', Client)
	result = await checkin.check_in_account(account(), 0, AppConfig.load_from_env())
	assert result[0] is True
	assert [call['proxy'] for call in launch_calls] == [{'server': PRIMARY}, {'server': FALLBACKS[0]}]
	assert [client.kwargs['proxy'] for client in clients] == (
		[FALLBACKS[0]] if first_failure in {'browser-launch', 'missing-waf'} else [PRIMARY, FALLBACKS[0]]
	)
	assert all(client.kwargs['trust_env'] is False for client in clients)
	assert clients[-1].cookies == {'session': 'FIXTURE_SESSION', 'acw_tc': 'waf-2'}
	assert all(browser.closed for browser in browsers)
	assert all(client.closed for client in clients)
	assert get_proxy_server() == PRIMARY


@pytest.mark.asyncio
async def test_email_flow_relogs_with_fresh_profile_when_proxy_changes(monkeypatch):
	config = AppConfig.load_from_env()
	config.providers['agentrouter'].persist_profile = True
	contexts, settings_seen, proxies = [], [], []

	async def launch(settings, **kwargs):
		settings_seen.append(settings)
		proxies.append(get_playwright_proxy(use_proxy=kwargs['use_proxy']))
		context = SimpleNamespace(
			new_page=AsyncMock(return_value=SimpleNamespace(url='https://agentrouter.org/login')),
			cookies=AsyncMock(return_value=[{'name': 'session', 'value': f'new-session-{len(proxies)}'}]),
			close=AsyncMock(),
		)
		contexts.append(context)
		return context

	monkeypatch.setattr(checkin, 'launch_login_context', launch)
	for name in ['prepare_browser_page', 'navigate_login_page', 'login_with_email_form', 'save_login_screenshot']:
		monkeypatch.setattr(checkin, name, AsyncMock())
	monkeypatch.setattr(checkin, 'is_logged_in', AsyncMock(return_value=False))
	monkeypatch.setattr(checkin, 'has_session_cookie', AsyncMock(return_value=False))
	monkeypatch.setattr(checkin, 'verify_browser_login', AsyncMock(return_value={'id': 1}))
	request_calls = []

	def requests(cookies, *args, **kwargs):
		request_calls.append((cookies, get_proxy_server()))
		return FAILED if len(request_calls) == 1 else PASSED

	monkeypatch.setattr(checkin, 'run_check_in_requests', requests)
	assert (await checkin.check_in_account(account(email='FIXTURE_EMAIL', password='FIXTURE_PASSWORD'), 0, config))[0]
	assert [settings.persist_profile for settings in settings_seen] == [True, False]
	assert proxies == [{'server': PRIMARY}, {'server': FALLBACKS[0]}]
	assert request_calls == [({'session': 'new-session-1'}, PRIMARY), ({'session': 'new-session-2'}, FALLBACKS[0])]
	assert checkin.login_with_email_form.await_count == 2
	assert all(context.close.await_count == 1 for context in contexts)


@pytest.mark.asyncio
async def test_main_counts_final_account_result_not_attempts(monkeypatch, capsys):
	monkeypatch.setattr(checkin, 'load_accounts_config', lambda: [account()])
	monkeypatch.setattr(checkin, 'load_balance_hash', lambda: None)
	monkeypatch.setattr(checkin.notify, 'push_message', lambda *args, **kwargs: None)
	# Keep the real main/account wrapper but avoid balance IO (the fixture has no quotas).
	monkeypatch.setattr(checkin, '_check_in_account_once', AsyncMock(side_effect=[FAILED, (True, None, None)]))
	with pytest.raises(SystemExit) as exc:
		await checkin.main()
	assert exc.value.code == 0
	logs = capsys.readouterr().out
	assert 'Attempt 2/4' in logs
	assert '[NOTIFY] TARGET failed' not in logs
	assert 'All accounts successful and no balance changes detected' in logs


@pytest.mark.asyncio
async def test_email_login_failure_restarts_instead_of_using_stale_cookie(monkeypatch):
	login = AsyncMock(side_effect=[None, checkin.BrowserLoginResult(cookies={'session': 'NEW_SESSION'})])
	monkeypatch.setattr(checkin, 'login_with_credentials', login)
	cookie_flow = AsyncMock()
	monkeypatch.setattr(checkin, 'prepare_cookies', cookie_flow)
	requests = []

	def run(cookies, *args, **kwargs):
		requests.append((cookies, get_proxy_server()))
		return PASSED

	monkeypatch.setattr(checkin, 'run_check_in_requests', run)
	assert (await checkin.check_in_account(account(email='FIXTURE', password='FIXTURE'), 0, AppConfig.load_from_env()))[
		0
	]
	assert login.await_count == 2
	cookie_flow.assert_not_awaited()
	assert requests == [({'session': 'NEW_SESSION'}, FALLBACKS[0])]


def test_anyrouter_http_stays_direct_even_with_environment_proxies(monkeypatch):
	monkeypatch.setenv('HTTPS_PROXY', 'http://UNRELATED_ENV_PROXY:8080')
	seen = []

	def client(**kwargs):
		seen.append(kwargs)
		raise RuntimeError('STOP_BEFORE_NETWORK')

	monkeypatch.setattr(checkin.httpx, 'Client', client)
	with proxy_attempt(PRIMARY):
		checkin.run_check_in_requests(
			{}, account('anyrouter'), 'TARGET', AppConfig.load_from_env().providers['anyrouter']
		)
	assert seen == [{'http2': True, 'timeout': 30.0, 'trust_env': False}]


@pytest.mark.asyncio
async def test_anyrouter_cookie_precedence_is_unchanged(monkeypatch):
	monkeypatch.setattr(checkin, 'get_waf_cookies_with_browser', AsyncMock(return_value={'acw_tc': 'FRESH'}))
	provider = AppConfig.load_from_env().providers['anyrouter']
	cookies = await checkin.prepare_cookies('TARGET', provider, {'session': 'FIXTURE', 'acw_tc': 'USER_VALUE'})
	assert cookies == {'session': 'FIXTURE', 'acw_tc': 'USER_VALUE'}


@pytest.mark.asyncio
async def test_main_exhaustion_notifies_and_counts_only_one_failed_account(monkeypatch, capsys):
	monkeypatch.setattr(checkin, 'load_accounts_config', lambda: [account()])
	monkeypatch.setattr(checkin, '_check_in_account_once', AsyncMock(return_value=FAILED))
	monkeypatch.setattr(checkin, 'load_balance_hash', lambda: None)
	notifications = []
	monkeypatch.setattr(checkin.notify, 'push_message', lambda *args, **kwargs: notifications.append(args))
	with pytest.raises(SystemExit) as exc:
		await checkin.main()
	assert exc.value.code == 1
	logs = capsys.readouterr().out
	assert 'Success: 0/1' in logs
	assert 'Failed: 1/1' in logs
	assert len(notifications) == 1
