"""
Web Proxy
"""

import asyncio
import base64
import logging
import re
import sys
import traceback
from typing import Dict, List, Optional, Tuple, Union, Any, Callable, Awaitable, cast  # pyright: ignore[reportDeprecated]

import pyppeteer

from . import utils

if sys.version_info >= (3, 8):
    from typing import TYPE_CHECKING
else:
    from typing_extensions import TYPE_CHECKING


class EnumResourceType(object):
    """资源类型"""

    Document: str = "Document"
    Stylesheet: str = "Stylesheet"
    Image: str = "Image"
    Media: str = "Media"
    Font: str = "Font"
    Script: str = "Script"
    TextTrack: str = "TextTrack"
    XHR: str = "XHR"
    Fetch: str = "Fetch"
    Prefetch: str = "Prefetch"
    EventSource: str = "EventSource"
    WebSocket: str = "WebSocket"
    Manifest: str = "Manifest"
    SignedExchange: str = "SignedExchange"
    Ping: str = "Ping"
    CSPViolationReport: str = "CSPViolationReport"
    Preflight: str = "Preflight"
    FedCM: str = "FedCM"
    Other: str = "Other"


class EnumProxyStage(object):
    """代理阶段"""

    Request: str = "request"
    Response: str = "response"


class EnumProxyType(object):
    """代理类型"""

    Block: int = 1
    Continue: int = 2
    Mock: int = 3


