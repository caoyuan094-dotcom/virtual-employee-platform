import json
import os
import re
import subprocess
import time
import uuid
import zipfile
import html
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape


REQUIRED_FIELDS = [
    ("product_category", "产品品类"),
    ("product_params", "主打产品参数"),
    ("target_country", "目标国家"),
    ("buyer_types", "目标客户类型"),
    ("advantages", "产品核心优势"),
    ("certifications", "认证资质"),
    ("moq", "起订量"),
    ("price_range", "价格区间"),
]

RETAIL_BLOCKLIST = [
    "amazon.", "walmart.", "target.", "aliexpress.", "ebay.", "etsy.",
    "reddit.", "youtube.", "pinterest.", "instagram.", "facebook.com",
    "tiktok.com", "wikipedia.", "google.", "yelp.", "trustpilot.",
]

WHOLESALE_SIGNALS = [
    "distributor", "dealer", "wholesale", "import", "b2b", "fleet",
    "retailer", "reseller", "store", "shop", "supplier", "brand",
]

AGENT_TOOL_LABELS = {
    "build_market_report": "市场分析",
    "build_keywords": "关键词生成",
    "web_search_live": "网页搜索",
    "web_fetch_page": "网页读取",
    "lead_filter": "线索清洗",
    "score_lead": "客户评分",
    "build_outreach_emails": "开发信生成",
    "build_followup_plan": "跟进计划",
    "browser_open_url": "打开浏览器",
    "write_xlsx": "Excel导出",
}


def validate_input(payload):
    missing = []
    for key, label in REQUIRED_FIELDS:
        value = payload.get(key)
        if isinstance(value, list):
            ok = bool(value)
        else:
            ok = bool(str(value or "").strip())
        if not ok:
            missing.append({"field": key, "label": label})
    return missing


def log_agent_action(logs, agent, tool, detail, status="completed", result=None):
    logs.append({
        "agent": agent,
        "tool": tool,
        "label": AGENT_TOOL_LABELS.get(tool, tool),
        "detail": detail,
        "status": status,
        "result": result or "",
        "time": time.strftime("%H:%M:%S"),
    })


def open_agent_browser_searches(agent_tools, keywords, logs):
    if not agent_tools or not agent_tools.get("open_browser") or not agent_tools.get("browser_enabled"):
        return 0
    queries = []
    for group in ("Google可复制组合", "LinkedIn可复制组合"):
        queries.extend(keywords.get(group, [])[:2])
    opened = 0
    for query in queries[:3]:
        url = "https://duckduckgo.com/?q=" + urllib.parse.quote(query)
        ok, result = open_url_for_agent(url, agent_tools.get("browser_app"))
        opened += 1 if ok else 0
        log_agent_action(
            logs,
            "海外线索搜索员",
            "browser_open_url",
            "打开搜索页供获客员工/人工核验：%s" % query,
            "completed" if ok else "failed",
            result,
        )
    return opened


def open_agent_customer_pages(agent_tools, leads, logs):
    if not agent_tools or not agent_tools.get("open_browser") or not agent_tools.get("browser_enabled"):
        return 0
    opened = 0
    for lead in leads[:3]:
        website = lead.get("website")
        if not website or website == "待验证":
            continue
        ok, result = open_url_for_agent(website, agent_tools.get("browser_app"))
        opened += 1 if ok else 0
        log_agent_action(
            logs,
            "客户背调员",
            "browser_open_url",
            "打开客户官网供背调核验：%s" % website,
            "completed" if ok else "failed",
            result,
        )
    return opened


def open_url_for_agent(url, browser_app=None):
    cmd = ["open"]
    app = str(browser_app or "").strip()
    if app:
        cmd.extend(["-a", app])
    cmd.append(normalize_url(url))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        detail = (r.stdout or r.stderr or "").strip()
        return r.returncode == 0, detail or ("opened: " + normalize_url(url))
    except Exception as e:
        return False, str(e)


def run_foreign_trade_sop(payload, base_dir, opener=None, live_search=True, agent_tools=None):
    missing = validate_input(payload)
    if missing:
        return {"ok": False, "missing": missing}

    normalized = normalize_payload(payload)
    run_id = "ft_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    agent_tools = agent_tools or {}
    tool_logs = []

    market = build_market_report(normalized)
    log_agent_action(tool_logs, "外贸SOP总控", "build_market_report", "分析目标市场、竞品痛点、采购关注点和合规风险。", "completed")

    keywords = build_keywords(normalized)
    log_agent_action(tool_logs, "海外线索搜索员", "build_keywords", "生成Google、LinkedIn可用的B2B检索词。", "completed")

    browser_opened = open_agent_browser_searches(agent_tools, keywords, tool_logs)
    leads = discover_leads(normalized, keywords, opener=opener, live_search=live_search)
    log_agent_action(
        tool_logs,
        "海外线索搜索员",
        "web_search_live",
        "按关键词搜索公开B2B客户候选；live_search=%s，browser_opened=%s。" % (bool(live_search), browser_opened),
        "completed",
    )

    profiles = [build_customer_profile(lead, normalized, opener=opener) for lead in leads]
    log_agent_action(tool_logs, "客户背调员", "web_fetch_page", "读取候选客户官网并提取公司画像；读不到则标记待验证。", "completed")
    open_agent_customer_pages(agent_tools, leads, tool_logs)

    for lead, profile in zip(leads, profiles):
        score = score_lead(lead, profile, normalized)
        lead.update(score)
        lead["profile_summary"] = profile["summary"]
        lead["risk_flags"] = "; ".join(profile["risks"] + score["risk_flags"])
        lead["core_match"] = score["core_match"]
        lead["outreach_angle"] = score["outreach_angle"]
    log_agent_action(tool_logs, "海外线索搜索员", "lead_filter", "过滤C端零售、平台页和低匹配线索，保留B2B候选。", "completed")
    log_agent_action(tool_logs, "客户背调员", "score_lead", "按A/B/C规则计算采购潜力和跟进优先级。", "completed")

    outreach = []
    followups = []
    for lead in leads:
        outreach.extend(build_outreach_emails(lead, normalized))
        followups.extend(build_followup_plan(lead, normalized))
    log_agent_action(tool_logs, "英文开发信专员", "build_outreach_emails", "结合客户画像生成英文开发信草稿，不自动发送。", "completed")
    log_agent_action(tool_logs, "外贸跟进SOP专员", "build_followup_plan", "生成30天7次跟进计划，不自动发送。", "completed")

    package = {
        "ok": True,
        "run_id": run_id,
        "started_at": started_at,
        "input": normalized,
        "workflow": build_workflow_status(),
        "market_report": market,
        "keywords": keywords,
        "leads": leads,
        "customer_profiles": profiles,
        "outreach_emails": outreach,
        "followups": followups,
        "text_report": build_text_report(market, keywords, leads, profiles, outreach, followups),
        "compliance_notice": build_compliance_notice(normalized),
        "agent_tool_logs": tool_logs,
        "tool_mode": {
            "live_search": bool(live_search),
            "browser_enabled": bool(agent_tools.get("browser_enabled")),
            "computer_enabled": bool(agent_tools.get("computer_enabled")),
            "browser_open_pages": bool(agent_tools.get("open_browser")),
        },
    }

    out_dir = os.path.join(base_dir, "agent_outputs", "foreign_trade", run_id)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(package, f, ensure_ascii=False, indent=2)
    write_xlsx(package, os.path.join(out_dir, "外贸客户开发SOP数据包.xlsx"))
    package["export_url"] = f"/foreign-trade/export/{run_id}"
    log_agent_action(tool_logs, "外贸跟进SOP专员", "write_xlsx", "导出Excel数据包并写入本地私有目录。", "completed")
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(package, f, ensure_ascii=False, indent=2)
    return package


