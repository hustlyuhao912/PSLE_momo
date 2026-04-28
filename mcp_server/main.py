import asyncio
import base64
import json
import os
import uuid
from datetime import datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# session_id -> asyncio.Queue for SSE responses
sessions: dict[str, asyncio.Queue] = {}

# Serialises all writes to mistakes.json (safe because WEB_CONCURRENCY=1)
_write_lock = asyncio.Lock()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = "hustlyuhao912/PSLE_momo"
BRANCH = "main"
API = "https://api.github.com"


def _headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_file(path: str) -> tuple[str | None, str | None]:
    url = f"{API}/repos/{GITHUB_REPO}/contents/{path}"
    # Cache-Control: no-cache forces GitHub's API to return the live blob SHA,
    # not a stale cached response from a previous write.
    headers = {**_headers(), "Cache-Control": "no-cache", "Pragma": "no-cache"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        return content, data["sha"]


async def _put_file(
    path: str,
    content: str | bytes,
    sha: str | None = None,
    message: str = "Update",
) -> None:
    url = f"{API}/repos/{GITHUB_REPO}/contents/{path}"
    if isinstance(content, str):
        b64 = base64.b64encode(content.encode("utf-8")).decode()
    else:
        b64 = base64.b64encode(content).decode()
    body: dict = {"message": message, "content": b64, "branch": BRANCH}
    if sha:
        body["sha"] = sha
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(url, headers=_headers(), json=body)
        r.raise_for_status()


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "save_mistake",
        "description": (
            "Save a new PSLE mistake entry. Call this after analysing the photo "
            "shared by the user. Extract all information from the image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "enum": ["Mathematics", "Science", "English", "Chinese"],
                    "description": "PSLE subject",
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Specific topic within the subject, e.g. Fractions, Ratio, "
                        "Photosynthesis, 阅读理解, Synthesis & Transformation, Editing"
                    ),
                },
                "question_type": {
                    "type": "string",
                    "enum": [
                        "MCQ",
                        "Short Answer",
                        "Problem Sum",
                        "Open-ended",
                        "Synthesis & Transformation",
                        "Editing",
                        "Fill in the Blank",
                        "Comprehension",
                        "Composition",
                    ],
                    "description": "Type of question",
                },
                "question_text": {
                    "type": "string",
                    "description": (
                        "Full text of the question or sub-part extracted from the "
                        "image. For Chinese, include Chinese characters."
                    ),
                },
                "background_context": {
                    "type": "string",
                    "description": (
                        "For sub-parts (e.g. part b): include enough of the question "
                        "stem or passage excerpt so the sub-question is self-contained."
                    ),
                },
                "correct_answer": {
                    "type": "string",
                    "description": (
                        "The correct answer. For MCQ state the letter and option text. "
                        "For open-ended give a complete model answer."
                    ),
                },
                "working": {
                    "type": "string",
                    "description": (
                        "Step-by-step working or explanation. Required for Mathematics "
                        "Problem Sums. Include for other types when helpful."
                    ),
                },
                "image_base64": {
                    "type": "string",
                    "description": "Base64-encoded image data of the original photo (include if possible).",
                },
            },
            "required": [
                "subject",
                "topic",
                "question_type",
                "question_text",
                "correct_answer",
            ],
        },
    },
    {
        "name": "update_mistake",
        "description": "Update fields on an existing mistake entry when the user says something was wrong.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "8-character entry ID"},
                "subject": {"type": "string"},
                "topic": {"type": "string"},
                "question_type": {"type": "string"},
                "question_text": {"type": "string"},
                "background_context": {"type": "string"},
                "correct_answer": {"type": "string"},
                "working": {"type": "string"},
            },
            "required": ["entry_id"],
        },
    },
    {
        "name": "delete_mistake",
        "description": "Permanently delete a mistake entry by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "8-character entry ID"}
            },
            "required": ["entry_id"],
        },
    },
    {
        "name": "list_topics",
        "description": "List all subjects and topics stored, with entry counts. Useful for giving the user an overview.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _save_mistake(args: dict) -> str:
    entry_id = str(uuid.uuid4())[:8]
    image_b64 = args.get("image_base64", "")
    has_image = bool(image_b64)

    entry = {
        "id": entry_id,
        "subject": args["subject"],
        "topic": args["topic"],
        "question_type": args["question_type"],
        "question_text": args["question_text"],
        "background_context": args.get("background_context", ""),
        "correct_answer": args["correct_answer"],
        "working": args.get("working", ""),
        "has_image": has_image,
        "image_path": f"images/{entry_id}.jpg" if has_image else "",
        "date_added": datetime.utcnow().strftime("%Y-%m-%d"),
    }

    if has_image:
        raw = image_b64.split(",", 1)[-1]  # strip data URI prefix if present
        try:
            await _put_file(
                f"images/{entry_id}.jpg",
                base64.b64decode(raw),
                message=f"Add image {entry_id}",
            )
        except Exception:
            entry["has_image"] = False
            entry["image_path"] = ""

    async with _write_lock:
        for attempt in range(4):
            content, sha = await _get_file("data/mistakes.json")
            mistakes: list = json.loads(content) if content else []
            mistakes.append(entry)
            try:
                await _put_file(
                    "data/mistakes.json",
                    json.dumps(mistakes, ensure_ascii=False, indent=2),
                    sha=sha,
                    message=f"Add {args['subject']} — {args['topic']} ({entry_id})",
                )
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409 and attempt < 3:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
    return (
        f"Saved ✓  ID: {entry_id} | "
        f"{args['subject']} › {args['topic']} › {args['question_type']}"
    )


async def _update_mistake(args: dict) -> str:
    updatable = [
        "subject", "topic", "question_type", "question_text",
        "background_context", "correct_answer", "working",
    ]
    async with _write_lock:
        for attempt in range(4):
            content, sha = await _get_file("data/mistakes.json")
            if not content:
                return "No mistakes stored yet."
            mistakes = json.loads(content)
            entry = next((m for m in mistakes if m["id"] == args["entry_id"]), None)
            if not entry:
                return f"Entry {args['entry_id']} not found."
            for field in updatable:
                if args.get(field):
                    entry[field] = args[field]
            try:
                await _put_file(
                    "data/mistakes.json",
                    json.dumps(mistakes, ensure_ascii=False, indent=2),
                    sha=sha,
                    message=f"Update {args['entry_id']}",
                )
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409 and attempt < 3:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
    return f"Updated ✓  {args['entry_id']}"


async def _delete_mistake(args: dict) -> str:
    async with _write_lock:
        for attempt in range(4):
            content, sha = await _get_file("data/mistakes.json")
            if not content:
                return "No mistakes stored yet."
            mistakes = json.loads(content)
            before = len(mistakes)
            new_mistakes = [m for m in mistakes if m["id"] != args["entry_id"]]
            if len(new_mistakes) == before:
                return f"Entry {args['entry_id']} not found."
            try:
                await _put_file(
                    "data/mistakes.json",
                    json.dumps(new_mistakes, ensure_ascii=False, indent=2),
                    sha=sha,
                    message=f"Delete {args['entry_id']}",
                )
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409 and attempt < 3:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                raise
    return f"Deleted ✓  {args['entry_id']}"


async def _list_topics() -> str:
    content, _ = await _get_file("data/mistakes.json")
    if not content:
        return "No mistakes stored yet."
    mistakes = json.loads(content)
    if not mistakes:
        return "No mistakes stored yet."
    summary: dict[str, dict[str, int]] = {}
    for m in mistakes:
        summary.setdefault(m["subject"], {})
        summary[m["subject"]][m["topic"]] = summary[m["subject"]].get(m["topic"], 0) + 1
    lines = [f"Total: {len(mistakes)} entr{'y' if len(mistakes) == 1 else 'ies'}"]
    for subj in ["Mathematics", "Science", "English", "Chinese"]:
        if subj not in summary:
            continue
        lines.append(f"\n{subj}:")
        for topic, count in sorted(summary[subj].items()):
            lines.append(f"  • {topic}: {count}")
    return "\n".join(lines)


async def _handle_tool(name: str, args: dict) -> str:
    if name == "save_mistake":
        return await _save_mistake(args)
    if name == "update_mistake":
        return await _update_mistake(args)
    if name == "delete_mistake":
        return await _delete_mistake(args)
    if name == "list_topics":
        return await _list_topics()
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# MCP SSE transport endpoints
# ---------------------------------------------------------------------------


@app.get("/sse")
async def sse_endpoint(request: Request) -> StreamingResponse:
    sid = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[sid] = queue

    base_url = str(request.base_url).rstrip("/")
    post_url = f"{base_url}/messages?sessionId={sid}"

    async def stream():
        try:
            yield f"event: endpoint\ndata: {post_url}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            sessions.pop(sid, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/messages")
async def messages_endpoint(request: Request) -> JSONResponse:
    sid = request.query_params.get("sessionId", "")
    queue = sessions.get(sid)
    body = await request.json()
    method = body.get("method", "")
    mid = body.get("id")

    resp: dict | None = None

    if method == "initialize":
        resp = {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "PSLE Mistake Tracker", "version": "1.0.0"},
            },
        }
    elif method == "tools/list":
        resp = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        name = body["params"]["name"]
        args = body["params"].get("arguments", {})
        try:
            result = await _handle_tool(name, args)
            resp = {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": result}],
                    "isError": False,
                },
            }
        except Exception as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }
    elif method in ("notifications/initialized", "ping"):
        if mid is not None:
            resp = {"jsonrpc": "2.0", "id": mid, "result": {}}

    if resp and queue:
        await queue.put(resp)

    return JSONResponse({"status": "ok"}, status_code=202)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
