"""Expert layer (CW-05, CW-07, AG-04): Foundry IQ policy grounding, the Fabric data agent (ontology), and gpt-4.1 for
narrative + intent/constraint extraction. Numbers always come from tools; LLM text is checked for invented numbers."""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import requests

from . import auth, settings

log = logging.getLogger(__name__)
SEARCH_API = "2026-08-01-preview"
AOAI_API = "2024-10-21"
KB_PREAMBLE = ("You answer questions about Kinetiq marketplace policies (SYNTHETIC). Sources have a ref_id that must be "
               "cited. If the sources do not contain the answer, respond with 'I do not know'.")


# ---------------------------------------------------------------- Foundry IQ (knowledge base over policy PDFs)
def policy(question: str) -> dict[str, Any]:
    s = settings.get()
    body = {"messages": [{"role": "assistant", "content": [{"type": "text", "text": KB_PREAMBLE}]},
                         {"role": "user", "content": [{"type": "text", "text": question}]}],
            "knowledgeSourceParams": [{"knowledgeSourceName": s.knowledge_source, "kind": "indexedSharePoint",
                                       "alwaysQuerySource": True, "includeReferences": True,
                                       "includeReferenceSourceData": True}]}
    r = requests.post(f"{s.search_endpoint}/knowledgebases/{s.knowledge_base}/retrieve?api-version={SEARCH_API}",
                      json=body, timeout=90, headers={"Authorization": "Bearer " + auth.token(auth.SEARCH)})
    if r.status_code != 200:
        raise RuntimeError(f"KB retrieve {r.status_code}: {r.text[:400]}")
    j = r.json()
    text = " ".join(x.get("text", "") for m in j.get("response", []) for x in m.get("content", []))
    return {"answer": text.strip(), "citations": [_ref(x) for x in j.get("references", [])][:6]}


def _ref(x: dict) -> dict:
    sd = x.get("sourceData") or {}
    blob = json.dumps(sd)
    doc = next(iter(re.findall(r"KQ-[A-Z]{3}-\d{3}", blob)), None)
    title = sd.get("title") or sd.get("name") or sd.get("fileName") or doc or "policy"
    url = x.get("webUrl") or x.get("docUrl") or sd.get("webUrl") or sd.get("url")
    return {"ref_id": str(x.get("id", "")), "doc": doc, "title": str(title)[:120], "url": url}