def dispatch_foreign_trade_task(payload, base_dir, opener=None, live_search=True, agent_tools=None):
    """Route a natural-language foreign-trade task to the right virtual employees."""
    message = str(payload.get("message") or "").strip()
    context = payload.get("context") or {}
    if not message:
        return {"ok": False, "need_info": True, "messages": [agent_msg("外贸SOP总控", "请先告诉我你要找什么客户、做什么市场或今天要发什么内容。")]}

    info = infer_task_info(message, context)
    intent = classify_foreign_trade_intent(message)
    assigned = assigned_agents_for_intent(intent)

    missing = required_missing_for_intent(intent, info, message)
    if missing:
        return {
            "ok": False,
            "need_info": True,
            "intent": intent,
            "assigned_agents": assigned,
            "missing": missing,
            "messages": [agent_msg("外贸SOP总控", "我先自动判断这是「%s」任务。还缺：%s。\n\n你补一句就行，例如：帮我找20个德国eBike经销商。" % (intent_label(intent), "、".join(missing)))],
        }

    if intent == "lead_generation":
        return dispatch_lead_generation(message, info, base_dir, opener, live_search, assigned, agent_tools=agent_tools)
    if intent == "social_content":
        return dispatch_social_content(message, info, assigned)
    if intent == "outreach_email":
        return dispatch_outreach_email(message, info, assigned)
    if intent == "customer_research":
        return dispatch_customer_research(message, info, opener, assigned)
    if intent == "followup":
        return dispatch_followup(message, info, assigned)
    if intent == "market_research":
        return dispatch_market_research(message, info, assigned)
    if intent == "keyword_research":
        return dispatch_keyword_research(message, info, assigned)
    return dispatch_general_foreign_trade(message, info, assigned)


def start_foreign_trade_workflow(payload, base_dir, opener=None, live_search=True, agent_tools=None):
    """Run the foreign-trade department workflow from factory profile to follow-up handoff."""
    profile = normalize_factory_profile(payload)
    missing = validate_factory_profile(profile)
    if missing:
        return {
            "ok": False,
            "need_info": True,
            "missing": missing,
            "messages": [agent_msg("外贸SOP总控", "厂家资料还不完整，缺少：%s。补齐后我会自动启动外贸部门流程。" % "、".join(missing))],
        }

    sop_payload = {
        "product_category": profile["product_category"],
        "product_params": profile["product_params"],
        "target_country": profile["target_country"],
        "buyer_types": profile["buyer_types"],
        "advantages": profile["advantages"],
        "certifications": profile["certifications"],
        "moq": profile["moq"],
        "price_range": profile["price_range"],
        "lead_count": profile["lead_count"],
    }
    package = run_foreign_trade_sop(sop_payload, base_dir, opener=opener, live_search=live_search, agent_tools=agent_tools)
    if not package.get("ok"):
        return package

    messages = build_workflow_messages(profile, package)
    return {
        "ok": True,
        "workflow": "factory_profile_to_customer_followup",
        "profile": profile,
        "messages": messages,
        "data": package,
        "deliverable": {
            "title": "🌐 外贸客户开发流程交付包",
            "stage": "外贸部门流程产出",
            "content": package.get("text_report", "") + "\n\nExcel数据包：" + package.get("export_url", ""),
        },
    }


def normalize_factory_profile(payload):
    buyer_types = payload.get("buyer_types") or []
    if isinstance(buyer_types, str):
        buyer_types = [x.strip() for x in re.split(r"[,，/、\n]", buyer_types) if x.strip()]
    return {
        "company_name": str(payload.get("company_name") or "").strip(),
        "factory_type": str(payload.get("factory_type") or "").strip(),
        "product_category": str(payload.get("product_category") or "").strip(),
        "product_params": str(payload.get("product_params") or "").strip(),
        "target_country": str(payload.get("target_country") or "").strip(),
        "buyer_types": buyer_types,
        "advantages": str(payload.get("advantages") or "").strip(),
        "certifications": str(payload.get("certifications") or "").strip(),
        "moq": str(payload.get("moq") or "").strip(),
        "price_range": str(payload.get("price_range") or "").strip(),
        "capacity": str(payload.get("capacity") or "").strip(),
        "website": str(payload.get("website") or "").strip(),
        "lead_count": max(5, min(int(payload.get("lead_count") or 20), 60)),
    }


def validate_factory_profile(profile):
    required = [
        ("company_name", "公司/工厂名称"),
        ("factory_type", "厂家类型"),
        ("product_category", "产品品类"),
        ("product_params", "主打产品参数"),
        ("target_country", "目标国家/区域"),
        ("buyer_types", "目标客户类型"),
        ("advantages", "产品核心优势"),
        ("certifications", "认证资质"),
        ("moq", "起订量"),
        ("price_range", "价格区间"),
    ]
    missing = []
    for key, label in required:
        value = profile.get(key)
        if isinstance(value, list):
            ok = bool(value)
        else:
            ok = bool(str(value or "").strip())
        if not ok:
            missing.append(label)
    return missing


def build_workflow_messages(profile, package):
    market = package.get("market_report", {})
    keywords = package.get("keywords", {})
    leads = package.get("leads", [])
    profiles = package.get("customer_profiles", [])
    outreach = package.get("outreach_emails", [])
    followups = package.get("followups", [])
    grade_count = {}
    for lead in leads:
        grade_count[lead.get("grade", "待定")] = grade_count.get(lead.get("grade", "待定"), 0) + 1

    lead_preview = "\n".join(
        "- [%s] %s | %s | %s | %s" % (
            l.get("grade", ""), l.get("company", ""), l.get("website", ""), l.get("email", "待验证"), l.get("core_match", ""),
        )
        for l in leads[:8]
    )
    profile_preview = "\n".join(
        "- %s：%s 风险：%s" % (p.get("company", ""), p.get("summary", ""), "；".join(p.get("risks", [])) or "无")
        for p in profiles[:5]
    )
    first_outreach = format_first_outreach(outreach)
    followup_preview = "\n".join(
        "- %s D+%s %s：%s" % (f.get("company", ""), f.get("interval_days", ""), f.get("purpose", ""), f.get("copy", ""))
        for f in followups[:7]
    )
    logs = package.get("agent_tool_logs", [])
    manager_text = "厂家资料已确认。\n公司：%s\n厂家类型：%s\n产品：%s\n目标市场：%s\n目标客户：%s\n\n我把外贸获客压成5人小队执行：主管定客户画像和边界，获客员工找客户，客户研究员工评分，开发内容员工写首轮信，跟进员工接手30天计划。\n\n市场重点：\n%s" % (
        profile["company_name"], profile["factory_type"], profile["product_category"], profile["target_country"], "、".join(profile["buyer_types"]),
        "\n".join("- " + x for x in (market.get("demand_analysis", []) + market.get("buyer_concerns", []))[:5])
    )
    search_text = "我已生成搜索词并寻找B2B客户候选，已过滤C端零售、平台商品页和明显低匹配对象。未确认官网、邮箱、联系人都标记为“待验证”。\n\n第一批候选：\n" + lead_preview
    research_text = "我已完成客户画像和A/B/C优先级判断。\n分级统计：%s。\n\n背调摘要：\n%s" % (
        "、".join("%s类%s条" % (k, v) for k, v in sorted(grade_count.items())),
        profile_preview,
    )
    followup_text = "我已接收A/B类客户和开发信，生成后续跟进节奏，并完成本地交付包归档。\n\n跟进预览：\n%s\n\nExcel数据包：%s\n发送边界：所有邮件/私信/社媒发布均需人工确认；不自动群发，不编造客户、联系人或邮箱。" % (followup_preview, package.get("export_url", ""))

    return [
        agent_msg("外贸SOP总控", manager_text, agent_tools_for("外贸SOP总控", logs)),
        agent_msg("海外线索搜索员", search_text, agent_tools_for("海外线索搜索员", logs)),
        agent_msg("客户背调员", research_text, agent_tools_for("客户背调员", logs)),
        agent_msg("英文开发信专员", first_outreach, agent_tools_for("英文开发信专员", logs)),
        agent_msg("外贸跟进SOP专员", followup_text, agent_tools_for("外贸跟进SOP专员", logs)),
    ]


