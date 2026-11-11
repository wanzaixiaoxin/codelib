"""证据门槛 + 同问题缓存 + 评估分流的测试（假 LLM，不联网）。"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ANSWER = ("结论：Service.run 用于启动服务，见 src/app.py:4-9。它先进入服务实例的方法，再执行该方法中定义的逻辑。"
          "从当前片段只能确认其启动用途和返回行为；具体调用者、运行时依赖以及失败处理需要结合调用点和配置继续核对。"
          "如果问题关注执行流程，应沿调用关系检查谁创建 Service、谁调用 run，以及返回值如何被消费。")
REVISED = ("结论：Service.run 是服务启动入口，见 src/app.py:4-9。当前候选显示它直接执行方法体并返回结果，"
           "没有展示它的上游调用者或运行时依赖，因此不能据此断言完整启动链路。分析完整流程还需检索实例创建处、"
           "run 的调用点和返回值处理逻辑；当前片段也没有说明错误处理或重试边界。")
BOND_QUESTION = "pri_nlp_worker_svr 工程中现券场景的券信息补全机制是如何实现的？"
BOND_PATH = "docs/券信息补全与校验逻辑.md"
BOND_ANSWER = "现券的券信息补全先根据代码查询券名和市场，只有券名时反查代码；单聊缺少券信息时继承最近上下文。见 docs/券信息补全与校验逻辑.md:1-8。"


class _H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        req = json.loads(raw or b"{}")
        msgs = req.get("messages") or []
        system = msgs[0].get("content", "") if msgs else ""
        user = msgs[-1].get("content", "") if msgs else ""
        if "对话理解助手" in system and "当前这句话：券补全机制" in user:
            content = BOND_QUESTION
        elif "代码问答质量检查器" in system and user.startswith(f"问题：{BOND_QUESTION}\n"):
            content = '{"relevant":true,"complete":true,"missing":[],"unsupported":[]}'
        elif "代码问答质量检查器" in system:
            content = '{"relevant":true,"complete":false,"missing":["调用链和失败边界"],"unsupported":[]}'
        elif "上一版回答的检查结果" in user:
            content = REVISED
        elif f"当前问题（已结合上下文理解为）：{BOND_QUESTION}\n" in user:
            content = BOND_ANSWER
        else:
            content = ANSWER
        body = {"choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110}}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="codelib_gate_"))
    lib = tmp / "lib"
    (lib / "src").mkdir(parents=True)
    (lib / "src" / "app.py").write_text(
        '"""应用。"""\n\n\nclass Service:\n    """服务。"""\n\n    def run(self):\n'
        '        """启动服务。"""\n        return "ok"\n', encoding="utf-8")
    (lib / "docs").mkdir()
    (lib / BOND_PATH).write_text(
        "# 债券代码、券名与市场补全业务逻辑\n\n"
        "## 普通现券补全规则\n\n"
        "现券代码存在时查询券名和市场；只有券名时反查代码和市场。\n"
        "单聊缺少券信息时可以继承最近会话上下文。\n\n"
        "## 字段优先级\n\n当前消息中明确填写的字段优先于上下文继承。\n", encoding="utf-8")

    import yaml
    cfgf = tmp / "config.yaml"
    cfgf.write_text(yaml.safe_dump({
        "app": {"data_dir": str(tmp / "data")},
        "workspace": {"id": "lib", "name": "lib", "path": str(lib)},
        "llm": {"type": "openai_compatible", "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "x", "model": "fake", "max_tokens": 800},
        "memory": {"enabled": True}, "knowledge": {"enabled": True, "retrieve": {
            "min_coverage": 0.2, "top_k": 7, "budget_tokens": 1234}},
        "channels": {},
    }, allow_unicode=True), encoding="utf-8")

    from app.bot import BotService
    from app.config import AppConfig
    from app.evidence import evidence_coverage
    from app.evaluate import judge_trace
    from app.feedback import extract_boost_candidates
    from app.knowledge.build import build_workspace
    from app.store import Store

    cfg = AppConfig.load(str(cfgf))
    store = Store(cfg.db_path)
    build_workspace(cfg, cfg.workspace("lib"), full=True)
    bot = BotService(cfg, store)

    def run(q, sid):
        a = asyncio.run(bot.handle("lib", q, sid))
        ev = {e["stage"]: e["data"] for e in store.get_trace(a.trace_id)["events"]}
        return a, ev

    print("== 1. 有证据 -> 正常作答 ==")
    a, ev = run("Service.run 是做什么的？", "s1")
    print("llm_used:", a.llm_used, "| gate:", ev.get("gate"))
    assert a.llm_used and not ev.get("gate")
    assert ev["retrieve"]["budget_tokens"] == 1234, "应采用全局 knowledge.retrieve 预算"

    print("\n== 2. 无证据（库里没有）-> 不调模型、不硬答 ==")
    a2, ev2 = run("ZebraQuantumFlux 的超频参数怎么调？", "s2")
    print("stages:", list(ev2.keys()))
    print("llm_used:", a2.llm_used, "| gate:", ev2.get("gate"))
    print("answer[:80]:", a2.text[:80])
    # 两种拦截路径：召回为空（无 retrieve/gate 事件）或证据覆盖不足（有 gate 事件）
    assert not a2.llm_used, "无证据时不应调用模型硬答"
    assert ("没有找到" in a2.text) or ("未找到" in a2.text) or ("没有在当前工作区索引中" in a2.text), a2.text

    print("\n== 3. 有完整答案和匹配来源时复用缓存 ==")
    a3, ev3 = run("Service.run 是做什么的？", "s4")
    print("llm_used:", a3.llm_used, "| cache:", ev3.get("cache"))
    assert ev3.get("cache"), "应命中同问题缓存"

    print("\n== 4. 负结果不缓存：再问无证据问题仍不调模型 ==")
    a4, ev4 = run("ZebraQuantumFlux 的超频参数怎么调？", "s3")
    print("llm_used:", a4.llm_used, "| cache:", bool(ev4.get("cache")))
    assert not a4.llm_used and not ev4.get("cache"), "负结果不应被缓存复用"

    print("\n== 4.5 中文实用问题不应被误拦（多粒度覆盖）==")
    a45, ev45 = run("Service 是干什么用的？", "s45")
    print("llm_used:", a45.llm_used, "| gate:", bool(ev45.get("gate")))
    assert a45.llm_used and not ev45.get("gate"), "合理的中文问题不应被证据门槛误拦"

    print("\n== 5. 评估：低覆盖问题应标 weight_only_insufficient ==")
    t = store.get_trace(a2.trace_id)
    rep = asyncio.run(judge_trace(t, bot.llm))
    print("verdict:", rep["verdict"], "| score:", rep["score"],
          "| weight_only_insufficient:", rep.get("weight_only_insufficient"))
    kinds = {x["kind"] for x in rep.get("improvement_actions", [])}
    print("improvement kinds:", kinds)
    assert rep["score"] <= 60, "低覆盖问题不应给高分"
    assert rep.get("weight_only_insufficient") is True, "应判定调权重无效"

    print("\n== 6. 分析类回答经过覆盖检查后修订 ==")
    a6, ev6 = run("分析 Service.run 的调用流程和失败边界", "s6")
    print("quality_review:", ev6.get("quality_review"))
    assert "这是分析类问题" in ev6["prompt"]["user"]
    assert ev6.get("quality_review", {}).get("revised") is True
    assert "当前候选显示" in a6.text

    print("\n== 7. 多轮改写加入工程名后仍检查中文主题 ==")
    history = [{"role": "user", "content": "介绍 pri_nlp_worker_svr 工程"},
               {"role": "assistant", "content": "这是私域 NLP 处理服务。"}] * 3
    bond = asyncio.run(bot.handle("lib", "券补全机制", "bond", history=history))
    bond_trace = store.get_trace(bond.trace_id)
    bond_events = {e["stage"]: e["data"] for e in bond_trace["events"]}
    assert bond_events["intent"]["effective_question"] == BOND_QUESTION
    assert bond.llm_used and "gate" not in bond_events, bond.text
    assert "继承最近上下文" in bond.text
    assert "pri_nlp_worker_svr" in bond_events["evidence"]["ignored_scope"]
    assert "券信息补全" in bond_events["evidence"]["covered"]
    assert any(c.path == BOND_PATH for c in bond.citations)
    bond_report = asyncio.run(judge_trace(bond_trace, None))
    bond_checks = {c["name"]: c for c in bond_report["rule"]["checks"]}
    assert bond_checks["证据覆盖问题"]["ok"], bond_checks["证据覆盖问题"]

    print("\n== 8. 无关中文和不存在的函数仍被拦截 ==")
    unrelated = evidence_coverage("量子超频", "现券代码与券名补全规则", [])
    assert unrelated["coverage"] == 0
    unknown, unknown_ev = run("ZebraQuantumFlux 的券补全机制如何实现？", "unknown-bond")
    assert not unknown.llm_used and unknown_ev.get("gate"), unknown.text
    assert evidence_coverage("Service.run", "Service.runner 只是相似名称", [])["coverage"] == 0

    print("\n== 9. 旧 trace 重评能识别门槛误拦，不会把拒答模板判为幻觉 ==")
    legacy = dict(bond_trace)
    legacy["events"] = [e for e in bond_trace["events"] if e["stage"] in (
        "sanitized", "intent", "index", "retrieve")]
    legacy["events"] += [
        {"stage": "gate", "data": {"action": "insufficient_evidence", "coverage": 0, "min_coverage": 0.2}},
        {"stage": "answer", "data": {"text": f"现有知识库中没有找到能直接回答「{BOND_QUESTION}」的内容。请执行 kb-build。",
                                     "citations": [c.to_dict() for c in bond.citations], "llm_used": False}},
    ]
    legacy_report = asyncio.run(judge_trace(legacy, None))
    legacy_checks = {c["name"]: c for c in legacy_report["rule"]["checks"]}
    assert legacy_checks["证据覆盖问题"]["ok"]
    assert not legacy_checks["证据门槛"]["ok"]
    assert not legacy_checks["调用模型"]["ok"]
    assert legacy_checks["回答有据可依"]["ok"]
    assert legacy_report["verdict"] == "poor" and legacy_report["score"] <= 55
    assert {a["kind"] for a in legacy_report["improvement_actions"]} == {"config"}
    assert extract_boost_candidates(legacy_report) == []

    srv.shutdown()
    print("\n证据门槛/缓存/评估分流测试通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