class ProxyRule(object):
    """代理规则基类

    用于定义请求拦截规则和响应处理逻辑
    """

    def __init__(
        self,
        url_pattern: str,
        callback: Callable[
            [Union["WebRequest", "WebResponse"]],  # pyright: ignore[reportDeprecated]
            Union[Dict[str, Any], Awaitable[Dict[str, Any]], None],  # pyright: ignore[reportDeprecated]
        ],
        resource_type: Optional[str] = None,  # pyright: ignore[reportDeprecated]
        stage: Optional[str] = None,  # pyright: ignore[reportDeprecated]
        methods: Optional[List[str]] = None,  # pyright: ignore[reportDeprecated]
    ) -> None:
        """初始化代理规则

        Args:
            url_pattern: URL 匹配模式，支持通配符 *
            callback: 回调函数
            resource_type: 资源类型，如 'Document', 'Script', 'Stylesheet', 'Image', 'XHR', 'Fetch', 'WebSocket' 等
            stage: 代理阶段
            methods: HTTP 方法列表，如 ['GET', 'POST']，None 表示匹配所有方法
        """
        self._url_pattern = url_pattern
        self._callback = callback
        self._resource_type = resource_type
        self._stage = stage or EnumProxyStage.Request
        self._methods = methods or []

    @property
    def stage(self) -> str:
        """拦截阶段"""
        return self._stage

    def matches(self, request: Any) -> bool:  # pyright: ignore[reportUnknownParameterType]
        """检查请求是否匹配此规则

        Args:
            request: pyppeteer 的 Request 对象（用于匹配）

        Returns:
            bool: 是否匹配
        """
        # pyppeteer Request 对象 / Fetch 适配对象
        url: str = request.url
        method: str = request.method

        # 检查 URL 模式
        if not self._match_url(url):
            return False

        # 检查 HTTP 方法
        if self._methods and method not in self._methods:
            return False

        # 检查资源类型（Fetch 模式下必须在 Python 里再过滤一次，
        # 因为 Fetch.enable 只用 "*" 通配，CDP 不再按 resourceType 分流。
        # 旧 Network 模式下这一步本来由 CDP 的 pattern 完成，
        # 现在补上后，Document 规则不会误伤同 URL 的 Script/XHR 请求。）
        if self._resource_type:
            resource_type = getattr(request, "resourceType", None)
            if resource_type and resource_type != self._resource_type:
                return False

        return True

    def _match_url(self, url: str) -> bool:
        """匹配 URL 模式

        支持 * 通配符，如 '*/web/book/read*' 或 '*/weread.qq.com/*'

        Args:
            url: 实际的 URL

        Returns:
            bool: 是否匹配
        """
        pattern = self._url_pattern.replace("*", ".*")
        # 确保模式匹配完整字符串
        if not pattern.startswith(".*"):
            pattern = "^.*" + pattern
        if not pattern.endswith(".*"):
            pattern = pattern + ".*$"

        return re.match(pattern, url) is not None

    async def handle_request(self, request: "WebRequest") -> Optional[Dict[str, Any]]:  # pyright: ignore[reportDeprecated]
        """处理请求，返回响应

        调用 callback 函数并返回其结果。支持同步和异步 callback。

        Args:
            request (WebRequest): WebRequest 请求对象

        Returns:
            dict: 包含响应信息的字典，格式为 {
                'status': int,
                'headers': dict,
                'body': bytes
            }
            或者 None 表示不处理此请求（继续原请求）
        """
        # 调用 callback 函数
        try:
            result = self._callback(request)

            # 如果 callback 是协程，等待其完成
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as ex:
            logging.warning(
                "[%s] Handle request [%s]%s failed: \n%s"
                % (
                    self.__class__.__name__,
                    request.method,
                    request.url,
                    traceback.format_exc(),
                )
            )
            return {"type": EnumProxyType.Continue}
        return result

    async def handle_response(
        self, response: "WebResponse"
    ) -> Optional[Dict[str, Any]]:  # pyright: ignore[reportDeprecated]
        """处理响应

        调用 callback 函数处理响应。支持同步和异步 callback。

        Args:
            response (WebResponse): WebResponse 响应对象

        Returns:
            dict: 包含自定义响应信息的字典，格式为 {
                'status': int,
                'headers': dict,
                'body': bytes
            }
            或者 None 表示不修改响应
        """
        try:
            result = self._callback(response)

            # 如果 callback 是协程，等待其完成
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as ex:
            logging.warning(
                "[%s] Handle response for %s failed: \n%s"
                % (self.__class__.__name__, response.url, traceback.format_exc())
            )
            return None

        return result

    def get_interception_pattern(self) -> Dict[str, Any]:  # pyright: ignore[reportDeprecated]
        """获取用于 CDP Network.setRequestInterception 的模式

        仅保留作兼容 / 调试。Chrome 156+ 已删除这个方法，
        实际拦截改用 get_fetch_pattern() + Fetch.enable。

        Returns:
            dict: CDP 拦截模式
        """
        pattern: Dict[str, Any] = {"urlPattern": self._url_pattern}  # pyright: ignore[reportDeprecated]
        if self._resource_type:
            pattern["resourceType"] = self._resource_type
        if self._stage != EnumProxyStage.Request:
            pattern["interceptionStage"] = "Response"
        return pattern

    def get_fetch_pattern(self) -> Dict[str, Any]:
        """获取用于 CDP Fetch.enable 的模式

        Fetch 用 requestStage 而不是 interceptionStage。
        """
        pattern: Dict[str, Any] = {
            "urlPattern": self._url_pattern,
            "requestStage": "Request"
            if self._stage == EnumProxyStage.Request
            else "Response",
        }
        if self._resource_type:
            pattern["resourceType"] = self._resource_type
        return pattern