def classify_foreign_trade_intent(message):
    text = message.lower()
    if any(x in message for x in ["社媒", "发帖", "内容", "小红书", "朋友圈"]) or any(x in text for x in ["linkedin", "facebook", "tiktok", "instagram", "post", "social"]):
        return "social_content"
    if any(x in message for x in ["开发信", "邮件", "私信", "dm", "邀约"]) or any(x in text for x in ["email", "outreach", "cold mail", "cold email"]):
        return "outreach_email"
    if any(x in message for x in ["背调", "查一下", "分析这个客户", "客户画像", "官网"]) or re.search(r"https?://", message):
        return "customer_research"
    if any(x in message for x in ["跟进", "复联", "催回复", "二封", "三封"]) or any(x in text for x in ["follow up", "follow-up"]):
        return "followup"
    if any(x in message for x in ["市场调研", "市场分析", "竞品", "价格带", "合规"]):
        return "market_research"
    if any(x in message for x in ["关键词", "搜索词", "检索词", "google词", "linkedin词"]):
        return "keyword_research"
    if any(x in message for x in ["找客户", "找", "线索", "客户名单", "经销商", "进口商", "批发商", "采购商", "品牌商"]) or any(x in text for x in ["lead", "prospect", "dealer", "distributor", "importer", "wholesaler", "buyer"]):
        return "lead_generation"
    return "general"


def assigned_agents_for_intent(intent):
    mapping = {
        "lead_generation": [
            ("外贸SOP总控", "确认ICP客户画像和执行边界"),
            ("海外线索搜索员", "生成搜索词、找客户并过滤低质量名单"),
            ("客户背调员", "读取官网、画像和A/B/C评分"),
            ("英文开发信专员", "生成首封开发信和切入角度"),
            ("外贸跟进SOP专员", "生成跟进计划并导出交付包"),
        ],
        "social_content": [
            ("英文开发信专员", "生成LinkedIn/Facebook/TikTok获客内容"),
            ("外贸SOP总控", "确认客户画像和发布边界"),
        ],
        "outreach_email": [
            ("客户背调员", "提取客户背景和匹配点"),
            ("英文开发信专员", "生成个性化开发信"),
            ("外贸跟进SOP专员", "检查发送边界和下一步动作"),
        ],
        "customer_research": [
            ("客户背调员", "读取官网、公开信息并判断采购潜力"),
            ("英文开发信专员", "给出切入角度"),
        ],
        "followup": [
            ("外贸跟进SOP专员", "生成跟进节奏"),
            ("英文开发信专员", "生成每次跟进文案"),
        ],
        "market_research": [
            ("外贸SOP总控", "分析需求、竞品、合规和获客方向"),
            ("海外线索搜索员", "提炼搜索方向"),
        ],
        "keyword_research": [
            ("海外线索搜索员", "生成检索词库并确认可用于找客户"),
        ],
        "general": [
            ("外贸SOP总控", "判断下一步外贸获客动作"),
        ],
    }
    return [{"agent": a, "reason": r} for a, r in mapping.get(intent, mapping["general"])]


def required_missing_for_intent(intent, info, message):
    missing = []
    if intent in ("lead_generation", "market_research", "keyword_research", "social_content"):
        if not info.get("product_category"):
            missing.append("产品品类")
    if intent == "lead_generation":
        if not info.get("target_country"):
            missing.append("目标国家/区域")
        if not info.get("buyer_types"):
            missing.append("客户类型")
    if intent == "customer_research" and not info.get("urls"):
        missing.append("客户官网链接")
    return missing


def infer_task_info(message, context=None):
    context = context or {}
    text = str(message or "")
    product = str(context.get("product_category") or "").strip()
    country = str(context.get("target_country") or "").strip()
    buyer_types = context.get("buyer_types") or []
    if isinstance(buyer_types, str):
        buyer_types = [x.strip() for x in re.split(r"[,，/、\n]", buyer_types) if x.strip()]

    product = product or infer_product_from_text(text)
    country = country or infer_country_from_text(text)
    inferred_buyers = infer_buyer_types_from_text(text)
    if inferred_buyers:
        buyer_types = inferred_buyers

    return {
        "product_category": product,
        "target_country": country,
        "buyer_types": buyer_types,
        "product_params": str(context.get("product_params") or default_product_params(product)),
        "advantages": str(context.get("advantages") or default_advantages(product)),
        "certifications": str(context.get("certifications") or default_certifications(product)),
        "moq": str(context.get("moq") or "待确认"),
        "price_range": str(context.get("price_range") or "待确认"),
        "lead_count": infer_count_from_text(text, context.get("lead_count") or 15),
        "urls": re.findall(r"https?://[^\s，。；;,]+", text),
        "raw_message": text,
    }


def dispatch_lead_generation(message, info, base_dir, opener, live_search, assigned, agent_tools=None):
    package = run_foreign_trade_sop(info, base_dir, opener=opener, live_search=live_search, agent_tools=agent_tools)
    if not package.get("ok"):
        return {"ok": False, "need_info": True, "intent": "lead_generation", "assigned_agents": assigned, "missing": package.get("missing", [])}
    leads = package.get("leads", [])
    grade_count = {}
    for lead in leads:
        grade_count[lead.get("grade", "待定")] = grade_count.get(lead.get("grade", "待定"), 0) + 1
    top_rows = "\n".join([
        "- [%s] %s | %s | %s | %s" % (
            lead.get("grade", ""),
            lead.get("company", ""),
            lead.get("website", ""),
            lead.get("email", "待验证"),
            lead.get("core_match", ""),
        )
        for lead in leads[:8]
    ])
    logs = package.get("agent_tool_logs", [])
    profile_preview = "\n".join(
        "- %s：%s 风险：%s" % (p.get("company", ""), p.get("summary", ""), "；".join(p.get("risks", [])) or "无")
        for p in package.get("customer_profiles", [])[:5]
    )
    messages = [
        agent_msg("外贸SOP总控", "已按5人小队自动派工：主管确认ICP和边界，获客员工找客户，客户研究员工评分，开发内容员工写信，跟进员工导出计划。", agent_tools_for("外贸SOP总控", logs)),
        agent_msg("海外线索搜索员", "已生成搜索词、打开/读取公开来源并过滤低匹配对象。未确认官网、邮箱、联系人均保留为“待验证”。\n\n第一批候选：\n" + top_rows, agent_tools_for("海外线索搜索员", logs)),
        agent_msg("客户背调员", "已完成客户画像和A/B/C优先级判断。\n分级统计：%s。\n\n背调摘要：\n%s" % ("、".join("%s类%s条" % (k, v) for k, v in sorted(grade_count.items())), profile_preview), agent_tools_for("客户背调员", logs)),
        agent_msg("英文开发信专员", format_first_outreach(package.get("outreach_emails", [])), agent_tools_for("英文开发信专员", logs)),
        agent_msg("外贸跟进SOP专员", "已为A/B类客户生成30天7次跟进节奏，并导出Excel数据包：%s\n发送边界：只生成草稿，不自动群发；每个客户发送前必须人工确认公司、联系人和邮箱。" % package.get("export_url", ""), agent_tools_for("外贸跟进SOP专员", logs)),
    ]
    return {
        "ok": True,
        "intent": "lead_generation",
        "title": "%s %s 客户开发" % (info.get("target_country"), info.get("product_category")),
        "assigned_agents": assigned,
        "messages": messages,
        "deliverable": {
            "title": "🌐 外贸客户线索与开发信",
            "stage": "外贸获客员工自动派工",
            "content": package.get("text_report", "") + "\n\nExcel数据包：" + package.get("export_url", ""),
        },
        "data": package,
    }


def dispatch_social_content(message, info, assigned):
    data = normalize_payload(fill_payload_defaults(info))
    package = build_social_media_package(data, message)
    content = social_package_to_text(package)
    return {
        "ok": True,
        "intent": "social_content",
        "title": "外贸社媒内容",
        "assigned_agents": assigned,
        "messages": [
            agent_msg("英文开发信专员", content + "\n\n建议标签/关键词：\n" + "\n".join("- " + x for x in package["hashtags"])),
            agent_msg("外贸跟进SOP专员", "发布前检查：不承诺虚假认证、不夸大续航/价格/交期；询盘入口建议统一导向官网表单或业务邮箱。所有发布需人工确认。"),
        ],
        "deliverable": {"title": "📣 外贸社媒内容包", "stage": "外贸社媒员工产出", "content": content},
        "data": package,
    }


