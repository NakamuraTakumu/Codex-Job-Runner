"""Function-tool definitions and the private MCP transport."""

from __future__ import annotations

import concurrent.futures
import inspect
import json
import queue
import re
import secrets
import threading
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, get_type_hints

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, create_model


_MCP_PROTOCOL_VERSION = "2025-06-18"
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_REQUEST_BYTES = 1048576
_MAX_BATCH_REQUESTS = 64
_STOP_WORKER = object()


class _ToolContractError(ValueError):
    pass


class _ToolHandlerError(RuntimeError):
    pass


class _DaemonExecutor:
    """Bounded daemon workers that cannot keep the interpreter alive."""

    def __init__(self, max_workers):
        self._queue = queue.Queue()
        self._lock = threading.RLock()
        self._closed = False
        self._threads = []
        for index in range(max_workers):
            thread = threading.Thread(
                target=self._run,
                name=f"codex_tool_{index}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def submit(self, function, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("tool executor is closed")
            future = concurrent.futures.Future()
            self._queue.put((future, function, args, kwargs))
            return future

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pending is not _STOP_WORKER:
                    pending[0].cancel()
            for _ in self._threads:
                self._queue.put(_STOP_WORKER)

    def _run(self):
        while True:
            item = self._queue.get()
            if item is _STOP_WORKER:
                return
            future, function, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = function(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)


class Tool:
    """Register one synchronous Python callable as a model-callable tool."""

    def __init__(
        self,
        handler,
        prompt,
        *,
        args,
        returns,
        name=None,
    ):
        if not callable(handler):
            raise TypeError("tool handler must be callable")
        if inspect.iscoroutinefunction(handler):
            raise TypeError("async tool handlers are not supported")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("tool prompt must be a non-empty string")
        if not isinstance(returns, str) or not returns.strip():
            raise ValueError("tool returns must be a non-empty string")
        if not isinstance(args, Mapping):
            raise TypeError("tool args must be a mapping of argument names to descriptions")
        if not all(isinstance(key, str) for key in args):
            raise TypeError("tool args keys must be strings")

        resolved_name = handler.__name__ if name is None else name
        if not isinstance(resolved_name, str) or not resolved_name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not _is_tool_name(resolved_name):
            raise ValueError(
                "tool name must contain only letters, numbers, underscores, "
                "hyphens, or dots"
            )

        signature = inspect.signature(handler)
        try:
            type_hints = get_type_hints(handler, include_extras=True)
        except (NameError, TypeError) as exc:
            raise TypeError(f"tool annotations could not be resolved: {exc}") from exc

        parameters = tuple(signature.parameters.values())
        unsupported = [
            parameter.name
            for parameter in parameters
            if parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }
        ]
        if unsupported:
            raise TypeError(
                "tool handlers do not support positional-only, *args, or **kwargs: "
                + ", ".join(unsupported)
            )

        parameter_names = {parameter.name for parameter in parameters}
        description_names = set(args)
        missing_descriptions = sorted(parameter_names - description_names)
        extra_descriptions = sorted(description_names - parameter_names)
        if missing_descriptions:
            raise ValueError(
                "tool args is missing descriptions for: "
                + ", ".join(missing_descriptions)
            )
        if extra_descriptions:
            raise ValueError(
                "tool args contains unknown arguments: "
                + ", ".join(extra_descriptions)
            )

        fields = {}
        normalized_args = {}
        for parameter in parameters:
            description = args[parameter.name]
            if not isinstance(description, str) or not description.strip():
                raise ValueError(
                    f"tool argument description must be non-empty: {parameter.name}"
                )
            annotation = type_hints.get(parameter.name, parameter.annotation)
            if annotation is inspect.Parameter.empty:
                raise TypeError(
                    f"tool argument requires a type annotation: {parameter.name}"
                )
            default = (
                ...
                if parameter.default is inspect.Parameter.empty
                else parameter.default
            )
            fields[parameter.name] = (
                annotation,
                Field(default=default, description=description.strip()),
            )
            normalized_args[parameter.name] = description.strip()

        return_annotation = type_hints.get("return", signature.return_annotation)
        if return_annotation is inspect.Signature.empty:
            raise TypeError("tool handler requires a return type annotation")

        model_name = f"_CodexToolArguments_{resolved_name}"
        try:
            input_model = create_model(
                model_name,
                __config__=ConfigDict(extra="forbid"),
                **fields,
            )
            input_schema = input_model.model_json_schema()
            output_adapter = TypeAdapter(return_annotation)
            output_schema = output_adapter.json_schema()
        except Exception as exc:
            raise TypeError(f"tool schema could not be generated: {exc}") from exc

        self.handler = handler
        self.prompt = prompt.strip()
        self.args = normalized_args
        self.returns = returns.strip()
        self.name = resolved_name
        self.signature = signature
        self.input_schema = input_schema
        self.output_schema = output_schema
        self._input_model = input_model
        self._output_adapter = output_adapter

    def invoke(self, arguments):
        """Validate arguments and return a JSON-compatible validated result."""
        try:
            validated = self._input_model.model_validate(arguments)
        except ValidationError as exc:
            raise _ToolContractError(
                _validation_message("invalid tool arguments", exc)
            ) from exc
        keyword_arguments = {
            name: getattr(validated, name)
            for name in self.signature.parameters
        }
        try:
            result = self.handler(**keyword_arguments)
        except Exception as exc:
            raise _ToolHandlerError(type(exc).__name__) from exc
        if inspect.isawaitable(result):
            raise _ToolContractError("tool handler returned an awaitable")
        try:
            validated_result = self._output_adapter.validate_python(result)
            return self._output_adapter.dump_python(validated_result, mode="json")
        except ValidationError as exc:
            raise _ToolContractError(
                _validation_message("invalid tool result", exc)
            ) from exc

    def mcp_definition(self):
        """Return this tool's MCP tools/list representation."""
        definition = {
            "name": self.name,
            "description": f"{self.prompt}\n\nReturns: {self.returns}",
            "inputSchema": self.input_schema,
        }
        if self.output_schema.get("type") == "object":
            definition["outputSchema"] = self.output_schema
        return definition


