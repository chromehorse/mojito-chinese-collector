"""Keyword gate over title+body. Recall-biased: ANY hit passes."""

KEYWORDS = [
    "人工智能", "生成式人工智能", "大模型", "智能体", "算法", "自动化",
    "人工智能治理", "科技伦理", "技术伦理", "对齐", "安全治理", "人机关系",
    "主体性", "劳动", "就业", "尊严", "以人为本", "智能向善", "共同富裕",
    "人的全面发展", "算力", "数智化", "具身智能",
]


def is_relevant(title, body_text):
    haystack = (title or "") + "\n" + (body_text or "")
    return any(kw in haystack for kw in KEYWORDS)