class WebRequest:
    """Web请求封装类

    封装HTTP请求的基本信息，包括URL、方法、请求头和请求体。
    用于在代理处理中传递请求信息。
    """

    def __init__(
        self, url: str, method: str, headers: Dict[str, str], body: Optional[bytes]  # pyright: ignore[reportDeprecated]
    ) -> None:
        """初始化 WebRequest

        Args:
            url (str): 请求的URL地址
            method (str): HTTP请求方法（GET、POST等）
            headers (dict): 请求头字典
            body (bytes): 请求体内容
        """
        self._url: str = url
        self._method: str = method
        self._headers: Dict[str, str] = headers  # pyright: ignore[reportDeprecated]
        self._body: bytes | None = body

    @staticmethod
    def from_request(request: Any) -> "WebRequest":  # pyright: ignore[reportUnknownParameterType]
        """从 pyppeteer 的 Request 对象创建 WebRequest 对象

        Args:
            request: pyppeteer 的 Request 对象

        Returns:
            WebRequest: 新创建的 WebRequest 对象
        """
        # 获取请求 URL
        url: str = request.url

        # 获取请求方法
        method: str = request.method if request.method else "GET"

        # 获取请求头
        headers: Dict[str, str] = dict(request.headers) if request.headers else {}  # pyright: ignore[reportDeprecated]

        # 获取请求体（POST 数据）
        body: Optional[bytes] = None  # pyright: ignore[reportDeprecated]
        if request.postData:
            if isinstance(request.postData, bytes):
                body = request.postData
            else:
                body = request.postData.encode("utf-8")

        return WebRequest(url=url, method=method, headers=headers, body=body)

    @property
    def url(self) -> str:
        """获取请求URL"""
        return self._url

    @property
    def method(self) -> str:
        """获取HTTP请求方法"""
        return self._method

    @property
    def headers(self) -> Dict[str, str]:  # pyright: ignore[reportDeprecated]
        """获取请求头字典"""
        return self._headers

    @property
    def body(self) -> Optional[bytes]:  # pyright: ignore[reportDeprecated]
        """获取请求体内容"""
        return self._body

    @property
    def content(self) -> str:
        if self._body:
            return self._body.decode("utf-8")
        return ""


class WebResponse:
    """Web请求响应类"""

    def __init__(
        self, request: WebRequest, status: int, headers: Dict[str, str], body: bytes  # pyright: ignore[reportDeprecated]
    ) -> None:
        """初始化

        Args:
            request: WebRequest 对象
            status (int): HTTP 状态码
            headers (dict): 响应头字典
            body (bytes): 响应体内容
        """
        self._request: WebRequest = request
        self._status: int = status
        self._headers: Dict[str, str] = {k.lower(): v for k, v in headers.items()}  # pyright: ignore[reportDeprecated]
        self._body: bytes = body

    @property
    def url(self) -> str:
        """获取响应对应的URL"""
        return self._request.url

    @property
    def status(self) -> int:
        """HTTP状态码"""
        return self._status

    @property
    def ok(self) -> bool:
        """判断响应是否成功

        Returns:
            bool: 如果状态码在200-299范围内返回True，否则返回False
        """
        return self._status == 0 or 200 <= self._status <= 299

    @property
    def headers(self) -> Dict[str, str]:  # pyright: ignore[reportDeprecated]
        """获取响应头字典

        所有header名称都是小写的。
        """
        return self._headers

    @property
    def body(self) -> bytes:
        return self._body

    @property
    def content(self) -> str:
        if self._body:
            return self._body.decode("utf-8")
        return ""

    @property
    def request(self) -> WebRequest:
        """获取关联的 Request 对象"""
        return self._request


