"""自定义模型连接的地址校验和 HTTPS 传输。"""
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlsplit

import httpx


def _public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def normalize_base_url(value: str) -> str:
    """校验公开 HTTPS 接口地址，兼容粘贴完整的 Chat Completions URL。"""
    value = value.strip()
    if not value or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value:
        raise ValueError("请输入有效的公开 HTTPS 接口地址")
    try:
        parsed = urlsplit(value)
        url = httpx.URL(value)
        if (url.scheme != "https" or not url.host or parsed.username is not None or parsed.password is not None
                or "?" in value or "#" in value or url.host.endswith(".") or "%" in parsed.netloc):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise ValueError("请输入公开的 HTTPS 接口地址，地址中无需填写密钥、查询参数或片段") from None
    host = url.host.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("自定义模型接口必须使用公开地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _public_ip(host):
            raise ValueError("自定义模型接口必须使用公开地址")
    return str(url).rstrip("/").removesuffix("/chat/completions").rstrip("/")


class PublicModelTransport(httpx.BaseTransport):
    """将连接固定在已校验的公网 IP，TLS 仍校验原域名。"""

    def __init__(self, base_url: str) -> None:
        self._origin = httpx.URL(base_url)
        self._transport = httpx.HTTPTransport(trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """只向配置的服务商发送请求，禁止跳转和访问内网。"""
        url = request.url
        if (url.scheme, url.host, url.port) != (self._origin.scheme, self._origin.host, self._origin.port):
            raise httpx.ConnectError("模型请求目的地与配置不一致", request=request)
        try:
            addresses = socket.getaddrinfo(url.host, url.port or 443, type=socket.SOCK_STREAM)
            ips = list(dict.fromkeys(item[4][0] for item in addresses))
            if not ips or not all(_public_ip(ip) for ip in ips):
                raise ValueError
        except (OSError, ValueError):
            raise httpx.ConnectError("模型接口无法解析为公开地址", request=request) from None
        pinned = httpx.Request(request.method, url.copy_with(host=ips[0]), headers=request.headers,
                               stream=request.stream, extensions={**request.extensions, "sni_hostname": url.raw_host.decode("ascii")})
        return self._transport.handle_request(pinned)

    def close(self) -> None:
        """释放连接池。"""
        self._transport.close()


def normalize_api_key(value: str) -> str:
    """检查密钥能安全地作为 HTTP 请求头发送。"""
    value = value.strip()
    if len(value) > 4096 or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise ValueError("请输入有效的 API Key")
    return value


def _safe_reason(message: str, api_key: str) -> str:
    message = message.replace(api_key, "[已隐藏]") if api_key else message
    message = re.sub(r"(?i)(?:bearer\s+|\b(?:api[_ -]?key|authorization|token)\s*[:=]\s*)[\"']?[^\s,;\"']+",
                     "[已隐藏]", message)
    return " ".join(message.split())[:400]


def _response_error(result: object) -> str:
    if not isinstance(result, dict):
        return ''
    error = result.get('error')
    if isinstance(error, dict):
        error = error.get('message') or error.get('code')
    message = error or result.get('message')
    return message if isinstance(message, str) else ''


def probe_connection(base_url: str, model: str, api_key: str, *, custom: bool,
                     extra_body: dict | None = None) -> dict:
    """发一次最小 Chat Completions 请求，返回耗时或去除密钥后的失败原因。"""
    started = time.monotonic()
    status = None
    reason = ''
    transport = PublicModelTransport(base_url) if custom else None
    limit_name = 'max_completion_tokens' if model.startswith(('gpt-5', 'gpt-6', 'o1', 'o3', 'o4')) else 'max_tokens'
    payload = {'model': model, 'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
               limit_name: 32, **(extra_body or {})}
    try:
        with httpx.Client(transport=transport, trust_env=not custom, follow_redirects=False,
                          timeout=httpx.Timeout(12, connect=5)) as client:
            with client.stream('POST', base_url.rstrip('/') + '/chat/completions', json=payload,
                               headers={'Authorization': f'Bearer {api_key}'}) as response:
                status = response.status_code
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    if len(data) + len(chunk) > 65536:
                        break
                    data.extend(chunk)
                    if time.monotonic() - started > 15:
                        raise httpx.ReadTimeout('等待模型回复超时')
        try:
            result = json.loads(data)
        except ValueError:
            result = None
        choices = result.get('choices') if isinstance(result, dict) else None
        if 300 <= status < 400:
            reason = '接口返回跳转，请填写最终的 Base URL'
        elif status >= 400:
            reason = _response_error(result) or {
                401: '身份验证失败，请核对 API Key', 403: '无权访问此模型',
                404: '接口路径或模型不存在', 429: '接口限流或额度不足',
            }.get(status, '服务商拒绝了请求')
        elif not (isinstance(choices, list) and choices and isinstance(choices[0], dict)
                  and isinstance(choices[0].get('message'), dict)):
            reason = _response_error(result) or '响应中没有模型回复，请确认地址支持 Chat Completions 接口'
    except httpx.TimeoutException:
        reason = '连接或等待模型回复超时'
    except httpx.ConnectError as error:
        reason = f'连接失败：{error}'
    except (httpx.RequestError, httpx.InvalidURL) as error:
        reason = f'接口请求失败：{error}'
    return {'ok': not reason, 'status_code': status, 'elapsed_ms': round((time.monotonic() - started) * 1000),
            'message': _safe_reason(reason, api_key) if reason else '模型调用成功'}