# ---------------------------------------------------------------- Fabric data agent (ontology-grounded measures) via MCP
def data_agent(question: str, timeout: int = 120) -> dict[str, Any]:
    s = settings.get()
    hdr = {"Authorization": "Bearer " + auth.token(auth.FABRIC), "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "kinetiq-coworker", "version": "1.0"}}}
    r = requests.post(s.data_agent_mcp_url, json=init, headers=hdr, timeout=60)
    sid = r.headers.get("mcp-session-id")
    if sid:
        hdr["mcp-session-id"] = sid
    tools = _rpc(s.data_agent_mcp_url, hdr, "tools/list", {}, 60).get("tools", [])
    if not tools:
        raise RuntimeError("data agent MCP exposes no tools")
    res = _rpc(s.data_agent_mcp_url, hdr, "tools/call",
               {"name": tools[0]["name"], "arguments": {"userQuestion": question}}, timeout)
    parts = res.get("content", [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    table = next((p.get("resource", {}).get("text") for p in parts if p.get("type") == "resource"), None)
    return {"answer": text, "table_csv": table, "source": "Fabric data agent (ApparelIQ ontology)"}


def _rpc(url: str, hdr: dict, method: str, params: dict, timeout: int) -> dict:
    r = requests.post(url, json={"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params},
                      headers=hdr, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"MCP {method} {r.status_code}: {r.text[:400]}")
    if "text/event-stream" in r.headers.get("Content-Type", ""):
        datas = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        msg = json.loads(datas[-1]) if datas else {}
    else:
        msg = r.json()
    if "error" in msg:
        raise RuntimeError(f"MCP {method} error: {msg['error']}")
    return msg.get("result", {})


# ---------------------------------------------------------------- gpt-4.1 (Foundry)
def chat(messages: list[dict], *, json_mode: bool = False, max_tokens: int = 700, temperature: float = 0.2) -> str:
    s = settings.get()
    body: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = requests.post(f"{s.openai_endpoint}/openai/deployments/{s.chat_deployment}/chat/completions?api-version={AOAI_API}",
                      json=body, timeout=90, headers={"Authorization": "Bearer " + auth.token(auth.COGNITIVE)})
    if r.status_code != 200:
        raise RuntimeError(f"AOAI {r.status_code}: {r.text[:400]}")
    return r.json()["choices"][0]["message"]["content"] or ""


SYSTEM = (
    "You are the Kinetiq Marketplace Rebalancing coworker (UC1). You work WITH planners, merchants and finance. "
    "All data is SYNTHETIC. Rules: (1) never invent or recompute numbers; only use numbers present in FACTS; "
    "(2) the human owner decides, you recommend; (3) be concise, specific and practical, like a senior planner; "
    "(4) when policy is relevant, cite the policy id (for example KQ-FIN-102); (5) name trade-offs plainly.")

_NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")


def _numbers(text: str) -> set[str]:
    out = set()
    for n in _NUM.findall(text):
        v = n.replace("$", "").replace(",", "").rstrip("%")
        try:
            f = float(v)
        except ValueError:
            continue
        if f >= 10:  # small integers (counts like 2, 3, option ids) are not checked
            out.add(f"{f:.1f}")
    return out


def invented_numbers(text: str, facts: Any) -> set[str]:
    blob = json.dumps(facts, default=str)
    allowed = _numbers(blob)
    for x in list(allowed):  # allow the rounded and percentage forms of fact numbers
        f = float(x)
        allowed |= {f"{round(f):.1f}", f"{f * 100:.1f}", f"{round(f * 100):.1f}", f"{round(f, -1):.1f}"}
        if f < 1:
            allowed.add(f"{round(f * 100, 1):.1f}")
    return {n for n in _numbers(text) if n not in allowed}


def narrative(facts: dict, fallback: str) -> str:
    """CW-05 expert brief: why now, what I recommend and why, what to watch. Falls back to a template if the model adds
    numbers that are not in FACTS (AG-04)."""
    try:
        txt = chat([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": "FACTS:\n" + json.dumps(facts, default=str) +
                     "\n\nWrite 3 short bullets for the decision owner: 'Why now', 'My recommendation', 'Watch out'. "
                     "Max 70 words total. Use markdown bullets, no headings."}], max_tokens=250)
    except Exception as e:  # noqa: BLE001
        log.warning("narrative failed: %s", e)
        return fallback
    bad = invented_numbers(txt, facts)
    if bad:
        log.warning("narrative rejected; numbers not in facts: %s", bad)
        return fallback
    return txt.strip()


INTENT_PROMPT = (
    "Classify the teammate message in a rebalancing case chat. Return JSON: {\"intent\": one of "
    "[\"constraint\",\"policy\",\"data\",\"case\",\"chitchat\"], \"question\": restated question or null, "
    "\"constraints\": {\"max_units\": int|null, \"exclude_source_markets\": [ISO market ids], "
    "\"exclude_source_locations\": [location ids such as ST-US-1003 or DC-GB-DAV], \"max_lead_days\": int|null, "
    "\"min_gm_pct\": float 0-1|null, \"note\": short paraphrase|null}}. "
    "'constraint' = the person states a limit or preference that should change the options (e.g. 'GB can't spare stock "
    "before the Black Friday event', 'keep it under 300 units', 'must land within 5 days'). "
    "'policy' = asks what a policy/guardrail/approval rule says. 'data' = asks for a metric not in the case facts "
    "(sales, cover, sell-through history). 'case' = asks about this case's options, evidence or recommendation. "
    "Market ids: US, CA, GB, DE, FR, JP, AU, BR, CN, KR.")


def classify(message: str, case_facts: dict) -> dict:
    try:
        out = json.loads(chat([{"role": "system", "content": INTENT_PROMPT},
                               {"role": "user", "content": f"CASE: {json.dumps(case_facts, default=str)[:3000]}\n\nMESSAGE: {message}"}],
                              json_mode=True, max_tokens=300, temperature=0))
    except Exception as e:  # noqa: BLE001
        log.warning("classify failed: %s", e)
        out = {"intent": "case", "question": message, "constraints": {}}
    out.setdefault("constraints", {})
    out["constraints"] = {k: v for k, v in (out.get("constraints") or {}).items() if v not in (None, [], "")}
    return out


def answer_case(question: str, facts: dict) -> str:
    txt = chat([{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "FACTS:\n" + json.dumps(facts, default=str)[:12000] +
                 f"\n\nQUESTION: {question}\nAnswer in at most 80 words using only FACTS. If FACTS do not contain the "
                 "answer, say what you would check and with whom."}], max_tokens=300)
    bad = invented_numbers(txt, facts)
    if bad:
        return txt + f"\n\n_Note: I could not verify {', '.join(sorted(bad))} against the case evidence; treat as unverified._"
    return txt


def health() -> dict:
    out = chat([{"role": "user", "content": "Reply with OK."}], max_tokens=5)
    p = policy("What is the maximum transfer cost as a share of full price for a launch running style?")
    return {"ok": bool(out) and bool(p["citations"]),
            "detail": f"gpt: {out.strip()[:10]}; KB citations: {[c['doc'] for c in p['citations']][:3]}"}
