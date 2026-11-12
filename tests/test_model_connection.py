"""自定义连接只能发送给已验证的公开服务商。"""
import json
import socket

import httpx
import pytest

from nl2sql import model_connection
from nl2sql.model_connection import PublicModelTransport, normalize_base_url, probe_connection


@pytest.mark.parametrize('url', ['http://example.com/v1', 'https://localhost/v1', 'https://127.0.0.1/v1',
                                 'https://[::ffff:127.0.0.1]/v1', 'https://example.com/v1?token=test',
                                 'https://test@example.com/v1', 'https://169.254.169.254/v1'])
def test_invalid_endpoint_urls_are_rejected(url):
    with pytest.raises(ValueError):
        normalize_base_url(url)


def test_transport_pins_public_ip_and_keeps_tls_domain(monkeypatch):
    destination = []
    transport = PublicModelTransport('https://api.example.com/v1')

    def send(request):
        destination.append((request.url.host, request.headers['Host'], request.extensions['sni_hostname']))
        return httpx.Response(200, json={})

    def resolve(address):
        return lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]

    monkeypatch.setattr(transport._transport, 'handle_request', send)
    request = httpx.Request('POST', 'https://api.example.com/v1/chat/completions')
    monkeypatch.setattr(socket, 'getaddrinfo', resolve('1.1.1.1'))
    assert transport.handle_request(request).status_code == 200
    monkeypatch.setattr(socket, 'getaddrinfo', resolve('127.0.0.1'))
    with pytest.raises(httpx.ConnectError, match='公开地址'):
        transport.handle_request(request)
    with pytest.raises(httpx.ConnectError, match='目的地'):
        transport.handle_request(httpx.Request('POST', 'https://other.example.com/v1/chat/completions'))
    assert destination == [('1.1.1.1', 'api.example.com', 'api.example.com')]


@pytest.mark.parametrize('status,reply,ok,reason', [
    (200, {'choices': [{'message': {'content': 'OK'}}]}, True, '模型调用成功'),
    (200, {'choices': [{}]}, False, '响应中没有模型回复'),
    (200, {'error': {'message': '模型维护中'}}, False, '模型维护中'),
    (401, {'error': {'message': 'Invalid key test-connection-key'}}, False, 'Invalid key [已隐藏]'),
    (404, {}, False, '接口路径或模型不存在'),
    (429, {'error': {'message': 'Quota exceeded'}}, False, 'Quota exceeded'),
    (307, {}, False, '接口返回跳转'),
])
def test_connection_probe_uses_chat_completions_and_reports_provider_response(monkeypatch, status, reply, ok, reason):
    """验证实际请求格式、失败原因、密钥遮盖以及禁止跳转。"""
    calls = []

    def send(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=reply, headers={'Location': 'https://other.example.com/v1'})

    monkeypatch.setattr(model_connection, 'PublicModelTransport', lambda base_url: httpx.MockTransport(send))
    result = probe_connection('https://api.example.com/v1', 'demo-model', 'test-connection-key', custom=True)
    assert result['ok'] is ok and result['status_code'] == status
    assert reason in result['message'] and 'test-connection-key' not in result['message']
    assert len(calls) == 1
    request = calls[0]
    assert request.method == 'POST' and request.url.path == '/v1/chat/completions'
    assert request.headers['Authorization'] == 'Bearer test-connection-key'
    payload = json.loads(request.content)
    assert payload['model'] == 'demo-model' and payload['max_tokens'] == 32
    assert len(payload['messages']) == 1 and payload['messages'][0]['role'] == 'user'


def test_connection_probe_reports_timeout(monkeypatch):
    """连接超时显示原因，且不返回密钥。"""
    def send(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('timeout', request=request)

    monkeypatch.setattr(model_connection, 'PublicModelTransport', lambda base_url: httpx.MockTransport(send))
    result = probe_connection('https://api.example.com/v1', 'demo-model', 'test-connection-key', custom=True)
    assert result['ok'] is False and result['status_code'] is None
    assert '超时' in result['message']