def dispatch_outreach_email(message, info, assigned):
    data = normalize_payload(fill_payload_defaults(info))
    lead = {
        "company": infer_company_from_text(message) or "目标客户",
        "grade": "B",
        "outreach_angle": "围绕采购匹配、稳定供货和低风险样品评估切入。",
    }
    emails = build_outreach_emails(lead, data)
    content = "\n\n".join("### %s\nSubject: %s\n%s" % (item["type"], item["subject"], item["body"]) for item in emails)
    return {
        "ok": True,
        "intent": "outreach_email",
        "title": "英文开发信",
        "assigned_agents": assigned,
        "messages": [
            agent_msg("客户背调员", "当前客户背景不足，已按“目标客户”生成可替换草稿。若给客户官网，我可以再做个性化背调。"),
            agent_msg("英文开发信专员", content),
            agent_msg("外贸跟进SOP专员", "发送前请人工确认客户公司名、联系人、邮箱和产品合规文件；不自动发送。"),
        ],
        "deliverable": {"title": "✉️ 英文开发信草稿", "stage": "开发信员工产出", "content": content},
        "data": {"outreach_emails": emails},
    }


def dispatch_customer_research(message, info, opener, assigned):
    data = normalize_payload(fill_payload_defaults(info))
    urls = info.get("urls") or []
    profiles = []
    leads = []
    for url in urls[:5]:
        lead = {"company": domain_from_url(url) or "待验证", "website": url, "email": "待验证", "customer_type": "待验证"}
        profile = build_customer_profile(lead, data, opener=opener)
        score = score_lead(lead, profile, data)
        lead.update(score)
        leads.append(lead)
        profiles.append(profile)
    content = "\n\n".join(
        "### %s\n官网：%s\n画像：%s\n主营：%s\n等级：%s\n风险：%s" % (
            p["company"], p["website"], p["summary"], p["main_products"], leads[i].get("grade", ""), "；".join(p["risks"]) or "无",
        )
        for i, p in enumerate(profiles)
    )
    return {
        "ok": True,
        "intent": "customer_research",
        "title": "客户背调",
        "assigned_agents": assigned,
        "messages": [
            agent_msg("客户背调员", content + "\n\n分级结果：\n" + "\n".join("- %s：%s类，%s" % (l["company"], l.get("grade", ""), l.get("core_match", "")) for l in leads)),
        ],
        "deliverable": {"title": "🏢 客户背调档案", "stage": "客户背调员工产出", "content": content},
        "data": {"profiles": profiles, "leads": leads},
    }


def dispatch_followup(message, info, assigned):
    data = normalize_payload(fill_payload_defaults(info))
    lead = {"company": infer_company_from_text(message) or "目标客户"}
    plan = build_followup_plan(lead, data)
    content = "\n".join("- D+%s %s：%s" % (p["interval_days"], p["purpose"], p["copy"]) for p in plan)
    return {
        "ok": True,
        "intent": "followup",
        "title": "30天跟进计划",
        "assigned_agents": assigned,
        "messages": [agent_msg("外贸跟进SOP专员", content), agent_msg("英文开发信专员", "每次跟进保持低压、具体、可回复；不要连续重复同一封模板。")],
        "deliverable": {"title": "📅 外贸客户跟进计划", "stage": "跟进员工产出", "content": content},
        "data": {"followups": plan},
    }


def dispatch_market_research(message, info, assigned):
    data = normalize_payload(fill_payload_defaults(info))
    market = build_market_report(data)
    content = market_report_to_text(market)
    return {"ok": True, "intent": "market_research", "title": market["title"], "assigned_agents": assigned, "messages": [agent_msg("外贸SOP总控", content)], "deliverable": {"title": "📊 外贸市场调研", "stage": "外贸主管产出", "content": content}, "data": {"market_report": market}}


def dispatch_keyword_research(message, info, assigned):
    data = normalize_payload(fill_payload_defaults(info))
    keywords = build_keywords(data)
    content = format_keywords_for_agent(keywords)
    return {"ok": True, "intent": "keyword_research", "title": "外贸检索词库", "assigned_agents": assigned, "messages": [agent_msg("海外线索搜索员", content)], "deliverable": {"title": "🔎 外贸拓客关键词库", "stage": "获客员工产出", "content": content}, "data": {"keywords": keywords}}


def dispatch_general_foreign_trade(message, info, assigned):
    content = "我会按外贸获客工作来分派员工。你可以直接说：\n- 帮我找20个德国eBike经销商\n- 今天发LinkedIn社媒，主题是经销商合作\n- 给这个客户官网做背调：https://example.com\n- 给A类客户写开发信\n- 给这些客户做30天跟进计划"
    return {"ok": True, "intent": "general", "assigned_agents": assigned, "messages": [agent_msg("外贸SOP总控", content)]}


def normalize_payload(payload):
    buyer_types = payload.get("buyer_types", [])
    if isinstance(buyer_types, str):
        buyer_types = [x.strip() for x in re.split(r"[,，/、\n]", buyer_types) if x.strip()]
    category = str(payload.get("product_category", "")).strip()
    return {
        "product_category": category,
        "product_params": str(payload.get("product_params", "")).strip(),
        "target_country": str(payload.get("target_country", "")).strip(),
        "buyer_types": buyer_types,
        "advantages": str(payload.get("advantages", "")).strip(),
        "certifications": str(payload.get("certifications", "")).strip(),
        "moq": str(payload.get("moq", "")).strip(),
        "price_range": str(payload.get("price_range", "")).strip(),
        "lead_count": max(5, min(int(payload.get("lead_count", 20) or 20), 60)),
        "category_template": detect_category_template(category),
    }


def fill_payload_defaults(info):
    data = dict(info or {})
    data["product_category"] = data.get("product_category") or "产品待确认"
    data["target_country"] = data.get("target_country") or "目标市场待确认"
    data["buyer_types"] = data.get("buyer_types") or ["distributor", "dealer", "importer"]
    data["product_params"] = data.get("product_params") or default_product_params(data["product_category"])
    data["advantages"] = data.get("advantages") or default_advantages(data["product_category"])
    data["certifications"] = data.get("certifications") or default_certifications(data["product_category"])
    data["moq"] = data.get("moq") or "待确认"
    data["price_range"] = data.get("price_range") or "待确认"
    data["lead_count"] = data.get("lead_count") or 15
    return data


def infer_product_from_text(text):
    lower = text.lower()
    candidates = [
        ("eBike / Electric Bicycle", ["ebike", "e-bike", "electric bike", "电动自行车", "电助力"]),
        ("baby products", ["母婴", "婴儿", "baby", "stroller", "儿童"]),
        ("outdoor products", ["户外", "露营", "outdoor", "camping"]),
        ("home goods", ["家居", "家具", "home", "furniture"]),
    ]
    for product, keys in candidates:
        if any(k in lower or k in text for k in keys):
            return product
    m = re.search(r"(?:产品|品类|卖|做|找)(?:是|：|:)?\s*([A-Za-z0-9\u4e00-\u9fa5 /+-]{2,40})", text)
    if m:
        value = m.group(1).strip(" ，。；,;")
        value = re.split(r"(客户|经销商|进口商|市场|国家|今天|社媒)", value)[0].strip()
        if value:
            return value
    return ""


