from unittest.mock import MagicMock, patch

import httpx
import pytest

from utils.notify import NotificationKit


@pytest.fixture
def wxpusher_kit(monkeypatch):
	monkeypatch.setenv('WXPUSHER_APP_TOKEN', ' AT_test_token \n')
	monkeypatch.setenv('WXPUSHER_UIDS', 'UID_first, UID_second')
	return NotificationKit()


@pytest.fixture
def wxpusher_http():
	with patch('httpx.Client') as factory:
		client = factory.return_value.__enter__.return_value
		client.post.return_value = httpx.Response(
			200,
			json={
				'code': 1000,
				'msg': '处理成功',
				'success': True,
				'data': [
					{'uid': uid, 'code': 1000, 'status': '创建发送任务成功'} for uid in ['UID_first', 'UID_second']
				],
			},
		)
		yield factory, client


def test_send_wxpusher(wxpusher_kit, wxpusher_http):
	factory, client = wxpusher_http
	wxpusher_kit.send_wxpusher('签到通知', '第一行\n余额：42.00')
	factory.assert_called_once_with(timeout=30.0)
	client.post.assert_called_once_with(
		'https://wxpusher.zjiecode.com/api/send/message',
		json={
			'appToken': 'AT_test_token',
			'content': '签到通知\n\n第一行\n余额：42.00',
			'summary': '签到通知',
			'contentType': 1,
			'uids': ['UID_first', 'UID_second'],
		},
	)


@pytest.mark.parametrize('uids', ['UID_first, UID_second, ,', ' UID_first\r\nUID_second\n', 'UID_first,\nUID_second'])
def test_wxpusher_uid_separators(monkeypatch, uids):
	monkeypatch.setenv('WXPUSHER_UIDS', uids)
	assert NotificationKit().wxpusher_uids == ['UID_first', 'UID_second']


def test_wxpusher_summary_limit_preserves_full_title(wxpusher_kit, wxpusher_http):
	_, client = wxpusher_http
	title = '签' * 120
	wxpusher_kit.send_wxpusher(title, '详情')
	payload = client.post.call_args.kwargs['json']
	assert payload['summary'] == title[:100]
	assert payload['content'] == f'{title}\n\n详情'


@pytest.mark.parametrize(
	('token', 'uids'), [('', 'UID_first'), ('AT_test', ''), (' \n', 'UID_first'), ('AT_test', ' , \n, ')]
)
def test_wxpusher_missing_config(monkeypatch, wxpusher_http, token, uids):
	factory, _ = wxpusher_http
	monkeypatch.setenv('WXPUSHER_APP_TOKEN', token)
	monkeypatch.setenv('WXPUSHER_UIDS', uids)
	with pytest.raises(ValueError, match='WxPusher App Token or UIDs not configured'):
		NotificationKit().send_wxpusher('标题', '详情')
	factory.assert_not_called()


@pytest.mark.parametrize('status', [400, 429, 500])
def test_wxpusher_http_failure(wxpusher_kit, wxpusher_http, status):
	_, client = wxpusher_http
	client.post.return_value = httpx.Response(status, text='request failed')
	with pytest.raises(RuntimeError, match=f'WxPusher request failed: HTTP {status}'):
		wxpusher_kit.send_wxpusher('标题', '详情')


def test_wxpusher_invalid_json(wxpusher_kit, wxpusher_http):
	_, client = wxpusher_http
	client.post.return_value = httpx.Response(200, text='<html>bad gateway</html>')
	with pytest.raises(RuntimeError, match='WxPusher request failed: invalid JSON response'):
		wxpusher_kit.send_wxpusher('标题', '详情')


@pytest.mark.parametrize(
	('payload', 'error'),
	[
		([], 'invalid response'),
		({'code': 1001, 'msg': 'invalid token'}, 'invalid token'),
		({'code': 200, 'msg': 'wrong service code'}, 'wrong service code'),
		({'code': 1000, 'success': False, 'msg': 'rejected'}, 'rejected'),
		({'code': 1000}, 'invalid delivery results'),
		({'code': 1000, 'data': []}, 'invalid delivery results'),
		({'code': 1000, 'data': {}}, 'invalid delivery results'),
		({'code': 1000, 'data': [None]}, 'invalid delivery result'),
		(
			{'code': 1000, 'data': [{'code': 1000}, {'code': 1001, 'status': 'user not subscribed'}]},
			'user not subscribed',
		),
	],
)
def test_wxpusher_api_failure(wxpusher_kit, wxpusher_http, payload, error):
	_, client = wxpusher_http
	client.post.return_value = httpx.Response(200, json=payload)
	with pytest.raises(RuntimeError, match=f'WxPusher request failed: {error}'):
		wxpusher_kit.send_wxpusher('标题', '详情')


@pytest.mark.parametrize('timeout', [False, True])
def test_wxpusher_failure_does_not_block_other_channels(wxpusher_kit, wxpusher_http, monkeypatch, capsys, timeout):
	_, client = wxpusher_http
	if timeout:
		client.post.side_effect = httpx.ReadTimeout('timed out')
	else:
		client.post.return_value = httpx.Response(200, json={'code': 1001, 'msg': 'invalid token'})
	other_channels = []
	for method in (
		'send_email',
		'send_pushplus',
		'send_serverPush',
		'send_dingtalk',
		'send_feishu',
		'send_wecom',
		'send_gotify',
		'send_telegram',
		'send_bark',
	):
		mock = MagicMock()
		monkeypatch.setattr(wxpusher_kit, method, mock)
		other_channels.append(mock)
	wxpusher_kit.push_message('标题', '详情')
	for mock in other_channels:
		mock.assert_called_once()
	output = capsys.readouterr().out
	assert '[WxPusher]: Message push failed!' in output
	assert '[Bark]: Message push successful!' in output
	assert 'AT_test_token' not in output


def test_wxpusher_success_code_is_not_accepted_by_pushplus(wxpusher_kit, wxpusher_http, monkeypatch):
	monkeypatch.setattr(wxpusher_kit, 'pushplus_token', 'test_pushplus_token')
	with pytest.raises(RuntimeError, match='PushPlus request failed'):
		wxpusher_kit.send_pushplus('标题', '详情')