class _ToolBridge:
    """One process-local Streamable HTTP MCP bridge shared by a client."""

    def __init__(self, tools, *, max_concurrency, tool_timeout):
        self._tools = {tool.name: tool for tool in tools}
        self._max_concurrency = max_concurrency
        self._tool_timeout = tool_timeout
        self._token = secrets.token_urlsafe(32)
        self._path = "/mcp/" + secrets.token_urlsafe(18)
        self._executor = _DaemonExecutor(max_concurrency)
        self._server = None
        self._thread = None
        self._lock = threading.RLock()

    @property
    def token(self):
        return self._token

    @property
    def url(self):
        with self._lock:
            if self._server is None:
                raise RuntimeError("tool bridge is not running")
            host, port = self._server.server_address
            return f"http://{host}:{port}{self._path}"

    def start(self):
        with self._lock:
            if self._server is not None:
                return
            bridge = self

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    bridge._handle_post(self)

                def do_GET(self):
                    bridge._write_json(
                        self,
                        HTTPStatus.METHOD_NOT_ALLOWED,
                        {"error": "GET is not supported"},
                    )

                def do_DELETE(self):
                    bridge._write_empty(self, HTTPStatus.NO_CONTENT)

                def log_message(self, format, *args):
                    return

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever,
                name="codex_tool_mcp_bridge",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()

    def close(self):
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._executor.close()

    def _handle_post(self, handler):
        if handler.path != self._path:
            self._write_json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        supplied_authorization = handler.headers.get("Authorization", "")
        if not secrets.compare_digest(
            supplied_authorization,
            f"Bearer {self._token}",
        ):
            self._write_json(
                handler,
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthorized"},
            )
            return
        try:
            raw_length = handler.headers.get("Content-Length")
            if raw_length is None or not raw_length.isdecimal():
                raise ValueError
            length = int(raw_length)
            if length > _MAX_REQUEST_BYTES:
                self._write_json(
                    handler,
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": "request body is too large"},
                )
                return
            payload = json.loads(handler.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._write_rpc_error(handler, None, -32700, "Parse error")
            return

        if isinstance(payload, list):
            if not payload or len(payload) > _MAX_BATCH_REQUESTS:
                self._write_json(
                    handler,
                    HTTPStatus.BAD_REQUEST,
                    _rpc_error(None, -32600, "Invalid Request"),
                )
                return
            responses = [
                response
                for item in payload
                if (response := self._dispatch(item)) is not None
            ]
            if responses:
                self._write_json(handler, HTTPStatus.OK, responses)
            else:
                self._write_empty(handler, HTTPStatus.ACCEPTED)
            return

        response = self._dispatch(payload)
        if response is None:
            self._write_empty(handler, HTTPStatus.ACCEPTED)
        else:
            self._write_json(handler, HTTPStatus.OK, response)

    def _dispatch(self, request):
        if not isinstance(request, dict):
            return _rpc_error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return _rpc_error(request_id, -32600, "Invalid Request")
        if request_id is None:
            return None
        if method == "initialize":
            return _rpc_result(
                request_id,
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "codex-job-runner-tools",
                        "version": "1",
                    },
                },
            )
        if method == "ping":
            return _rpc_result(request_id, {})
        if method == "tools/list":
            return _rpc_result(
                request_id,
                {"tools": [tool.mcp_definition() for tool in self._tools.values()]},
            )
        if method == "tools/call":
            return _rpc_result(request_id, self._call_tool(request.get("params")))
        return _rpc_error(request_id, -32601, "Method not found")

    def _call_tool(self, params):
        if not isinstance(params, dict):
            return _tool_error("tool call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments", {})
        tool = self._tools.get(name)
        if tool is None:
            return _tool_error("unknown tool")
        if not isinstance(arguments, dict):
            return _tool_error("tool arguments must be an object")
        try:
            future = self._executor.submit(tool.invoke, arguments)
        except RuntimeError:
            return _tool_error("tool bridge is closed")
        try:
            result = future.result(timeout=self._tool_timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return _tool_error("tool call timed out")
        except _ToolContractError as exc:
            return _tool_error(str(exc))
        except _ToolHandlerError as exc:
            return _tool_error(f"tool handler failed: {exc}")
        except Exception as exc:
            return _tool_error(f"tool execution failed: {type(exc).__name__}")
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        structured = result if isinstance(result, dict) else {"result": result}
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": False,
        }

    @staticmethod
    def _write_rpc_error(handler, request_id, code, message):
        _ToolBridge._write_json(
            handler,
            HTTPStatus.BAD_REQUEST,
            _rpc_error(request_id, code, message),
        )

    @staticmethod
    def _write_json(handler, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("MCP-Protocol-Version", _MCP_PROTOCOL_VERSION)
        handler.end_headers()
        handler.wfile.write(data)

    @staticmethod
    def _write_empty(handler, status):
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.end_headers()


def _normalize_tools(tools):
    if tools is None:
        return ()
    if isinstance(tools, Tool):
        raise TypeError("tools must be an iterable of Tool instances")
    try:
        normalized = tuple(tools)
    except TypeError as exc:
        raise TypeError("tools must be an iterable of Tool instances") from exc
    for tool in normalized:
        if not isinstance(tool, Tool):
            raise TypeError("tools must contain only Tool instances")
    names = [tool.name for tool in normalized]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError("duplicate tool names: " + ", ".join(duplicates))
    return normalized


def _is_tool_name(value):
    return _TOOL_NAME_PATTERN.fullmatch(value) is not None


def _validation_message(prefix, error):
    details = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        details.append(f"{location}: {item.get('msg', 'validation failed')}")
    return prefix + ": " + "; ".join(details)


def _rpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_error(message):
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }
