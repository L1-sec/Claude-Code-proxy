import sys
import os
import json
import uuid
import time
from flask import Flask, request, Response, jsonify
from openai import OpenAI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 英伟达
# API_KEY = "nvapi-*******************************************"
# MODEL = "mistralai/mistral-nemotron"
# BASE_URL = "https://integrate.api.nvidia.com/v1"

# sensenova
API_KEY = "sk-************************************"
MODEL = "deepseek-v4-flash"
BASE_URL = "https://token.sensenova.cn/v1"

app = Flask(__name__)

# 禁用 Flask 的日志
import logging

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)


def convert_to_openai_messages(claude_messages):
    """转换 Claude 消息格式到 OpenAI 格式"""
    openai_messages = []

    for msg in claude_messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 处理内容格式
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_result":
                        text_parts.append(item.get("content", ""))
                    elif item.get("type") == "tool_use":
                        continue
            content = " ".join(text_parts) if text_parts else ""

        # 处理系统消息
        if role == "system":
            if content:
                openai_messages.insert(0, {"role": "system", "content": content})
            continue

        # 添加用户/助手消息
        if content:
            openai_messages.append({
                "role": role if role in ["user", "assistant"] else "user",
                "content": content
            })

    # 如果没有系统消息，添加一个默认的
    if not openai_messages or openai_messages[0].get("role") != "system":
        openai_messages.insert(0, {"role": "system", "content": "You are a helpful assistant."})

    return openai_messages


@app.route("/v1/messages", methods=["POST", "OPTIONS"])
def messages():
    if request.method == "OPTIONS":
        return Response()

    try:
        data = request.get_json()

        # 强制使用 NVIDIA 模型
        requested_model = data.get("model", "unknown")
        if requested_model != MODEL:
            print(f"模型替换: {requested_model} -> {MODEL}")

        claude_messages = data.get("messages", [])
        print(f"收到 {len(claude_messages)} 条消息")

        # 转换消息
        openai_messages = convert_to_openai_messages(claude_messages)
        print(f"转换后 {len(openai_messages)} 条消息")

        # 调用 NVIDIA API
        client = OpenAI(
            base_url=BASE_URL,
            api_key=API_KEY,
            timeout=60.0
        )

        stream = data.get("stream", False)

        if stream:
            return handle_stream(client, openai_messages, data)
        else:
            return handle_sync(client, openai_messages, data)

    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        error_response = {
            "error": {
                "type": "api_error",
                "message": str(e)
            }
        }
        return jsonify(error_response), 500


def handle_sync(client, messages, original_data):
    """处理非流式请求"""
    try:
        print("发送同步请求到 NVIDIA...")
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=original_data.get("max_tokens", 4096),
            temperature=original_data.get("temperature", 0.7),
            top_p=original_data.get("top_p", 0.95)
        )

        content = response.choices[0].message.content
        print(f"收到响应: {content[:100]}...")

        # 构造 Claude 格式响应
        claude_response = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [
                {
                    "type": "text",
                    "text": content
                }
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0
            }
        }

        return jsonify(claude_response)

    except Exception as e:
        print(f"同步请求错误: {e}")
        raise


def handle_stream(client, messages, original_data):
    """处理流式请求"""

    def generate():
        try:
            print("发送流式请求到 NVIDIA...")
            stream_response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=original_data.get("max_tokens", 4096),
                temperature=original_data.get("temperature", 0.7),
                top_p=original_data.get("top_p", 0.95),
                stream=True
            )

            message_id = f"msg_{uuid.uuid4().hex}"

            # 发送 message_start
            yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': message_id, 'type': 'message', 'role': 'assistant', 'model': MODEL, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}})}\n\n"

            # 发送 content_block_start
            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

            full_text = ""
            chunk_count = 0
            for chunk in stream_response:
                if chunk.choices and chunk.choices[0].delta.content:
                    delta_text = chunk.choices[0].delta.content
                    full_text += delta_text
                    chunk_count += 1

                    # 发送 content_block_delta
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': delta_text}})}\n\n"

            print(f"流式响应完成，共 {chunk_count} 块，总长度 {len(full_text)}")

            # 发送 content_block_stop
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

            # 发送 message_delta
            yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': len(full_text) // 4}})}\n\n"

            # 发送 message_stop
            yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

        except Exception as e:
            print(f"流式请求错误: {e}")
            import traceback
            traceback.print_exc()
            error_data = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": str(e)
                }
            }
            yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream"
        }
    )


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def models():
    """返回可用模型列表 - 伪装成 Claude 模型"""
    if request.method == "OPTIONS":
        return Response()

    # 返回伪装成 Claude 的模型列表，让 Claude Code 认为使用的是官方模型
    return jsonify({
        "data": [
            {
                "id": "claude-3-opus-20240229",
                "type": "model",
                "display_name": "Claude 3 Opus (via NVIDIA Mistral)",
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
    return jsonify({"status": "ok", "model": MODEL, "note": "Claude Code proxy for NVIDIA API"})


if __name__ == "__main__":
    print("=" * 60)
    print("  Claude Code Proxy (最终修复版)")
    print("=" * 60)
    print(f"  代理地址: http://127.0.0.1:5000")
    print(f"  使用模型: {MODEL}")
    print("=" * 60)
    print("\n请按以下步骤操作:")
    print("1. 在 Claude Code 终端设置环境变量:")
    print("   set ANTHROPIC_BASE_URL=http://127.0.0.1:5000")
    print("   set ANTHROPIC_AUTH_TOKEN=dummy")
    print(f"   set ANTHROPIC_MODEL={MODEL}")
    print("   claude")
    print("=" * 60)
    print("\n代理已启动，等待连接...\n")

    from waitress import serve

    serve(app, host="127.0.0.1", port=5000, threads=4)
