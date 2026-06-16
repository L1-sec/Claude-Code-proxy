import sys
import os
import json
import uuid
import queue
import time
import logging
import threading
import traceback
import httpx
from flask import Flask, request, Response, jsonify
from openai import OpenAI

class _Fmt(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record.created))},{int(record.msecs):03d}"

_fmt = _Fmt('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_h = logging.StreamHandler()
_h.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_h], force=True)

logger = logging.getLogger('proxy')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

API_KEY = "sk-************************************"
MODEL = "deepseek-v4-flash"
BASE_URL = "https://token.sensenova.cn/v1"

app = Flask(__name__)

_client = OpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    timeout=httpx.Timeout(600.0, connect=30.0, read=600.0),
)


@app.before_request
def _log_req():
    if request.method == 'POST':
        logger.info(f"→ {request.path}")


@app.after_request
def _log_res(response):
    if request.method == 'POST':
        logger.info(f"← {response.status_code}")
    return response


def convert_to_openai_messages(claude_messages):
    openai_messages = []

    for i, msg in enumerate(claude_messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            if content:
                openai_messages.insert(0, {"role": "system", "content": content})
            continue

        if role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    tool_calls.append({
                        "id": item.get("id", f"call_{uuid.uuid4().hex}"),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": json.dumps(item.get("input", {}))
                        }
                    })
            msg_content = " ".join(text_parts) if text_parts else None
            openai_msg = {"role": "assistant"}
            if msg_content:
                openai_msg["content"] = msg_content
            if tool_calls:
                openai_msg["tool_calls"] = tool_calls
            openai_messages.append(openai_msg)
            continue

        if role == "user" and isinstance(content, list):
            text_parts = []
            tool_results = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "tool_result":
                    tool_use_id = item.get("tool_use_id", "")
                    result_content = item.get("content", "")
                    if isinstance(result_content, list):
                        result_text = " ".join(
                            c.get("text", "") for c in result_content
                            if isinstance(c, dict) and c.get("type") == "text"
                        )
                    else:
                        result_text = str(result_content)
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_use_id,
                        "content": result_text
                    })
            if text_parts:
                openai_messages.append({"role": "user", "content": " ".join(text_parts)})
            openai_messages.extend(tool_results)
            continue

        if content:
            openai_messages.append({
                "role": role if role in ["user", "assistant"] else "user",
                "content": content if isinstance(content, str) else json.dumps(content)
            })

    if not openai_messages or openai_messages[0].get("role") != "system":
        openai_messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})

    return openai_messages


def convert_tools(tools):
    if not tools:
        return None
    openai_tools = []
    for tool in tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        schema = tool.get("input_schema", {})
        openai_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": schema
            }
        })
    logger.debug(f"  Tools: {[t['function']['name'] for t in openai_tools]}")
    return openai_tools if openai_tools else None


def convert_tool_choice(tool_choice):
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "auto")
        if tc_type == "any":
            return "required"
        elif tc_type == "tool":
            return {"type": "function", "function": {"name": tool_choice.get("name", "")}}
        elif tc_type == "none":
            return "none"
    return "auto"


@app.route("/v1/messages", methods=["POST", "OPTIONS"])
def messages():
    if request.method == "OPTIONS":
        return Response()

    try:
        data = request.get_json()
        tools = data.get("tools", None)
        tool_choice = data.get("tool_choice", None)

        openai_tools = convert_tools(tools)
        openai_tool_choice = convert_tool_choice(tool_choice)
        openai_messages = convert_to_openai_messages(data.get("messages", []))

        system = data.get("system", "")
        if system and (not openai_messages or openai_messages[0].get("role") != "system"):
            openai_messages.insert(0, {"role": "system", "content": system})

        kwargs = {
            "model": MODEL,
            "messages": openai_messages,
            "max_tokens": data.get("max_tokens", 8192),
            "temperature": data.get("temperature", 0.7),
            "top_p": data.get("top_p", 0.95),
        }

        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = openai_tool_choice

        if data.get("stream", False):
            return handle_stream(_client, kwargs)
        else:
            return handle_sync(_client, kwargs)

    except Exception as e:
        logger.exception(f"Request failed [path={request.path}, method={request.method}]")
        return jsonify({"error": {"type": "api_error", "message": str(e)}}), 500


def handle_sync(client, kwargs):
    try:
        response = client.chat.completions.create(**kwargs)
        choice = response.choices[0].message

        content_blocks = []

        if choice.content:
            content_blocks.append({"type": "text", "text": choice.content})

        if choice.tool_calls:
            for tc in choice.tool_calls:
                try:
                    parsed_input = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    if not isinstance(parsed_input, dict):
                        parsed_input = {"_value": parsed_input}
                except json.JSONDecodeError:
                    arguments = tc.function.arguments
                    parsed_input = {"command": arguments} if isinstance(arguments, str) and arguments.strip() else {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": parsed_input
                })

        stop_reason = "tool_use" if choice.tool_calls else "end_turn"

        claude_response = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content_blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0
            }
        }

        return jsonify(claude_response)

    except Exception as e:
        logger.exception("Sync response failed")
        raise