def infer_country_from_text(text):
    lower = text.lower()
    countries = {
        "United States": ["美国", "美区", "usa", "us ", "u.s.", "america", "united states"],
        "Germany": ["德国", "germany", "deutschland"],
        "United Kingdom": ["英国", "uk", "united kingdom", "britain"],
        "France": ["法国", "france"],
        "Italy": ["意大利", "italy"],
        "Spain": ["西班牙", "spain"],
        "Canada": ["加拿大", "canada"],
        "Australia": ["澳洲", "澳大利亚", "australia"],
        "Netherlands": ["荷兰", "netherlands"],
        "Europe": ["欧洲", "eu", "europe"],
    }
    padded = " " + lower + " "
    for country, keys in countries.items():
        if any(k in lower or k in padded for k in keys):
            return country
    m = re.search(r"(?:目标国家|国家|市场|区域)(?:是|：|:)?\s*([A-Za-z\u4e00-\u9fa5 ]{2,30})", text)
    if m:
        return m.group(1).strip(" ，。；,;")
    return ""


def infer_buyer_types_from_text(text):
    lower = text.lower()
    mapping = [
        ("distributor", ["经销商", "渠道商", "distributor", "dealer"]),
        ("importer", ["进口商", "importer"]),
        ("wholesaler", ["批发商", "wholesaler", "wholesale"]),
        ("brand", ["品牌商", "brand"]),
        ("e-commerce seller", ["电商卖家", "amazon卖家", "shopify卖家", "online seller", "e-commerce"]),
        ("retailer", ["零售商", "门店", "retailer", "store"]),
    ]
    found = []
    for value, keys in mapping:
        if any(k in lower or k in text for k in keys):
            found.append(value)
    return found


def infer_count_from_text(text, default):
    m = re.search(r"(\d{1,3})\s*(?:个|家|条|位|名)?", text)
    if m:
        return max(5, min(int(m.group(1)), 60))
    try:
        return max(5, min(int(default), 60))
    except Exception:
        return 15


def infer_company_from_text(text):
    m = re.search(r"(?:公司|客户|给|Hi|hi)\s*([A-Z][A-Za-z0-9 &.-]{2,60})", text)
    if m:
        return m.group(1).strip()
    urls = re.findall(r"https?://[^\s，。；;,]+", text)
    if urls:
        return domain_from_url(urls[0]) or ""
    return ""


def default_product_params(product):
    text = (product or "").lower()
    if "ebike" in text or "electric bicycle" in text or "电动自行车" in product:
        return "电机、电池、续航、刹车、车架、质保等参数待补充"
    return "核心规格、材质、尺寸、包装、交期和质保信息待补充"


def default_advantages(product):
    text = (product or "").lower()
    if "ebike" in text or "electric bicycle" in text or "电动自行车" in product:
        return "稳定供货、可做贴牌、备件支持、样品评估、经销商物料支持"
    return "稳定供货、批发价格、贴牌支持、样品评估、售后响应"


def default_certifications(product):
    text = (product or "").lower()
    if "ebike" in text or "electric bicycle" in text or "电动自行车" in product:
        return "UL/FCC/CE/UN38.3按型号待确认"
    if "baby" in text or "母婴" in product:
        return "CPSIA/ASTM/EN71按产品待确认"
    return "按目标市场和品类待确认"


def agent_tools_for(agent, logs):
    return [log for log in logs if log.get("agent") == agent]


def agent_msg(agent, text, tools=None):
    msg = {"agent": agent, "text": text}
    if tools:
        msg["tools"] = tools
    return msg


def intent_label(intent):
    labels = {
        "lead_generation": "找客户/线索开发",
        "social_content": "社媒内容",
        "outreach_email": "开发信",
        "customer_research": "客户背调",
        "followup": "客户跟进",
        "market_research": "市场调研",
        "keyword_research": "关键词拓客",
        "general": "外贸获客",
    }
    return labels.get(intent, intent)


def format_keywords_for_agent(keywords):
    if not keywords:
        return "暂无关键词。"
    lines = []
    for group, items in keywords.items():
        lines.append("### " + group)
        lines.extend("- " + item for item in items[:8])
    return "\n".join(lines)


def format_first_outreach(outreach):
    if not outreach:
        return "暂无开发信。"
    item = outreach[0]
    return "首封开发信草稿：\nSubject: %s\n\n%s\n\n风险提醒：%s" % (item.get("subject", ""), item.get("body", ""), item.get("risk_note", ""))


def market_report_to_text(market):
    lines = ["# " + market.get("title", "外贸市场分析")]
    for title, key in [
        ("需求分析", "demand_analysis"),
        ("采购关注点", "buyer_concerns"),
        ("竞品痛点", "competitor_pain_points"),
        ("价格带", "price_band"),
        ("热销款式", "hot_styles"),
        ("合规提醒", "compliance"),
    ]:
        lines.append("\n## " + title)
        lines.extend("- " + x for x in market.get(key, []))
    return "\n".join(lines)


def build_social_media_package(data, message):
    product = data["product_category"]
    country = data["target_country"]
    advantages = data["advantages"]
    hashtags = [product.replace(" ", ""), "B2B", "Wholesale", "Distributor", country.replace(" ", "")]
    if data["category_template"] == "ebike":
        hashtags.extend(["ElectricBike", "EbikeDealer", "Micromobility"])
    linkedin = f"""Looking for new {product} supply options for {country} channels?

We support B2B buyers with sample evaluation, wholesale cooperation and stable replenishment. Current focus: {advantages}.

For importers, distributors and dealer networks, the key is not only price. Certification readiness, spare parts support, packaging, replenishment stability and after-sales response often decide whether a SKU can scale.

If your team is reviewing new {product} suppliers or SKU upgrades, we can share a compact product sheet for evaluation.

Manual review note: certification files, pricing and delivery terms should be confirmed by model before quotation."""
    facebook = f"""New {product} cooperation option for {country} distributors and dealers.

What we can support:
- Sample evaluation
- Wholesale cooperation
- Private-label discussion
- Spare parts and after-sales support
- Product sheet and video materials

Message us if your team is reviewing new suppliers or dealer-ready SKUs."""
    tiktok = [
        f"Hook: Still comparing {product} suppliers only by unit price?",
        "Shot 1: Show product details and packaging.",
        "Shot 2: Show battery/spec/quality-control points.",
        "Shot 3: Show dealer materials, spare parts and sample process.",
        "CTA: Ask distributors to request a product sheet before the next buying cycle.",
    ]
    return {
        "linkedin": linkedin,
        "facebook": facebook,
        "tiktok_script": tiktok,
        "hashtags": ["#" + re.sub(r"[^A-Za-z0-9]", "", x) for x in hashtags if x],
        "send_boundary": "人工确认后发布",
    }


def social_package_to_text(package):
    return """### LinkedIn
%s

### Facebook
%s

### TikTok短视频脚本
%s

### 发布边界
%s""" % (
        package["linkedin"],
        package["facebook"],
        "\n".join("- " + x for x in package["tiktok_script"]),
        package["send_boundary"],
    )


def detect_category_template(category):
    text = category.lower()
    if any(k in text for k in ["ebike", "e-bike", "electric bike", "电动自行车", "电助力"]):
        return "ebike"
    if any(k in text for k in ["baby", "母婴", "stroller", "儿童"]):
        return "baby"
    if any(k in text for k in ["outdoor", "户外", "camping"]):
        return "outdoor"
    if any(k in text for k in ["home", "furniture", "家居"]):
        return "home"
    return "general"