class _FetchRequest:
    """Fetch.requestPaused 事件的适配器

    让现有的 WebProxy.handle_request() 不用改逻辑就能用在 Fetch 域上：
    它只要求 request 对象有 url / method / headers / postData /
    resourceType / _interceptionHandled，以及 continue_() / respond() /
    abort() 这三个异步方法——原来是 pyppeteer 的 Request，现在由
    Fetch.continueRequest / fulfillRequest / failRequest 实现。

    关键点：
    - 每个 requestId 只放行 / 回包 / 中断一次，重复调用直接忽略，
      否则 Chrome 会报 request not found / already handled。
    - respond() 里去掉 content-length / content-encoding 等头，
      因为 body 被改过（注入 hook 脚本），旧长度会把页面截断；
      Fetch.fulfillRequest 不给 content-length 时 Chrome 会自己算。
    - continue_() 不传 headers，保持 Chrome 原始请求不变。
      Document 的 cookie 改动只影响 Python 侧用 aiohttp 重新拉取
      的那一次（见 handle_request 里的 get_http_response），
      和原来 Network 拦截时的行为一致。
    """

    def __init__(self, proxy: "WebProxy", event: Dict[str, Any]) -> None:
        self._proxy = proxy
        self._client: Any = proxy._client
        self._request_id: str = event.get("requestId", "")
        req: Dict[str, Any] = event.get("request", {}) or {}
        self.url: str = req.get("url", "")
        self.method: str = req.get("method", "GET")
        # Fetch 事件里的 headers 是 dict；复制一份，回调改它不会影响原事件
        self.headers: Dict[str, str] = dict(req.get("headers", {}) or {})
        self.postData: Any = req.get("postData")
        self.resourceType: Any = event.get("resourceType")
        self._interceptionHandled: bool = False
        self._handled: bool = False

    async def _send_once(self, method: str, params: Dict[str, Any]) -> bool:
        if self._handled:
            return False
        self._handled = True
        self._interceptionHandled = True
        try:
            await self._client.send(method, params)
        except Exception as ex:
            logging.warning(
                "[_FetchRequest] %s %s failed: %s" % (method, self.url, ex)
            )
            return False
        return True

    async def continue_(self) -> None:
        await self._send_once(
            "Fetch.continueRequest", {"requestId": self._request_id}
        )

    async def respond(self, result: Dict[str, Any]) -> None:
        status: int = int(result.get("status", 200) or 200)
        headers_in = result.get("headers", {}) or {}
        # headers 可能是 dict，也可能是 aiohttp 的 multidict / list
        headers_list: List[Dict[str, str]] = []
        if isinstance(headers_in, dict):
            items = list(headers_in.items())
        elif isinstance(headers_in, (list, tuple)):
            items = list(headers_in)
        else:
            items = []
        skip = {
            "content-length",
            "transfer-encoding",
            "content-encoding",
            "connection",
            "keep-alive",
        }
        for item in items:
            try:
                name, value = item[0], item[1]
            except Exception:
                continue
            if str(name).lower() in skip:
                continue
            headers_list.append(
                {"name": str(name), "value": str(value)}
            )
        body = result.get("body", b"")
        if body is None:
            body_bytes = b""
        elif isinstance(body, str):
            body_bytes = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            body_bytes = bytes(body)
        else:
            body_bytes = str(body).encode("utf-8")
        await self._send_once(
            "Fetch.fulfillRequest",
            {
                "requestId": self._request_id,
                "responseCode": status,
                "responseHeaders": headers_list,
                "body": base64.b64encode(body_bytes).decode("ascii"),
            },
        )

    async def abort(self) -> None:
        await self._send_once(
            "Fetch.failRequest",
            {"requestId": self._request_id, "errorReason": "Aborted"},
        )


