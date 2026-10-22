"""代理配置：读取环境变量并供浏览器 / HTTP 客户端使用。"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

# None means no override; (None,) explicitly pins an attempt to a direct connection.
_attempt_proxy: ContextVar[tuple[str | None] | None] = ContextVar('checkin_attempt_proxy', default=None)


@contextmanager
def proxy_attempt(server: str | None):
	"""Pin browser and HTTP helpers to one proxy without mutating process-wide env."""
	token = _attempt_proxy.set((server,))
	try:
		yield
	finally:
		_attempt_proxy.reset(token)


def get_proxy_server(*, use_proxy: bool = True) -> str | None:
	"""按平台配置读取 CHECKIN_PROXY_URL；use_proxy=False 时不返回代理地址。"""
	if not use_proxy:
		return None
	selected = _attempt_proxy.get()
	if selected is not None:
		return selected[0]
	server = os.getenv('CHECKIN_PROXY_URL', '').strip()
	return server or None


def get_playwright_proxy(*, use_proxy: bool = True) -> dict[str, str] | None:
	server = get_proxy_server(use_proxy=use_proxy)
	if not server:
		return None
	return {'server': server}


def redact_proxy_url(server: str | None) -> str:
	"""Log only the endpoint, never credentials, paths or query strings."""
	if not server:
		return 'direct'
	try:
		parsed = urlsplit(server)
		if not parsed.scheme or not parsed.hostname:
			return '<invalid proxy>'
		host = f'[{parsed.hostname}]' if ':' in parsed.hostname else parsed.hostname
		port = f':{parsed.port}' if parsed.port else ''
		return f'{parsed.scheme}://{host}{port}'
	except ValueError:
		return '<invalid proxy>'


def _proxy_key(server: str) -> tuple[str | None, int]:
	parsed = urlsplit(server)
	return parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def get_agentrouter_proxy_attempts() -> list[str | None]:
	"""Primary first, then unused endpoints; MAX_RETRIES counts *additional* tries."""
	primary = get_proxy_server()
	raw_limit = os.getenv('AGENTROUTER_MAX_RETRIES', '3').strip()
	try:
		max_retries = int(raw_limit)
		if max_retries < 0:
			raise ValueError
	except ValueError:
		print('[WARN] Invalid AGENTROUTER_MAX_RETRIES; using 3 additional retries')
		max_retries = 3
	if max_retries == 0:
		return [primary]

	raw_pool = os.getenv('AGENTROUTER_PROXY_FALLBACKS', '').strip()
	try:
		pool = json.loads(raw_pool) if raw_pool.startswith('[') else raw_pool.splitlines()
		if not isinstance(pool, list):
			raise ValueError
	except (ValueError, TypeError):
		print('[WARN] Invalid AGENTROUTER_PROXY_FALLBACKS; expected a JSON array or one URL per line')
		pool = []

	attempts: list[str | None] = [primary]
	seen = set()
	if primary:
		try:
			seen.add(_proxy_key(primary))
		except ValueError:
			pass  # An invalid primary can still fail over to a valid configured fallback.
	for index, entry in enumerate(pool, 1):
		try:
			if not isinstance(entry, str):
				raise ValueError
			server = entry.strip()
			parsed = urlsplit(server)
			if (
				parsed.scheme not in {'http', 'https'}
				or not parsed.hostname
				or parsed.port == 0
				or parsed.path not in {'', '/'}
				or parsed.query
				or parsed.fragment
			):
				raise ValueError
			key = _proxy_key(server)
		except ValueError:
			print(f'[WARN] Ignoring invalid AgentRouter fallback #{index} (expected HTTP/HTTPS proxy URL)')
			continue
		if key in seen:
			continue
		seen.add(key)
		attempts.append(server)
		if len(attempts) >= max_retries + 1:
			break
	return attempts