def build_market_report(data):
    country = data["target_country"]
    category = data["product_category"]
    template = data["category_template"]
    compliance = build_compliance_notice(data)
    category_notes = {
        "ebike": [
            "采购方通常关注电机功率、电池容量、续航、售后配件、整车重量、刹车系统与质保周期。",
            "欧美买家会重点审查电池安全、充电器标准、限速规则、整车标签和当地道路法规。",
            "高转化款式通常集中在 commuter、cargo、folding、fat tire、city trekking 等场景。",
        ],
        "baby": [
            "采购方会优先关注材料安全、年龄段、测试报告、召回风险和包装警示。",
            "欧美市场对儿童用品合规要求更高，必须避免夸大安全承诺。",
        ],
        "outdoor": [
            "采购方关注耐用性、便携性、季节性库存、渠道陈列和售后退换率。",
        ],
        "home": [
            "采购方关注材质、尺寸体系、包装破损率、组合 SKU 和长期供货稳定性。",
        ],
        "general": [
            "采购方关注供货稳定性、认证资质、价格带、交期、样品效率和售后响应。",
        ],
    }[template]
    return {
        "title": f"{country} {category} B2B客户开发市场分析",
        "demand_analysis": [
            f"目标市场：{country}；目标品类：{category}。",
            "系统按B2B批发采购逻辑分析，不面向C端零售消费者。",
            *category_notes,
        ],
        "buyer_concerns": [
            "落地成本：FOB/EXW价格、海运体积、关税、渠道毛利空间。",
            "供货确定性：MOQ、交期、备件、质保、淡旺季补货能力。",
            "市场证明：同类案例、图片视频素材、认证文件、包装和说明书。",
        ],
        "competitor_pain_points": [
            "竞品同质化严重，买家通常希望看到更清晰的差异化卖点。",
            "若缺少当地合规文件，A类客户会降低回复意愿。",
            "售后备件和电池/电子部件质保说明不清，会直接影响经销商采购。",
        ],
        "price_band": [
            f"当前输入价格区间：{data['price_range']}。",
            "建议在开发信中表达为 target wholesale range 或 sample quotation available，而不是硬性低价承诺。",
        ],
        "hot_styles": build_hot_styles(template),
        "compliance": compliance,
        "risk_items": [
            {"level": "red", "item": "合规缺失", "advice": "未能提供FCC/CE/UL等文件时，只能先做意向沟通，不建议承诺可立即进口。"},
            {"level": "red", "item": "客户规模不匹配", "advice": "小零售店不应作为第一优先级，优先经销商、批发商、品牌商和多门店渠道。"},
            {"level": "red", "item": "竞品强势", "advice": "开发信必须给出差异化场景和利润空间，不要只说质量好价格低。"},
        ],
    }


def build_hot_styles(template):
    if template == "ebike":
        return ["commuter e-bike", "folding e-bike", "cargo e-bike", "fat tire e-bike", "city trekking e-bike"]
    if template == "baby":
        return ["travel stroller", "foldable baby gear", "non-toxic feeding product", "nursery storage"]
    if template == "outdoor":
        return ["portable camping gear", "lightweight outdoor equipment", "weather-resistant accessories"]
    if template == "home":
        return ["space-saving furniture", "modular storage", "easy-ship home accessories"]
    return ["best-selling wholesale SKU", "private-label ready product", "seasonal replenishment product"]


def build_compliance_notice(data):
    country = data["target_country"].lower()
    template = data["category_template"]
    notices = ["仅用于B2B商务开发；不自动发送邮件，不抓取隐私数据。"]
    if template == "ebike":
        notices.extend(["eBike需关注电池运输、UL/FCC/CE、充电器标准、当地限速与道路法规。"])
    if template == "baby":
        notices.extend(["母婴/儿童用品需关注CPSIA、ASTM、EN71、标签警示、材料安全和召回风险。"])
    if any(k in country for k in ["us", "usa", "america", "美国"]):
        notices.extend(["美国市场需关注FCC、UL、CPSC、进口关税和州级法规差异。"])
    if any(k in country for k in ["eu", "europe", "germany", "france", "italy", "spain", "欧洲", "德国", "法国"]):
        notices.extend(["欧洲市场需关注CE、RoHS/REACH、电池法规、WEEE、包装法和当地VAT要求。"])
    return notices


def build_keywords(data):
    product = data["product_category"]
    country = data["target_country"]
    buyer_words = data["buyer_types"] or ["importer", "distributor", "dealer"]
    competitor = {
        "ebike": ["Rad Power Bikes dealer", "Trek e-bike reseller", "Specialized e-bike dealer"],
        "baby": ["baby products distributor", "nursery products wholesaler"],
        "outdoor": ["outdoor gear distributor", "camping equipment wholesaler"],
        "home": ["home goods importer", "furniture distributor"],
        "general": [f"{product} distributor", f"{product} wholesaler"],
    }[data["category_template"]]
    core = [
        f"{product} distributor {country}",
        f"{product} wholesaler {country}",
        f"{product} importer {country}",
        f"{product} dealer network {country}",
    ]
    scene = [f"{style} distributor {country}" for style in build_hot_styles(data["category_template"])]
    identity = [f"{product} {bt} {country}" for bt in buyer_words]
    return {
        "产品核心词": core,
        "行业场景词": scene,
        "客户身份词": identity,
        "竞品对标词": competitor,
        "Google可复制组合": [f'"{q}" -amazon -walmart -aliexpress' for q in (core + identity)[:10]],
        "LinkedIn可复制组合": [f'site:linkedin.com/company "{product}" "{country}" "{bt}"' for bt in buyer_words[:4]],
    }


def discover_leads(data, keywords, opener=None, live_search=True):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    collected = []
    seen = set()
    search_terms = (keywords["产品核心词"] + keywords["客户身份词"] + keywords["行业场景词"])[:8]
    if live_search:
        for term in search_terms:
            for item in search_duckduckgo(term, opener=opener):
                domain = domain_from_url(item["website"])
                if not domain or domain in seen or is_blocked_domain(domain):
                    continue
                seen.add(domain)
                lead = lead_from_search_result(item, data, term)
                if is_b2b_candidate(lead):
                    collected.append(lead)
                if len(collected) >= data["lead_count"]:
                    return collected
    if len(collected) < max(8, min(data["lead_count"], 20)):
        collected.extend(fallback_leads(data, seen, data["lead_count"] - len(collected)))
    return collected[:data["lead_count"]]


def search_duckduckgo(query, opener=None):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html_text = opener.open(req, timeout=12).read().decode("utf-8", errors="replace")
    except Exception:
        return []
    results = []
    pattern = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    for href, title in pattern.findall(html_text):
        clean_title = re.sub(r"<[^>]+>", " ", title)
        clean_title = re.sub(r"\s+", " ", html.unescape(clean_title)).strip()
        parsed = urllib.parse.urlparse(html.unescape(href))
        target = href
        if "uddg" in urllib.parse.parse_qs(parsed.query):
            target = urllib.parse.parse_qs(parsed.query)["uddg"][0]
        results.append({"company": company_from_title(clean_title), "website": target, "source_url": url, "title": clean_title})
        if len(results) >= 8:
            break
    return results


def lead_from_search_result(item, data, query):
    website = normalize_url(item.get("website", ""))
    title = item.get("title") or item.get("company") or domain_from_url(website)
    company = company_from_title(title) or domain_from_url(website) or "待验证"
    domain = domain_from_url(website)
    email = "待验证"
    customer_type = infer_customer_type(title + " " + domain)
    return {
        "company": company,
        "country": data["target_country"],
        "website": website,
        "email": email,
        "main_category": "待验证",
        "customer_type": customer_type,
        "source_link": item.get("source_url", ""),
        "source_query": query,
        "verification_status": "待验证",
        "data_source": "公开搜索结果",
        "notes": "需人工打开官网二次确认联系人与采购角色。",
    }


def fallback_leads(data, seen, count):
    buyer_words = data["buyer_types"] or ["distributor", "dealer", "importer"]
    product = slugify(data["product_category"])
    country = slugify(data["target_country"])
    leads = []
    for i in range(max(0, count)):
        role = buyer_words[i % len(buyer_words)]
        company = f"{data['target_country']} {data['product_category']} {role.title()} Lead {i + 1}"
        query = f"{data['product_category']} {role} {data['target_country']}"
        leads.append({
            "company": company,
            "country": data["target_country"],
            "website": "待验证",
            "email": "待验证",
            "main_category": "待验证",
            "customer_type": role,
            "source_link": "https://duckduckgo.com/?q=" + urllib.parse.quote(query),
            "source_query": query,
            "verification_status": "待验证",
            "data_source": "搜索待验证占位",
            "notes": "网络搜索不可用时生成的待验证线索位，不得当作真实客户直接发送。",
        })
    return leads