def handle_stream(client, kwargs):
    KEEPALIVE_INTERVAL = 8

    def generate():
        message_id = f"msg_{uuid.uuid4().hex}"
        chunk_queue = queue.Queue(maxsize=500)
        stop_event = threading.Event()

        def upstream_worker():
            try:
                kwargs["stream"] = True
                stream = client.chat.completions.create(**kwargs)
                for chunk in stream:
                    if stop_event.is_set():
                        stream.close()
                        return
                    chunk_queue.put(("chunk", chunk))
                chunk_queue.put(("done", None))
            except Exception as e:
                chunk_queue.put(("error", e))

        def ping_worker():
            while not stop_event.is_set():
                if stop_event.wait(KEEPALIVE_INTERVAL):
                    return
                try:
                    chunk_queue.put(("ping", None), timeout=1)
                except queue.Full:
                    pass

        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': message_id, 'type': 'message', 'role': 'assistant', 'model': MODEL, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n"

        t_up = threading.Thread(target=upstream_worker, daemon=True)
        t_ping = threading.Thread(target=ping_worker, daemon=True)
        t_up.start()
        t_ping.start()

        try:
            text_block_started = False
            text_block_closed = False
            tool_call_buffers = {}
            current_block_index = 0
            stream_done = False
            input_tokens = 0
            output_tokens = 0

            while not stream_done:
                try:
                    msg_type, data = chunk_queue.get(timeout=60)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue

                if msg_type == "ping":
                    yield ": keepalive\n\n"
                    continue

                if msg_type == "done":
                    stream_done = True
                    break

                if msg_type == "error":
                    raise data

                chunk = data
                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens or 0
                    output_tokens = chunk.usage.completion_tokens or 0

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                if delta.content:
                    if not text_block_started:
                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': current_block_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                        text_block_started = True
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': current_block_index, 'delta': {'type': 'text_delta', 'text': delta.content}})}\n\n"

                if delta.tool_calls:
                    if text_block_started and not text_block_closed:
                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': current_block_index})}\n\n"
                        text_block_closed = True
                        current_block_index += 1

                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_call_buffers:
                            tc_id = tc.id or f"toolu_{uuid.uuid4().hex}"
                            tc_name = tc.function.name if tc.function else ""
                            tool_call_buffers[idx] = {
                                "id": tc_id,
                                "name": tc_name,
                                "args": "",
                                "block_index": current_block_index,
                                "started": False
                            }
                            logger.debug(f"  Tool call: {tc_name}")

                        buf = tool_call_buffers[idx]

                        if tc.function:
                            if tc.function.name and tc.function.name != buf["name"]:
                                buf["name"] = tc.function.name
                            if tc.function.arguments:
                                buf["args"] += tc.function.arguments

                        if tc.id and tc.id != buf["id"]:
                            buf["id"] = tc.id

                        if not buf["started"]:
                            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': buf['block_index'], 'content_block': {'type': 'tool_use', 'id': buf['id'], 'name': buf['name'], 'input': {}}})}\n\n"
                            buf["started"] = True
                            current_block_index += 1

                        if tc.function and tc.function.arguments:
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': buf['block_index'], 'delta': {'type': 'input_json_delta', 'partial_json': tc.function.arguments}})}\n\n"

            if text_block_started and not text_block_closed:
                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

            for idx in sorted(tool_call_buffers.keys()):
                buf = tool_call_buffers[idx]
                if buf["started"]:
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': buf['block_index']})}\n\n"
                    logger.debug(f"  Args [{buf['name']}]: {buf['args'][:200]}")

            stop_reason = "tool_use" if tool_call_buffers else "end_turn"
            yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens}})}\n\n"
            yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

        except Exception as e:
            logger.exception("Stream response failed")
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}})}\n\n"
        finally:
            stop_event.set()

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def models():
    if request.method == "OPTIONS":
        return Response()
    return jsonify({
        "data": [
            {
                "id": "claude-3-opus-20240229",
                "type": "model",
                "display_name": "Claude 3 Opus",
                "created_at": int(time.time())
            },
            {
                "id": MODEL,
                "type": "model",
                "display_name": MODEL,
                "created_at": int(time.time())
            }
        ]
    })


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "model": MODEL, "note": "Claude Code proxy with tool support"})


if __name__ == "__main__":
    logger.info(f"Proxy started → http://127.0.0.1:5000 | model={MODEL} | threads=12")

    from waitress import serve
    serve(app, host="127.0.0.1", port=5000, threads=12)