class WebProxy(object):
    """Web Proxy

    使用代理规则处理网络请求拦截和响应
    """

    def __init__(self, page: pyppeteer.page.Page, proxy_rules: List[ProxyRule]) -> None:  # pyright: ignore[reportDeprecated]
        """初始化 Web 代理

        Args:
            page: pyppeteer.page.Page 实例
            proxy_rules: ProxyRule 对象列表
        """
        self._page: pyppeteer.page.Page = page
        self._client: Any = page._client
        self._proxy_rules: List[ProxyRule] = proxy_rules  # pyright: ignore[reportDeprecated]

    async def setup_interception(self) -> None:
        """启用网络请求拦截（Fetch.enable 版，Chrome 156+）

        原来用的是 CDP 的 Network.setRequestInterception，Chrome 156
        已删除它（Puppeteer 3.27 changelog：Regenerating the protocol
        against Chrome 156 drops ... the protocol-level
        setRequestInterception），在 Chrome 157 上会报
        "Network.setRequestInterception wasn't found"，随后 hook.js
        注入失败、get_markdown 报 canvasContextHandler is not defined。

        改法：
        - 不再碰 pyppeteer 的 NetworkManager 拦截开关——它内部同样会
          调 Network.setRequestInterception，留着只会让 pyppeteer
          自己再去调那个已删除的方法。两个开关都保持 False。
        - 用 Fetch.enable + Fetch.requestPaused。Fetch 是 Chrome 156+
          里 setRequestInterception 的官方替代，高层
          page.setRequestInterception 在新版 Puppeteer 里走的也是
          Fetch 域。
        - 这里只用一个 "*" Request 阶段 pattern，真正的 URL /
          resourceType 匹配仍在 Python 里由 ProxyRule.matches()
          做（和原来 get_rule 的逻辑一致），避免 Fetch 的
          urlPattern 写法差异导致某条规则收不到事件。
          非 http(s) 请求不拦截，直接放行。
        """
        logging.info("[%s] Setup interception (Fetch.enable)" % self.__class__.__name__)
        # 关键：不要启用 pyppeteer 的 Network 拦截，否则它内部会再调
        # 已删除的 Network.setRequestInterception。
        try:
            self._page._networkManager._userRequestInterceptionEnabled = False
            self._page._networkManager._protocolRequestInterceptionEnabled = False
        except Exception:
            pass
        try:
            await self._client.send(
                "Network.setCacheDisabled",
                {"cacheDisabled": True},
            )
        except Exception as ex:
            logging.warning(
                "[%s] Network.setCacheDisabled failed (ignored): %s"
                % (self.__class__.__name__, ex)
            )
        await self._client.send(
            "Fetch.enable",
            {
                "patterns": [{"urlPattern": "*", "requestStage": "Request"}],
                "handleAuthRequests": False,
            },
        )
        self._client.on(
            "Fetch.requestPaused",
            lambda event: asyncio.ensure_future(self.handle_fetch_paused(event)),
        )
        logging.info(
            "[%s] Fetch interception enabled" % self.__class__.__name__
        )

    async def handle_fetch_paused(self, event: Dict[str, Any]) -> None:
        """处理 Fetch.requestPaused 事件

        把事件包成 _FetchRequest，复用原来的 handle_request()，
        规则回调（on_document_request / on_document_response /
        on_hook_script_request / on_log_request 等）完全不用改。
        """
        request_id = event.get("requestId")
        if not request_id:
            return
        req: Dict[str, Any] = event.get("request", {}) or {}
        url: str = req.get("url", "")
        # 非 http(s)（data: / blob: / chrome: / about: 等）直接放行，
        # 不要走规则逻辑，避免无谓的暂停。
        if not url.startswith(("http://", "https://")):
            try:
                await self._client.send(
                    "Fetch.continueRequest", {"requestId": request_id}
                )
            except Exception:
                pass
            return
        # 我们只开了 Request 阶段，万一收到 Response 阶段事件也直接放行。
        if event.get("responseStatusCode") is not None:
            try:
                await self._client.send(
                    "Fetch.continueRequest", {"requestId": request_id}
                )
            except Exception:
                pass
            return
        fake = _FetchRequest(self, event)
        try:
            await self.handle_request(fake)
        except Exception:
            logging.exception(
                "[%s] handle_fetch_paused failed for %s" % (self.__class__.__name__, url)
            )
            # 兜底：绝不能让请求一直暂停，否则页面会卡死。
            try:
                await fake.continue_()
            except Exception:
                pass

    async def get_http_response(self, request: WebRequest) -> Optional[WebResponse]:  # pyright: ignore[reportDeprecated]
        """通过aiohttp获取http返回包，返回WebResponse对象

        Args:
            request (WebRequest): WebRequest 请求对象

        Returns:
            WebResponse 对象
        """
        # 获取请求 URL
        print("get_http_response", request.url)
        url = request.url

        # 获取请求头并转换为字典
        headers: Dict[str, str] = {}  # pyright: ignore[reportDeprecated]
        for key, value in request.headers.items():
            headers[key] = value

        try:
            # 使用 utils.fetch 替代 aiohttp 直接调用
            result = await utils.fetch(
                url=url,
                method=request.method,
                headers=headers,
                respond_with_headers=True,
            )
            status, response_headers, body = cast(
                Tuple[int, Dict[str, str], bytes], result  # pyright: ignore[reportDeprecated]
            )

            # 返回 WebResponse 对象
            return WebResponse(
                request=request,
                status=status,
                headers=response_headers,
                body=body,
            )
        except Exception as ex:
            logging.warning(
                "[WebProxy] Get HTTP response failed for %s: %s" % (url, str(ex))
            )
            return None

    def get_rule(
        self, request: Any  # pyright: ignore[reportAny]
    ) -> Tuple[Optional[ProxyRule], Optional[ProxyRule]]:  # pyright: ignore[reportDeprecated]
        """获取与request匹配的规则

        Args:
            request: pyppeteer 的 Request 对象

        Returns:
            tuple: (request_rule, response_rule)
        """
        request_rule: Optional[ProxyRule] = None  # pyright: ignore[reportDeprecated]
        response_rule: Optional[ProxyRule] = None  # pyright: ignore[reportDeprecated]

        # 遍历所有规则，查找匹配的规则
        for rule in self._proxy_rules:
            if rule.matches(request):
                if rule.stage == EnumProxyStage.Request and request_rule is None:
                    request_rule = rule
                elif rule.stage == EnumProxyStage.Response and response_rule is None:
                    response_rule = rule

        return request_rule, response_rule

    async def handle_request(self, request: Any) -> None:  # pyright: ignore[reportAny]
        """处理请求

        遍历所有代理规则，找到第一个匹配的规则并调用其 handle 方法

        Args:
            request: pyppeteer 的 Request 对象
        """
        if request._interceptionHandled:  # pyright: ignore[reportAny]
            logging.warning(
                "[%s] Ignore handled request %s %s %s"
                % (
                    self.__class__.__name__,
                    request.method,
                    request.resourceType,
                    request.url,
                )
            )
            return
        logging.info(
            "[%s] Handle request %s %s %s"
            % (
                self.__class__.__name__,
                request.method,
                request.resourceType,
                request.url,
            )
        )
        request_rule, response_rule = self.get_rule(request)
        if request_rule:
            # 将 pyppeteer Request 转换为 WebRequest
            web_request = WebRequest.from_request(request)
            result = await request_rule.handle_request(web_request)
            if not isinstance(result, dict):
                logging.warning(
                    "[WebProxy] Unexpected return type %s from callback, continuing request: %s"
                    % (type(result), request.url)
                )
                await request.continue_()
                return
            proxy_type: int = result.pop("type", 0)
            if proxy_type == EnumProxyType.Mock:
                await request.respond(result)
                return
            elif proxy_type == EnumProxyType.Block:
                await request.abort()
                return
            elif not response_rule:
                await request.continue_()
                return

            response = await self.get_http_response(web_request)
            if not response:
                # 没有获取到回包
                await request.continue_()
                return
            result = await response_rule.handle_response(response)
            if result:
                await request.respond(result)
            else:
                await request.continue_()
            return

        # 没有匹配的 request 规则，继续原请求
        logging.info(
            "[WebProxy] No request rule matched, continue request: %s" % request.url
        )
        await request.continue_()

    def get_interception_patterns(self) -> List[Dict[str, Any]]:
        """获取所有规则的拦截模式（旧 Network 版，仅保留作兼容）

        Returns:
            list: CDP 拦截模式列表
        """
        patterns: List[Dict[str, Any]] = []
        for rule in self._proxy_rules:
            patterns.append(rule.get_interception_pattern())
        return patterns

    def get_fetch_patterns(self) -> List[Dict[str, Any]]:
        """获取所有规则的 Fetch 拦截模式"""
        patterns: List[Dict[str, Any]] = []
        for rule in self._proxy_rules:
            patterns.append(rule.get_fetch_pattern())
        return patterns