def build_customer_profile(lead, data, opener=None):
    website = lead.get("website", "")
    text = ""
    emails = []
    if website and website != "待验证":
        text = fetch_text(website, opener=opener)
        emails = extract_emails(text, website)
    summary = summarize_company_text(text, lead, data)
    if emails:
        lead["email"] = emails[0]
        lead["verification_status"] = "已找到公开邮箱，仍需人工确认"
    risks = []
    if lead.get("website") == "待验证":
        risks.append("官网待验证")
    if lead.get("email") == "待验证":
        risks.append("邮箱待验证")
    if not text:
        risks.append("官网内容未读取")
    return {
        "company": lead["company"],
        "website": lead.get("website", ""),
        "summary": summary,
        "business_scale": infer_scale(text),
        "main_products": infer_products(text, data),
        "service_area": infer_area(text, data),
        "purchase_potential": "待验证",
        "risks": risks,
    }


def fetch_text(url, opener=None):
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(normalize_url(url), headers={"User-Agent": "Mozilla/5.0"})
        raw = opener.open(req, timeout=12).read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:10000]


def extract_emails(text, website):
    candidates = sorted(set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")))
    filtered = []
    for email in candidates:
        lower = email.lower()
        if any(x in lower for x in ["example.", "sentry.", "wixpress", "schema@", "domain.com"]):
            continue
        filtered.append(email)
    return filtered[:3]


def score_lead(lead, profile, data):
    text = " ".join([lead.get("company", ""), lead.get("customer_type", ""), profile.get("summary", ""), profile.get("main_products", "")]).lower()
    score = 45
    risk_flags = []
    if any(x in text for x in [data["product_category"].lower(), "e-bike", "ebike", "electric bike"]):
        score += 20
    if any(x in text for x in ["distributor", "dealer", "importer", "wholesale", "reseller"]):
        score += 18
    if lead.get("website") != "待验证":
        score += 8
    if lead.get("email") != "待验证":
        score += 7
    if "retail" in text and not any(x in text for x in ["dealer", "distributor", "wholesale"]):
        score -= 10
        risk_flags.append("可能偏零售")
    if "官网待验证" in profile.get("risks", []):
        score -= 12
    if score >= 78:
        grade = "A"
        priority = "优先跟进"
    elif score >= 58:
        grade = "B"
        priority = "二级跟进"
    else:
        grade = "C"
        priority = "暂缓跟进"
    return {
        "score": score,
        "grade": grade,
        "priority": priority,
        "core_match": build_match_reason(lead, data, grade),
        "risk_flags": risk_flags,
        "outreach_angle": build_outreach_angle(lead, data, grade),
    }


def build_match_reason(lead, data, grade):
    if grade == "A":
        return f"疑似{data['product_category']}相关B端渠道，适合优先验证采购入口。"
    if grade == "B":
        return "行业相关但采购匹配度待确认，适合用轻量开发信测试兴趣。"
    return "信息不足或偏零售，先保留为补充名单，不建议优先投入。"


def build_outreach_angle(lead, data, grade):
    product = data["product_category"]
    if grade == "A":
        return f"围绕{product}批发利润、稳定供货和差异化款式切入。"
    if grade == "B":
        return f"用新品补充SKU和低风险样品测试切入。"
    return "只做一次轻触达或后续人工验证。"


def build_outreach_emails(lead, data):
    company = lead["company"]
    product = data["product_category"]
    advantages = data["advantages"]
    certs = data["certifications"]
    moq = data["moq"]
    price = data["price_range"]
    angle = lead.get("outreach_angle", "")
    return [
        {
            "company": company,
            "grade": lead.get("grade", ""),
            "type": "简短拓客版",
            "subject": f"Potential {product} supply cooperation",
            "body": f"""Hi {company} Team,

I noticed your business may be connected with {product} distribution in {data['target_country']}. We supply {product} for B2B buyers and can support sample evaluation, wholesale pricing and stable replenishment.

Key points: {advantages}. Certifications/documents available: {certs}. MOQ: {moq}. Target wholesale range: {price}.

If you are reviewing new suppliers or SKU upgrades, I can send a short product sheet and a sample quotation for your team.

Best regards,""",
            "risk_note": "请人工确认公司与联系人后再发送。",
        },
        {
            "company": company,
            "grade": lead.get("grade", ""),
            "type": "深度合作代理版",
            "subject": f"Distribution opportunity for {product} in {data['target_country']}",
            "body": f"""Hi {company} Team,

Your channel looks relevant for a potential {product} distribution discussion. We are looking for partners who care about reliable supply, product differentiation and after-sales support, rather than one-off low-price sourcing.

For your market, our proposed angle is: {angle}

We can provide product specifications, certification files ({certs}), wholesale quotation, packaging information and marketing materials for dealer evaluation. MOQ is {moq}, with an estimated price range of {price}.

Would it be useful if I send a compact comparison sheet and recommended SKUs for your current product line?

Best regards,""",
            "risk_note": "适合A/B类客户，发送前需人工检查客户规模和品类匹配。",
        },
        {
            "company": company,
            "grade": lead.get("grade", ""),
            "type": "新品众筹供货版",
            "subject": f"New {product} models for launch or campaign testing",
            "body": f"""Hi {company} Team,

If your team is planning a new product launch, preorder campaign or dealer test program, we can support {product} supply with product documentation, sample preparation and launch-ready materials.

The main value is {advantages}. We can share specs, certification status ({certs}), MOQ ({moq}) and target price range ({price}) for your internal review.

Would you be open to checking 2-3 suitable models for your market before your next buying cycle?

Best regards,""",
            "risk_note": "不承诺众筹结果，只表达供货与资料支持能力。",
        },
    ]


def build_followup_plan(lead, data):
    company = lead["company"]
    today = time.time()
    steps = [
        (0, "首次开发", "确认对方是否负责采购或渠道合作。"),
        (2, "资料补充", "发送精简产品表、认证状态和批发区间。"),
        (5, "痛点切入", "围绕利润空间、SKU补充、售后配件和交期跟进。"),
        (9, "案例/场景", "补充目标市场热销款式与应用场景。"),
        (14, "样品推进", "询问是否需要样品报价或视频验厂资料。"),
        (21, "低压提醒", "确认是否在本季度有采购计划。"),
        (30, "结束归档", "礼貌收尾，保留后续新品更新许可。"),
    ]
    rows = []
    for idx, (offset, purpose, intent) in enumerate(steps, 1):
        date = time.strftime("%Y-%m-%d", time.localtime(today + offset * 86400))
        rows.append({
            "company": company,
            "step": idx,
            "send_date": date,
            "interval_days": offset,
            "purpose": purpose,
            "intent": intent,
            "copy": build_followup_copy(company, data, purpose),
            "send_boundary": "人工确认后发送",
        })
    return rows


def build_followup_copy(company, data, purpose):
    product = data["product_category"]
    if purpose == "首次开发":
        return f"Hi {company} Team, checking whether your team handles {product} sourcing or dealer partnerships."
    if purpose == "资料补充":
        return f"Following up with a compact {product} product sheet, certification status and wholesale range for your review."
    if purpose == "痛点切入":
        return f"Many buyers are comparing SKU margin, replenishment stability and after-sales parts. We can support these points for {product}."
    if purpose == "案例/场景":
        return f"I can recommend 2-3 {product} models based on your market segment and target customer profile."
    if purpose == "样品推进":
        return "Would a sample quotation or short factory/product video help your team evaluate fit?"
    if purpose == "低压提醒":
        return "Just checking whether this category is on your buying plan this quarter. If not, I can follow up later."
    return "I will close the loop for now. May I share future product updates when we have a better fit for your market?"


def build_workflow_status():
    names = [
        "基础信息校验", "目标市场调研", "拓客关键词生成", "潜客抓取与清洗", "客户背调",
        "A/B/C分级", "个性化开发信", "30天跟进SOP", "Excel数据包汇总",
    ]
    return [{"step": i + 1, "name": name, "status": "completed"} for i, name in enumerate(names)]


def build_text_report(market, keywords, leads, profiles, outreach, followups):
    lines = []
    lines.append(f"# {market['title']}")
    lines.append("\n## 1. 市场分析")
    lines.extend("- " + x for x in market["demand_analysis"] + market["buyer_concerns"])
    lines.append("\n## 2. 关键词库")
    for group, items in keywords.items():
        lines.append(f"### {group}")
        lines.extend("- " + x for x in items)
    lines.append("\n## 3. 客户线索清单")
    for lead in leads:
        lines.append(f"- [{lead.get('grade','')}] {lead['company']} | {lead.get('website','')} | {lead.get('verification_status','待验证')} | {lead.get('core_match','')}")
    lines.append("\n## 4. 客户背调档案")
    for profile in profiles:
        lines.append(f"- {profile['company']}: {profile['summary']} 风险: {', '.join(profile['risks']) or '无'}")
    lines.append("\n## 5. 分级结果")
    for lead in leads:
        lines.append(f"- {lead['company']}: {lead.get('grade','')}类，{lead.get('priority','')}，评分 {lead.get('score','')}")
    lines.append("\n## 6. 开发信合集")
    for item in outreach[: min(len(outreach), 9)]:
        lines.append(f"### {item['company']} - {item['type']}\nSubject: {item['subject']}\n{item['body']}")
    lines.append("\n## 7. 30天跟进SOP")
    for item in followups[: min(len(followups), 21)]:
        lines.append(f"- {item['company']} D+{item['interval_days']} {item['purpose']}: {item['copy']}")
    return "\n".join(lines)


def write_xlsx(package, path):
    sheets = [
        ("客户线索清单", rows_for_leads(package["leads"])),
        ("市场分析", rows_for_market(package["market_report"])),
        ("关键词库", rows_for_keywords(package["keywords"])),
        ("客户背调档案", rows_for_profiles(package["customer_profiles"])),
        ("开发信合集", rows_for_outreach(package["outreach_emails"])),
        ("30天跟进SOP", rows_for_followups(package["followups"])),
    ]
    make_xlsx(path, sheets)


def rows_for_leads(leads):
    rows = [["客户公司名", "官网", "邮箱", "主营品类", "客户类型", "意向等级", "评分", "核心匹配点", "风险项", "开发角度", "首次发送时间", "下次跟进日期", "来源链接", "验证状态"]]
    today = time.strftime("%Y-%m-%d")
    next_date = time.strftime("%Y-%m-%d", time.localtime(time.time() + 2 * 86400))
    for lead in leads:
        rows.append([
            lead.get("company", ""), lead.get("website", ""), lead.get("email", ""), lead.get("main_category", ""),
            lead.get("customer_type", ""), lead.get("grade", ""), lead.get("score", ""), lead.get("core_match", ""),
            lead.get("risk_flags", ""), lead.get("outreach_angle", ""), today, next_date,
            lead.get("source_link", ""), lead.get("verification_status", ""),
        ])
    return rows


def rows_for_market(market):
    rows = [["板块", "内容"]]
    for key in ["demand_analysis", "buyer_concerns", "competitor_pain_points", "price_band", "hot_styles", "compliance"]:
        for item in market.get(key, []):
            rows.append([key, item])
    for item in market.get("risk_items", []):
        rows.append(["风险项", f"{item['item']} - {item['advice']}"])
    return rows


def rows_for_keywords(keywords):
    rows = [["分类", "关键词"]]
    for group, items in keywords.items():
        for item in items:
            rows.append([group, item])
    return rows


def rows_for_profiles(profiles):
    rows = [["客户公司名", "官网", "企业简介", "业务规模", "主营产品", "业务区域", "采购潜力", "风险项"]]
    for p in profiles:
        rows.append([p["company"], p["website"], p["summary"], p["business_scale"], p["main_products"], p["service_area"], p["purchase_potential"], "; ".join(p["risks"])])
    return rows


def rows_for_outreach(outreach):
    rows = [["客户公司名", "意向等级", "开发信类型", "标题", "开发信内容", "风险提醒"]]
    for item in outreach:
        rows.append([item["company"], item["grade"], item["type"], item["subject"], item["body"], item["risk_note"]])
    return rows


def rows_for_followups(followups):
    rows = [["客户公司名", "跟进次数", "发送日期", "间隔天数", "跟进目的", "转化引导", "跟进文案", "发送边界"]]
    for item in followups:
        rows.append([item["company"], item["step"], item["send_date"], item["interval_days"], item["purpose"], item["intent"], item["copy"], item["send_boundary"]])
    return rows


def make_xlsx(path, sheets):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        z.writestr("_rels/.rels", rels_xml())
        z.writestr("xl/workbook.xml", workbook_xml(sheets))
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        z.writestr("xl/styles.xml", styles_xml())
        for idx, (_, rows) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml(rows))


def content_types_xml(count):
    overrides = "".join([f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, count + 1)])
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{overrides}
</Types>'''


def rels_xml():
    return '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''


def workbook_xml(sheets):
    entries = "".join([f'<sheet name="{escape(sheet_name[:31])}" sheetId="{i}" r:id="rId{i}"/>' for i, (sheet_name, _) in enumerate(sheets, 1)])
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{entries}</sheets>
</workbook>'''


def workbook_rels_xml(count):
    entries = "".join([f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, count + 1)])
    entries += f'<Relationship Id="rId{count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{entries}</Relationships>'''


def styles_xml():
    return '''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><name val="Arial"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>'''


def sheet_xml(rows):
    xml_rows = []
    for r_idx, row in enumerate(rows, 1):
        cells = []
        for c_idx, value in enumerate(row, 1):
            ref = f"{column_name(c_idx)}{r_idx}"
            style = ' s="1"' if r_idx == 1 else ""
            cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(str(value or ""))}</t></is></c>')
        xml_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>{"".join(xml_rows)}</sheetData>
</worksheet>'''


def column_name(index):
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def normalize_url(url):
    url = str(url or "").strip()
    if not url or url == "待验证":
        return url
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        return "https://" + url
    return url


def domain_from_url(url):
    try:
        host = urllib.parse.urlparse(normalize_url(url)).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_blocked_domain(domain):
    return any(b in domain for b in RETAIL_BLOCKLIST)


def is_b2b_candidate(lead):
    text = " ".join([lead.get("company", ""), lead.get("customer_type", ""), lead.get("website", "")]).lower()
    return any(sig in text for sig in WHOLESALE_SIGNALS) or lead.get("customer_type") != "待验证"


def infer_customer_type(text):
    lower = (text or "").lower()
    for label in ["distributor", "dealer", "importer", "wholesale", "reseller", "brand", "retailer"]:
        if label in lower:
            return label
    return "待验证"


def company_from_title(title):
    title = re.sub(r"\s*[-|–].*$", "", title or "").strip()
    return title[:80] or "待验证"


def summarize_company_text(text, lead, data):
    if not text:
        return f"{lead['company']} 官网内容暂未读取，需人工打开来源链接验证是否为{data['product_category']}相关B端客户。"
    snippets = []
    for sentence in re.split(r"(?<=[.!?。！？])\s+", text[:1600]):
        if any(k in sentence.lower() for k in WHOLESALE_SIGNALS + [data["product_category"].lower()]):
            snippets.append(sentence.strip())
        if len(snippets) >= 3:
            break
    if not snippets:
        snippets = [text[:260]]
    return " ".join(snippets)[:500]


def infer_scale(text):
    lower = (text or "").lower()
    if any(k in lower for k in ["locations", "nationwide", "warehouse", "dealer network", "since 19", "since 20"]):
        return "中大型渠道商迹象，需验证"
    if text:
        return "小中型企业迹象，需验证"
    return "待验证"


def infer_products(text, data):
    if not text:
        return "待验证"
    product = data["product_category"]
    lower = text.lower()
    styles = [x for x in build_hot_styles(data["category_template"]) if x.lower() in lower]
    if styles:
        return ", ".join(styles)
    if product.lower() in lower:
        return product
    return "行业相关产品待验证"


def infer_area(text, data):
    if not text:
        return data["target_country"]
    if any(k in text.lower() for k in ["nationwide", "across", "shipping", "dealer network"]):
        return f"{data['target_country']} 多区域服务迹象"
    return data["target_country"]


def slugify(text):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", str(text).lower()).strip("-")
    return value or "lead"
